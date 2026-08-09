"""PERF. COMP. como marca, e Qtd como limite superior.

A última coluna da TPL102 esteve modelada como comprimento em milímetros. Nas
folhas reais nunca teve milímetros: tem vistos. Quando tem visto, a linha vale
por todas as referências daquele perfil na obra — daí não ter modelo nem
quantidade.
"""

import pytest

from app.matching.cross_check import check_row, check_sheet, plan_quantity_for
from app.matching.carryover import resolve
from app.matching.params import CrossParams
from app.matching.refs import FieldSpec, IndexSpec, PlanIndex
from app.matching.scorer import Scorer
from app.templates_spec import CANTONEIRAS_KANBAN, field_value, is_marked

SPEC = IndexSpec(
    identity_fields=(
        FieldSpec("of", "code", "of", code_prefix="OF", max_candidate_entries=None),
        FieldSpec("modelo", "code", "modelo"),
        FieldSpec("perfil", "profile", "perfil"),
    ),
    key_field="plan_key",
)

PLANO = [
    {"plan_key": "K1", "of": "OF263323", "modelo": "AT1T220", "perfil": "L40X40X4",
     "qtd_planeada": 56},
    {"plan_key": "K2", "of": "OF263323", "modelo": "AEH89", "perfil": "L40X40X4",
     "qtd_planeada": 12},
    {"plan_key": "K3", "of": "OF263323", "modelo": "AEH90", "perfil": "L40X40X4",
     "qtd_planeada": 12},
]


def scorer():
    return Scorer(PlanIndex(PLANO, SPEC), CrossParams())


def cells_of(row, **kw):
    check = check_row(row, 0, scorer(), **kw)
    return {c.field: c for c in check.cells}


# ---- a marca ----

@pytest.mark.parametrize("valor,esperado", [
    ("x", True), ("X", True), (" x ", True), ("✓", True),
    ("1", False), ("150", False), (None, False), ("", False),
])
def test_is_marked(valor, esperado):
    assert is_marked(valor) is esperado


def test_coluna_da_folha_chama_se_perf_comp():
    assert CANTONEIRAS_KANBAN.row_fields[-1] == "perf_comp"
    assert "mm" not in CANTONEIRAS_KANBAN.field_labels["perf_comp"].lower()


def test_folhas_lidas_antes_da_mudanca_continuam_a_ser_lidas():
    """`raw_extraction` é a transcrição original — não se reescreve para arrumar
    o schema, lê-se pelos dois nomes."""
    assert field_value({"comp_mm": "x"}, "perf_comp") == "x"
    assert field_value({"perf_comp": "x"}, "perf_comp") == "x"


def test_marca_nao_e_cruzada_como_comprimento():
    """Antes, o motor propunha escrever milímetros do plano por cima do visto."""
    cells = cells_of({"of": "263323", "modelo": "AEH89", "perf_comp": "x"})
    assert "perf_comp" not in cells or cells["perf_comp"].proposal is None


# ---- o limite ----

def test_quantidade_dentro_do_plano_fica_confirmada():
    cells = cells_of({"of": "263323", "modelo": "AT1T220", "qtd": "50"})
    assert cells["qtd"].status == "confirmed"
    assert cells["qtd"].plan_limit == 56


def test_quantidade_acima_do_plano_e_assinalada_mas_nao_bloqueia():
    cells = cells_of({"of": "263323", "modelo": "AEH89", "qtd": "20"})
    assert cells["qtd"].status == "over_limit"
    assert cells["qtd"].plan_limit == 12
    assert cells["qtd"].auto_write is False, "nunca reescrever produção"
    assert cells["qtd"].proposal is None, "o plano não dita o que foi produzido"


def test_linha_marcada_nao_leva_limite():
    """A linha de perfil completo não tem quantidade própria: vale por todas."""
    cells = cells_of({"of": "263323", "perfil": "40x4", "qtd": "999", "perf_comp": "x"})
    assert "qtd" not in cells or cells["qtd"].plan_limit is None


def test_valor_que_nao_e_numero_nao_dispara_alarme():
    """`2x` daria 2 em parse_number, `1+1` daria 11 — nenhum é uma quantidade."""
    for escrito in ("2x", "1+1", "aprox"):
        cells = cells_of({"of": "263323", "modelo": "AEH89", "qtd": escrito})
        assert "qtd" not in cells or cells["qtd"].plan_limit is None, escrito


def test_limite_soma_quando_a_referencia_aparece_repetida():
    plano = PLANO + [{"plan_key": "K4", "of": "OF263323", "modelo": "AEH89",
                      "perfil": "L40X40X4", "qtd_planeada": 8}]
    index = PlanIndex(plano, SPEC)
    assert plan_quantity_for(index, "263323", "AEH89") == 20


def test_sem_of_nao_ha_limite():
    index = PlanIndex(PLANO, SPEC)
    assert plan_quantity_for(index, "", "AEH89") is None
    assert plan_quantity_for(index, "999999", "AEH89") is None


def test_limite_usa_a_identidade_herdada():
    """A linha que não repete a OF continua a ser verificada contra o plano."""
    rows = [
        {"of": "263323", "modelo": "AT1T220", "qtd": "10"},
        {"modelo": "AEH89", "qtd": "20"},          # sem OF escrita
    ]
    res = check_sheet(rows, scorer(), {})
    qtd = {c["field"]: c for c in res["rows"][1]["cells"]}["qtd"]
    assert qtd["status"] == "over_limit"
    assert qtd["plan_limit"] == 12


def test_linha_marcada_nao_recebe_proposta_de_modelo():
    """A linha de perfil completo vale por todas as referências do perfil.

    Propor-lhe «o» modelo seria escolher uma à sorte entre dezenas; o que
    aquela linha precisa é da lista, que é o que o pop-up mostra.
    """
    cells = cells_of({"of": "263323", "perfil": "40x4", "perf_comp": "x"})
    assert cells["modelo"].proposal is None
    assert cells["modelo"].status == "na"


def test_celula_herdada_confere_contra_o_valor_herdado():
    """A célula em branco por «idem» não é «vazia»: vale o valor de cima."""
    rows = [
        {"of": "263323", "modelo": "AEH89"},
        {"modelo": "AEH90"},                 # herda a OF
    ]
    res = check_sheet(rows, scorer(), {})
    of_cell = {c["field"]: c for c in res["rows"][1]["cells"]}["of"]
    assert of_cell["status"] == "confirmed", "herdado e igual ao plano = confirmado"
    assert of_cell["written"] is None, "no papel continua em branco"
    assert of_cell["inherited_from"] == 0
