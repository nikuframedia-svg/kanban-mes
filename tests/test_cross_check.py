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
    """Linha com «"» em cliente/OV/OF: cruza com a identidade herdada e mostra
    a herança. Com a substituição total (default), a linha forte materializa o
    valor do plano na célula; com a política desligada, a aspa fica intocada."""
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
    if linha["mode"] == "strong":
        assert by_field["of"]["auto_write"], \
            "substituição total materializa o herdado (folha auto-contida)"
    assert result["summary"]["cells_inherited"] >= 3

    # política desligada: o regime antigo — herdado nunca se auto-escreve
    s.params.policy.replace_with_plan = False
    result2 = check_sheet(rows, s)
    by_field2 = {c["field"]: c for c in result2["rows"][1]["cells"]}
    assert not by_field2["of"]["auto_write"]


def _cantoneiras_scorer(entries):
    from app.matching.loaders import CANTONEIRAS_SPEC
    from app.matching.refs import PlanIndex

    return Scorer(PlanIndex(entries, CANTONEIRAS_SPEC, plan_age_days=1.0), CrossParams())


def _entries_obra(nome="PAINHAS, SA", n_clientes=1):
    return [
        {"plan_key": f"A{i}", "of": "OF262796", "ov": "OV2603660",
         "cliente": "painhas, sa", "cliente_nome": nome, "n_clientes": n_clientes,
         "modelo": f"QS12{i}", "perfil": "L60X60X4"}
        for i in range(3)
    ]


def test_cliente_vazio_com_of_confiavel_propoe_nome():
    s = _cantoneiras_scorer(_entries_obra())
    row = {"of": "262796", "ov": "2603660", "modelo": "QS120", "perfil": "60 x 4"}
    rc = check_row(row, 0, s)
    by_field = {c.field: c for c in rc.cells}
    assert by_field["cliente"].status == "snapped"
    assert by_field["cliente"].proposal == "PAINHAS, SA"
    if rc.p_correct >= 0.95:
        assert by_field["cliente"].auto_write


def test_cliente_do_plano_substitui_em_linha_forte():
    """Política de 19/08: o cliente é SEMPRE o do planeamento. Em linha com
    match forte, o nome do plano escreve-se por cima do que o operador
    escreveu (que fica no raw + trilho); a linha não entra na revisão."""
    s = _cantoneiras_scorer(_entries_obra(nome="C.M.E.-CONST. E"))
    row = {"of": "262796", "ov": "2603660", "cliente": "CMF",
           "modelo": "QS120", "perfil": "60 x 4"}
    rc = check_row(row, 0, s)
    by_field = {c.field: c for c in rc.cells}
    if rc.mode == "strong":
        assert by_field["cliente"].status == "snapped"
        assert by_field["cliente"].proposal == "C.M.E.-CONST. E"
        assert by_field["cliente"].auto_write, "cliente vem sempre do planeamento"


def test_cliente_alias_quando_substituicao_desligada():
    """Com `replace_with_plan` desligado volta o regime de 17/08: proposta
    visível («alias»), nunca auto-escrita, fora da fila de revisão."""
    s = _cantoneiras_scorer(_entries_obra(nome="C.M.E.-CONST. E"))
    s.params.policy.replace_with_plan = False
    row = {"of": "262796", "ov": "2603660", "cliente": "CMF",
           "modelo": "QS120", "perfil": "60 x 4"}
    rc = check_row(row, 0, s)
    by_field = {c.field: c for c in rc.cells}
    assert by_field["cliente"].status == "alias"
    assert by_field["cliente"].proposal == "C.M.E.-CONST. E"
    assert not by_field["cliente"].auto_write
    assert rc.review_priority == 0.0, \
        "alias não pode mandar a linha para revisão — era o vermelho-para-sempre"


