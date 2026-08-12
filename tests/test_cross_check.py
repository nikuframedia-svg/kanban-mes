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
    by_field = {c.field: c for c in rc.cells}
    assert by_field["of"].status == "unmatched", "OF que NÃO existe continua vermelha"


def test_of_exata_confirma_mesmo_sem_linha_credivel():
    """Caso real cd83d1: OF escrita que existe exata no plano ficava vermelha
    («unmatched») só porque o resto da linha não casava com nada. A linha
    continua sem ligação, mas cada célula valida-se contra o plano: o que
    existe fica verde, o que não existe fica vermelho."""
    from app.matching.loaders import CANTONEIRAS_SPEC
    from app.matching.params import CrossParams
    from app.matching.refs import PlanIndex
    from app.matching.scorer import Scorer

    entries = []
    for i in range(3):
        entries.append({"plan_key": f"A{i}", "of": "OF262796", "ov": "OV2603660",
                        "cliente": "C.M.E.", "modelo": f"QS12{i}", "perfil": "L60X60X4"})
        entries.append({"plan_key": f"B{i}", "of": "OF262797", "ov": "OV2699999",
                        "cliente": "OUTRO", "modelo": f"QA4{i}", "perfil": "L80X80X8"})
    s = Scorer(PlanIndex(entries, CANTONEIRAS_SPEC, plan_age_days=30.0), CrossParams())
    # OF de uma obra, OV de outra (contradição), modelo e perfil inexistentes:
    # nenhuma linha do plano é credível, mas OF e OV existem lá
    row = {"of": "262796", "ov": "2699999", "modelo": "ZZZ 999", "perfil": "45 x 9"}
    rc = check_row(row, 0, s)
    assert rc.p_correct < s.params.policy.propose_threshold, "cenário deve cair no ramo H₀"
    assert rc.matched_plan_key is None, "sem linha credível não há ligação"
    by_field = {c.field: c for c in rc.cells}
    assert by_field["of"].status == "confirmed", \
        "a OF existe no plano — não pode aparecer como inexistente"
    assert by_field["ov"].status == "confirmed"
    assert by_field["of"].proposal is None and not by_field["of"].auto_write
    assert by_field["modelo"].status == "unmatched"
    assert by_field["perfil"].status == "unmatched"


def test_auto_write_nao_usa_probabilidade_de_outro_valor():
    """Reproduzido em revisão: o marginal do campo apontava para o valor da
    obra rival (p=0.91) e era ESSA probabilidade que autorizava gravar o valor
    do vencedor (p real 0.007). O marginal só vale se apontar para a proposta."""
    from app.matching.loaders import CANTONEIRAS_SPEC
    from app.matching.params import CrossParams
    from app.matching.refs import PlanIndex
    from app.matching.scorer import Scorer

    # obra gigante A (perfil L60X60X5) domina o marginal do perfil;
    # 1 linha da obra B com perfil raro L99X99X9
    entries = [
        {"plan_key": f"A{i}", "of": "OF111111", "ov": "OV1", "modelo": f"MA{i:04d}",
         "perfil": "L60X60X5"}
        for i in range(300)
    ]
    entries.append({"plan_key": "B0", "of": "OF222222", "ov": "OV2",
                    "modelo": "MB0001", "perfil": "L99X99X9"})
    s = Scorer(PlanIndex(entries, CANTONEIRAS_SPEC), CrossParams())
    # linha ligada à obra B (of+modelo), perfil em branco: a proposta é
    # L99X99X9; o marginal do perfil (dominado pela obra A) diz L60X60X5
    rc = check_row({"of": "222222", "modelo": "MB0001"}, 0, s)
    perfil = next((c for c in rc.cells if c.field == "perfil"), None)
    if perfil is not None and perfil.proposal:
        assert perfil.proposal == "L99X99X9"
        # a confiança da célula nunca pode vir do valor rival
        assert not (perfil.auto_write and perfil.p_correct > rc.p_correct + 0.3), \
            "p emprestada de outro valor a autorizar escrita"


def test_aspas_de_idem_cruzam_como_heranca():
    """Linha com «"» em cliente/OV/OF: cruza com a identidade herdada, mostra
    a herança, e nunca propõe escrever por cima da aspa."""
    s = make_scorer()
    rows = [
        {"of": "OF259999", "ov": "OV2409999", "cliente": "SILVA & VINHA SA",
         "comp_mm": 1234},
        {"of": '"', "ov": '"', "cliente": '"', "comp_mm": 1234},
    ]
    result = check_sheet(rows, s)
    linha = result["rows"][1]
    by_field = {c["field"]: c for c in linha["cells"]}
    assert linha["matched_plan_key"] == "B0", "a herança liga a linha ao plano"
    assert by_field["of"]["inherited"] == "OF259999"
    assert by_field["of"]["written"] is None, "a aspa não é um valor escrito"
    assert not by_field["of"]["auto_write"], "herdado nunca se auto-escreve"
    assert result["summary"]["cells_inherited"] >= 3


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
