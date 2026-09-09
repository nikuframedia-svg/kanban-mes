"""Cross V3: immutable observations, global maximum, mandatory substitution.

The caller supplies original OCR plus human observations, never identities
previously written by the matcher. This module is pure: no database, network,
sheet mutation, acceptance threshold, or production allocation.

OF/profile groups are reduced with MAX, not a sum or a mean. A large order
must neither gain evidence by duplicating rows nor lose it by having many
unrelated references. Continuity is decoded over OFs with max-product.
"""

from __future__ import annotations

import bisect
import hashlib
import heapq
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from decimal import Decimal

from . import carryover, similarity as sim
from .channel import CharChannel
from .geometry import (
    Profile, canonical_code, decimal_text, family_for_material, material_key, parse_decimal,
    parse_profile, profile_interpretations, profile_key,
)
from .angle_geometry import parse_profile, profile_interpretations, profile_key
from .params import CrossParams
from .refs import PlanIndex
from ..templates_spec import field_value, is_marked


VERSION = "cross-v3"
IDENTITY_FIELDS = ("of", "ov", "perfil", "modelo", "cliente")
# Fixed evidence weights: OF frequency in *plan rows* never changes its weight.
CODE_WEIGHTS = {"of": 12.0, "ov": 7.0, "modelo": 10.0}
PROFILE_BITS = 10.0
LENGTH_BITS = 6.0
MATERIAL_MATCH_BITS = 2.0
MATERIAL_CONFLICT_BITS = -4.0
SPLIT_COST_BITS = 6.5
CONTINUITY_BLANK_BITS = 2.0
CONTINUITY_WRITTEN_BITS = 0.5
FUZZY_VALUES = 10
_PRECISION = 1_000_000
_ACTIVITIES = re.compile(
    r"\b(?:LIMPEZAS?|ARRUMACAO|ARRUMACOES|MANUTENCAO|MANUTENCOES|"
    r"SEM\s+TRABALHO|AGUARDAR\s+MATERIAL|ESPERA\s+DE\s+MATERIAL)\b"
)


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _written(row: dict, name: str) -> str:
    value = row.get(name)
    if name == "perfil" and value in (None, ""):
        value = row.get("tubo")
    if name == "modelo" and value in (None, ""):
        value = row.get("referencia")
    return "" if carryover.is_ditto(value) else _text(value)


def classify_row(row: dict) -> str:
    """Classification depends on observations, even when no plan is available."""
    if row.get("_deleted") is True:
        return "deleted"
    if row.get("_row_kind") == "activity":
        return "activity"
    values = [
        _text(value) for key, value in row.items()
        if not str(key).startswith("_") and value is not None
        and not carryover.is_ditto(value)
    ]
    if not any(values):
        return "empty"
    quantity = parse_decimal(row.get("qtd"))
    if quantity is not None and quantity > 0:
        return "production"
    if row.get("atividade") or row.get("activity"):
        return "activity"
    activity_text = " ".join(values).upper()
    # Accent folding without removing word boundaries.
    import unicodedata
    activity_text = "".join(
        ch for ch in unicodedata.normalize("NFKD", activity_text)
        if not unicodedata.combining(ch)
    )
    if _ACTIVITIES.search(activity_text):
        return "activity"
    return "production"


def _internal(row: dict) -> bool:
    if any(canonical_code(row.get(f)) == "INTERNO" for f in carryover.CARRY_FIELDS):
        return True
    # Explicit workshop destination from the real source sheet, not an OF.
    customer = canonical_code(row.get("cliente"))
    return bool(re.fullmatch(r"PAV(?:ILHAO)?\d+(?:EQ\d+)?", customer))


def _code(row: dict, field: str) -> str:
    value = _written(row, field)
    if canonical_code(value) == "INTERNO":
        return ""
    return canonical_code(value, field.upper() if field in ("of", "ov") else "")


def _entry_value(entry: dict, field: str):
    aliases = {
        "of": "production_order_no", "ov": "sales_order_no",
        "modelo": "component_ref", "perfil": "profile_type",
        "comp_mm": "length_mm", "cliente": "customer_name",
        "maquina": "cutting_machine", "qtd_planeada": "quantity_planned",
    }
    if field == "cliente":
        return entry.get("cliente_nome") or entry.get("cliente") or entry.get("customer_name")
    value = entry.get(field)
    return entry.get(aliases.get(field, "")) if value is None else value


def _entry_code(entry: dict, field: str) -> str:
    return canonical_code(_entry_value(entry, field), field.upper() if field in ("of", "ov") else "")


def _length(row: dict) -> tuple[Decimal | None, str]:
    return None, "absent"


def _units(bits: float) -> int:
    return round(bits * _PRECISION)


def _bits(units: int) -> float:
    return units / _PRECISION


