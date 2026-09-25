import copy
from datetime import datetime, timezone
import pytest
from app import historical_quantities as h


def example():
    data = {"header": {"data": "16/09/2026"}, "rows": [
        {"of": "264534", "perfil": "L55X55X5", "perf_comp": "X"}]}
    return {"status": "in_review", "sheet_data": data}, data, {"rows": [{"row_index": 0, "matched_plan_key": "current"}]}


def entries():
    return [{"plan_key": ref, "production_order_no": "264534", "profile_type": "L55X55X5",
        "component_ref": ref, "length_mm": 1000, "remaining_quantity": qty,
        "remaining_valid": True, "remaining_rule": "calculated:qtd_minus_maq"}
        for ref, qty in [("EA8B78", 54), ("EA8B79", 52)]]


def test_strict_lisbon_cutoff_dst_and_future_rejection():
    snapshots = [{"snapshot_id": sid, "loaded_at": date} for sid, date in [
        ("before", "2026-09-15T22:59:59+00:00"), ("midnight", "2026-09-15T23:00:00+00:00"),
        ("same_day", "2026-09-16T12:00:00+00:00"), ("future", "2026-09-17T00:00:00+00:00")]]
    assert h.select_snapshot(snapshots, "16/09/2026")["snapshot_id"] == "before"
    assert h.production_cutoff("16/01/2026") == datetime(2026, 1, 16, tzinfo=timezone.utc)
    with pytest.raises(ValueError): h.select_snapshot(snapshots[1:], "16/09/2026")


def test_new_zero_plan_cannot_change_frozen_basis_but_date_and_decision_can():
    sheet, data, cross = example()
    calls = []
    def snapshot(day):
        calls.append(day)
        return {"snapshot_id": "past", "loaded_at": "2026-09-15T12:00:00Z"}
    h.apply(sheet, data, cross, snapshot_loader=snapshot, order_loader=lambda *a: entries())
    assert cross["rows"][0]["full_profile_quantity"] == 106
    sheet["cross_check"] = copy.deepcopy(cross)
    again = {"rows": [{"row_index": 0, "matched_plan_key": "new-zero-plan"}]}
    h.apply(sheet, data, again, snapshot_loader=lambda *_: pytest.fail("must reuse frozen basis"))
    assert again["rows"][0]["plan_refs"] == cross["rows"][0]["plan_refs"]
    data["header"]["data"] = "17/09/2026"
    h.apply(sheet, data, again, snapshot_loader=snapshot, order_loader=lambda *a: entries())
    assert calls == ["2026-09-16", "2026-09-17"]
    sheet["cross_check"] = copy.deepcopy(again)
    h.apply(sheet, data, again, decisions=[{"id": 1, "source": "human", "field_path": "rows[0].of"}],
            snapshot_loader=snapshot, order_loader=lambda *a: entries())
    assert len(calls) == 3


@pytest.mark.parametrize("failure", ["no_history", "unknown", "duplicate", "unresolved"])
def test_unknown_balances_block_without_zero(failure):
    sheet, data, cross = example()
    rows = entries()
    if failure == "unknown": rows[0]["remaining_quantity"] = None
    if failure == "duplicate": rows.append({**rows[0], "plan_key": "dup"})
    if failure == "unresolved": data["rows"][0]["_identity_unresolved"] = True
    def snapshot(day):
        if failure == "no_history": raise ValueError("Sem histórico")
        return {"snapshot_id": "past"}
    h.apply(sheet, data, cross, snapshot_loader=snapshot, order_loader=lambda *a: rows)
    assert cross["rows"][0]["full_profile_quantity"] is None
    assert not cross["rows"][0]["plan_refs_valid"]


def test_validated_facts_immutable():
    sheet, data, cross = example()
    sheet["status"] = "validated"
    before = copy.deepcopy(cross)
    h.apply(sheet, data, cross, snapshot_loader=lambda *_: pytest.fail("validated"))
    assert cross == before


def test_x_in_quantity_is_a_full_profile_mark_not_zero():
    from app.templates_spec import field_value, get_template
    from app.production_facts import materialize_sheet
    sheet, data, cross = example()
    data['rows'][0].update(qtd='X', perf_comp=None)
    assert field_value(data['rows'][0], 'perf_comp') == 'X'
    h.apply(sheet, data, cross, snapshot_loader=lambda day:{'snapshot_id':'past'},
            order_loader=lambda *a:entries())
    sheet['cross_check'] = cross
    fact = materialize_sheet(sheet, get_template('cantoneiras_kanban'))['parents'][0]
    assert fact['row']['qtd'] == 106 and fact['row']['perf_comp'] == 'X'


def test_of_que_entra_no_plano_no_proprio_dia_usa_a_primeira_carga_com_a_of():
    """Caso real OF263210: nenhuma carga anterior ao dia de produção tinha a OF."""
    sheet, data, cross = example()
    orders = {"before": [], "same_day": entries()}
    h.apply(sheet, data, cross,
            snapshot_loader=lambda day: {"snapshot_id": "before",
                                         "loaded_at": "2026-09-15T12:18:00+00:00"},
            later_loader=lambda day: [{"snapshot_id": "same_day",
                                       "loaded_at": "2026-09-16T12:18:00+00:00"}],
            order_loader=lambda snapshot_id, of: orders[snapshot_id])
    row = cross["rows"][0]
    assert row["quantity_basis"]["status"] == "ready"
    assert row["quantity_basis"]["approximate"] is True
    assert row["quantity_basis"]["snapshot_id"] == "same_day"
    assert row["full_profile_quantity"] == 106


def test_sem_nenhuma_carga_com_a_of_fica_por_confirmar_sem_inventar_zero():
    sheet, data, cross = example()
    def no_before(day):
        raise ValueError("Não existe plano guardado antes do dia de produção.")
    h.apply(sheet, data, cross, snapshot_loader=no_before, later_loader=lambda day: [],
            order_loader=lambda *a: entries())
    row = cross["rows"][0]
    assert row["quantity_basis"]["status"] == "unavailable"
    assert row["plan_refs"] == [] and row["full_profile_quantity"] is None


def test_correspondencia_fraca_ja_nao_impede_o_saldo():
    sheet, data, cross = example()
    cross["rows"][0].update(mode="weak_guess", review_required=True)
    h.apply(sheet, data, cross, snapshot_loader=lambda day: {"snapshot_id": "past"},
            order_loader=lambda *a: entries())
    assert cross["rows"][0]["quantity_basis"]["status"] == "ready"
    assert cross["rows"][0]["quantity_basis"]["approximate"] is False


def test_cargas_seguintes_por_ordem_a_partir_do_dia_de_producao():
    snapshots = [{"snapshot_id": sid, "loaded_at": date} for sid, date in [
        ("before", "2026-09-15T22:59:59+00:00"), ("later", "2026-09-17T12:00:00+00:00"),
        ("midnight", "2026-09-15T23:00:00+00:00")]]
    assert [s["snapshot_id"] for s in h.select_later_snapshots(snapshots, "16/09/2026")] == [
        "midnight", "later"]
