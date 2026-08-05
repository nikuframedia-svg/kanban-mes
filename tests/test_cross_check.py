from app.matching.cross_check import check_row, check_sheet
from app.matching.params import CrossParams
from tests.test_scorer import SPEC, make_index
from app.matching.scorer import Scorer


def make_scorer():
    return Scorer(make_index(), CrossParams())


def test_confirmed_cells():
    s = make_scorer()
    row = {"of": "OF259999", "ov": "OV2409999", "cliente": "SILVA & VINHA SA", "comp_mm": 1234}
    rc = check_row(row, 0, s)
    assert rc.matched_plan_key == "B0"
    by_field = {c.field: c for c in rc.cells}
    assert by_field["of"].status == "confirmed"
    assert by_field["comp_mm"].status == "confirmed"


def test_snap_fills_empty_cell_only_when_confident():
    s = make_scorer()
    # OV em branco: se a confiança passar o limiar, o motor propõe preencher
    row = {"of": "OF259999", "cliente": "SILVA & VINHA", "comp_mm": 1234}
    rc = check_row(row, 0, s)
    by_field = {c.field: c for c in rc.cells}
    assert by_field["ov"].proposal == "OV2409999"
    assert by_field["ov"].status == "snapped"
    if rc.p_correct >= 0.95:
        assert by_field["ov"].auto_write


def test_human_fields_never_overwritten():
    s = make_scorer()
    row = {"of": "OF259999", "ov": "ERRADO-HUMANO", "cliente": "SILVA & VINHA", "comp_mm": 1234}
    rc = check_row(row, 0, s, human_fields={"ov"})
    by_field = {c.field: c for c in rc.cells}
    assert not by_field["ov"].auto_write


def test_unmatched_row_has_no_proposals():
    s = make_scorer()
    row = {"of": "OF990000", "ov": "OV9900000", "cliente": "FANTASMA", "comp_mm": 77777}
    rc = check_row(row, 0, s)
    assert rc.matched_plan_key is None
    assert all(c.proposal is None for c in rc.cells)


def test_check_sheet_summary_and_review_order():
    s = make_scorer()
    rows = [
        {"of": "OF259999", "ov": "OV2409999", "cliente": "SILVA & VINHA SA", "comp_mm": 1234},
        {"of": "OF990000", "ov": "OV9900000", "cliente": "FANTASMA", "comp_mm": 77777},
    ]
    result = check_sheet(rows, s)
    assert result["summary"]["rows"] == 2
    assert result["summary"]["matched"] == 1
    # a linha problemática (1) deve vir primeiro na fila de revisão
    assert result["review_order"][0] == 1
