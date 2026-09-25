"""Escolhas explícitas religadas à mesma linha depois de uma carga nova."""

from app.matching import bindings
from app.matching.loaders import CANTONEIRAS_SPEC
from app.matching.refs import PlanIndex
from tests.test_web import client  # noqa: F401


def index(snapshot, rows):
    return PlanIndex([{"plan_key": f"{snapshot}:plan:{n}", "of": of, "modelo": modelo,
                       "perfil": perfil, "comp_mm": comp}
                      for n, (of, modelo, perfil, comp) in enumerate(rows)],
                     CANTONEIRAS_SPEC, snapshot_id=snapshot)


OLD = {"snapshot_id": "s1", "plan_key": "s1:plan:7", "selected_explicitly": True,
       "identity": {"of": "OF263210", "modelo": "EA8B79", "perfil": "L200X200X20",
                    "comp_mm": 1000}}


def test_escolha_de_outra_carga_religa_a_mesma_linha():
    current = index("s2", [("OF263210", "EA8B78", "L200X200X20", 1000),
                           ("OF263210", "EA8B79", "L200X200X20", 1000),
                           ("OF263210", "EA8B79", "L150X150X15", 1000)])
    got = bindings.reattach({0: OLD}, current)[0]
    assert got["snapshot_id"] == "s2" and got["plan_key"] == "s2:plan:1"
    assert got["reattached_from"] == {"snapshot_id": "s1", "plan_key": "s1:plan:7"}


def test_perfil_noutro_formato_e_a_mesma_linha():
    current = index("s2", [("OF263210", "EA8B79", "200 X 20", 1000)])
    assert bindings.reattach({0: OLD}, current)[0]["plan_key"] == "s2:plan:0"


def test_sem_linha_unica_fica_como_estava():
    twins = index("s2", [("OF263210", "EA8B79", "L200X200X20", 1000),
                         ("OF263210", "EA8B79", "L200X200X20", 1000)])
    assert bindings.reattach({0: OLD}, twins)[0] is OLD
    gone = index("s2", [("OF263210", "EA8B79", "L150X150X15", 1000)])
    assert bindings.reattach({0: OLD}, gone)[0] is OLD


def test_escolha_antiga_sem_identidade_e_escolha_da_carga_atual_nao_mudam():
    legacy = {"snapshot_id": "s1", "plan_key": "s1:plan:7", "selected_explicitly": True}
    current = index("s2", [("OF263210", "EA8B79", "L200X200X20", 1000)])
    assert bindings.reattach({0: legacy}, current)[0] is legacy
    same = {**OLD, "snapshot_id": "s2", "plan_key": "s2:plan:0"}
    assert bindings.reattach({0: same}, current)[0] is same


def test_identidade_de_linha_do_indice_ou_da_consulta():
    assert bindings.identity_of({"production_order_no": "OF1", "component_ref": "A",
                                 "profile_type": "L50X50X5", "length_mm": 920}) == {
        "of": "OF1", "modelo": "A", "perfil": "L50X50X5", "comp_mm": 920}


def test_cruzamento_religa_escolha_feita_antes_da_carga_nova(client, monkeypatch):
    from app import db
    from app.web import main
    from tests.test_web import create_sheet, edit
    current = index("s2", [("OF250001", "EA8B78", "L50X50X5", None),
                           ("OF250001", "EA8B79", "L50X50X5", None)])
    monkeypatch.setattr(main, "get_index", lambda *_a, **_k: current)
    uid = create_sheet(client)
    edit(client, uid, "rows[0].of", "250001")
    old = {"snapshot_id": "s1", "plan_key": "s1:plan:99", "selected_explicitly": True,
           "identity": {"of": "OF250001", "modelo": "EA8B79", "perfil": "L50X50X5",
                        "comp_mm": None}}
    conn = db.connect()
    try:
        sheet = db.get_sheet(conn, uid)
        data = sheet["sheet_data"]
        data["rows"][0]["_plan_binding"] = old
        assert db.save_sheet_data_with_edits(conn, uid, data, sheet["revision"], [
            ("rows[0]._plan_binding", None, old, "human", "teste")])
        assert main.run_cross_check(conn, uid)
        row = db.get_sheet(conn, uid)["cross_check"]["rows"][0]
    finally:
        conn.close()
    assert row["matched_plan_key"] == "s2:plan:1"
