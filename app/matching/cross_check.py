"""Cruzamento de uma folha completa: aplica o scorer a cada linha e converte o
resultado em estados de célula + fila de revisão.

Estados de célula:
- confirmed      — o escrito coincide com o plano (verde);
- snapped        — correção/atribuição automática com confiança acima do limiar (amarelo);
- very_different — o motor propõe algo distante do escrito; propõe mas exige olho humano (vermelho);
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
    status: str            # confirmed | snapped | very_different | unmatched | na
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
        priority = max(
            (params.policy.criticality.get(c.field, params.policy.criticality_default)
             for c in cells if c.status not in ("confirmed", "na")), default=1,
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
        proposal = str(raw_proposal).strip() if raw_proposal is not None else ""
        # Confiança por campo: o valor de um campo pode ser certo (todas as
        # irmãs concordam) mesmo quando a linha exacta é incerta. MAS o
        # marginal só vale para a proposta se apontar para o MESMO valor —
        # senão autorizava-se a escrita do valor do vencedor com a
        # probabilidade do valor rival (aconteceu: perfil errado gravado
        # com «91%» que era a probabilidade do perfil certo).
        marginal = match.marginals.get(f.name)
        if marginal and proposal and marginal[0] in index.variants_for(f.name, proposal):
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
            w_num, t_num = sim.parse_number(efectivo), sim.parse_number(proposal)
            similarity = sim.numeric_similarity(w_num, t_num, f.tolerance)
        elif f.kind in ("code", "profile"):
            # Comparar na convenção do plano: `263323` e `OF263323` são o mesmo
            # número de obra, e `60 x 5` é o mesmo perfil que `L60X60X5`. Sem
            # isto o motor marcava a vermelho valores certos e propunha
            # reescrevê-los só para lhes acrescentar o prefixo.
            truth = index.normalize_written(f.name, proposal)
            if truth and truth in index.variants_for(f.name, efectivo):
                similarity = 1.0
            else:
                similarity = sim.code_similarity(
                    index.normalize_written(f.name, efectivo), truth
                )
        else:
            similarity = sim.text_similarity(efectivo, proposal)

        threshold = _threshold_for(f.name, params)
        # Campo herdado nunca é auto-escrito: seria transformar uma inferência
        # nossa num valor registado como se o operador o tivesse escrito.
        writable = (f.name not in human_fields
                    and f.name not in inherited_from
                    and p_field >= threshold)

        if efectivo and similarity >= 1.0:
            status, auto = "confirmed", False
        elif similarity >= params.score.sim_near or not efectivo:
            # correção suave ou preenchimento de célula vazia
            status, auto = "snapped", writable
        else:
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

    priority = max(
        (params.policy.criticality.get(c.field, params.policy.criticality_default) * (1.0 - p)
         for c in cells if c.status not in ("confirmed", "na")),
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
    )


def check_sheet(rows: list[dict], scorer: Scorer,
                human_fields_by_row: dict[int, set[str]] | None = None) -> dict:
    """Cruza a folha inteira e devolve um dicionário serializável (JSON)."""
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
    review_order = sorted(
        (c.row_index for c in checks if c.review_priority > 0),
        key=lambda i: -checks[i].review_priority,
    )
    return {
        "summary": summary,
        "review_order": review_order,
        "rows": [asdict(c) for c in checks],
    }
