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
    assert ids[2].values == {}, "modelo é sempre próprio da linha, nunca identidade herdada"


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
    # perfil incluído desde 19/08: escreve-se uma vez por bloco nas folhas
    # reais, e herdá-lo desempata linhas irmãs (caso AT1T515/AT2T515)
    assert CARRY_FIELDS == ("of", "ov", "cliente", "perfil")


def test_perfil_herda_por_bloco_e_corta_com_perfil_novo():
    rows = [
        {"of": "263322", "perfil": "50x6", "modelo": "AT2T562", "qtd": "4"},
        {"modelo": "AT2T561", "qtd": "3"},
        {"perfil": "50x5", "modelo": "AEH46", "qtd": "4"},
        {"modelo": "AT2T515", "qtd": "2"},
    ]
    ids = ident(rows)
    assert ids[1].values["perfil"] == "50x6"
    assert ids[1].is_inherited("perfil")
    assert ids[2].values["perfil"] == "50x5", "perfil novo escrito manda"
    assert ids[3].values["perfil"] == "50x5", "e passa às linhas seguintes"


def test_aspas_de_idem_herdam_e_nao_cortam_o_bloco():
    """Folha real fd88081e: o operador escreveu «"» em cliente/OV/OF e o motor
    cortava o bloco — 5 linhas boas ficavam sem identidade nenhuma."""
    rows = [
        {"cliente": "Tennet", "ov": "2504634", "of": "263322",
         "perfil": "40x5", "modelo": "AT2T562", "qtd": "4"},
        {"cliente": '"', "ov": '"', "of": '"', "perfil": "50x5",
         "modelo": "AEH46", "qtd": "4"},
        {"modelo": "AT1T515", "qtd": "2"},
    ]
    ids = ident(rows)
    assert ids[1].values["of"] == "263322", "a aspa é um pedido de herança"
    assert ids[1].is_inherited("of")
    assert ids[1].values["cliente"] == "Tennet"
    assert ids[2].values["of"] == "263322", "o bloco continua depois das aspas"
    assert ids[2].inherited_from["of"] == 0


def test_variantes_de_aspas_e_idem_sao_reconhecidas():
    from app.matching.carryover import is_ditto
    # «,,» é a aspa escrita rente à linha (folha real 6c9c634e: 5 linhas
    # boas caíram para weak porque a vírgula dupla contava como valor)
    for mark in ('"', "”", "“", "„", "''", "=", ",", ",,", ", ,",
                 "idem", "IDEM", " Idem "):
        assert is_ditto(mark), mark
    for value in ("263323", "", None, "x", "C.M.E.", "1,5"):
        assert not is_ditto(value), value


def test_aspas_com_producao_escrita_herdam():
    """Linha de perfil-completo real: identidade em aspas + qtd + visto. A qtd
    e o perf_comp não estão no IndexSpec, e a linha era tratada como muda —
    ficava sem OF e ainda cortava o bloco às seguintes."""
    rows = [
        {"cliente": "CMF", "ov": "2504650", "of": "263323",
         "perfil": "40x4", "modelo": "AT1T220", "qtd": "56"},
        {"cliente": '"', "ov": '"', "of": '"', "qtd": "12", "perf_comp": "x"},
        {"modelo": "AEH89", "qtd": "12"},
    ]
    ids = ident(rows)
    assert ids[1].values["of"] == "263323", "produção escrita = linha real = herda"
    assert ids[2].values["of"] == "263323", "e o bloco não se corta"


def test_linha_so_de_aspas_nao_conta_como_conteudo():
    """Aspas sem produção à frente são lixo de OCR, não uma linha de trabalho."""
    rows = [
        {"of": "263323", "modelo": "A"},
        {"cliente": '"', "ov": '"', "of": '"'},
        {"modelo": "B"},
    ]
    ids = ident(rows)
    assert ids[1].values == {}, "linha vazia — e corta o bloco como qualquer vazia"
    assert ids[2].values == {}


def test_modelo_e_quantidade_nunca_herdam():
    rows = [
        {"of": "263323", "perfil": "50x5", "modelo": "AT2T515", "qtd": "2"},
        {"qtd": "3"},
        {"of": "263324", "perfil": "60x6", "qtd": "4"},
        {"modelo": "NOVO", "qtd": "1"},
    ]
    ids = ident(rows)
    assert "modelo" not in ids[1].values
    assert "modelo" not in ids[2].values
    assert "modelo" not in ids[3].values
    assert "qtd" not in ids[1].values


def test_linha_eliminada_nao_fornece_nem_corta_a_heranca():
    rows = [
        {"of": "263323", "modelo": "A", "qtd": "1"},
        {"modelo": "ERRADO", "qtd": "2", "_deleted": True},
        {"qtd": "3"},
    ]
    ids = ident(rows)
    assert ids[1].values == {}
    assert ids[2].values["of"] == "263323"
    assert ids[2].inherited_from["of"] == 0


def test_editar_origem_atualiza_dependentes_e_novo_valor_abre_bloco():
    rows = [
        {"of": "263323", "cliente": "MG GROUP ENERGY", "ov": "2508335",
         "perfil": "100x12", "modelo": "ZE-242", "qtd": "4"},
        {"perfil": "110x8", "modelo": "ZE-329", "qtd": "4"},
        {"modelo": "ZE-330", "qtd": "4"},
    ]
    rows[0]["cliente"] = "MG GROUP ENERGY CORRIGIDO"
    ids = ident(rows)
    assert ids[1].values["cliente"] == "MG GROUP ENERGY CORRIGIDO"
    assert ids[2].values["cliente"] == "MG GROUP ENERGY CORRIGIDO"
    assert ids[1].values["perfil"] == "110x8"
    assert ids[2].values["perfil"] == "110x8"