def _numeric(value: Decimal | None) -> float | None:
    if value is None:
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _json(value):
    if isinstance(value, Decimal):
        return _numeric(value)
    if isinstance(value, dict):
        return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _profile_score(written: Profile, truth: Profile, material: object) -> tuple[float, str, float]:
    if not written.literal:
        return 0.0, "absent", 0.0
    if not truth.literal:
        return -3.0, "missing_plan_value", 0.0
    truth_family = truth.family or family_for_material(material)
    if written.family and truth_family and written.family != truth_family:
        return -14.0, "family_conflict", 0.0
    if written.dimensions and truth.dimensions:
        n = min(len(written.dimensions), len(truth.dimensions))
        same = all(abs(written.dimensions[i] - truth.dimensions[i]) <= Decimal("0.001") for i in range(n))
        if same and len(written.dimensions) == len(truth.dimensions):
            return PROFILE_BITS, "geometry_exact", 1.0
        if same and len(written.dimensions) < len(truth.dimensions):
            return 7.0, "geometry_partial", 0.9
        if len(written.dimensions) < len(truth.dimensions) and all(
            abs(w - t) <= (max(Decimal("0.5"), abs(t) * Decimal("0.01")) if i == 0
                           else max(Decimal("0.05"), abs(t) * Decimal("0.01")))
            for i, (w, t) in enumerate(zip(written.dimensions, truth.dimensions))
        ):
            # A nominal Ø42 or Ø60 legitimately abbreviates Ø42.4 or Ø60.3;
            # omitted thickness is not a contradictory extra dimension.
            return 6.0, "geometry_partial_near", 0.85
        if len(written.dimensions) == len(truth.dimensions) and all(
            abs(w - t) <= max(Decimal("0.01"), abs(t) * Decimal("0.01"))
            for w, t in zip(written.dimensions, truth.dimensions)
        ):
            return 4.0, "geometry_near", 0.8
        return -10.0, "geometry_conflict", 0.0
    if written.key == truth.key:
        return 4.0, "text_exact", 1.0
    similarity = sim.ratio(written.key, truth.key)
    return (2.0 * similarity if similarity >= 0.8 else -4.0), "text_profile", similarity


def _length_score(written: Decimal, truth: Decimal | None) -> tuple[float, str]:
    if truth is None:
        return -2.0, "missing_plan_length"
    delta = abs(written - truth)
    if delta <= 1:
        return LENGTH_BITS, "length_exact"
    scale = max(Decimal(1), abs(truth) * Decimal("0.005"))
    score = max(-8.0, LENGTH_BITS - float((delta - 1) / scale) * 2.0)
    return score, "length_near" if score > 0 else "length_conflict"


def _geometry(row: dict, entry: dict, *, full_profile: bool = False) -> dict:
    raw_profile = _written(row, "perfil")
    truth_raw = _entry_value(entry, "perfil")
    truth = parse_profile(truth_raw)
    plan_length = parse_decimal(_entry_value(entry, "comp_mm"))
    direct_length, length_source = _length(row)
    if full_profile:
        direct_length = None
    best: tuple[float, dict] | None = None
    for profile, embedded_length in profile_interpretations(raw_profile):
        p_bits, p_reason, similarity = _profile_score(
            profile, truth, entry.get("material") or entry.get("material_type")
        )
        plan_material = truth.material or material_key(entry.get("material") or entry.get("material_type"))
        m_bits, m_reason = 0.0, "not_comparable"
        if profile.material and plan_material:
            m_bits, m_reason = ((MATERIAL_MATCH_BITS, "material_exact") if profile.material == plan_material
                                else (MATERIAL_CONFLICT_BITS, "material_conflict"))
        lengths = [direct_length] if direct_length is not None else []
        if embedded_length is not None and not full_profile:
            if not lengths or abs(embedded_length - lengths[0]) > 1:
                lengths.append(embedded_length)
        length_results = [_length_score(value, plan_length) for value in lengths]
        l_bits = sum(value for value, _ in length_results) / len(length_results) if length_results else 0.0
        split_cost = SPLIT_COST_BITS if embedded_length is not None else 0.0
        score = p_bits + l_bits + m_bits - split_cost - profile.ocr_cost_bits
        detail = {
            "score": round(score, 6), "reason": (
                "profile_length_split" if embedded_length is not None else
                p_reason if raw_profile else length_results[0][1] if length_results else "absent"
            ),
            "written": raw_profile or None, "truth": _text(truth_raw) or None,
            "profile_score": p_bits, "profile_reason": p_reason,
            "observed_material": profile.material or None, "plan_material": plan_material or None,
            "material_score": m_bits, "material_reason": m_reason,
            "similarity": similarity, "length_score": l_bits,
            "length_reason": length_results[0][1] if length_results else "absent",
            "length_source": "profile_split" if embedded_length is not None else length_source,
            "observed_length_mm": _numeric(embedded_length if embedded_length is not None else direct_length),
            "plan_length_mm": _numeric(plan_length),
            "ocr_interpretation_cost": profile.ocr_cost_bits,
        }
        if best is None or score > best[0]:
            best = score, detail
    assert best is not None
    return best[1]


@dataclass
class _Candidate:
    idx: int
    of: str
    profile: str
    model: str
    semantic: tuple[str, ...]
    score: int
    groups: dict
    remaining_priority: tuple[int, int]
    support: float | None = None

    @property
    def state(self) -> tuple[str, str]:
        return self.of, self.profile


def _candidate_order(candidate: _Candidate) -> tuple:
    return (-candidate.score, -candidate.remaining_priority[0],
            -candidate.remaining_priority[1], candidate.semantic)


@dataclass
class _Row:
    index: int
    source: dict
    identity: carryover.RowIdentity
    pool: list[_Candidate]
    states: dict[tuple[str, str], _Candidate]
    fallback: bool
    forced: bool
    binding_status: str | None
    max_marginals: dict[tuple[str, str], int] | None = None
    chosen: _Candidate | None = None
    continuity_bits: float = 0.0
    support_offset: int = 0