def test_cliente_parecido_confirma():
    s = _cantoneiras_scorer(_entries_obra())
    row = {"of": "262796", "ov": "2603660", "cliente": "PAINHAS",
           "modelo": "QS120", "perfil": "60 x 4"}
    rc = check_row(row, 0, s)
    by_field = {c.field: c for c in rc.cells}
    assert by_field["cliente"].status == "confirmed"
    assert not by_field["cliente"].auto_write


def test_cliente_sem_proposta_com_varios_clientes_na_of():
    """`n_clientes` > 1 = a coluna do cliente no Excel cru trazia lixo
    (datas, designações de material) — o nome agregado é um artefacto de
    min() e propô-lo seria espalhar esse lixo."""
    s = _cantoneiras_scorer(_entries_obra(nome="2026-07-26 00:00:00", n_clientes=2))
    row = {"of": "262796", "ov": "2603660", "modelo": "QS120", "perfil": "60 x 4"}
    rc = check_row(row, 0, s)
    assert "cliente" not in {c.field for c in rc.cells}


def test_h0_com_of_exata_resolve_cliente():
    """Mesmo sem linha vencedora, uma OF exata identifica a obra — e a obra
    tem dono. Mas no ramo H₀ nunca se escreve nada."""
    entries = _entries_obra()
    entries += [
        {"plan_key": f"B{i}", "of": "OF262797", "ov": "OV2699999",
         "cliente": "outro", "cliente_nome": "OUTRO, LDA", "n_clientes": 1,
         "modelo": f"QA4{i}", "perfil": "L80X80X8"}
        for i in range(3)
    ]
    s = _cantoneiras_scorer(entries)
    # OF de uma obra, OV de outra, modelo/perfil inexistentes → H₀
    row = {"of": "262796", "ov": "2699999", "modelo": "ZZZ 999", "perfil": "45 x 9"}
    rc = check_row(row, 0, s)
    assert rc.p_correct < s.params.policy.propose_threshold, "cenário deve cair no ramo H₀"
    by_field = {c.field: c for c in rc.cells}
    assert by_field["of"].status == "confirmed"
    assert by_field["cliente"].proposal == "PAINHAS, SA"
    assert not by_field["cliente"].auto_write, "no ramo H₀ nunca se auto-escreve"

    # OF que não existe no plano → sem célula de cliente
    rc2 = check_row({"of": "990000", "modelo": "ZZZ 999"}, 0, s)
    assert "cliente" not in {c.field for c in rc2.cells}


def test_cliente_herdado_sem_substituicao_nao_e_auto_escrito():
    """Sem a política de substituição total, um valor herdado nunca é gravado
    como se o operador o tivesse escrito. (Com a política ligada — o default —
    a linha forte fica com o valor do plano, herdadas incluídas.)"""
    s = _cantoneiras_scorer(_entries_obra(nome="C.M.E.-CONST. E"))
    s.params.policy.replace_with_plan = False
    rows = [
        {"of": "262796", "ov": "2603660", "cliente": "CMF",
         "modelo": "QS120", "perfil": "60 x 4"},
        {"of": '"', "ov": '"', "cliente": '"', "modelo": "QS121", "perfil": "60 x 4"},
    ]
    result = check_sheet(rows, s)
    by_field = {c["field"]: c for c in result["rows"][1]["cells"]}
    assert by_field["cliente"]["written"] is None, "a aspa não é um valor escrito"
    assert by_field["cliente"]["inherited"] == "CMF"
    assert not by_field["cliente"]["auto_write"], "herdado nunca se auto-escreve"


def test_plan_customer_for():
    from app.matching.cross_check import plan_customer_for
    from app.matching.loaders import CANTONEIRAS_SPEC
    from app.matching.refs import PlanIndex

    index = PlanIndex(_entries_obra(), CANTONEIRAS_SPEC)
    assert plan_customer_for(index, "262796") == "PAINHAS, SA", "OF sem prefixo resolve"
    assert plan_customer_for(index, "OF262796") == "PAINHAS, SA"
    assert plan_customer_for(index, "999999") is None, "OF desconhecida"
    assert plan_customer_for(index, "") is None

    ambigua = PlanIndex(_entries_obra(n_clientes=2), CANTONEIRAS_SPEC)
    assert plan_customer_for(ambigua, "262796") is None, "n_clientes > 1 recusa"

    sem_nome = [dict(e, cliente_nome=None) for e in _entries_obra()]
    assert plan_customer_for(PlanIndex(sem_nome, CANTONEIRAS_SPEC), "262796") is None


