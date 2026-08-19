"""Cruzamento de uma folha completa: aplica o scorer a cada linha e converte o
resultado em estados de célula + fila de revisão.

Estados de célula:
- confirmed      — o escrito coincide com o plano (verde);
- snapped        — correção/atribuição automática com confiança acima do limiar (amarelo);
- very_different — o motor propõe algo distante do escrito; propõe mas exige olho humano (vermelho);
- alias          — só no cliente: o escrito difere do nome do plano, mas isso é
                   esperado (cliente final vs. cliente interno da Metalogalva) —
                   mostra-se a proposta em tom neutro, sem entrar na revisão;
- unmatched      — sem vencedor credível no plano (H₀ venceu ou não há candidatos);
- na             — campo sem referência para cruzar.

A escrita automática segue perda esperada: só substitui quando
P(certo) > limiar do campo. Edições humanas nunca são sobrescritas (imposto na
camada web, que marca células com origem humana antes de chamar isto).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from . import carryover
from .carryover import RowIdentity
from .params import CrossParams
from .refs import PlanIndex
from ..templates_spec import field_value, is_marked
from .scorer import RowMatch, Scorer
from . import similarity as sim


@dataclass
class CellCheck:
    field: str
    written: str | None
    proposal: str | None
    status: str            # confirmed | snapped | very_different | alias | unmatched | na
    similarity: float
    auto_write: bool
    p_correct: float
    # Valor herdado da linha de cima (convenção «idem») e de que linha veio.
    # Só serve para cruzar e para mostrar — nunca é gravado como se fosse
    # escrito pelo operador.
    inherited: str | None = None
    inherited_from: int | None = None
    # Quantidade planeada para esta linha do plano. A Qtd escrita compara-se
    # com ela como limite superior, não como valor esperado: produzir menos do
    # que o previsto é normal, produzir mais é que merece um olhar.
    plan_limit: float | None = None


@dataclass
class RowCheck:
    row_index: int
    matched_plan_key: str | None
    p_correct: float
    margin_bits: float
    mode: str
    review_priority: float
    cells: list[CellCheck] = field(default_factory=list)
    rivals: list[str] = field(default_factory=list)
    # Comprimento da peça no plano (mm) e metros teóricos da linha
    # (qtd × comprimento). É contra a soma disto que os METROS PRODUZIDOS do
    # rodapé se conferem — a diferença é o desperdício/excedente.
    plan_length_mm: float | None = None
    line_meters: float | None = None


def plan_quantity_for(index: PlanIndex, of: str, modelo: str) -> float | None:
    """Quantidade planeada para uma referência dentro de uma obra.

    Usa-se `quantity_planned` e não `remaining_quantity`: a segunda é derivada,
    está travada a zero quando já se produziu tudo, e só se actualiza quando
    alguém volta a gravar o Excel — dava limites de zero em obras que estão a
    ser produzidas neste turno.
    """
    if not of or not modelo:
        return None
    of_hits = set(index.exact_matches("of", of))
    if not of_hits:
        return None
    modelo_hits = set(index.exact_matches("modelo", modelo))
    both = of_hits & modelo_hits
    if not both:
        return None
    total = 0.0
    seen = False
    for idx in both:
        value = sim.parse_number(index.entries[idx].get("qtd_planeada"))
        if value is not None:
            total += value
            seen = True
    return total if seen else None


def plan_customer_for(index: PlanIndex, of: str) -> str | None:
    """Cliente da obra, quando é inequívoco.

    A OF determina o cliente por construção (cada ordem de fabrico pertence a
    uma ordem de venda de um cliente). Mas o agregado do plano usa min() quando
    as linhas cruas trazem nomes diferentes — e nesses casos (`n_clientes` > 1)
    o nome guardado é um artefacto: no snapshot real aparecem datas e
    designações de material na coluna do cliente. Só se devolve o nome quando
    todas as linhas da OF apontam para exactamente um.
    """
    if not of:
        return None
    hits = index.exact_matches("of", of)
    if not hits:
        return None
    nomes: set[str] = set()
    for idx in hits:
        entry = index.entries[idx]
        n_clientes = sim.parse_number(entry.get("n_clientes"))
        if n_clientes is not None and n_clientes > 1:
            return None
        nome = str(entry.get("cliente_nome") or "").strip()
        if nome:
            nomes.add(nome)
    if len(nomes) != 1:
        return None
    return next(iter(nomes))


def _looks_numeric(text: str) -> bool:
    """`2x` ou `1+1` não são quantidades — parse_number daria 2 e 11."""
    return bool(text) and all(ch.isdigit() or ch in " .,-" for ch in text)


def _threshold_for(field_name: str, params: CrossParams) -> float:
    pol = params.policy
    crit = pol.criticality.get(field_name, pol.criticality_default)
    if crit >= 5:
        return pol.write_threshold_critical_dim
    if crit >= 3:
        return pol.write_threshold_identity
    return pol.write_threshold_default


def _cliente_check(row: dict, scored_row: dict, index: PlanIndex,
                   params: CrossParams, inherited_values: dict,
                   inherited_from: dict, human_fields: set[str],
                   p: float, permitir_escrita: bool,
                   replace_all: bool = False) -> CellCheck | None:
    """Célula do cliente, fora do scorer.

    O cliente não entra na identificação da linha (o plano guarda o cliente
    interno da Metalogalva, o operador escreve o final — discordar é o caso
    normal, não um erro de OCR). Mas resolvida a OF, o plano sabe de quem é a
    obra: célula vazia recebe proposta; escrita e parecida confirma; escrita e
    diferente fica `alias` — proposta visível, sem revisão. Com
    `replace_all` (linha com match forte e política de substituição total), o
    nome do plano é ESCRITO por cima do que difere: o cliente da folha passa a
    ser sempre o do planeamento; o que o operador escreveu fica no raw e no
    trilho de auditoria.
    """
    nome = plan_customer_for(index, str(scored_row.get("of") or ""))
    if not nome:
        return None
    written = row.get("cliente")
    if carryover.is_ditto(written):
        written = None
    written_s = str(written).strip() if written is not None else ""
    efectivo = written_s or str(inherited_values.get("cliente") or "").strip()
    if not efectivo:
        writable = (permitir_escrita
                    and "cliente" not in human_fields
                    and (replace_all
                         or ("cliente" not in inherited_from
                             and p >= _threshold_for("cliente", params))))
        status, proposal, similarity, auto = "snapped", nome, 0.0, writable
    else:
        similarity = sim.text_similarity(efectivo, nome)
        if similarity >= params.score.sim_near:
            # mesmo cliente. Se o texto difere só na forma (restos antigos da
            # customer_key: «tecpoles gmbh» vs «TECPOLES GMBH»), a
            # substituição escreve a forma legível do plano.
            canonizar = (replace_all and permitir_escrita
                         and "cliente" not in human_fields
                         and written_s and written_s != nome
                         and sim.compact(written_s) == sim.compact(nome))
            status, proposal, auto = "confirmed", (nome if canonizar else None), canonizar
        elif replace_all and permitir_escrita and "cliente" not in human_fields:
            status, proposal, auto = "snapped", nome, True
        else:
            status, proposal, auto = "alias", nome, False
    return CellCheck(
        field="cliente", written=written_s or None, proposal=proposal,
        status=status, similarity=similarity, auto_write=auto, p_correct=p,
        inherited=inherited_values.get("cliente"),
        inherited_from=inherited_from.get("cliente"),
    )


def check_row(row: dict, row_index: int, scorer: Scorer,
              human_fields: set[str] | None = None,
              identity: RowIdentity | None = None) -> RowCheck:
    """Cruza uma linha.

    `human_fields` = campos já editados por humanos (invioláveis).
    `identity` = identidade efectiva incluindo o que foi herdado da linha de
    cima; o cruzamento usa-a, mas as células continuam a mostrar o que está
    escrito, com o herdado à parte.
    """
    human_fields = human_fields or set()
    params = scorer.params
    index: PlanIndex = scorer.index
    scored_row = carryover.effective_row(row, identity) if identity else row
    match: RowMatch = scorer.match_row(scored_row)
    inherited_from = dict(identity.inherited_from) if identity else {}
    inherited_values = {f: identity.values.get(f) for f in inherited_from} if identity else {}

    spec_fields = list(index.spec.identity_fields) + list(index.spec.numeric_fields)
    cells: list[CellCheck] = []

    # A pergunta que decide se há ligação ao plano é «de que OF é esta linha?»,
    # não «que linha exacta do plano é esta». Numa OF com 300 irmãs a segunda
    # nunca passa de 0,3 por construção, e usá-la deitava fora tudo.
    confidence = max(match.p_primary, match.p_correct)
    if match.winner is None or confidence < params.policy.propose_threshold:
        # H₀ plausível: nada de propostas. Mas mesmo sem linha vencedora há
        # uma pergunta respondível célula a célula: este VALOR existe no
        # plano? Caso real: OF escrita e exata ficava vermelha só porque o
        # modelo ao lado não existe — a linha é incerta, a OF não é.
        for f in spec_fields:
            written = row.get(f.name)
            # marca de «idem»: o que lá está não é um valor, é a herança
            if carryover.is_ditto(written):
                written = None
            written_s = str(written).strip() if written is not None else ""
            efectivo = written_s or str(inherited_values.get(f.name) or "").strip()
            # Sem teto de entradas: o teto serve para não GERAR candidatos a
            # partir de valores comuns, mas aqui a pergunta é só «existe?» —
            # um perfil com 1700 linhas no plano existe, obviamente.
            exists = bool(
                efectivo and f.kind in ("code", "profile")
                and index.exact_matches(f.name, efectivo)
            )
            p_field = confidence
            if exists:
                # o marginal, quando aponta para o mesmo valor, dá um p mais
                # honesto do que a confiança (esmagada pelo H₀) da linha
                marginal = match.marginals.get(f.name)
                if marginal and marginal[0] in index.variants_for(f.name, efectivo):
                    p_field = max(p_field, marginal[1])
            cells.append(CellCheck(
                field=f.name, written=written_s or None, proposal=None,
                status="confirmed" if exists else ("unmatched" if written_s else "na"),
                similarity=1.0 if exists else 0.0, auto_write=False,
                p_correct=p_field,
                inherited=inherited_values.get(f.name),
                inherited_from=inherited_from.get(f.name),
            ))
        # Mesmo sem linha vencedora, uma OF exata identifica a obra — e a obra
        # tem dono. Sem escrita automática (invariante do ramo: nada se grava
        # quando a linha não tem correspondência credível); o p é o da célula
        # OF, porque o cliente é função dela.
        of_cell = next((c for c in cells if c.field == "of"), None)
        if of_cell is not None and of_cell.status == "confirmed":
            cliente_cell = _cliente_check(
                row, scored_row, index, params, inherited_values,
                inherited_from, human_fields,
                p=of_cell.p_correct, permitir_escrita=False,
            )
            if cliente_cell is not None:
                cells.append(cliente_cell)
        priority = max(
            (params.policy.criticality.get(c.field, params.policy.criticality_default)
             for c in cells if c.status not in ("confirmed", "alias", "na")), default=1,
        ) * (1.0 - confidence)
        return RowCheck(
            row_index=row_index, matched_plan_key=None,
            p_correct=confidence, margin_bits=match.margin_bits,
            mode="no_match" if match.winner is None else match.mode,
            review_priority=priority, cells=cells,
        )

    entry = index.entries[match.winner.idx]
    p = confidence
    # Linha de perfil completo: representa todas as referências daquele perfil
    # na obra, não uma. Propor-lhe «o» modelo seria escolher uma à sorte entre
    # dezenas — o que essa linha precisa é do pop-up com a lista.
    # `field_value` e não `.get`: folhas lidas antes do rename guardaram o
    # visto em `comp_mm`, e ignorá-las punha o motor a propor modelos nelas.
    linha_marcada = is_marked(field_value(scored_row, "perf_comp"))
    # Política de substituição total (decisão do Luís, 19/08): linha com match
    # FORTE fica com os valores do plano — very_different e herdadas
    # incluídas. Só as edições humanas continuam invioláveis; linhas incertas
    # (weak) mantêm o regime de propostas.
    replace_all = params.policy.replace_with_plan and match.mode == "strong"

    for f in spec_fields:
        written = row.get(f.name)
        # Marca de «idem» (aspas, =): não é um valor escrito, é o pedido de
        # herança — a célula cruza e mostra-se como herdada.
        if carryover.is_ditto(written):
            written = None
        written_s = str(written).strip() if written is not None else ""
        # Numa célula deixada em branco por «idem», o valor da linha é o
        # herdado — é contra esse que o plano se confere. Sem isto a célula
        # aparecia como «vazia, a preencher» e o motor propunha escrever o que
        # a herança já dizia.
        efectivo = written_s or str(inherited_values.get(f.name) or "").strip()
        raw_proposal = entry.get(f.entry_key)
        # A COMPARAÇÃO usa o valor completo do plano (com prefixo): se o
        # operador escreveu «OF251525», tem de bater com «OF251525» — despir
        # antes de comparar pintava de vermelho valores idênticos. O strip é
        # só apresentação/gravação (convenção do planeamento: números puros).
        proposal_plan = str(raw_proposal).strip() if raw_proposal is not None else ""
        proposal = (sim.strip_ref_prefix(proposal_plan)
                    if f.code_prefix and proposal_plan else proposal_plan)
        # Confiança por campo: o valor de um campo pode ser certo (todas as
        # irmãs concordam) mesmo quando a linha exacta é incerta. MAS o
        # marginal só vale para a proposta se apontar para o MESMO valor —
        # senão autorizava-se a escrita do valor do vencedor com a
        # probabilidade do valor rival (aconteceu: perfil errado gravado
        # com «91%» que era a probabilidade do perfil certo).
        marginal = match.marginals.get(f.name)
        if marginal and proposal_plan and marginal[0] in index.variants_for(f.name, proposal_plan):
            p_field = marginal[1]
        else:
            p_field = p
        if not proposal or (linha_marcada and f.name == "modelo" and not written_s):
            cells.append(CellCheck(
                f.name, written_s or None, None, "na", 0.0, False, p_field,
                inherited=inherited_values.get(f.name),
                inherited_from=inherited_from.get(f.name),
            ))
            continue

        if f.kind == "numeric":
            w_num, t_num = sim.parse_number(efectivo), sim.parse_number(proposal_plan)
            similarity = sim.numeric_similarity(w_num, t_num, f.tolerance)
        elif f.kind in ("code", "profile"):
            # Comparar na convenção do plano: `263323` e `OF263323` são o mesmo
            # número de obra, e `60 x 5` é o mesmo perfil que `L60X60X5`. Sem
            # isto o motor marcava a vermelho valores certos e propunha
            # reescrevê-los só para lhes acrescentar o prefixo.
            truth = index.normalize_written(f.name, proposal_plan)
            if truth and truth in index.variants_for(f.name, efectivo):
                similarity = 1.0
            else:
                similarity = sim.code_similarity(
                    index.normalize_written(f.name, efectivo), truth
                )
        else:
            similarity = sim.text_similarity(efectivo, proposal_plan)

        threshold = _threshold_for(f.name, params)
        # A lição AT1T515: «strong» responde «é desta OF», nunca «é esta
        # irmã». of/ov são função da OF e a substituição total pode confiar
        # neles; modelo/perfil escolhem a linha ENTRE irmãs e continuam a
        # exigir o marginal do campo — sem isto gravou-se um modelo com
        # p_field=0.013, escolhido por ordem alfabética entre 8 empatadas.
        anchored = f.name in ("of", "ov")
        if replace_all:
            writable = (f.name not in human_fields
                        and (anchored or p_field >= threshold))
        else:
            # Campo herdado nunca é auto-escrito: seria transformar uma
            # inferência nossa num valor registado como se o operador o
            # tivesse escrito.
            writable = (f.name not in human_fields
                        and f.name not in inherited_from
                        and p_field >= threshold)

        if efectivo and similarity >= 1.0:
            # Certo — mas se o valor só existe por herança/aspas (nada escrito
            # na célula), a substituição total materializa-o: a folha fica
            # auto-contida, sem células vazias «a valer» por outras. É seguro
            # mesmo sem marginal: o valor efetivo JÁ é este.
            auto_materialize = bool(replace_all and f.name not in human_fields
                                    and not written_s)
            status, auto = "confirmed", auto_materialize
        elif similarity >= params.score.sim_near or not efectivo:
            # correção suave ou preenchimento de célula vazia
            status, auto = "snapped", writable
        else:
            # com substituição total escreve-se na mesma quando o campo tem
            # confiança própria; a cor vermelha continua a pedir revisão
            status, auto = "very_different", writable
        cells.append(CellCheck(
            f.name, written_s or None, proposal, status, similarity, auto, p_field,
            inherited=inherited_values.get(f.name),
            inherited_from=inherited_from.get(f.name),
        ))

    # Qtd: limite superior, não valor esperado. Não entra nos campos cruzados
    # porque a pergunta não é «é parecido com o plano?» mas «cabe no plano?».
    qtd_written = str(row.get("qtd") or "").strip()
    if qtd_written and not is_marked(field_value(scored_row, "perf_comp")):
        qtd_num = sim.parse_number(qtd_written) if _looks_numeric(qtd_written) else None
        limite = plan_quantity_for(
            index,
            str(scored_row.get("of") or ""),
            str(scored_row.get("modelo") or ""),
        ) if qtd_num is not None else None
        if limite is not None:
            over = qtd_num > limite
            cells.append(CellCheck(
                field="qtd", written=qtd_written,
                proposal=None,                      # o plano não dita a produção
                status="over_limit" if over else "confirmed",
                similarity=0.0 if over else 1.0,
                auto_write=False,                   # nunca reescrever produção
                p_correct=p,
                plan_limit=limite,
            ))

    # Cliente: também fora dos campos cruzados — não é «parecido com o plano?»
    # mas «de quem é esta obra?», e a resposta vem da OF (ver _cliente_check).
    cliente_cell = _cliente_check(
        row, scored_row, index, params, inherited_values,
        inherited_from, human_fields, p=p, permitir_escrita=True,
        replace_all=replace_all,
    )
    if cliente_cell is not None:
        cells.append(cliente_cell)

    # Metros teóricos da linha: qtd × comprimento da peça no plano (mm→m).
    # Linhas de perfil completo não têm «a» peça, portanto não têm metros.
    plan_length = sim.parse_number(entry.get("comp_mm"))
    line_meters = None
    if plan_length is not None and not linha_marcada:
        qtd_m = sim.parse_number(qtd_written) if _looks_numeric(qtd_written) else None
        if qtd_m is not None:
            line_meters = round(qtd_m * plan_length / 1000.0, 2)

    priority = max(
        (params.policy.criticality.get(c.field, params.policy.criticality_default) * (1.0 - p)
         for c in cells if c.status not in ("confirmed", "alias", "na")),
        default=0.0,
    )
    return RowCheck(
        row_index=row_index,
        matched_plan_key=match.winner.plan_key,
        p_correct=p,
        margin_bits=match.margin_bits,
        mode=match.mode,
        review_priority=priority,
        cells=cells,
        rivals=[r.plan_key for r in match.rivals],
        plan_length_mm=plan_length,
        line_meters=line_meters,
    )


def check_sheet(rows: list[dict], scorer: Scorer,
                human_fields_by_row: dict[int, set[str]] | None = None,
                footer: dict | None = None) -> dict:
    """Cruza a folha inteira e devolve um dicionário serializável (JSON).

    `footer` traz os totais manuscritos (METROS PRODUZIDOS) para o confronto
    com os metros teóricos do plano."""
    human_fields_by_row = human_fields_by_row or {}
    # A identidade resolve-se em conjunto, não linha a linha: o operador
    # escreve a OF uma vez e as linhas seguintes valem-se dela.
    content_fields = tuple(
        f.name for f in list(scorer.index.spec.identity_fields)
        + list(scorer.index.spec.numeric_fields)
        if f.name not in carryover.CARRY_FIELDS
    )
    identities = carryover.resolve(rows, content_fields, human_fields_by_row)
    checks = [
        check_row(row, i, scorer, human_fields_by_row.get(i), identities[i])
        for i, row in enumerate(rows)
    ]
    summary = {
        "rows": len(checks),
        "matched": sum(1 for c in checks if c.matched_plan_key),
        "strong": sum(1 for c in checks if c.mode == "strong"),
        "weak_guess": sum(1 for c in checks if c.mode == "weak_guess"),
        "no_match": sum(1 for c in checks if c.mode == "no_match"),
        "cells_confirmed": sum(1 for c in checks for x in c.cells if x.status == "confirmed"),
        "cells_snapped": sum(1 for c in checks for x in c.cells if x.status == "snapped"),
        "cells_very_different": sum(1 for c in checks for x in c.cells if x.status == "very_different"),
        "cells_inherited": sum(1 for c in checks for x in c.cells if x.inherited_from is not None),
        "cells_over_limit": sum(1 for c in checks for x in c.cells if x.status == "over_limit"),
    }
    # METROS PRODUZIDOS (rodapé, em metros) vs Σ qtd × comprimento do plano
    # (mm→m): a diferença é a coluna principal do controlo — positiva é
    # excedente/desperdício, negativa é produção abaixo do teórico.
    metros_teoricos = round(
        sum(c.line_meters for c in checks if c.line_meters is not None), 2)
    metros_produzidos = sim.parse_number((footer or {}).get("metros_produzidos"))
    # Linhas de produção sem metros (perfil completo, sem match): o total
    # teórico é PARCIAL e a diferença deixa de ser um desperdício honesto —
    # numa folha real, 2 linhas de perfil completo faziam «desperdício» de
    # 200 m que era só produção não contada.
    parcial = any(
        c.line_meters is None
        and i < len(rows)
        and any(v is not None and str(v).strip() for v in rows[i].values())
        for i, c in enumerate(checks)
    )
    summary["metros_teoricos"] = metros_teoricos if metros_teoricos else None
    summary["metros_produzidos"] = metros_produzidos
    summary["metros_parciais"] = parcial
    summary["desperdicio_m"] = (
        round(metros_produzidos - metros_teoricos, 2)
        if metros_produzidos is not None and metros_teoricos and not parcial
        else None
    )
    review_order = sorted(
        (c.row_index for c in checks if c.review_priority > 0),
        key=lambda i: -checks[i].review_priority,
    )
    return {
        "summary": summary,
        "review_order": review_order,
        "rows": [asdict(c) for c in checks],
    }