class _Prepared:
    """Immutable reference features owned by one immutable PlanIndex."""

    def __init__(self, index: PlanIndex, params: CrossParams):
        self.index, self.params = index, params
        self.channel = CharChannel(params.channel)
        self.maps: dict[str, dict[str, set[int]]] = {f: defaultdict(set) for f in ("of", "ov", "modelo", "perfil")}
        self.keys: dict[str, list[str]] = {}
        self.geometry_cache: dict[tuple, dict] = {}
        self.code_cache: dict[tuple, tuple] = {}
        self.fuzzy_cache: dict[tuple, set[int]] = {}
        self.lengths: list[tuple[Decimal, int]] = []
        self.remaining: list[tuple[int, int]] = []
        self.semantic: list[tuple[str, ...]] = []
        self.quantities: dict[tuple[str, str], Decimal] = {}
        self.codes = {f: [] for f in ("of", "ov", "modelo", "cliente")}
        self.text_values = {f: [] for f in ("of", "ov", "modelo")}
        self.geometry_keys = []
        self.key_to_idx = {}
        for i, entry in enumerate(index.entries):
            for f in ("of", "ov", "modelo"):
                value = _entry_code(entry, f)
                if value:
                    self.maps[f][value].add(i)
            for f in self.codes:
                self.codes[f].append(_entry_code(entry, f))
            for f in self.text_values:
                self.text_values[f].append(_text(_entry_value(entry, f)) or None)
            self.key_to_idx[_text(entry.get(index.spec.key_field) or i)] = i
            p_key = profile_key(_entry_value(entry, "perfil"))
            if p_key:
                self.maps["perfil"][p_key].add(i)
            length = parse_decimal(_entry_value(entry, "comp_mm"))
            if length is not None:
                self.lengths.append((length, i))
            current_remaining = None
            if entry.get("falta_valida") is True or entry.get("remaining_valid") is True:
                current_remaining = parse_decimal(entry.get("qtd_restante", entry.get("remaining_quantity")))
            self.remaining.append((
                0,
                int(current_remaining is not None and current_remaining > 0),
            ))
            semantic = (
                _entry_code(entry, "of"), p_key, _entry_code(entry, "modelo"),
                decimal_text(length), canonical_code(_entry_value(entry, "maquina")),
                _text(entry.get(index.spec.key_field) or i),
            )
            self.semantic.append(semantic)
            self.geometry_keys.append((
                _text(_entry_value(entry, "perfil")), decimal_text(length),
                _text(entry.get("material") or entry.get("material_type")),
            ))
            quantity = parse_decimal(_entry_value(entry, "qtd_planeada"))
            if quantity is not None:
                key = (_entry_code(entry, "of"), _entry_code(entry, "modelo"))
                self.quantities[key] = self.quantities.get(key, Decimal(0)) + quantity
        self.reference_keys = [key[:-1] for key in self.semantic]
        self.lengths.sort()
        self.keys = {field: sorted(values) for field, values in self.maps.items()}
        # Rarity is measured across distinct orders, never physical plan rows.
        # It has no corpus-size denominator: unrelated additions do not alter
        # existing scores, and repeated references inside one OF add no weight.
        self.entity_frequency = {
            field: {value: len({self.codes["of"][i] for i in hits})
                    for value, hits in self.maps[field].items()}
            for field in ("ov", "modelo")
        }

        self.bags = {field: {value: Counter(value) for value in values}
                     for field, values in self.keys.items()}


