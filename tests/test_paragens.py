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


def make_inked_image(path):
    """Imagem com tinta suficiente para passar a deteção de página em branco."""
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (400, 300), "white")
    d = ImageDraw.Draw(im)
    for y in range(20, 280, 30):
        d.line([(10, y), (390, y)], fill="black", width=3)
    im.save(path)
    return path


def test_classificacao_escolhe_template_paragens(client, tmp_path, monkeypatch):
    img = make_inked_image(tmp_path / "verso.png")

    class FakeProvider:
        name = "fake"

        def extract_auto(self, image_path, templates):
            template = templates["paragens"]
            return "paragens", {
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
    img = make_inked_image(tmp_path / "f.png")

    calls = {"n": 0}

    class FlakyProvider:
        name = "flaky"

        def extract_auto(self, image_path, templates):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ocr.OcrError("Gemini HTTP 429: quota")
            template = templates["producao"]
            return "producao", {
                "header": {f: None for f in template.header_fields},
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


def test_lote_retenta_falhas_transitorias(client, tmp_path, monkeypatch):
    """503 a meio do lote: segunda passagem única apanha a folha, sem clique."""
    img = make_inked_image(tmp_path / "x.png")
    calls = {"n": 0}

    class Flaky503:
        name = "flaky"

        def extract_auto(self, image_path, templates):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ocr.OcrError("Gemini [m] HTTP 503: high demand")
            t = templates["producao"]
            return "producao", {
                "header": {f: None for f in t.header_fields},
                "rows": [{f: "ok" if f == "of" else None for f in t.row_fields}],
                "footer": {f: None for f in t.footer_fields}}

    monkeypatch.setattr(main, "get_provider", lambda: Flaky503())
    monkeypatch.setattr(main, "get_index", lambda name: __import__(
        "tests.test_web", fromlist=["make_index"]).make_index())
    monkeypatch.setattr(main, "_RETRY_DELAY_S", 0.0)
    monkeypatch.setattr(main, "_BATCH_SLEEP_S", 0.0)

    r = client.post("/upload", data={"template_name": "cantoneiras_kanban"},
                    files=[("photos", ("x.png", img.read_bytes(), "image/png"))])
    uid = r.headers["location"].rsplit("/", 1)[1]
    conn = db.connect()
    try:
        sheet = db.get_sheet(conn, uid)
    finally:
        conn.close()
    assert calls["n"] == 2
    assert not sheet["sheet_data"].get("_ocr_error")
    assert sheet["sheet_data"]["rows"][0]["of"] == "ok"


def test_quota_429_nao_e_retentada_no_lote(client, tmp_path, monkeypatch):
    img = make_inked_image(tmp_path / "y.png")
    calls = {"n": 0}

    class Quota:
        name = "quota"

        def extract_auto(self, image_path, templates):
            calls["n"] += 1
            raise ocr.OcrError("Gemini [m] HTTP 429: quota exceeded")

    monkeypatch.setattr(main, "get_provider", lambda: Quota())
    monkeypatch.setattr(main, "_RETRY_DELAY_S", 0.0)
    monkeypatch.setattr(main, "_BATCH_SLEEP_S", 0.0)

    r = client.post("/upload", data={"template_name": "cantoneiras_kanban"},
                    files=[("photos", ("y.png", img.read_bytes(), "image/png"))])
    uid = r.headers["location"].rsplit("/", 1)[1]
    conn = db.connect()
    try:
        sheet = db.get_sheet(conn, uid)
    finally:
        conn.close()
    assert calls["n"] == 1, "quota não recupera em segundos — fica para o clique"
    assert "429" in sheet["sheet_data"]["_ocr_error"]


def test_parse_duracao():
    from app.matching import similarity as sim
    assert sim.parse_number("1H") == 1.0
    assert sim.parse_number("6.5") == 6.5


def test_extract_auto_uma_chamada_por_pagina(tmp_path):
    """kind + transcrição vêm do MESMO pedido; kind inválido cai para produção."""
    import json as jsonlib
    import types

    from app.templates_spec import CANTONEIRAS_KANBAN, CANTONEIRAS_PARAGENS

    templates = {"producao": CANTONEIRAS_KANBAN, "paragens": CANTONEIRAS_PARAGENS}
    img = make_inked_image(tmp_path / "p.png")

    def fake_call(payload):
        def _call(self, body):
            _call.n += 1
            return {"candidates": [{"content": {"parts": [
                {"text": jsonlib.dumps(payload)}]}}]}
        _call.n = 0
        return _call

    p = ocr.GeminiOcrProvider(api_key="t", model="m")
    call = fake_call({"kind": "paragens", "header": {"operador": "Zé"},
                      "rows": [{"motivo": "Avaria", "of": "ignorado-na-face-errada"}],
                      "footer": {}})
    p._call = types.MethodType(call, p)
    kind, out = p.extract_auto(img, templates)
    assert call.n == 1, "uma chamada, não duas"
    assert kind == "paragens"
    assert out["rows"][0]["motivo"] == "Avaria"
    assert "of" not in out["rows"][0], "campos da outra face ficam de fora"

    # kind em falta/inválido → primeira face (produção), como o classificador antigo
    call = fake_call({"kind": "outra-coisa", "header": {}, "rows": [], "footer": {}})
    p._call = types.MethodType(call, p)
    kind, out = p.extract_auto(img, templates)
    assert kind == "producao"
    assert set(out["rows"][0]) == set(CANTONEIRAS_KANBAN.row_fields)


def test_auto_schema_cobre_as_duas_faces():
    from app.templates_spec import CANTONEIRAS_KANBAN, CANTONEIRAS_PARAGENS

    p = ocr.GeminiOcrProvider(api_key="t", model="m")
    schema = p._auto_schema({"producao": CANTONEIRAS_KANBAN,
                             "paragens": CANTONEIRAS_PARAGENS})
    assert schema["properties"]["kind"]["enum"] == ["producao", "paragens"]
    row_props = set(schema["properties"]["rows"]["items"]["properties"])
    assert set(CANTONEIRAS_KANBAN.row_fields) <= row_props
    assert set(CANTONEIRAS_PARAGENS.row_fields) <= row_props