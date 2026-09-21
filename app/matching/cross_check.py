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

Escrita automática: existindo candidato, todos os campos que pertencem ao
planeamento são materializados a partir do vencedor determinístico, incluindo
vazios, herdados, equivalentes só na forma e valores antes editados à mão. A
edição humana continua no trilho de auditoria, mas não é um veto sobre factos
do plano. Cabeçalho, quantidades e restantes factos de produção ficam fora
desta política e nunca são reescritos.
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
    # A proposta foi mesmo gravada na folha (loop de aplicação da camada web).
    # Depois de aplicar, a célula recalculada descreve o valor final.
    applied: bool = False


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
    selected_snapshot_id: str | None = None
    selected_explicitly: bool = False
    binding_stale: bool = False
    plan_refs: list[dict] = field(default_factory=list)
    plan_refs_valid: bool | None = None
    plan_refs_error: str | None = None
    full_profile_quantity: float | None = None
    plan_line_meters: float | None = None
    plan_meters_error: str | None = None
    plan_refs_expanded: bool = False


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
                   replace_all: bool = False,
                   forced_name: str | None = None) -> CellCheck | None:
    """Célula do cliente, fora do scorer.

    O cliente não entra na identificação da linha (o plano guarda o cliente
    interno da Metalogalva, o operador escreve o final — discordar é o caso
    normal, não um erro de OCR). Mas resolvida a OF, o plano sabe de quem é a
    obra: célula vazia recebe proposta; escrita e parecida confirma; escrita e
    diferente é substituída pelo nome do plano (política de 26/08: o cliente da
    folha é sempre o do planeamento, mesmo em linha incerta); `alias` fica só
    para quando a substituição está desligada ou o ramo não permite escrita.
    O que o operador escreveu fica no raw e no trilho de auditoria.
    """
    nome = (str(forced_name).strip() if forced_name is not None
            else plan_customer_for(index, str(scored_row.get("of") or "")))
    if not nome:
        return None
    written = row.get("cliente")
    if carryover.is_ditto(written):
        written = None
    written_s = str(written).strip() if written is not None else ""
    efectivo = written_s or str(inherited_values.get("cliente") or "").strip()
    if params.policy.replace_with_plan and permitir_escrita:
        similarity = sim.text_similarity(efectivo, nome) if efectivo else 0.0
        status = (
            "confirmed" if efectivo and similarity >= params.score.sim_near
            else "snapped"
        )
        materialize = written_s != nome
        return CellCheck(
            field="cliente", written=written_s or None,
            proposal=nome if materialize else None,
            status=status, similarity=similarity,
            auto_write=materialize, p_correct=p,
            inherited=inherited_values.get("cliente"),
            inherited_from=inherited_from.get("cliente"),
        )
    if not efectivo:
        writable = (permitir_escrita
                    and "cliente" not in human_fields
                    and (replace_all
                         or ("cliente" not in inherited_from
                             and p >= _threshold_for("cliente", params))))
        status, proposal, similarity, auto = "snapped", nome, 0.0, writable
    else:
        similarity = sim.text_similarity(efectivo, nome)
        # Substituição total (26/08): um cliente ESCRITO que difere do plano
        # substitui-se sempre que a linha tem ligação credível — mesmo em
        # linha incerta. O que o operador escreveu fica no raw e visível por
        # baixo da célula na revisão.
        substituir = (params.policy.replace_with_plan and permitir_escrita
                      and "cliente" not in human_fields)
        if similarity >= params.score.sim_near:
            # mesmo cliente. Se o texto difere só na forma (restos antigos da
            # customer_key: «tecpoles gmbh» vs «TECPOLES GMBH»), a
            # substituição escreve a forma legível do plano.
            canonizar = (substituir and written_s and written_s != nome
                         and sim.compact(written_s) == sim.compact(nome))
            status, proposal, auto = "confirmed", (nome if canonizar else None), canonizar
        elif substituir:
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
    if row.get("_identity_unresolved"):
        return RowCheck(row_index, None, 0.0, 0.0, "no_match", 1.0)
    human_fields = human_fields or set()
    params = scorer.params
    index: PlanIndex = scorer.index
    scored_row = carryover.effective_row(row, identity) if identity else row
    match: RowMatch = scorer.match_row(scored_row)
    linha_marcada = is_marked(field_value(scored_row, "perf_comp"))
    binding = (
        row.get("_plan_binding")
        if not linha_marcada and isinstance(row.get("_plan_binding"), dict)
        else {}
    )
    current_snapshot = str(index.snapshot_id) if index.snapshot_id is not None else None
    bound_snapshot = str(binding.get("snapshot_id")) if binding.get("snapshot_id") else None
    bound_key = str(binding.get("plan_key")) if binding.get("plan_key") else None
    binding_stale = bool(
        binding and current_snapshot and bound_snapshot != current_snapshot
    )
    selected_explicitly = False
    if bound_key and not binding_stale:
        bound_idx = next(
            (idx for idx, entry in enumerate(index.entries)
             if str(entry.get(index.spec.key_field)) == bound_key),
            None,
        )
        if bound_idx is not None:
            selected_explicitly = True
            bound_score = scorer.score_entry(scored_row, bound_idx)
            rivals = list(match.rivals)
            if match.winner is not None and match.winner.plan_key != bound_key:
                rivals = [match.winner, *rivals]
            match = RowMatch(
                winner=bound_score, p_correct=1.0, p_primary=1.0,
                margin_bits=match.margin_bits, mode="explicit",
                rivals=rivals[:5], candidates_evaluated=match.candidates_evaluated,
                marginals=match.marginals,
            )
        else:
            # Um binding explícito sem chave no snapshot que diz pertencer é
            # inválido; não o degradar silenciosamente para escolha automática.
            binding_stale = True
    inherited_from = dict(identity.inherited_from) if identity else {}
    inherited_values = {f: identity.values.get(f) for f in inherited_from} if identity else {}

    spec_fields = list(index.spec.identity_fields) + list(index.spec.numeric_fields)
    cells: list[CellCheck] = []

    # A pergunta que decide se há ligação ao plano é «de que OF é esta linha?»,
    # não «que linha exacta do plano é esta». Numa OF com 300 irmãs a segunda
    # nunca passa de 0,3 por construção, e usá-la deitava fora tudo.
    confidence = max(match.p_primary, match.p_correct)
    if match.winner is None:
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
            selected_snapshot_id=current_snapshot,
            binding_stale=binding_stale,
        )

    entry = index.entries[match.winner.idx]
    p = confidence
    # Linha de perfil completo: representa todas as referências daquele perfil
    # na obra, não uma. Propor-lhe «o» modelo seria escolher uma à sorte entre
    # dezenas — o que essa linha precisa é do pop-up com a lista.
    # `field_value` e não `.get`: folhas lidas antes do rename guardaram o
    # visto em `comp_mm`, e ignorá-las punha o motor a propor modelos nelas.
    # Política de substituição total (19/08, alargada a 26/08): uma célula
    # ESCRITA que difere do plano substitui-se SEMPRE que a linha tem ligação
    # credível — mesmo weak, mesmo com confiança baixa; o valor lido fica no
    # raw e visível por baixo da célula na revisão. `replace_all` (match
    # forte) continua a mandar só nas células SEM valor escrito
    # (materialização de herdadas e preenchimento de vazias).
    replace_all = params.policy.replace_with_plan

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
        if linha_marcada and f.name == "modelo":
            # A linha física representa TODAS as referências da combinação
            # OF+Perfil. Um modelo isolado (mesmo escrito à mão) seria uma
            # falsa redução; limpa-se sem tocar na marca Perf. Comp.
            cells.append(CellCheck(
                f.name, written_s or None, "" if written_s else None,
                "snapped" if written_s else "na", 0.0, bool(written_s), p_field,
                inherited=inherited_values.get(f.name),
                inherited_from=inherited_from.get(f.name),
            ))
            continue
        if not proposal:
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
                compare = sim.ratio if f.kind == "profile" else sim.code_similarity
                similarity = compare(
                    index.normalize_written(f.name, efectivo), truth
                )
        else:
            similarity = sim.text_similarity(efectivo, proposal_plan)

        threshold = _threshold_for(f.name, params)
        # O vencedor já foi ordenado por score, nº de concordâncias e
        # plan_key. Com a política obrigatória, o limiar afeta apenas a cor e
        # a confiança mostrada; nunca impede materializar um campo do plano.
        writable = (
            True if replace_all
            else (f.name not in human_fields
                  and f.name not in inherited_from
                  and p_field >= threshold)
        )

        if efectivo and similarity >= 1.0:
            # Certo — mas se o valor só existe por herança/aspas (nada escrito
            # na célula), a substituição total materializa-o: a folha fica
            # auto-contida, sem células vazias «a valer» por outras. É seguro
            # mesmo sem marginal: o valor efetivo JÁ é este.
            auto_materialize = bool(
                writable and (not written_s or written_s != proposal)
            )
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
        limite = sim.parse_number(entry.get("qtd_planeada")) if qtd_num is not None else None
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

    # Cliente não participa na escolha, mas pertence ao planeamento: depois de
    # escolhido o vencedor vem diretamente dessa entry, sem agregar a OF.
    cliente_cell = _cliente_check(
        row, {**scored_row, "of": entry.get("of")}, index, params,
        inherited_values, inherited_from,
        (set() if replace_all else human_fields), p=p, permitir_escrita=True,
        replace_all=replace_all,
        forced_name=(entry.get("cliente_nome") or entry.get("cliente")),
    )
    if cliente_cell is not None:
        cells.append(cliente_cell)

    # Metros teóricos da linha: qtd × comprimento da peça no plano (mm→m).
    plan_length = sim.parse_number(entry.get("comp_mm"))
    line_meters = None
    plan_refs: list[dict] = []
    plan_refs_valid: bool | None = None
    plan_refs_error: str | None = None
    full_profile_quantity = None
    plan_meters_error = None
    plan_refs_expanded = False
    if linha_marcada:
        from .full_profile import expand_entries

        hits = set(index.exact_matches("of", str(entry.get("of") or "")))
        hits &= set(index.exact_matches("perfil", str(entry.get("perfil") or "")))
        expanded = expand_entries(
            [index.entries[i] for i in sorted(hits)],
            index.snapshot_id,
            precision=2,
        )
        plan_refs = expanded["plan_refs"]
        plan_refs_valid = expanded["plan_refs_valid"]
        plan_refs_error = expanded["plan_refs_error"]
        full_profile_quantity = expanded["full_profile_quantity"]
        line_meters = expanded["line_meters"]
        plan_meters_error = expanded["plan_meters_error"]
        plan_refs_expanded = True
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
        selected_snapshot_id=current_snapshot,
        selected_explicitly=selected_explicitly,
        binding_stale=binding_stale,
        plan_refs=plan_refs,
        plan_refs_valid=plan_refs_valid,
        plan_refs_error=plan_refs_error,
        full_profile_quantity=full_profile_quantity,
        plan_line_meters=line_meters,
        plan_meters_error=plan_meters_error,
        plan_refs_expanded=plan_refs_expanded,
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
    # Edições humanas dos campos do planeamento são evidência de auditoria,
    # não um veto à convenção de células vazias («igual à linha anterior»).
    identities = carryover.resolve(rows, content_fields, {})
    checks = [
        check_row(row, i, scorer, human_fields_by_row.get(i), identities[i])
        for i, row in enumerate(rows)
        if not carryover.is_deleted(row)
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
    # Linhas de produção sem metros tornam o total teórico parcial. Linhas de
    # perfil completo válidas já trazem a soma dos filhos e deixam de criar
    # esse falso buraco.
    parcial = any(
        c.line_meters is None
        and c.row_index < len(rows)
        and any(
            v is not None and str(v).strip()
            for key, v in rows[c.row_index].items() if not str(key).startswith("_")
        )
        for c in checks
    )
    summary["metros_teoricos"] = metros_teoricos if metros_teoricos else None
    summary["metros_produzidos"] = metros_produzidos
    summary["metros_parciais"] = parcial
    summary["desperdicio_m"] = (
        round(metros_produzidos - metros_teoricos, 2)
        if metros_produzidos is not None and metros_teoricos and not parcial
        else None
    )
    review_order = [
        c.row_index for c in sorted(
            (check for check in checks if check.review_priority > 0),
            key=lambda check: -check.review_priority,
        )
    ]
    result = {
        "snapshot_id": scorer.index.snapshot_id,
        "summary": summary,
        "review_order": review_order,
        "rows": [asdict(c) for c in checks],
    }

    from .full_profile import attach_plan_facts
    attach_plan_facts(
        result, scorer.index, rows, precision=2, reuse_expanded=True,
    )
    return result
