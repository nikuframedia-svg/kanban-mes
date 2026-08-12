"""Deteção de páginas em branco — o caso das folhas alucinadas.

O scanner alimentado com folhas de um só lado mete versos em branco no PDF, e
o LLM posto a transcrever uma página vazia inventava uma folha inteira
plausível. A deteção é local (contar píxeis) e corre antes de qualquer chamada.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from app import db, imaging
from app.web import main

THRESHOLD = 0.0008  # o default de settings.blank_ink_threshold


def make_blank(path: Path) -> Path:
    Image.new("L", (800, 1100), 255).save(path)
    return path


def make_sheet_like(path: Path) -> Path:
    """Imagem com grelha e texto rasteiro, como uma folha kanban real."""
    im = Image.new("L", (800, 1100), 255)
    d = ImageDraw.Draw(im)
    for y in range(100, 1000, 60):
        d.line([(40, y), (760, y)], fill=0, width=2)
    for x in range(40, 780, 120):
        d.line([(x, 100), (x, 960)], fill=0, width=2)
    for y in range(110, 400, 60):
        d.text((60, y), "OF263323  L60X60X5  AT1T115  12", fill=0)
    im.save(path)
    return path


def make_speckled_blank(path: Path) -> Path:
    """Branca com o ruído de scanner que as páginas reais p10/p14 têm."""
    im = Image.new("L", (800, 1100), 255)
    d = ImageDraw.Draw(im)
    for i in range(6):                       # meia dúzia de píxeis de pó
        d.point((50 + i * 130, 30 + i * 170), fill=0)
    im.save(path)
    return path


def test_pagina_branca_e_detetada(tmp_path):
    blank = make_blank(tmp_path / "blank.png")
    assert imaging.ink_fraction(blank) < THRESHOLD
    assert imaging.is_blank_page(blank, THRESHOLD)


def test_ruido_de_scanner_continua_a_ser_branca(tmp_path):
    speckled = make_speckled_blank(tmp_path / "speckled.png")
    assert imaging.is_blank_page(speckled, THRESHOLD)


def test_folha_com_grelha_nao_e_branca(tmp_path):
    sheet = make_sheet_like(tmp_path / "sheet.png")
    assert imaging.ink_fraction(sheet) > 0.008
    assert not imaging.is_blank_page(sheet, THRESHOLD)


def test_imagem_ilegivel_nao_e_declarada_branca(tmp_path):
    corrupt = tmp_path / "corrupt.png"
    corrupt.write_bytes(b"isto nao e um png")
    # na dúvida deixa-se o OCR tentar — nunca esconder conteúdo por erro nosso
    assert not imaging.is_blank_page(corrupt, THRESHOLD)


@pytest.fixture()
def staging(tmp_path, monkeypatch):
    real_connect = db.connect
    monkeypatch.setattr(db, "connect", lambda path=None: real_connect(tmp_path / "test.db"))
    monkeypatch.setattr(main, "run_cross_check", lambda conn, uid: None)
    return tmp_path


class ProviderFake:
    """Provider controlável: rebenta se `explode`, senão devolve uma linha."""

    name = "fake"

    def __init__(self, explode: bool = False):
        self.explode = explode

    def extract_auto(self, image_path, templates):
        assert not self.explode, "página em branco não pode chegar ao OCR"
        template = templates["producao"]
        return "producao", {
            "header": {f: None for f in template.header_fields},
            "rows": [dict.fromkeys(template.row_fields) | {"of": "263323"}],
            "footer": {f: None for f in template.footer_fields},
        }


def _create(image: Path) -> str:
    conn = db.connect()
    try:
        return db.create_sheet(conn, "cantoneiras_kanban", str(image), "sha-teste")
    finally:
        conn.close()


def _sheet(uid: str) -> dict:
    conn = db.connect()
    try:
        return db.get_sheet(conn, uid)
    finally:
        conn.close()


def test_worker_nao_gasta_ocr_em_pagina_branca(staging, monkeypatch):
    """A folha fica marcada _blank_page e o provider nunca é chamado."""
    monkeypatch.setattr(main, "get_provider", lambda: ProviderFake(explode=True))
    uid = _create(make_blank(staging / "blank.png"))

    main._process_sheet(uid)

    sheet = _sheet(uid)
    assert sheet["status"] == "extracted"
    assert sheet["raw_extraction"].get("_blank_page") is True
    assert all(v is None for row in sheet["raw_extraction"]["rows"] for v in row.values())


def test_force_ocr_ignora_a_detecao(staging, monkeypatch):
    """`?force=1` no re-OCR: o revisor manda mais que a heurística."""
    monkeypatch.setattr(main, "get_provider", lambda: ProviderFake())
    uid = _create(make_blank(staging / "blank.png"))

    main._process_sheet(uid, force_ocr=True)

    sheet = _sheet(uid)
    assert not sheet["raw_extraction"].get("_blank_page")
    assert sheet["raw_extraction"]["rows"][0]["of"] == "263323"


def test_folha_real_continua_a_ser_lida(staging, monkeypatch):
    monkeypatch.setattr(main, "get_provider", lambda: ProviderFake())
    uid = _create(make_sheet_like(staging / "sheet.png"))

    main._process_sheet(uid)

    sheet = _sheet(uid)
    assert not sheet["raw_extraction"].get("_blank_page")
    assert sheet["raw_extraction"]["rows"][0]["of"] == "263323"
