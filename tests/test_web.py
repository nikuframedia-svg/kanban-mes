"""Testes da camada web: fluxo completo capturar → editar → cruzar → validar,
com staging SQLite temporário, índice do plano sintético e Postgres simulado.
O caminho real para o Postgres é coberto pelo teste E2E manual (não aqui)."""

import pytest
from fastapi.testclient import TestClient

from app import db, pg_store
from app.matching.loaders import CANTONEIRAS_SPEC
from app.matching.refs import PlanIndex
from app.web import main


def make_index() -> PlanIndex:
    entries = [
        {"plan_key": "P1", "of": "OF250001", "ov": "OV2400001",
         "cliente": "SILVA & VINHA SA", "modelo": "L50X50X5", "comp_mm": 1500},
        {"plan_key": "P2", "of": "OF250002", "ov": "OV2400002",
         "cliente": "PROEF EURICO FERREIRA", "modelo": "L60X60X6", "comp_mm": 2000},
    ]
    return PlanIndex(entries, CANTONEIRAS_SPEC, plan_age_days=1.0)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    real_connect = db.connect
    monkeypatch.setattr(db, "connect", lambda path=None: real_connect(tmp_path / "test.db"))
    monkeypatch.setattr(main, "get_index", lambda loader_name: make_index())
    monkeypatch.setattr(main.loaders, "load_active_ofs", lambda: set())

    stored_calls: list[dict] = []

    def fake_store(sheet, template, edit_count, actor):
        stored_calls.append({"sheet": sheet, "template": template,
                             "edit_count": edit_count, "actor": actor})
        rows = (sheet["sheet_data"] or {}).get("rows") or []
        return sum(1 for r in rows
                   if any(v is not None and str(v).strip() != "" for v in r.values()))

    monkeypatch.setattr(pg_store, "store_validated_sheet", fake_store)
    c = TestClient(main.app, follow_redirects=False)
    c.stored_calls = stored_calls
    return c


def create_sheet(client) -> str:
    r = client.post("/upload", data={"template_name": "cantoneiras_kanban"})
    assert r.status_code == 303
    return r.headers["location"].rsplit("/", 1)[1]


def get_revision(client, uid) -> int:
    conn = db.connect()
    try:
        return db.get_sheet(conn, uid)["revision"]
    finally:
        conn.close()


def edit(client, uid, field_path, value):
    r = client.post(f"/sheet/{uid}/edit", data={
        "field_path": field_path, "value": value,
        "revision": get_revision(client, uid), "actor": "teste",
    })
    return r


def test_home_and_capture_render(client):
    assert client.get("/").status_code == 200
    r = client.get("/capture")
    assert r.status_code == 200
    assert "cantoneiras_kanban" in r.text


def test_upload_creates_sheet_and_review_screen_renders(client):
    uid = create_sheet(client)
    r = client.get(f"/sheet/{uid}")
    assert r.status_code == 200
    assert "Cantoneiras" in r.text
    assert "Validar folha" in r.text


def test_edit_row_triggers_cross_check_with_colours(client):
    uid = create_sheet(client)
    assert edit(client, uid, "rows[0].of", "OF250001").status_code == 303
    assert edit(client, uid, "rows[0].comp_mm", "1500").status_code == 303
    r = client.get(f"/sheet/{uid}")
    assert r.status_code == 200
    # a linha cruzou contra o plano sintético e ganhou o P1
    assert "cell-confirmed" in r.text or "cell-snapped" in r.text
    conn = db.connect()
    try:
        sheet = db.get_sheet(conn, uid)
    finally:
        conn.close()
    cross = sheet["cross_check"]
    assert cross["rows"][0]["matched_plan_key"] == "P1"


def test_edit_with_stale_revision_conflicts(client):
    uid = create_sheet(client)
    r = client.post(f"/sheet/{uid}/edit", data={
        "field_path": "rows[0].of", "value": "OF250001",
        "revision": 999, "actor": "teste",
    })
    assert r.status_code == 409


def test_add_row_and_recheck(client):
    uid = create_sheet(client)
    conn = db.connect()
    try:
        n_before = len(db.get_sheet(conn, uid)["sheet_data"]["rows"])
    finally:
        conn.close()
    assert client.post(f"/sheet/{uid}/add-row").status_code == 303
    conn = db.connect()
    try:
        assert len(db.get_sheet(conn, uid)["sheet_data"]["rows"]) == n_before + 1
    finally:
        conn.close()
    assert client.post(f"/sheet/{uid}/recheck").status_code == 303


def test_validate_requires_header_then_stores_and_freezes(client):
    uid = create_sheet(client)
    edit(client, uid, "rows[0].of", "OF250001")

    # sem operador/data → recusa
    assert client.post(f"/sheet/{uid}/validate", data={"actor": "luis"}).status_code == 422

    edit(client, uid, "header.operador", "João")
    edit(client, uid, "header.data", "2026-08-06")
    r = client.post(f"/sheet/{uid}/validate", data={"actor": "luis"})
    assert r.status_code == 303
    assert "stored=" in r.headers["location"]
    assert len(client.stored_calls) == 1
    assert client.stored_calls[0]["actor"] == "luis"

    # imutável depois de validada
    assert client.post(f"/sheet/{uid}/validate", data={"actor": "luis"}).status_code == 409
    assert edit(client, uid, "rows[0].of", "OF999999").status_code == 409
    r = client.get(f"/sheet/{uid}")
    assert r.status_code == 200
    assert "validada" in r.text


def test_dashboard_renders_without_postgres(client):
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "Trabalho em curso" in r.text
