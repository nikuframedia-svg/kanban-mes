"""Recolha dos avisos da validação: cada antigo portão vira um aviso."""

from app import validation_warnings as vw


def sheet(rows, checks, header=None, **cross_extra):
    return {
        "sheet_data": {"header": header or {"operador": "ANA", "data": "16/09/2026"},
                       "rows": rows},
        "cross_check": {"snapshot_id": "s1", "plan_reference": {"status": "available"},
                        "rows": checks, **cross_extra},
    }


def test_folha_ligada_ao_plano_nao_tem_avisos():
    clean = sheet([{"of": "1", "qtd": "2"}], [{"row_index": 0, "matched_plan_key": "k"}])
    assert vw.collect(clean, current_snapshot="s1") == []


def test_cada_antigo_portao_vira_um_aviso_com_a_linha_visivel():
    rows = [
        {"of": "1", "qtd": "2"},
        {"of": "2", "perfil": "L55X55X5", "perf_comp": "X"},
        {"of": "3", "_deleted": True},
        {"of": "4", "qtd": "1", "_identity_unresolved": "OF da linha restaurada por confirmar."},
    ]
    checks = [
        {"row_index": 0},
        {"row_index": 1, "matched_plan_key": "k", "plan_refs_valid": False,
         "plan_refs_error": "Sem referências para OF + perfil.",
         "quantity_basis": {"status": "unavailable"}},
        {"row_index": 3, "matched_plan_key": "k2", "binding_status": "stale"},
    ]
    got = vw.collect(sheet(rows, checks, header={"operador": "", "data": ""}, fixed_point=False),
                     current_snapshot="s2", assumed_date="15/09/2026",
                     extra=[{"code": "ultima_coluna", "message": "Última coluna por confirmar."}])
    by_code = {w["code"]: w for w in got}
    assert set(by_code) == {
        "operador_vazio", "data_assumida", "ultima_coluna", "plano_mudou",
        "cruzamento_instavel", "sem_ligacao_ao_plano", "saldo_por_confirmar",
        "identidade_por_confirmar", "escolha_de_outra_carga",
    }
    assert by_code["sem_ligacao_ao_plano"]["row"] == 1
    assert by_code["saldo_por_confirmar"]["row"] == 2
    # a linha apagada não conta para a numeração visível
    assert by_code["escolha_de_outra_carga"]["row"] == 3
    assert by_code["escolha_de_outra_carga"]["row_index"] == 3
    assert "Sem referências para OF + perfil" in by_code["saldo_por_confirmar"]["message"]


def test_saldo_aproximado_e_plano_indisponivel():
    rows = [{"of": "263210", "perfil": "200 X 20", "perf_comp": "X"}]
    checks = [{"row_index": 0, "matched_plan_key": "k", "plan_refs": [{"plan_key": "a"}],
               "plan_refs_valid": True,
               "quantity_basis": {"status": "ready", "approximate": True, "snapshot_id": "mtg_x"}}]
    got = vw.collect(sheet(rows, checks, plan_reference={"status": "no_reference"}),
                     current_snapshot="s9")
    codes = [w["code"] for w in got]
    assert codes == ["plano_indisponivel", "saldo_aproximado"]
    assert "mtg_x" in got[1]["message"]


def test_avisos_de_uma_linha_para_o_registo_de_producao():
    warnings = [{"code": "a", "message": "m1", "row": 1, "row_index": 0},
                {"code": "b", "message": "m2"},
                {"code": "c", "message": "m3", "row": 2, "row_index": 1}]
    assert vw.for_row(warnings, 1) == [{"code": "c", "message": "m3"}]
    assert vw.for_row(None, 0) == []


def test_correspondencia_fraca_valida_com_aviso():
    rows = [{"of": "263210", "perfil": "200 X 20"}]
    checks = [{"row_index": 0, "matched_plan_key": "k", "mode": "weak_guess"}]
    got = vw.collect(sheet(rows, checks))
    assert [w["code"] for w in got] == ["correspondencia_fraca"]
    assert got[0]["row"] == 1
