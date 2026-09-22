"""Linked Cantoneiras rows remain validatable despite cross-check warnings."""
from dataclasses import replace
from urllib.parse import unquote_plus

import pytest

from app import db
from app.matching.params import CrossParams
from app.matching.refs import PlanIndex
from app.matching.scorer import Scorer
from app.web import main
from tests.test_web import client, make_index  # noqa: F401


@pytest.mark.parametrize("engine", ["legacy", "v3"])
@pytest.mark.parametrize("warning", ["profile", "low_confidence", "over_limit"])
def test_linked_row_validates_with_advisory_warnings(client, monkeypatch, engine, warning):
    profile = "L50X50X5"
    entries = [dict(
        plan_key=f"test:{i}", snapshot_id="test", of=f"OF{42+i}", ov=f"OV{21+i}",
        cliente="CLIENTE", cliente_nome="CLIENTE", perfil=profile, modelo=f"REF-{i}",
        comp_mm=1000, qtd_planeada=10, qtd_restante=4, falta_valida=True,
    ) for i in range(3)]
    index = PlanIndex(entries, make_index().spec, snapshot_id="test")
    # Keep the advisory evidence visible through legacy's materialization loop.
    # This exercises its supported suggestion-only policy without changing it.
    params = CrossParams()
    params.policy = replace(params.policy, replace_with_plan=False,
                            write_threshold_identity=1, write_threshold_critical_dim=1,
                            write_threshold_default=1)
    scorer = Scorer(index, params)
    monkeypatch.setattr(main, "settings", replace(main.settings, cross_engine=engine))
    monkeypatch.setattr(main, "get_index", lambda *_args, **_kwargs: index)
    monkeypatch.setattr(main, "make_scorer", lambda *_args, **_kwargs: scorer)
    monkeypatch.setattr(main.loaders, "plan_snapshot_info", lambda: {"snapshot_id": "test"})
    monkeypatch.setattr(main, "_load_header_machines", lambda: [])
    monkeypatch.setattr(main, "get_employees", lambda: {})
    row = {"of": "42", "modelo": "REF-0", "perfil": profile, "qtd": "2"}
    if warning == "profile":
        row["perfil"] = "L99X99X9"
    elif warning == "low_confidence":
        row = {"perfil": profile, "qtd": "2"}
    elif warning == "over_limit":
        row["qtd"] = "99"
    with db.connect() as conn:
        uid = db.create_sheet(conn, "cantoneiras_kanban")
        db.set_extraction(conn, uid, {
            "header": {"operador": "ANA", "data": "22/09/2026"}, "rows": [row], "footer": {},
        })
        original = db.get_sheet(conn, uid)["raw_extraction"]
        main.run_cross_check(conn, uid, scorer_override=scorer,
                             engine_override=engine, historical_context_override=None)
        sheet = db.get_sheet(conn, uid)
    check = sheet["cross_check"]["rows"][0]
    assert check["matched_plan_key"] == "test:0"
    cells = {cell["field"]: cell for cell in check["cells"]}
    if warning == "profile":
        assert cells["perfil"]["status"] == "very_different"
    elif warning == "low_confidence":
        assert check["mode"] == "weak_guess" and check["p_correct"] < .5
    elif warning == "over_limit":
        assert cells["qtd"]["status"] == "over_limit"

    response = client.post(f"/sheet/{uid}/validate", data={"revision": sheet["revision"]})
    assert response.status_code == 303
    assert "erro=" not in response.headers["location"], unquote_plus(response.headers["location"])
    with db.connect() as conn:
        validated = db.get_sheet(conn, uid)
    assert validated["status"] == "validated"
    assert validated["raw_extraction"] == original
    assert len(client.stored_calls) == 1
    stored = client.stored_calls[0]["sheet"]
    assert stored["sheet_data"]["rows"][0]["qtd"] == row["qtd"]
    stored_check = stored["cross_check"]["rows"][0]
    assert stored_check["matched_plan_key"] == "test:0"
    if warning == "low_confidence":
        assert stored_check["p_correct"] < .5
    else:
        assert any(cell["status"] == {"profile": "very_different", "over_limit": "over_limit"}[warning]
                   for cell in stored_check["cells"])
    client.post(f"/sheet/{uid}/validate", data={"revision": validated["revision"]})
    assert len(client.stored_calls) == 1