def test_escrito_com_prefixo_confirma_contra_proposta_nua():
    """Bug apanhado no teste de qualidade: quem escreveu «OF251525» via a
    célula vermelha porque a proposta despida («251525») comparava mal — a
    comparação usa o valor completo do plano; o strip é só apresentação."""
    s = _cantoneiras_scorer(_entries_obra())
    row = {"of": "OF262796", "ov": "OV2603660", "modelo": "QS120", "perfil": "60 x 4"}
    rc = check_row(row, 0, s)
    by_field = {c.field: c for c in rc.cells}
    assert by_field["of"].status == "confirmed", "OF262796 escrito = OF262796 do plano"
    assert by_field["ov"].status == "confirmed"
    # e a proposta, quando existe, continua nua
    rc2 = check_row({"of": "262796", "modelo": "QS120", "perfil": "60 x 4"}, 0, s)
    ov = next(c for c in rc2.cells if c.field == "ov")
    if ov.proposal:
        assert ov.proposal == "2603660"


def test_substituicao_total_em_linha_forte():
    """Política de 19/08 (revista): linha com match forte substitui os campos
    ANCORADOS na obra (of/ov/cliente) sem limiar; modelo/perfil escolhem a
    linha ENTRE irmãs e exigem o marginal do campo — a lição AT1T515."""
    s = _cantoneiras_scorer(_entries_obra())
    row = {"of": "262796", "modelo": "QS128", "perfil": "60 x 4"}
    rc = check_row(row, 0, s)
    by_field = {c.field: c for c in rc.cells}
    if rc.mode == "strong":
        assert by_field["ov"].proposal == "2603660", "proposta sem prefixo OV"
        assert by_field["ov"].auto_write, "OV é função da obra — substitui-se"
        modelo = by_field["modelo"]
        assert modelo.auto_write, \
            "política 26/08: o escrito que difere substitui-se sempre"
        assert modelo.written == "QS128", "o manuscrito fica visível na célula"
    # humano continua inviolável
    rc2 = check_row(row, 0, s, human_fields={"modelo"})
    by_field2 = {c.field: c for c in rc2.cells}
    assert not by_field2["modelo"].auto_write