class _Plan:
    def __init__(self, index: PlanIndex, params: CrossParams, history):
        # Parameters belong to the cache key even when a feature currently
        # does not use them. Never share remaining/history or row observations.
        cache_key = repr(asdict(params))
        cached = getattr(index, "_cross_v3_prepared", None)
        if cached is None or cached[0] != cache_key:
            cached = (cache_key, _Prepared(index, params))
            index._cross_v3_prepared = cached
        self.__dict__.update(cached[1].__dict__)
        self.history = history
        # Memoized pair comparisons are pure functions of this snapshot's
        # parameters. They never add observations or historical priorities.
        # Bound them between documents; in-flight readers keep their tables.
        prepared = cached[1]
        if len(prepared.code_cache) > 250_000:
            prepared.code_cache = {}
        if len(prepared.geometry_cache) > 50_000:
            prepared.geometry_cache = {}
        self.code_cache = prepared.code_cache
        self.geometry_cache = prepared.geometry_cache
        self.row_cache = {}
        if history is not None and hasattr(history, "remaining_for"):
            self.remaining = [
                (int((parse_decimal(history.remaining_for(entry)) or 0) > 0), current[1])
                for entry, current in zip(index.entries, self.remaining)
            ]

    def fuzzy(self, field: str, written: str) -> set[int]:
        key = field, written
        if key in self.fuzzy_cache:
            return self.fuzzy_cache[key]
        scores = []
        written_bag = Counter(written)
        for value in self.keys[field]:
            if abs(len(value) - len(written)) > 3:
                continue
            bag = self.bags[field][value]
            # Each substitution repairs at most two unmatched characters;
            # insertion/deletion repairs one. This is a safe lower bound.
            difference = sum(abs(count - bag.get(ch, 0)) for ch, count in written_bag.items())
            difference += sum(count for ch, count in bag.items() if ch not in written_bag)
            if difference / 2 > 0.4 * max(len(written), len(value)) + 1e-12:
                continue
            ratio = sim.ratio(written, value)
            if ratio >= 0.6:
                scores.append((-ratio, value))
        out: set[int] = set()
        for _, value in sorted(scores)[:FUZZY_VALUES]:
            out.update(self.maps[field][value])
        self.fuzzy_cache[key] = out
        return out

    def candidates(self, row: dict, identity: carryover.RowIdentity) -> tuple[set[int], bool]:
        out: set[int] = set()
        for field in ("of", "ov", "modelo"):
            if field == "modelo" and is_marked(field_value(row, "perf_comp")):
                continue
            written = _code(row, field)
            inherited = identity.values.get(field) if identity.is_inherited(field) else None
            value = written or canonical_code(inherited, field.upper() if field in ("of", "ov") else "")
            if value:
                out.update(self.maps[field].get(value, ()))
                out.update(self.fuzzy(field, value))
        profile_value = _written(row, "perfil") or identity.values.get("perfil")
        for profile, length in profile_interpretations(profile_value):
            if profile.key:
                out.update(self.maps["perfil"].get(profile.key, ()))
                out.update(self.fuzzy("perfil", profile.key))
            if length is not None and _written(row, "perfil"):
                out.update(self.with_length(length))
        length, _ = _length(row)
        if length is not None:
            out.update(self.with_length(length))
        fallback = not out
        return (set(range(self.index.n)) if fallback else out), fallback

    def with_length(self, value: Decimal) -> set[int]:
        lo = bisect.bisect_left(self.lengths, (value - 1, -1))
        hi = bisect.bisect_right(self.lengths, (value + 1, self.index.n))
        return {i for _, i in self.lengths[lo:hi]}

    def code_evidence(self, field: str, written: str, truth: str) -> tuple[float, str, float]:
        key = field, written, truth
        if key in self.code_cache:
            return self.code_cache[key]
        weight = CODE_WEIGHTS[field]
        # MTG3 reuses component codes across orders. Reuse must not make
        # an exact written model weaker than a rare glyph-confusable model.
        # Geometry and the bounded sequence context can still correct it.
        if field == "ov":
            distinct_orders = max(1, self.entity_frequency[field].get(truth, 1))
            weight *= max(0.7, 1.0 - 0.1 * math.log2(distinct_orders))
        if not written:
            result = (0.0, "absent", 0.0)
        elif not truth:
            result = (-0.25 * weight, "missing_plan_value", 0.0)
        elif written == truth:
            result = (weight, "exact", 1.0)
        else:
            ratio = sim.ratio(written, truth)
            channel = self.channel.evidence(written, truth)
            if ratio >= 0.9:
                result = (max(0.45, channel) * weight, "near", ratio)
            elif channel > 0:
                result = (channel * weight, "glyph", ratio)
            else:
                result = (-0.55 * weight, "disagree", ratio)
        self.code_cache[key] = result
        return result

    def score(self, row: dict, identity: carryover.RowIdentity, idx: int, *, details: bool = False) -> _Candidate:
        entry = self.index.entries[idx]
        row_key = id(row)
        if row_key not in self.row_cache:
            full = is_marked(field_value(row, "perf_comp"))
            observations = {field: ("" if full and field == "modelo" else _code(row, field),
                                    _written(row, field) or None)
                            for field in ("of", "ov", "modelo")}
            geometry_key = (
                _written(row, "perfil"), decimal_text(parse_decimal(row.get("comp_mm"))),
                decimal_text(parse_decimal(row.get("qtd"))),
                decimal_text(parse_decimal(field_value(row, "qtd_total_mm"))), full,
            )
            self.row_cache[row_key] = (full, observations, geometry_key, {field: {} for field in observations})
        full, observations, geometry_key, group_cache = self.row_cache[row_key]
        groups = {} if details else None
        value = 0.0
        for field, (written, literal) in observations.items():
            truth = self.codes[field][idx]
            detail_key = truth, self.text_values[field][idx]
            detail = group_cache[field].get(detail_key)
            if detail is None:
                bits, reason, similarity = self.code_evidence(field, written, truth)
                detail = {"score": bits, "reason": reason, "written": literal,
                          "truth": self.text_values[field][idx], "similarity": similarity}
                group_cache[field][detail_key] = detail
            value += detail["score"]
            if details:
                groups[field] = detail
        cache_key = geometry_key, self.geometry_keys[idx]
        if cache_key not in self.geometry_cache:
            self.geometry_cache[cache_key] = _geometry(row, entry, full_profile=full)
        geometry = self.geometry_cache[cache_key]
        value += geometry["score"]
        if details:
            groups["geometry"] = geometry
        score = _units(value)
        semantic = self.semantic[idx]
        return _Candidate(idx, self.codes["of"][idx], semantic[1],
                          self.codes["modelo"][idx], semantic, score, groups, self.remaining[idx])


def _transition(row: _Row) -> int:
    return _units(CONTINUITY_WRITTEN_BITS if _code(row.source, "of") else CONTINUITY_BLANK_BITS)


@dataclass(frozen=True)
class _Path:
    score: int
    historical_positive: int
    current_positive: int
    semantic: tuple
    states: tuple[tuple[str, str], ...]


