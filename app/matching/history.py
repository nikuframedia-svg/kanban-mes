"""Contexto temporal de desempate, sem acrescentar candidatos ao plano atual.

Um snapshot histórico é uma observação do planeamento, não um livro de
produção. A falta positiva só ajuda a ordenar candidatos com igual evidência;
nunca altera a quantidade observada nem substitui a identidade atual.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

from .geometry import canonical_code, parse_decimal
from .angle_geometry import profile_key

logger = logging.getLogger(__name__)


def _date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        # Importação local evita o ciclo pg_store -> production_facts -> cross.
        from ..pg_store import InvalidSheetDate, normalize_sheet_date

        try:
            return date.fromisoformat(normalize_sheet_date(text))
        except InvalidSheetDate:
            return None


def _timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime.combine(value, datetime.min.time())
    else:
        try:
            result = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        except ValueError:
            return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


@dataclass(frozen=True)
class SnapshotChoice:
    snapshot_id: str
    loaded_at: str
    sheet_date: str
    approximate_future: bool
    distance_days: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "loaded_at": self.loaded_at,
            "sheet_date": self.sheet_date,
            "approximate_future": self.approximate_future,
            "distance_days": self.distance_days,
            "reason": self.reason,
        }


def select_snapshot(
    snapshots: Iterable[dict], sheet_date: object,
    current_snapshot_id: str | None = None,
) -> SnapshotChoice | None:
    """Snapshot mais próximo do dia da folha; empate favorece o passado.

    O dia, e não uma hora fictícia da folha, define a distância. Duas cargas
    do mesmo dia preferem a última carga desse dia. O snapshot atual também
    pode ser o mais próximo; o argumento identifica o seu papel no resultado.
    """
    target = _date(sheet_date)
    if target is None:
        return None
    candidates = []
    for snapshot in snapshots:
        sid = str(snapshot.get("snapshot_id") or "").strip()
        loaded = _timestamp(snapshot.get("loaded_at") or snapshot.get("snapshot_loaded_at"))
        if not sid or loaded is None:
            continue
        delta = (loaded.date() - target).days
        candidates.append((abs(delta), delta > 0, -loaded.timestamp(), sid, loaded))
    if not candidates:
        return None
    distance, future, _, sid, loaded = min(candidates)
    reason = "nearest_future_approximation" if future else "nearest_observed_snapshot"
    if sid == current_snapshot_id:
        reason += ":current"
    return SnapshotChoice(sid, loaded.isoformat(), target.isoformat(), future, distance, reason)


def business_identity(entry: dict) -> tuple[str, str, str, str] | None:
    """Identidade sem snapshot; uma dimensão desconhecida não é um wildcard."""
    of = canonical_code(entry.get("of"), prefix="OF")
    model = canonical_code(entry.get("modelo"))
    profile = profile_key(entry.get("perfil"))
    length = parse_decimal(entry.get("comp_mm"))
    if not of or not model or not profile or length is None or length <= 0:
        return None
    return of, model, profile, str(length.normalize())


@dataclass
class HistoricalContext:
    choice: SnapshotChoice
    _evidence: dict[tuple[str, str, str, str], dict] = field(default_factory=dict, repr=False)
    eligible_current_count: int = 0
    matched_current_count: int = 0
    ambiguous_identity_count: int = 0
    provenance: str = "frozen_snapshot"

    @property
    def snapshot_id(self) -> str:
        return self.choice.snapshot_id

    @property
    def approximate_future(self) -> bool:
        return self.choice.approximate_future

    def remaining_for(self, entry: dict) -> Decimal | None:
        evidence = self.evidence_for(entry)
        if not evidence or not evidence["remaining_valid"]:
            return None
        # Texto numérico produzido por nós, não escrita portuguesa. Repassar
        # "25.000" pelo parser de OCR transformaria Decimal(25) em 25000.
        return Decimal(evidence["remaining_quantity"])

    def evidence_for(self, entry: dict) -> dict | None:
        identity = business_identity(entry)
        evidence = self._evidence.get(identity) if identity is not None else None
        return {**evidence, **self.choice.to_dict(), "current_plan_key": entry.get("plan_key")} if evidence is not None else None

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.choice.to_dict(),
            "provenance": self.provenance,
            "role": "equal_evidence_tiebreak_only",
            "eligible_current_count": self.eligible_current_count,
            "matched_current_count": self.matched_current_count,
            "ambiguous_identity_count": self.ambiguous_identity_count,
        }


# Nome curto usado nos consumidores que já falam em HistoryContext.
HistoryContext = HistoricalContext


def _observation_identity(entry: dict) -> tuple:
    """Equivalent observations may repeat without becoming extra support."""
    remaining = parse_decimal(entry.get("qtd_restante"))
    return (
        canonical_code(entry.get("maquina")),
        entry.get("falta_valida") is True,
        str(remaining.normalize()) if remaining is not None else None,
    )


def build_history_context(
    current_entries: Iterable[dict], historical_entries: Iterable[dict],
    choice: SnapshotChoice, *, provenance: str = "frozen_snapshot",
) -> HistoricalContext:
    """Une identidades inequívocas; linhas históricas não entram no universo.

    Repetições equivalentes representam a mesma observação, sem somar falta
    nem aumentar o suporte. Quantidades, validade ou máquinas conflitantes
    tornam a identidade ambígua e não fornecem contexto.
    """
    current: dict[tuple, list[dict]] = defaultdict(list)
    history: dict[tuple, list[dict]] = defaultdict(list)
    for entry in current_entries:
        key = business_identity(entry)
        if key is not None:
            current[key].append(entry)
    for entry in historical_entries:
        if entry.get("snapshot_id") not in (None, choice.snapshot_id):
            continue
        key = business_identity(entry)
        if key in current:
            history[key].append(entry)
    result = HistoricalContext(choice, provenance=provenance)
    result.eligible_current_count = sum(map(len, current.values()))
    for key, current_rows in current.items():
        historical_rows = history.get(key, [])
        if not historical_rows:
            continue
        if (len({_observation_identity(row) for row in current_rows}) != 1
                or len({_observation_identity(row) for row in historical_rows}) != 1):
            result.ambiguous_identity_count += 1
            continue
        historical = min(historical_rows, key=lambda row: str(row.get("plan_key") or ""))
        remaining = parse_decimal(historical.get("qtd_restante"))
        valid = historical.get("falta_valida") is True and remaining is not None
        # A regra do contrato devolve o excedente à parte; falta negativa é
        # inválida nesta interface, não uma evidência de produção negativa.
        valid = valid and remaining >= 0
        result._evidence[key] = {
            "plan_key": historical.get("plan_key"),
            "historical_plan_keys": sorted({str(row.get("plan_key") or "") for row in historical_rows}),
            "current_plan_keys": sorted({str(row.get("plan_key") or "") for row in current_rows}),
            "remaining_valid": bool(valid),
            "remaining_quantity": str(remaining) if valid else None,
            "positive_remaining": bool(valid and remaining > 0),
            "remaining_rule": historical.get("regra_calculo"),
            "remaining_source": historical.get("historical_remaining_source", "canonical_plan_contract"),
            "role": "equal_evidence_tiebreak_only",
        }
        result.matched_current_count += len(current_rows)
    return result



def load_history_context(current_index, sheet_date: object) -> HistoricalContext | None:
    """MTG3 only, canonical QTD - Maq. remaining; never read MES production."""
    if _date(sheet_date) is None or not getattr(current_index, "entries", None):
        return None
    from . import loaders
    from .. import pg
    import psycopg
    try:
        with pg.read_connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '3000ms'")
            cur.execute(
                "SELECT snapshot_id, loaded_at FROM audit_mtg.snapshots "
                "WHERE snapshot_id LIKE %s", (loaders._CANTONEIRAS_LIKE,),
            )
            choice = select_snapshot(cur.fetchall(), sheet_date, current_index.snapshot_id)
            if choice is None:
                return None
            cache = dict(getattr(current_index, "_cross_history_contexts", {}))
            if choice.snapshot_id in cache:
                return replace(cache[choice.snapshot_id], choice=choice)
            if choice.snapshot_id == current_index.snapshot_id:
                historical = current_index.entries
            else:
                cur.execute(
                    "SELECT plan_key, snapshot_id, production_order_no AS of, "
                    "component_ref AS modelo, profile_type AS perfil, "
                    "length_mm AS comp_mm, cutting_machine AS maquina, "
                    "remaining_quantity AS qtd_restante, remaining_valid AS falta_valida, "
                    "remaining_rule AS regra_calculo "
                    "FROM analytics_mtg.kanban_plan_lines "
                    "WHERE source_app = %s AND snapshot_id = %s",
                    ("kanban-mes", choice.snapshot_id),
                )
                historical = cur.fetchall()
        result = build_history_context(current_index.entries, historical, choice, provenance="postgres_read_only")
        # Bound memory by two reference snapshots. The temporal choice is a
        # per-document value; cached business identities contain no sheet data.
        if len(cache) >= 2:
            cache.pop(next(iter(cache)))
        cache[choice.snapshot_id] = result
        current_index._cross_history_contexts = cache
        return result
    except (psycopg.Error, OSError, ValueError, TypeError):
        logger.warning("Contexto histórico indisponível; CROSS usa apenas o plano atual.")
        return None