def test_caso_at1t515_substitui_mas_preserva_o_original():
    """Reprodução do caso real, revista pela política de 26/08: o modelo
    manuscrito inexistente É substituído pelo valor do plano (o original fica
    no `written`/raw e visível por baixo da célula na revisão). O que NÃO pode
    regredir do fix «palpites entre linhas irmãs»: o palpite apresentado tem
    de vir da família do perfil herdado do bloco, e uma célula EM BRANCO
    continua sem escrita — nunca se materializa um palpite fraco num vazio."""
    entries = []
    # obra grande com famílias AT1Txxx (40x5) e AT2T5xx (50x5)
    for i in range(100, 200):
        entries.append({"plan_key": f"A{i}", "of": "OF263322", "ov": "OV2504634",
                        "cliente": "meta", "cliente_nome": "METALOGALVA GMBH",
                        "n_clientes": 1, "modelo": f"AT1T{i}", "perfil": "L40X40X5"})
    for i in range(500, 600):
        entries.append({"plan_key": f"B{i}", "of": "OF263322", "ov": "OV2504634",
                        "cliente": "meta", "cliente_nome": "METALOGALVA GMBH",
                        "n_clientes": 1, "modelo": f"AT2T{i}", "perfil": "L50X50X5"})
    s = _cantoneiras_scorer(entries)
    rows = [
        {"of": "263322", "ov": "2504634", "perfil": "50x5", "modelo": "AEH46", "qtd": "4"},
        {"modelo": "AT1T515", "qtd": "2"},   # manuscrito; não existe no plano
        {"qtd": "3"},                        # tudo herdado; modelo EM BRANCO
    ]
    result = check_sheet(rows, s)
    linha = result["rows"][1]
    by_field = {c["field"]: c for c in linha["cells"]}
    modelo = by_field["modelo"]
    if modelo["proposal"]:
        # com o perfil 50x5 herdado do bloco e o indel mais caro, o palpite
        # apresentado tem de ser da família do perfil certo
        assert modelo["proposal"].startswith("AT2T5"), \
            f"o perfil do bloco devia desempatar (veio {modelo['proposal']})"
        assert modelo["auto_write"], \
            "política 26/08: o escrito que difere substitui-se sempre"
        assert modelo["written"] == "AT1T515", \
            "o manuscrito nunca desaparece — fica no written/raw"
    # a célula em branco é outra história: um palpite entre irmãs empatadas
    # (p_field ínfimo) nunca se materializa num vazio
    branco = {c["field"]: c for c in result["rows"][2]["cells"]}["modelo"]
    if branco["proposal"]:
        assert not branco["auto_write"], \
            "palpite entre irmãs NUNCA preenche uma célula em branco"


def test_linha_incerta_nao_preenche_vazios_com_palpites():
    """Política de 26/08: mesmo numa linha incerta, o ESCRITO que difere do
    plano substitui-se (o original fica visível). As células VAZIAS é que
    mantêm o regime de propostas — preencher um vazio com um palpite a ~50%
    continuaria a propagar matches errados em massa."""
    entries = _entries_obra() + [
        {"plan_key": f"B{i}", "of": "OF262797", "ov": "OV2699999",
         "cliente": "outro", "cliente_nome": "OUTRO, LDA", "n_clientes": 1,
         "modelo": f"QS12{i}", "perfil": "L60X60X4"}
        for i in range(3)
    ]
    s = _cantoneiras_scorer(entries)
    # sem OF: modelo QS120 existe nas duas obras → incerto
    rc = check_row({"modelo": "QS120", "perfil": "60 x 4"}, 0, s)
    if rc.mode != "strong":
        limiar = s.params.policy.write_threshold_identity
        for c in rc.cells:
            if c.written is None and c.proposal and c.p_correct < limiar:
                assert not c.auto_write, \
                    f"vazio de «{c.field}» preenchido com palpite a {c.p_correct:.2f}"


def test_escrito_divergente_substitui_mesmo_com_confianca_baixa():
    """Política de 26/08: uma célula ESCRITA cuja proposta difere aplica-se
    sempre, mesmo com p_field ínfimo — o original fica no written/raw e
    visível na revisão. Só a edição humana trava; a Qtd nunca se reescreve;
    e desligar a política devolve o regime antigo."""
    s = _cantoneiras_scorer(_entries_obra())
    row = {"of": "262796", "ov": "2603660", "perfil": "60 x 4",
           "modelo": "ZZZ999", "qtd": "999999"}
    rc = check_row(row, 0, s)
    by_field = {c.field: c for c in rc.cells}
    modelo = by_field["modelo"]
    assert modelo.status == "very_different"
    assert modelo.auto_write, "escrito que difere substitui-se SEMPRE"
    assert modelo.written == "ZZZ999"
    # a Qtd é produção: mesmo acima do limite do plano, nunca há proposta
    if "qtd" in by_field:
        assert by_field["qtd"].proposal is None
        assert not by_field["qtd"].auto_write

    # edição humana continua inviolável
    rc2 = check_row(row, 0, s, human_fields={"modelo"})
    assert not {c.field: c for c in rc2.cells}["modelo"].auto_write

    # kill switch: sem a política volta o regime de propostas
    s.params.policy.replace_with_plan = False
    rc3 = check_row(row, 0, s)
    modelo3 = {c.field: c for c in rc3.cells}["modelo"]
    assert modelo3.auto_write == (modelo3.p_correct
                                  >= s.params.policy.write_threshold_identity)
    s.params.policy.replace_with_plan = True


