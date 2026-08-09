"""Herança «idem»: a identidade escrita uma vez vale para as linhas seguintes.

Os casos são as formas que as folhas reais têm — incluindo a folha que muda de
OF a meio e a folha manual que nasce com dez linhas em branco.
"""

from app.matching.carryover import CARRY_FIELDS, effective_row, resolve

CONTENT = ("perfil", "modelo", "qtd")


def ident(rows, human=None):
    return resolve(rows, CONTENT, human)


def test_linha_em_branco_herda_a_identidade_de_cima():
    rows = [
        {"cliente": "Tennet", "ov": "2504650", "of": "263323", "perfil": "100 x 50 x 6"},
        {"modelo": "AT1T115", "qtd": "1"},
    ]
    ids = ident(rows)
    assert ids[1].values["of"] == "263323"
    assert ids[1].inherited_from["of"] == 0
    assert ids[1].is_inherited("of")
    assert not ids[0].is_inherited("of")


def test_of_nova_a_meio_corta_o_bloco():
    """Folha real 1945de30: a linha 2 abre outra obra — não pode arrastar a OV da 0."""
    rows = [
        {"cliente": "Tennet", "ov": "2504650", "of": "263323", "perfil": "60x5"},
        {"modelo": "AT1T115", "qtd": "1"},
        {"cliente": "Falta", "of": "264051", "modelo": "X1", "qtd": "2"},
        {"modelo": "X2", "qtd": "3"},
    ]
    ids = ident(rows)
    assert ids[2].values["of"] == "264051"
    assert "ov" not in ids[2].values, "a OV da obra anterior não pode passar para a nova"
    assert ids[3].values["of"] == "264051", "a linha seguinte herda a OF nova"


def test_mesma_of_repetida_continua_o_bloco():
    rows = [
        {"cliente": "Tennet", "ov": "2504650", "of": "263323", "modelo": "A"},
        {"of": "263323", "modelo": "B"},
    ]
    ids = ident(rows)
    assert ids[1].values["ov"] == "2504650"
    assert ids[1].is_inherited("ov")
    assert not ids[1].is_inherited("of")


def test_linha_totalmente_vazia_nao_herda_e_corta():
    """Uma folha manual nasce com 10 linhas em branco — não podem aparecer a
    cruzar com o plano por herdarem a identidade da última linha escrita."""
    rows = [
        {"cliente": "Tennet", "ov": "2504650", "of": "263323", "modelo": "A"},
        {},
        {"modelo": "B"},
    ]
    ids = ident(rows)
    assert ids[1].values == {}
    assert ids[2].values == {}, "depois de um vazio o bloco recomeça"


def test_campo_apagado_por_humano_nao_e_reposto():
    """Apagar a OF é uma decisão do revisor; herdar por cima desfazia-a."""
    rows = [
        {"of": "263323", "modelo": "A"},
        {"modelo": "B"},
    ]
    ids = ident(rows, human={1: {"of"}})
    assert "of" not in ids[1].values


def test_effective_row_nao_toca_no_original():
    rows = [{"of": "263323", "modelo": "A"}, {"modelo": "B"}]
    ids = ident(rows)
    eff = effective_row(rows[1], ids[1])
    assert eff["of"] == "263323"
    assert "of" not in rows[1], "a linha original tem de ficar como está"


def test_campos_transportados_sao_os_de_identidade_da_obra():
    assert CARRY_FIELDS == ("of", "ov", "cliente")
