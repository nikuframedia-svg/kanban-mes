"""Testes do verso da folha TPL102 (paragens): classificação, fluxo e validação."""

import pytest
from fastapi.testclient import TestClient

from app import db, pg_store
from app.ocr import provider as ocr
from app.web import main


@pytest.fixture()
def client(tmp_path, monkeypatch):
    real_connect = db.connect
    monkeypatch.setattr(db, "connect", lambda path=None: real_connect(tmp_path / "t.db"))
    monkeypatch.setattr(main, "get_index", lambda loader_name: (_ for _ in ()).throw(
        AssertionError("paragens não deviam pedir índice do plano")))
    stored: list = []

    def fake_store(sheet, template, edit_count, actor):
        stored.append(template.name)
        return len((sheet["sheet_data"] or {}).get("rows") or [])

    monkeypatch.setattr(pg_store, "store_validated_sheet", fake_store)
    monkeypatch.setattr(main, "PROCESS_IN_BACKGROUND", False)  # determinístico
    c = TestClient(main.app, follow_redirects=False)
    c.stored = stored
    return c


def create_paragens(client) -> str:
    r = client.post("/upload", data={"template_name": "cantoneiras_paragens"})
    assert r.status_code == 303
    return r.headers["location"].rsplit("/", 1)[1]


def test_paragens_sheet_sem_cruzamento(client):
    uid = create_paragens(client)
    r = client.get(f"/sheet/{uid}")
    assert r.status_code == 200
    assert "Motivo da paragem" in r.text
    assert "Ligação ao plano" not in r.text        # sem coluna de match
    conn = db.connect()
    try:
        assert db.get_sheet(conn, uid)["cross_check"] is None
    finally:
        conn.close()


def test_paragens_edit_e_validar(client):
    uid = create_paragens(client)

    def edit(field_path, value):
        conn = db.connect()
        try:
            rev = db.get_sheet(conn, uid)["revision"]
        finally:
            conn.close()
        r = client.post(f"/sheet/{uid}/edit", data={
            "field_path": field_path, "value": value, "revision": rev, "actor": "t"})
        assert r.status_code == 303

    edit("rows[0].motivo", "Consulta de Medicina do Trabalho")
    edit("rows[0].duracao", "1H")
    edit("header.operador", "Daniel Venâncio")
    edit("header.data", "06/08/2026")
    r = client.post(f"/sheet/{uid}/validate", data={"actor": "luis"})
    assert r.status_code == 303
    assert client.stored == ["cantoneiras_paragens"]


def test_classificacao_escolhe_template_paragens(client, tmp_path, monkeypatch):
    img = tmp_path / "verso.png"
    from PIL import Image
    Image.new("RGB", (60, 60), "white").save(img)

    class FakeProvider:
        name = "fake"

        def classify_page(self, image_path):
            return "paragens"

        def extract(self, image_path, template):
            assert template.name == "cantoneiras_paragens"
            return {
                "header": {f: None for f in template.header_fields},
                "rows": [{"motivo": "Sem problemas", "inicio": None, "fim": None,
                          "duracao": None, "resolvido": None}],
                "footer": {},
            }

    monkeypatch.setattr(main, "get_provider", lambda: FakeProvider())
    r = client.post("/upload", data={"template_name": "cantoneiras_kanban"},
                    files=[("photos", ("verso.png", img.read_bytes(), "image/png"))])
    assert r.status_code == 303
    uid = r.headers["location"].rsplit("/", 1)[1]
    conn = db.connect()
    try:
        sheet = db.get_sheet(conn, uid)
    finally:
        conn.close()
    assert sheet["template_name"] == "cantoneiras_paragens"
    assert sheet["sheet_data"]["rows"][0]["motivo"] == "Sem problemas"


def test_reocr_reprocessa_folha_com_foto(client, tmp_path, monkeypatch):
    img = tmp_path / "f.png"
    from PIL import Image
    Image.new("RGB", (60, 60), "white").save(img)

    calls = {"n": 0}

    class FlakyProvider:
        name = "flaky"

        def classify_page(self, image_path):
            return "producao"

        def extract(self, image_path, template):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ocr.OcrError("Gemini HTTP 429: quota")
            return {"header": {f: None for f in template.header_fields},
                    "rows": [{f: "ok" if f == "of" else None for f in template.row_fields}],
                    "footer": {f: None for f in template.footer_fields}}

    monkeypatch.setattr(main, "get_provider", lambda: FlakyProvider())
    monkeypatch.setattr(main, "get_index", lambda name: __import__("tests.test_web", fromlist=["make_index"]).make_index())
    r = client.post("/upload", data={"template_name": "cantoneiras_kanban"},
                    files=[("photos", ("f.png", img.read_bytes(), "image/png"))])
    uid = r.headers["location"].rsplit("/", 1)[1]
    conn = db.connect()
    try:
        assert db.get_sheet(conn, uid)["sheet_data"].get("_ocr_error")
    finally:
        conn.close()

    assert client.post(f"/sheet/{uid}/reocr").status_code == 303
    conn = db.connect()
    try:
        sheet = db.get_sheet(conn, uid)
    finally:
        conn.close()
    assert not sheet["sheet_data"].get("_ocr_error")
    assert sheet["sheet_data"]["rows"][0]["of"] == "ok"


def test_parse_duracao():
    from app.matching import similarity as sim
    assert sim.parse_number("1H") == 1.0
    assert sim.parse_number("6.5") == 6.5


def test_classify_fallback_para_producao():
    p = ocr.GeminiOcrProvider(api_key="t", model="m")
    # resposta inválida → assume produção (comportamento antigo)
    import types
    p._call = types.MethodType(lambda self, body: {"candidates": []}, p)
    from pathlib import Path
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".png") as f:
        f.write(b"png")
        f.flush()
        assert p.classify_page(Path(f.name)) == "producao"