def test_h0_continua_sem_propostas_nem_escrita():
    """O ramo H₀ não muda com a substituição total: sem linha credível não há
    proposta nenhuma nos campos, e nada se escreve."""
    s = make_scorer()
    rc = check_row({"of": "OF990000", "ov": "OV9900000", "cliente": "FANTASMA",
                    "comp_mm": 77777}, 0, s)
    assert rc.matched_plan_key is None
    assert all(c.proposal is None for c in rc.cells)
    assert all(not c.auto_write for c in rc.cells)


def test_metros_por_linha_e_desperdicio():
    """qtd × comprimento do plano (mm→m) por linha; o rodapé confere os
    METROS PRODUZIDOS contra o total — a diferença é a coluna principal."""
    entries = [
        {"plan_key": "A0", "of": "OF262796", "ov": "OV2603660",
         "cliente": "painhas, sa", "cliente_nome": "PAINHAS, SA", "n_clientes": 1,
         "modelo": "QS120", "perfil": "L60X60X4", "comp_mm": 1500},
        {"plan_key": "A1", "of": "OF262796", "ov": "OV2603660",
         "cliente": "painhas, sa", "cliente_nome": "PAINHAS, SA", "n_clientes": 1,
         "modelo": "QS121", "perfil": "L60X60X4", "comp_mm": 2000},
    ]
    s = _cantoneiras_scorer(entries)
    rows = [
        {"of": "262796", "ov": "2603660", "modelo": "QS120", "perfil": "60 x 4",
         "qtd": "10"},                                   # 10 × 1.5 m = 15 m
        {"modelo": "QS121", "qtd": "4"},                 # 4 × 2.0 m = 8 m
    ]
    result = check_sheet(rows, s, footer={"metros_produzidos": "25"})
    assert result["rows"][0]["line_meters"] == 15.0
    assert result["rows"][0]["plan_length_mm"] == 1500.0
    assert result["rows"][1]["line_meters"] == 8.0
    su = result["summary"]
    assert su["metros_teoricos"] == 23.0
    assert su["metros_produzidos"] == 25.0
    assert not su["metros_parciais"]
    assert su["desperdicio_m"] == 2.0, "produzido acima do teórico = excedente"

    # sem rodapé preenchido não há diferença para mostrar
    sem = check_sheet(rows, s)["summary"]
    assert sem["metros_teoricos"] == 23.0
    assert sem["desperdicio_m"] is None

    # linha de perfil completo sem metros → total parcial, desperdício
    # desonesto não se mostra (caso real: 200 m de «desperdício» que era só
    # produção não contada)
    rows_parcial = rows + [{"of": "262796", "perfil": "60 x 4",
                            "perf_comp": "x", "qtd": "50"}]
    par = check_sheet(rows_parcial, s, footer={"metros_produzidos": "25"})["summary"]
    assert par["metros_parciais"]
    assert par["desperdicio_m"] is None


def test_linha_perf_comp_nao_tem_metros():
    entries = [
        {"plan_key": "A0", "of": "OF262796", "ov": "OV2603660",
         "cliente": "painhas, sa", "cliente_nome": "PAINHAS, SA", "n_clientes": 1,
         "modelo": "QS120", "perfil": "L60X60X4", "comp_mm": 1500},
    ]
    s = _cantoneiras_scorer(entries)
    rc = check_row({"of": "262796", "perfil": "60 x 4", "perf_comp": "x",
                    "qtd": "10"}, 0, s)
    assert rc.line_meters is None, \
        "perfil completo cobre várias referências — não há «o» comprimento"


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