def _path_order(path: _Path) -> tuple:
    return -path.score, -path.historical_positive, -path.current_positive, path.semantic


def _decode(rows: list[_Row]) -> None:
    """Exact max-product over (OF, profile), with bounded total continuity.

    An omitted profile shares the 2/.5 bit budget with the OF; it never adds
    another bonus. Group maxima make the transition step linear in states.
    """
    if not rows:
        return
    paths: dict[tuple[str, str], _Path] = {}
    forward: list[dict[tuple[str, str], int]] = []
    for row in rows:
        new = {}
        best_previous = min(paths.values(), key=_path_order) if paths else None
        by_of: dict[str, _Path] = {}
        for state, path in paths.items():
            if state[0] not in by_of or _path_order(path) < _path_order(by_of[state[0]]):
                by_of[state[0]] = path
        budget = _transition(row)
        of_bonus = budget if _written(row.source, "perfil") else budget // 2
        for state, candidate in row.states.items():
            if best_previous is None:
                chosen = _Path(candidate.score, *candidate.remaining_priority, (candidate.semantic,), (state,))
            else:
                options = [(best_previous, 0)]
                if state[0] in by_of:
                    options.append((by_of[state[0]], of_bonus))
                if state in paths:
                    options.append((paths[state], budget))
                previous, bonus = min(options, key=lambda item: (
                    -item[0].score - item[1], -item[0].historical_positive,
                    -item[0].current_positive, item[0].semantic,
                ))
                chosen = _Path(previous.score + bonus + candidate.score,
                               previous.historical_positive + candidate.remaining_priority[0],
                               previous.current_positive + candidate.remaining_priority[1],
                               previous.semantic + (candidate.semantic,), previous.states + (state,))
            new[state] = chosen
        paths = new
        forward.append({state: path.score for state, path in paths.items()})
    final = min(paths.values(), key=_path_order)
    previous_state = None
    for row, state in zip(rows, final.states):
        row.chosen = row.states[state]
        if previous_state is not None and previous_state[0] == state[0]:
            budget = _transition(row)
            if not _written(row.source, "perfil") and previous_state != state:
                budget //= 2
            row.continuity_bits = _bits(budget)
        previous_state = state
    # Max-marginal support is diagnostic only; it never changes the path.
    backward = {state: 0 for state in rows[-1].states}
    for pos in range(len(rows) - 1, -1, -1):
        row = rows[pos]
        row.max_marginals = {state: forward[pos][state] + backward[state] for state in row.states}
        row.support_offset = max(row.max_marginals.values()) - row.chosen.score
        if pos:
            future = {state: candidate.score + backward[state] for state, candidate in row.states.items()}
            base = max(future.values())
            best_by_of: dict[str, int] = {}
            for state, value in future.items():
                best_by_of[state[0]] = max(value, best_by_of.get(state[0], value))
            budget = _transition(row)
            of_bonus = budget if _written(row.source, "perfil") else budget // 2
            backward = {previous: max(
                base,
                best_by_of.get(previous[0], base - of_bonus) + of_bonus,
                future.get(previous, base - budget) + budget,
            ) for previous in rows[pos - 1].states}


def _support(row: _Row, candidate: _Candidate) -> float:
    assert row.chosen is not None
    if candidate.support is not None:
        return candidate.support
    if candidate.state not in row.states or not row.max_marginals:
        return _bits(candidate.score)
    offset = row.support_offset
    candidate.support = _bits(row.max_marginals[candidate.state] - row.states[candidate.state].score + candidate.score - offset)
    return candidate.support


def _confidence(scores: dict[object, float], selected: object, params: CrossParams) -> float:
    if selected not in scores:
        return 0.0
    temperature = max(0.1, float(params.posterior.temperature_bits))
    # Absolute lack of evidence remains visible even with one fallback option.
    h0 = float(params.posterior.b_h0_raw_bits)
    high = max(h0, *scores.values())
    weights = {k: 2 ** max(-1074.0, (score - high) / temperature) for k, score in scores.items()}
    h0_weight = 2 ** max(-1074.0, (h0 - high) / temperature)
    return weights[selected] / (sum(weights.values()) + h0_weight)


def _cell(field: str, written, proposal, status: str, *, confidence: float = 0.0,
          similarity: float = 0.0, auto: bool = False, identity=None, **extra) -> dict:
    inherited = identity.values.get(field) if identity and identity.is_inherited(field) else None
    source = identity.inherited_from.get(field) if identity else None
    return {
        "field": field, "written": _text(written) or None, "proposal": proposal,
        "status": status, "similarity": similarity, "auto_write": auto,
        "p_correct": confidence, "inherited": inherited, "inherited_from": source,
        "plan_limit": None, "expected_total_mm": None, "applied": False,
        "message": "", **extra,
    }


def _empty_check(i: int, source: dict, kind: str) -> dict:
    return {
        "row_index": i, "row_kind": kind, "mode": "no_match" if kind == "production" else kind,
        "matched_plan_key": None, "p_correct": 0.0, "margin_bits": 0.0,
        "review_priority": 0.0, "cells": [], "rivals": [],
        "plan_length_mm": None, "plan_line_meters": None, "quantity_total_mm": None,
        "line_meters": None, "actual_line_meters": None, "binding_status": None,
        "selected_explicitly": False, "candidates_evaluated": 0,
        "confidence_of": 0.0, "confidence_profile": 0.0, "confidence_model": 0.0,
        "confidence_reference": 0.0, "evidence_groups": {}, "alternatives": [],
        "selection_reason": kind, "substituted": False, "low_evidence": kind == "production",
    }


