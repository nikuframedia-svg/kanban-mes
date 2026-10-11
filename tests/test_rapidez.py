"""Rapidez (11/10): o que antes voltava ao Postgres ou lia tudo em cada clique."""

import pytest

from app import db, historical_quantities as h
from app.matching import loaders
from app.matching.loaders import CANTONEIRAS_SPEC
from app.matching.refs import PlanIndex
from app.web import main
from tests.test_historical_quantities import entries, example
from tests.test_web import client, create_sheet, edit  # noqa: F401


def counting_fetch(monkeypatch, rows):
    calls = []

    def fetch(sql, params=None):
        calls.append(sql)
        return rows(sql) if callable(rows) else rows
    monkeypatch.setattr(loaders, "_fetch", fetch)
    return calls


def test_linhas_de_uma_carga_e_lista_de_cargas_vem_do_postgres_uma_vez(monkeypatch):
    calls = counting_fetch(monkeypatch, lambda sql: entries() if "kanban_plan_lines" in sql
                           else [{"snapshot_id": "s1", "loaded_at": None}])
    for _ in range(3):
        assert len(h.load_order("s1", "264534")) == 2
        h._plan_snapshots()
    assert sum("kanban_plan_lines" in c for c in calls) == 1
    assert sum("audit_mtg.snapshots" in c for c in calls) == 1


def test_falha_de_ligacao_nao_se_repete_por_cada_linha():
    sheet, data, cross = example()
    data["rows"].append(dict(data["rows"][0]))
    cross["rows"].append({"row_index": 1, "matched_plan_key": "current"})
    tries = []

    def down(day):
        tries.append(day)
        raise OSError("túnel em baixo")
    h.apply(sheet, data, cross, snapshot_loader=down, order_loader=lambda *a: entries())
    assert len(tries) == 1, "a segunda linha não volta a esperar pelo mesmo timeout"
    assert all(r["quantity_basis"]["transient"] for r in cross["rows"])


def test_carga_atual_do_plano_perguntada_uma_vez_em_15_segundos(monkeypatch):
    calls = counting_fetch(monkeypatch, [{"snapshot_id": "mtg_x", "loaded_at": None,
                                          "source_filename": "f"}])
    for _ in range(4):
        assert loaders.plan_snapshot_info()["snapshot_id"] == "mtg_x"
    assert len(calls) == 1


def test_procura_por_chave_do_plano_sem_percorrer_as_linhas():
    index = PlanIndex([{"plan_key": f"k{i}", "of": f"OF{i}"} for i in range(1000)],
                      CANTONEIRAS_SPEC, snapshot_id="s")
    assert index.indices_of("k734") == [734] and index.indices_of("nada") == []


def test_validar_nao_refaz_o_cruzamento_quando_a_folha_nao_mudou(client, monkeypatch):
    uid = create_sheet(client)
    edit(client, uid, "rows[0].of", "OF250001")
    edit(client, uid, "header.operador", "João")
    edit(client, uid, "header.data", "2026-08-06")
    monkeypatch.setattr(main, "run_cross_check", lambda *a, **k: pytest.fail(
        "o cruzamento já descrevia esta revisão"))
    response = client.post(f"/sheet/{uid}/validate", data={"actor": "luis"})
    assert response.status_code == 303 and "erro=" not in response.headers["location"]


def test_historico_le_so_a_pagina_pedida(client):
    uids = [create_sheet(client) for _ in range(5)]
    conn = db.connect()
    try:
        page = db.list_sheets(conn, limit=2, offset=2)
        assert db.count_sheets(conn) == 5 and len(page) == 2
        everything = [s["uid"] for s in db.list_sheets(conn)]
        assert [s["uid"] for s in page] == everything[2:4]
        assert set(everything) == set(uids)
        assert sum(db.status_counts(conn).values()) == 5
    finally:
        conn.close()
    assert client.get("/").status_code == 200


def test_cabecalho_recuperado_na_leitura_e_nao_ao_abrir(client, tmp_path, monkeypatch):
    """Abrir uma folha com «N.º» em branco punha o OCR a correr com o operador
    à espera; agora a tentativa fica feita na leitura em segundo plano."""
    from app import header_recovery
    from app.web import header_recovery_routes as routes
    from tests.test_header_recovery import EMPLOYEES, MACHINES, Provider, create
    monkeypatch.setattr(main, "get_provider", lambda: Provider())
    monkeypatch.setattr(main, "get_employees", lambda: EMPLOYEES)
    monkeypatch.setattr(main, "_load_header_machines", lambda: MACHINES)
    with db.connect() as conn:
        uid = create(conn, tmp_path)
        assert routes.header_eligible(conn, db.get_sheet(conn, uid))
        routes.recover_at_ingest(conn, uid)
        sheet = db.get_sheet(conn, uid)
        assert header_recovery.current_recovery(sheet)
        assert not routes.header_eligible(conn, sheet), "ao abrir já não relê o cabeçalho"
