"""Contrato do cruzamento determinístico do cabeçalho (setor de cantoneiras)."""

from datetime import date

from app.matching.header_cross import (
    canonical_date,
    check_header,
    previous_business_day,
    template_machine,
)
from app.matching.operador import Employee
from app.templates_spec import get_template


EMPLOYEES = {
    2105: Employee(cod=2105, pernr="00002105", full_name="MARCO LOPES"),
    3480: Employee(cod=3480, pernr="10003480", full_name="GURPINDER SINGH"),
}

# Nomes reais do catálogo core_mtg.machines; os dois últimos são do setor de
# perfis (app irmã) e nunca podem confirmar uma folha de cantoneiras.
CATALOG = [
    {"display_name": "Ficep Rapid 20T -1"},
    {"display_name": "Ficep Rapid 20T -2"},
    {"display_name": "Ficep Rapid 25T"},
    {"display_name": "Ficep XP T4"},
    {"display_name": "Peddi 8"},
    {"display_name": "Serrote MTG2"},
    {"display_name": "Vanguard MTG2"},
]


def _header(**changes):
    return {
        "operador": None,
        "n_operador": None,
        "setor_maquina": None,
        "data": None,
        "turno": None,
    } | changes


def _check(header, template="cantoneiras_kanban", **sources):
    return check_header(header, get_template(template), **sources)


def test_operador_so_e_auto_substituido_com_identidade_inequivoca():
    result = _check(
        _header(operador="Gurpinder", n_operador="3480"), employees=EMPLOYEES,
    )

    assert result["operator"]["accepted"] is True
    assert result["operator"]["pernr"] == "10003480"
    assert result["cells"]["operador"]["proposal"] == "GURPINDER SINGH"
    assert result["cells"]["operador"]["auto_write"] is True
    assert result["cells"]["n_operador"]["status"] == "confirmed"
    assert result["cells"]["n_operador"]["proposal"] is None
    assert result["cells"]["n_operador"]["auto_write"] is False


def test_numero_de_operador_corrigido_e_apenas_proposta():
    # «348O» é a leitura O↔0 real: identidade provável, número por confirmar.
    result = _check(
        _header(operador="Gurpinder", n_operador="348O"), employees=EMPLOYEES,
    )
    assert result["operator"]["accepted"] is False
    assert result["operator"]["candidate_pernr"] == "10003480"
    for field_name in ("operador", "n_operador"):
        assert result["cells"][field_name]["status"] == "review"
        assert result["cells"][field_name]["auto_write"] is False


def test_sugestao_de_operador_nunca_e_aceite_nem_auto_escrita():
    for name, number in (("FLAVIO LOPES", "3480"), ("GURPINDER SINGH", "3481")):
        result = _check(
            _header(operador=name, n_operador=number), employees=EMPLOYEES,
        )
        assert result["operator"]["accepted"] is False
        assert result["operator"]["pernr"] is None
        assert result["operator"]["candidate_pernr"] == "10003480"
        assert all(
            not result["cells"][field]["auto_write"]
            for field in ("operador", "n_operador")
        )


def test_edicao_humana_bloqueia_canonicalizacao_automatica():
    result = _check(
        _header(operador="Gurpinder", n_operador="3480"),
        employees=EMPLOYEES,
        human_fields={"operador"},
    )
    cell = result["cells"]["operador"]
    assert cell["proposal"] == "GURPINDER SINGH"
    assert cell["human_protected"] is True
    assert cell["auto_write"] is False


def test_data_so_usa_o_valor_escrito_e_normaliza_formato():
    assert canonical_date("15-08-26") == "15/08/2026"
    assert canonical_date("2026-08-15") == "15/08/2026"
    assert canonical_date("31/02/2026") is None

    result = _check(
        _header(data=None),
        source_document={"filename": "18-08-2026.pdf", "page": 2},
    )
    assert result["cells"]["data"]["status"] == "missing"
    assert result["cells"]["data"]["proposal"] is None
    assert result["source_document"]["filename"] == "18-08-2026.pdf"

    normalized = _check(_header(data="15-08-26"))["cells"]["data"]
    assert normalized["proposal"] == "15/08/2026"
    assert normalized["auto_write"] is True


def test_dia_util_anterior_salta_fim_de_semana_mas_nao_feriados():
    assert previous_business_day(date(2026, 8, 18)) == date(2026, 8, 17), "terça → segunda"
    assert previous_business_day(date(2026, 8, 17)) == date(2026, 8, 14), "segunda → sexta"
    assert previous_business_day(date(2026, 8, 16)) == date(2026, 8, 14), "domingo → sexta"
    assert previous_business_day(date(2026, 8, 15)) == date(2026, 8, 14), "sábado → sexta"
    # feriados não se saltam: 15/08/2025 (feriado nacional) caiu numa sexta
    # e continua a contar como dia útil — não há calendário fiável aqui
    assert previous_business_day(date(2025, 8, 16)) == date(2025, 8, 15)