def _render(row: _Row, plan: _Plan, provenance: dict) -> dict:
    winner = row.chosen
    assert winner is not None
    winner.groups = plan.score(row.source, row.identity, winner.idx, details=True).groups
    entry = plan.index.entries[winner.idx]
    source, identity = row.source, row.identity
    full = is_marked(field_value(source, "perf_comp"))
    support_fields = ("of", "profile", "model", "reference", "ov", "cliente")
    supports: dict[str, dict] = {f: {} for f in support_fields}
    rivals_by_reference = {}
    winner_reference = plan.reference_keys[winner.idx]
    for candidate in row.pool:
        reference = plan.reference_keys[candidate.idx]
        keys = (candidate.of, candidate.profile, candidate.model, reference,
                plan.codes["ov"][candidate.idx], plan.codes["cliente"][candidate.idx])
        score = _support(row, candidate)
        for field, key in zip(support_fields, keys):
            values = supports[field]
            if score > values.get(key, -math.inf):
                values[key] = score
        if reference != winner_reference:
            previous = rivals_by_reference.get(reference)
            if previous is None or (-score, *_candidate_order(candidate)) < (-_support(row, previous), *_candidate_order(previous)):
                rivals_by_reference[reference] = candidate
    selected = {"of": winner.of, "profile": winner.profile, "model": winner.model,
                "reference": winner.semantic[:-1], "ov": _entry_code(entry, "ov"),
                "cliente": canonical_code(_entry_value(entry, "cliente"))}
    confidences = {f: _confidence(values, selected[f], plan.params) for f, values in supports.items()}
    score = _support(row, winner)
    distinct_rivals = heapq.nsmallest(5, rivals_by_reference.values(),
                                    key=lambda c: (-_support(row, c), *_candidate_order(c)))
    margin = score - _support(row, distinct_rivals[0]) if distinct_rivals else score
    direct = sum(1 for name, g in winner.groups.items() if g.get("score", 0) > 0
                 and g.get("reason") not in ("inherited", "absent"))
    low = direct < 2 or row.fallback or score <= 0
    local_best = min(row.pool, key=_candidate_order)
    same_score = [c for c in row.pool if c.score == winner.score and c.idx != winner.idx]
    tie = None
    if same_score:
        tie = ("historical_positive_remaining" if winner.remaining_priority[0]
               and any(not c.remaining_priority[0] for c in same_score) else
               "current_positive_remaining" if winner.remaining_priority[1]
               and any(c.remaining_priority[0] == winner.remaining_priority[0] and not c.remaining_priority[1] for c in same_score)
               else "semantic_key")
    reason = ("explicit" if row.forced else "fallback" if row.fallback else
              "sheet_context" if local_best.idx != winner.idx else
              "positive_remaining_tiebreak" if tie in ("historical_positive_remaining", "current_positive_remaining") else
              "stable_tiebreak" if tie else "direct_evidence")
    mode = ("explicit" if row.forced else "fallback" if row.fallback else
            "strong" if margin >= plan.params.score.margin_decisive_bits and not low else "weak_guess")
    cells = []
    confidence_names = {"of": "of", "ov": "ov", "perfil": "profile", "modelo": "model", "cliente": "cliente"}
    for field in IDENTITY_FIELDS:
        written = _written(source, field)
        proposed = _text(_entry_value(entry, field))
        if field in ("of", "ov"):
            proposed = sim.strip_ref_prefix(proposed)
        if full and field == "modelo":
            proposed = ""
        if field in ("of", "ov", "modelo"):
            similarity = winner.groups[field]["similarity"]
            exact = bool(written and _code(source, field) == canonical_code(proposed, field.upper() if field in ("of", "ov") else ""))
        elif field == "perfil":
            similarity = winner.groups["geometry"]["similarity"]
            exact = bool(written and profile_key(written) == profile_key(proposed))
        else:
            similarity = sim.text_similarity(written, proposed)
            exact = bool(written and canonical_code(written) == canonical_code(proposed))
        status = "confirmed" if exact else "snapped" if not written or similarity >= 0.8 else "very_different"
        cells.append(_cell(field, written, proposed, status, confidence=confidences[confidence_names[field]],
                           similarity=similarity, auto=True, identity=identity))
    qty = parse_decimal(source.get("qtd"))
    total = parse_decimal(field_value(source, "qtd_total_mm"))
    length = parse_decimal(_entry_value(entry, "comp_mm"))
    limit = plan.quantities.get((winner.of, winner.model)) if not full else None
    if _text(source.get("qtd")) and not full:
        if qty is None:
            cells.append(_cell("qtd", source.get("qtd"), None, "invalid_numeric"))
        elif limit is not None:
            over = qty > limit
            cells.append(_cell("qtd", source.get("qtd"), None, "over_limit" if over else "confirmed",
                               confidence=confidences["reference"], similarity=0.0 if over else 1.0,
                               plan_limit=_numeric(limit)))
    expected = qty * length if qty is not None and length is not None and not full else None
    if _text(field_value(source, "qtd_total_mm")):
        matches = total is not None and expected is not None and abs(total - expected) <= 1
        status = "na" if full else "confirmed" if matches else "total_mismatch"
        cells.append(_cell("qtd_total_mm", field_value(source, "qtd_total_mm"),
                           decimal_text(expected) if expected is not None else None, status,
                           confidence=confidences["reference"], similarity=1.0 if matches else 0.0,
                           expected_total_mm=_numeric(expected)))
    meters = round(float(expected / 1000), 3) if expected is not None else None
    actual_length = None  # TPL102 comp_mm is a historical checkbox alias.
    actual = (total / 1000 if total is not None else
              qty * actual_length / 1000 if qty is not None and actual_length is not None and not full else None)
    alternatives, seen = [], set()
    for candidate in distinct_rivals:
        if candidate.semantic[:-1] in seen:
            continue
        seen.add(candidate.semantic[:-1])
        other = plan.index.entries[candidate.idx]
        alternatives.append({
            "plan_key": _text(other.get(plan.index.spec.key_field) or candidate.idx),
            "of": sim.strip_ref_prefix(_entry_value(other, "of")), "perfil": _entry_value(other, "perfil"),
            "modelo": _entry_value(other, "modelo"), "comp_mm": _numeric(parse_decimal(_entry_value(other, "comp_mm"))),
            "score": _support(row, candidate),
            "confidence_reference": _confidence(supports["reference"], candidate.semantic[:-1], plan.params),
        })
        if len(alternatives) == 5:
            break
    source_prefix = f"rows[{row.index}]."
    sources = {key[len(source_prefix):]: value for key, value in (provenance.get("field_sources") or {}).items()
               if key.startswith(source_prefix)}
    p = min(confidences["of"], confidences["profile"]) if full else confidences["reference"]
    priority = max((plan.params.policy.criticality.get(c["field"], plan.params.policy.criticality_default)
                    * (1.0 if c["status"] in ("over_limit", "total_mismatch", "invalid_numeric") else 1.0 - c["p_correct"])
                    for c in cells if c["status"] not in ("confirmed", "na")), default=0.0)
    groups = dict(winner.groups)
    groups["continuity"] = {"score": row.continuity_bits, "reason": "sheet_context",
                            "written": None, "truth": winner.of}
    result = {
        "row_index": row.index, "row_kind": "production", "matched_plan_key": _text(entry.get(plan.index.spec.key_field) or winner.idx),
        "p_correct": p, "margin_bits": margin, "mode": mode,
        "review_priority": max(priority, 1.0 - p if low else 0.0), "cells": cells,
        "rivals": [alternative["plan_key"] for alternative in alternatives],
        "plan_length_mm": _numeric(length) if not full else None, "plan_line_meters": meters,
        "quantity_total_mm": _numeric(total), "line_meters": meters,
        "actual_line_meters": round(float(actual), 3) if actual is not None else None,
        "binding_status": row.binding_status, "selected_explicitly": row.forced,
        "candidates_evaluated": len(row.pool), "confidence_of": confidences["of"],
        "confidence_profile": confidences["profile"], "confidence_model": confidences["model"],
        "confidence_reference": confidences["reference"], "evidence_groups": groups,
        "selection_reason": reason, "score": score, "alternatives": alternatives,
        "evidence_source": {"source": provenance.get("source", "extracted"), "fields": sources},
        "low_evidence": low, "substituted": True, "tie_breaker": tie,
        "full_profile": full,
    }
    if full:
        from .full_profile import expand_group
        result.update(expand_group(plan, winner))
    if plan.history is not None and hasattr(plan.history, "evidence_for"):
        result["historical_context"] = plan.history.evidence_for(entry)
    return result


