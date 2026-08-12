"""Ingest automático dos PDFs de kanban do Drive.

O sync do DATARESEARCHMTG traz os PDFs do scanner para o servidor; o
POST /ingest/drive mete-os na fila da app sem mão humana — e sem duplicar o
que já entrou por upload manual.
"""

from __future__ import annotations

import shutil

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app import db
from app.web import main


def make_pdf(path, n_pages: int, seed: str) -> None:
    """PDF de teste com páginas distintas (o conteúdo entra no sha da página)."""
    pages = []
    for i in range(n_pages):
        im = Image.new("RGB", (400, 300), "white")
        d = ImageDraw.Draw(im)
        d.text((20, 20), f"{seed} pagina {i}", fill="black")
        for y in range(60, 280, 30):
            d.line([(10, y), (390, y)], fill="black", width=2)
        pages.append(im)
    pages[0].save(path, save_all=True, append_images=pages[1:], format="PDF")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import dataclasses

    real_connect = db.connect
    monkeypatch.setattr(db, "connect", lambda path=None: real_connect(tmp_path / "t.db"))
    drive = tmp_path / "drive"
    drive.mkdir()
    # Settings é frozen: substitui-se a instância inteira no módulo web
    monkeypatch.setattr(main, "settings", dataclasses.replace(
        main.settings, data_dir=tmp_path, drive_dir=drive))
    monkeypatch.setattr(main, "PROCESS_IN_BACKGROUND", False)
    monkeypatch.setattr(main, "_BATCH_SLEEP_S", 0.0)
    monkeypatch.setattr(main, "_RETRY_DELAY_S", 0.0)
    monkeypatch.setattr(main, "run_cross_check", lambda conn, uid: None)

    class NullProvider:                     # OCR fora do que este teste cobre
        name = "null"

        def extract_auto(self, image_path, templates):
            t = templates["producao"]
            return "producao", {
                "header": {f: None for f in t.header_fields},
                "rows": [{f: "x" if f == "of" else None for f in t.row_fields}],
                "footer": {f: None for f in t.footer_fields}}

    monkeypatch.setattr(main, "get_provider", lambda: NullProvider())
    c = TestClient(main.app, follow_redirects=False)
    c.drive = drive
    return c


def n_sheets(client) -> int:
    conn = db.connect()
    try:
        return len(db.list_sheets(conn))
    finally:
        conn.close()


def test_ingest_cria_folhas_dos_pdfs_novos(client):
    make_pdf(client.drive / "07-08-2026_Rapid 20t 1_2.PDF", 2, "lote7")
    make_pdf(client.drive / "08-08-2026_Rapid 20t 1_2.PDF", 3, "lote8")
    (client.drive / "Met3_Plan_Cantoneiras.xlsm").write_bytes(b"nao sou um kanban")

    r = client.post("/ingest/drive")
    assert r.status_code == 200
    out = r.json()
    assert out["pdfs_vistos"] == 2, "o Excel do plano não é um PDF de kanban"
    assert out["pdfs_novos"] == 2
    assert out["folhas_criadas"] == 5
    assert n_sheets(client) == 5


def test_ingest_e_idempotente(client):
    make_pdf(client.drive / "09-08-2026_Rapid 20t 1_2.PDF", 2, "lote9")
    assert client.post("/ingest/drive").json()["folhas_criadas"] == 2
    out = client.post("/ingest/drive").json()
    assert out["pdfs_novos"] == 0
    assert out["folhas_criadas"] == 0
    assert n_sheets(client) == 2


def test_ingest_salta_paginas_ja_entradas_por_upload(client):
    """Os lotes de 06 e 10-08 entraram à mão — o backfill não os pode duplicar."""
    make_pdf(client.drive / "10-08-2026_Rapid 20t 1_2.PDF", 2, "lote10")
    content = (client.drive / "10-08-2026_Rapid 20t 1_2.PDF").read_bytes()
    # upload manual das mesmas páginas, como aconteceu na realidade
    r = client.post("/upload", data={"template_name": "cantoneiras_kanban"},
                    files=[("photos", ("10-08-2026_Rapid 20t 1_2.PDF", content,
                                       "application/pdf"))])
    assert r.status_code == 303
    assert n_sheets(client) == 2

    out = client.post("/ingest/drive").json()
    assert out["pdfs_novos"] == 1, "o PDF em si nunca tinha passado pelo ingest"
    assert out["folhas_criadas"] == 0
    assert out["paginas_repetidas"] == 2
    assert n_sheets(client) == 2


def test_versao_nova_do_mesmo_pdf_reprocessa_so_paginas_novas(client):
    make_pdf(client.drive / "11-08-2026_Rapid 20t 1_2.PDF", 2, "lote11")
    assert client.post("/ingest/drive").json()["folhas_criadas"] == 2
    # o scanner voltou a gravar o ficheiro com mais uma página
    make_pdf(client.drive / "11-08-2026_Rapid 20t 1_2.PDF", 3, "lote11")
    out = client.post("/ingest/drive").json()
    assert out["pdfs_novos"] == 1
    assert out["folhas_criadas"] == 1, "só a página nova entra"
    assert out["paginas_repetidas"] == 2


def test_token_de_admin_quando_configurado(client, monkeypatch):
    import dataclasses
    monkeypatch.setattr(main, "settings",
                        dataclasses.replace(main.settings, admin_token="segredo"))
    assert client.post("/ingest/drive").status_code == 403
    r = client.post("/ingest/drive", headers={"X-Admin-Token": "segredo"})
    assert r.status_code == 200


def test_pasta_do_drive_em_falta_da_503(client, monkeypatch):
    shutil.rmtree(client.drive)
    assert client.post("/ingest/drive").status_code == 503
