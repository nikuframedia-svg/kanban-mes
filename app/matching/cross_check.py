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

from .params import CrossParams
from .refs import PlanIndex
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


def _threshold_for(field_name: str, params: CrossParams) -> float:
    pol = params.policy
    crit = pol.criticality.get(field_name, pol.criticality_default)
    if crit >= 5:
        return pol.write_threshold_critical_dim
    if crit >= 3:
        return pol.write_threshold_identity
    return pol.write_threshold_default


def check_row(row: dict, row_index: int, scorer: Scorer,
              human_fields: set[str] | None = None) -> RowCheck:
    """Cruza uma linha. `human_fields` = campos já editados por humanos (invioláveis)."""
    human_fields = human_fields or set()
    params = scorer.params
    index: PlanIndex = scorer.index
    match: RowMatch = scorer.match_row(row)

    spec_fields = list(index.spec.identity_fields) + list(index.spec.numeric_fields)
    cells: list[CellCheck] = []

    if match.winner is None or match.p_correct < 0.5:
        # H₀ plausível: nada de propostas; célula a célula fica "unmatched"
        for f in spec_fields:
            written = row.get(f.name)
            written_s = str(written).strip() if written is not None else ""
            cells.append(CellCheck(
                field=f.name, written=written_s or None, proposal=None,
                status="unmatched" if written_s else "na",
                similarity=0.0, auto_write=False,
                p_correct=match.p_correct,
            ))
        priority = max(
            (params.policy.criticality.get(f.name, params.policy.criticality_default)
             for f in spec_fields), default=1,
        ) * (1.0 - match.p_correct)
        return RowCheck(
            row_index=row_index, matched_plan_key=None,
            p_correct=match.p_correct, margin_bits=match.margin_bits,
            mode="no_match" if match.winner is None else match.mode,
            review_priority=priority, cells=cells,
        )

    entry = index.entries[match.winner.idx]
    p = match.p_correct

    for f in spec_fields:
        written = row.get(f.name)
        written_s = str(written).strip() if written is not None else ""
        raw_proposal = entry.get(f.entry_key)
        proposal = str(raw_proposal).strip() if raw_proposal is not None else ""
        if not proposal:
            cells.append(CellCheck(f.name, written_s or None, None, "na", 0.0, False, p))
            continue

        if f.kind == "numeric":
            w_num, t_num = sim.parse_number(written_s), sim.parse_number(proposal)
            similarity = sim.numeric_similarity(w_num, t_num, f.tolerance)
        elif f.kind == "code":
            similarity = sim.code_similarity(written_s, proposal)
        else:
            similarity = sim.text_similarity(written_s, proposal)

        threshold = _threshold_for(f.name, params)
        writable = f.name not in human_fields and p >= threshold

        if written_s and similarity >= 1.0:
            status, auto = "confirmed", False
        elif similarity >= params.score.sim_near or not written_s:
            # correção suave ou preenchimento de célula vazia
            status, auto = "snapped", writable
        else:
            status, auto = "very_different", writable
        cells.append(CellCheck(f.name, written_s or None, proposal, status, similarity, auto, p))

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
    checks = [
        check_row(row, i, scorer, human_fields_by_row.get(i))
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