def check_sheet_v3(sheet_data: dict, params: CrossParams | None = None, *, index: PlanIndex,
                   historical_context=None, explicit_bindings: dict[int, dict] | None = None,
                   provenance: dict | None = None, include_candidates: bool = False) -> dict:
    """Return the existing cross contract plus immutable-evidence diagnostics.

    ``sheet_data`` is evidence, not the current automatically materialized
    sheet. Historical context may break exact score ties via remaining_for;
    it never adds candidates or contributes to the evidence score.
    """
    params, provenance = params or CrossParams(), provenance or {}
    explicit_bindings = explicit_bindings or {}
    source_rows = sheet_data.get("rows") or []
    plan = _Plan(index, params, historical_context)
    checks: dict[int, dict] = {}
    segments: list[list[int]] = []
    current: list[int] = []
    previous_internal = False
    for i, source in enumerate(source_rows):
        kind = classify_row(source)
        if kind == "deleted":
            continue
        internal = _internal(source)
        if kind != "production" or internal or previous_internal:
            if current:
                segments.append(current)
                current = []
        if kind == "production":
            current.append(i)
        else:
            checks[i] = _empty_check(i, source, kind)
        previous_internal = internal
    if current:
        segments.append(current)
    key_to_idx = plan.key_to_idx
    for indices in segments:
        subset = [source_rows[i] for i in indices]
        inherited = carryover.resolve(subset, ("modelo", "qtd", "qtd_total_mm"))
        # Look in both directions before decoding. A following explicit OF,
        # OV or model may identify the block whose first code was damaged.
        # This expands candidates only; it adds no independent evidence.
        anchor_orders: set[str] = set()
        for source in subset:
            for field in ("of", "ov", "modelo"):
                value = _code(source, field)
                if field == "modelo" and is_marked(field_value(source, "perf_comp")):
                    continue
                for hit in plan.maps[field].get(value, ()):
                    anchor_orders.add(plan.codes["of"][hit])
        segment_candidates: set[int] = set()
        for of in anchor_orders:
            segment_candidates.update(plan.maps["of"].get(of, ()))
        resolved: list[_Row] = []
        for local, i in enumerate(indices):
            source = source_rows[i]
            if not index.entries:
                checks[i] = _empty_check(i, source, "production")
                continue
            identity = carryover.RowIdentity(
                inherited[local].values, {field: indices[pos] for field, pos in inherited[local].inherited_from.items()},
            )
            ids, fallback = plan.candidates(source, identity)
            if fallback and segment_candidates:
                ids = set(segment_candidates)
            else:
                ids.update(segment_candidates)
            binding = explicit_bindings.get(i) or explicit_bindings.get(str(i)) or source.get("_plan_binding") or {}
            full = is_marked(field_value(source, "perf_comp"))
            requested = isinstance(binding, dict) and binding.get("selected_explicitly") and not full
            key = _text(binding.get("plan_key")) if requested else ""
            valid = bool(requested and key in key_to_idx and binding.get("snapshot_id")
                         and _text(binding.get("snapshot_id")) == _text(index.snapshot_id))
            binding_status = "current" if valid else "reselected" if requested else None
            if valid:
                ids.add(key_to_idx[key])
            pool = [plan.score(source, identity, idx) for idx in sorted(ids)]
            states = {}
            for candidate in sorted(pool, key=_candidate_order):
                if valid and candidate.idx != key_to_idx[key]:
                    continue
                states.setdefault(candidate.state, candidate)
            resolved.append(_Row(i, source, identity, pool, states, fallback, valid, binding_status))
        if resolved:
            _decode(resolved)
            for row in resolved:
                checks[row.index] = _render(row, plan, provenance)
                if include_candidates:
                    checks[row.index]["candidate_plan_keys"] = [
                        _text(index.entries[candidate.idx].get(index.spec.key_field) or candidate.idx)
                        for candidate in row.pool
                    ]
    rows = [checks[i] for i in sorted(checks)]
    production = [row for row in rows if row["row_kind"] == "production"]
    cells = [cell for row in production for cell in row["cells"]]
    summary = {
        "rows": len(production), "matched": sum(bool(r["matched_plan_key"]) for r in production),
        "activity_rows": sum(r["row_kind"] == "activity" for r in rows),
        "empty_rows": sum(r["row_kind"] == "empty" for r in rows),
        **{mode: sum(r["mode"] == mode for r in production) for mode in ("strong", "weak_guess", "no_match", "fallback", "explicit")},
        **{"cells_" + name: sum(c["status"] == name for c in cells) for name in ("confirmed", "snapped", "very_different", "over_limit", "total_mismatch")},
        "cells_inherited": sum(c.get("inherited_from") is not None for c in cells),
    }
    known_meters = [r["plan_line_meters"] for r in production if r["plan_line_meters"] is not None]
    total_meters = round(sum(known_meters), 2) if known_meters else None
    measured = _numeric(parse_decimal((sheet_data.get("footer") or {}).get("metros_produzidos")))
    partial = any(r["plan_line_meters"] is None for r in production)
    summary.update({"metros_teoricos": total_meters, "metros_produzidos": measured, "metros_parciais": partial,
                    "desperdicio_m": round(measured - total_meters, 2) if measured is not None and total_meters is not None and not partial else None})
    result = {
        "engine": VERSION, "version": VERSION, "snapshot_id": index.snapshot_id,
        "summary": summary, "rows": rows,
        "review_order": [r["row_index"] for r in sorted(production, key=lambda r: (-r["review_priority"], r["row_index"])) if r["review_priority"] > 0],
        "confidence_method": "selected-value max-support; informational, not an acceptance threshold",
    }
    effective_params = {
        "version": VERSION + "-cantoneiras-2", "code_weights": CODE_WEIGHTS,
        "profile_bits": PROFILE_BITS, "length_bits": LENGTH_BITS,
        "material_match_bits": MATERIAL_MATCH_BITS, "material_conflict_bits": MATERIAL_CONFLICT_BITS,
        "split_cost_bits": SPLIT_COST_BITS,
        "continuity_blank_bits": CONTINUITY_BLANK_BITS,
        "continuity_written_bits": CONTINUITY_WRITTEN_BITS,
        "rarity_fields": ["ov"],
        "rarity_distinct_orders_min_fraction": 0.7,
        "rarity_distinct_orders_log2_slope": 0.1,
        "fuzzy_values": FUZZY_VALUES, "score_precision": _PRECISION,
        "channel": asdict(params.channel),
        "temperature_bits": params.posterior.temperature_bits,
        "h0_bits": params.posterior.b_h0_raw_bits,
        "margin_decisive_bits": params.score.margin_decisive_bits,
        "criticality": params.policy.criticality,
        "criticality_default": params.policy.criticality_default,
    }
    result["params_version"] = effective_params["version"]
    result["params_hash"] = hashlib.sha256(json.dumps(
        effective_params, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()
    if historical_context is not None and hasattr(historical_context, "to_dict"):
        result["historical_context"] = historical_context.to_dict()
    from .full_profile import attach_plan_facts
    attach_plan_facts(result, index, source_rows, precision=2)
    return _json(result)
