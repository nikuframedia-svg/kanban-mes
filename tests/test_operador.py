"""Resolver o operador contra a lista de colaboradores do SAP.

Os casos são reais, tirados das folhas em staging: o número vem sempre bem
lido, o nome nem por isso.
"""

import pytest

from app.matching.operador import Employee, resolve

LISTA = {
    1659: Employee(1659, "10001659", "FABIO ARAUJO"),
    2105: Employee(2105, "10002105", "MARCO LOPES"),
    3208: Employee(3208, "10003208", "HARVINDER SINGH"),
    3480: Employee(3480, "10003480", "GURPINDER SINGH"),
    2980: Employee(2980, "10002980", "SIMARJIT SINGH"),
    13: Employee(13, "10000013", "ANTONIO SOUSA"),
}


def test_nome_e_numero_certos():
    m = resolve("FABIO ARAUJO", "1659", LISTA)
    assert (m.cod, m.pernr, m.rule, m.confident) == (1659, "10001659", "exact", True)


def test_nome_abreviado_pelo_operador():
    """«Fabio» por FABIO ARAUJO — o operador escreve o primeiro nome."""
    m = resolve("Fabio", "1659", LISTA)
    assert m.name == "FABIO ARAUJO"
    assert m.rule == "token" and m.confident


def test_letra_trocada_pelo_ocr():
    m = resolve("Havinder Singh", "3208", LISTA)
    assert m.name == "HARVINDER SINGH"
    assert m.confident


def test_nome_ilegivel_confia_no_numero_mas_pede_confirmacao():
    """«Fúsio» para 1659: o número manda, mas ninguém troca um nome em silêncio."""
    m = resolve("Fúsio", "1659", LISTA)
    assert m.name == "FABIO ARAUJO"
    assert m.rule == "so_numero"
    assert not m.confident


def test_nome_proprio_diferente_nao_passa_por_partilhar_apelido():
    """O caso 2105: lido «Flavio Lopes» e «Mauro Lopes», no SAP é MARCO LOPES.

    Partilhar «LOPES» não chega para trocar o nome próprio sem avisar.
    """
    for lido in ("Flavio Lopes", "Mauro Lopes"):
        m = resolve(lido, "2105", LISTA)
        assert m.name == "MARCO LOPES"
        assert not m.confident, lido


def test_digito_trocado_no_numero():
    """2480 não existe; 3480 existe e o nome quase bate.

    Distingue-se da regra da app da MTG2, que exigia só um token comum: SINGH
    aparece em 186 dos 720 nomes e deixava candidatos empatados.
    """
    m = resolve("Gurbinder Singh", "2480", LISTA)
    assert (m.cod, m.name, m.rule) == (3480, "GURPINDER SINGH", "corrigido")
    assert not m.confident, "corrigir um número pede sempre confirmação"


def test_numero_desconhecido_nao_inventa_identidade():
    """Pode ser um contratado recente; fabricar um nº SAP seria pior que um vazio."""
    m = resolve("Alguem Novo", "99999", LISTA)
    assert m.pernr is None
    assert m.name == "Alguem Novo", "mantém-se o que está escrito"
    assert not m.confident


def test_sem_numero_nao_casa_por_nome():
    """60 dos 720 nomes estão repetidos — casar por nome seria adivinhar."""
    m = resolve("ANTONIO SOUSA", "", LISTA)
    assert m.pernr is None
    assert m.rule == "sem_numero"


def test_sem_lista_nao_quebra():
    m = resolve("Fabio", "1659", {})
    assert m.rule == "sem_ref" and m.name == "Fabio"