def test_data_assumida_manda_sobre_o_escrito():
    """Regra da fábrica: a folha digitalizada é sempre do dia útil anterior.
    A data assumida substitui o que o OCR leu; só a edição humana a trava."""
    # vazio → assumida, com escrita automática
    cell = _check(_header(data=None), assumed_date="17/08/2026")["cells"]["data"]
    assert cell["proposal"] == "17/08/2026"
    assert cell["auto_write"] is True
    assert cell["reason"] == "assumed_prev_business_day"
    assert "dia útil anterior" in cell["message"]

    # escrito diferente → substitui-se na mesma
    cell = _check(_header(data="15/08/2026"), assumed_date="17/08/2026")["cells"]["data"]
    assert cell["proposal"] == "17/08/2026"
    assert cell["auto_write"] is True

    # escrito igual ao assumido → confirmado, sem escrita
    cell = _check(_header(data="17/08/2026"), assumed_date="17/08/2026")["cells"]["data"]
    assert cell["status"] == "confirmed"
    assert cell["auto_write"] is False

    # edição humana da data manda: sem escrita, regime normal de validação
    cell = _check(_header(data="15/08/2026"), assumed_date="17/08/2026",
                  human_fields={"data"})["cells"]["data"]
    assert cell["auto_write"] is False
    assert cell["human_protected"] is True
    assert cell["status"] == "confirmed", "a data humana valida-se como sempre"


def test_template_unico_nao_fixa_maquina_nenhuma():
    # A MESMA TPL102 serve todas as máquinas do setor: nenhum template pode
    # afirmar onde a folha foi produzida.
    assert template_machine(get_template("cantoneiras_kanban")) is None
    assert template_machine(get_template("cantoneiras_paragens")) is None
    empty = _check(_header(), machines=CATALOG)["cells"]["setor_maquina"]
    assert empty["status"] == "missing"
    assert empty["auto_write"] is False


def test_maquina_alias_catalogo_e_plano_so_escrevem_quando_provam_o_valor():
    alias = _check(
        _header(setor_maquina="Rapid 20T - 2"), machines=CATALOG,
    )["cells"]["setor_maquina"]
    assert alias["proposal"] == "Ficep Rapid 20T -2"
    assert alias["auto_write"] is True

    exact = _check(
        _header(setor_maquina="Ficep XP T4"), machines=CATALOG,
    )["cells"]["setor_maquina"]
    assert exact["status"] == "confirmed"
    assert exact["auto_write"] is False

    plan_only = _check(
        _header(), machines=CATALOG,
        plan_machines=["Ficep XP T4"],
    )["cells"]["setor_maquina"]
    assert plan_only["status"] == "review"
    assert plan_only["proposal"] == "Ficep XP T4"
    assert plan_only["auto_write"] is False

    multiple = _check(
        _header(), machines=CATALOG,
        plan_machines=["Ficep XP T4", "Peddi 8"],
    )["cells"]["setor_maquina"]
    assert multiple["status"] == "ambiguous"
    assert set(multiple["candidates"]) == {"Ficep XP T4", "Peddi 8"}
    assert multiple["auto_write"] is False


def test_maquina_de_outro_setor_e_familia_generica_nao_confirmam():
    wrong_sector = _check(
        _header(setor_maquina="Vanguard MTG2"), machines=CATALOG,
    )["cells"]["setor_maquina"]
    assert wrong_sector["status"] != "confirmed"
    assert wrong_sector["auto_write"] is False

    generic = _check(
        _header(setor_maquina="RAPID"), machines=CATALOG,
    )["cells"]["setor_maquina"]
    assert generic["status"] == "ambiguous"
    assert generic["auto_write"] is False
    assert "Ficep Rapid 20T -2" in generic["candidates"]

    # A chapa não tem catálogo neste snapshot: sem referência, valor mantido.
    chapa = _check(
        _header(setor_maquina="Laser 1"), template="chapa_kanban",
        machines=CATALOG,
    )["cells"]["setor_maquina"]
    assert chapa["status"] == "no_reference"
    assert chapa["auto_write"] is False


def test_turno_tpl102_so_tem_tres_caixas():
    lower = _check(_header(turno="m"))["cells"]["turno"]
    assert lower["proposal"] == "M"
    assert lower["auto_write"] is True

    unknown = _check(_header(turno="T"))["cells"]["turno"]
    assert unknown["status"] == "review"
    assert unknown["auto_write"] is False

    verso = _check(_header(turno="T"), template="cantoneiras_paragens")["cells"]["turno"]
    assert verso["status"] == "review"

    multiple = _check(_header(turno="M+R"))["cells"]["turno"]
    assert multiple["status"] == "ambiguous"
    assert multiple["auto_write"] is False
