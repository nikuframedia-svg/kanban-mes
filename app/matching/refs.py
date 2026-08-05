"""Índices de referência do plano de produção.

Uma PlanIndex é construída a partir de uma lista de entradas (dicts) e prepara:
- frequência de cada VALOR por campo de identidade → u por valor (raridade);
- lookup exato valor→entradas (com variantes O↔0 nos códigos);
- arrays ordenados por dimensão numérica → interseção de conjuntos compatíveis
  (as dimensões pontuam em conjunto, não à peça).
"""

from __future__ import annotations

import bisect
from collections import Counter, defaultdict
from dataclasses import dataclass

from . import similarity as sim


@dataclass(frozen=True)
class FieldSpec:
    """Liga um campo da folha kanban a um campo da entrada do plano."""

    name: str                 # nome na folha (ex.: "of", "cliente", "esp")
    kind: str                 # "code" | "text" | "numeric"
    entry_key: str            # chave no dict da entrada do plano
    tolerance: float = 0.0    # só para numeric


@dataclass
class IndexSpec:
    identity_fields: tuple[FieldSpec, ...]
    numeric_fields: tuple[FieldSpec, ...] = ()
    key_field: str = "plan_key"   # identificador único da entrada do plano


class PlanIndex:
    def __init__(self, entries: list[dict], spec: IndexSpec, plan_age_days: float = 0.0):
        self.spec = spec
        self.entries = entries
        self.n = len(entries)
        self.plan_age_days = plan_age_days

        # normalização por entrada + estruturas de lookup
        self._norm: dict[str, list[str]] = {}          # field -> [valor normalizado por entrada]
        self._freq: dict[str, Counter] = {}            # field -> Counter(valor)
        self._lookup: dict[str, dict[str, list[int]]] = {}  # field -> valor -> [idx]
        self._keys: dict[str, list[str]] = {}          # field -> valores distintos (fuzzy)

        for f in spec.identity_fields:
            norm_fn = sim.normalize_code if f.kind == "code" else sim.compact
            values = [norm_fn(e.get(f.entry_key)) for e in entries]
            self._norm[f.name] = values
            self._freq[f.name] = Counter(v for v in values if v)
            lookup: dict[str, list[int]] = defaultdict(list)
            for idx, v in enumerate(values):
                if v:
                    lookup[v].append(idx)
            self._lookup[f.name] = dict(lookup)
            self._keys[f.name] = list(self._freq[f.name].keys())

        # dimensões: (valor, idx) ordenado para janelas por bisect
        self._dims: dict[str, list[tuple[float, int]]] = {}
        for f in spec.numeric_fields:
            pairs = []
            for idx, e in enumerate(entries):
                v = sim.parse_number(e.get(f.entry_key))
                if v is not None:
                    pairs.append((v, idx))
            pairs.sort()
            self._dims[f.name] = pairs

        # caches de desempenho (o plano é imutável durante a vida do índice)
        self._tol_cache: dict[tuple[str, float, float], frozenset[int]] = {}
        self._bags: dict[str, dict[str, Counter]] = {}

    # ---- u por valor (a ideia central) ----

    def value_frequency(self, field_name: str, value: str) -> int:
        return self._freq.get(field_name, Counter()).get(value, 0)

    # ---- lookups ----

    def exact_matches(self, field_name: str, written: str,
                      max_entries: int | None = None) -> list[int]:
        """Entradas cujo valor bate certo com o escrito ou com uma variante O↔0.
        `max_entries`: ignora valores demasiado comuns para gerar candidatos —
        um cliente com 10 mil linhas não identifica nada sozinho (continua a
        contar como evidência ao pontuar candidatos vindos de outros campos)."""
        lookup = self._lookup.get(field_name, {})
        out: list[int] = []
        for variant in sim.zero_o_variants(written):
            hits = lookup.get(variant, ())
            if max_entries is not None and len(hits) > max_entries:
                continue
            out.extend(hits)
        return out

    def fuzzy_candidates(self, field_name: str, written: str, top_k: int = 10,
                         max_entries: int | None = None) -> list[int]:
        """Top-K valores distintos mais parecidos com o escrito → entradas.
        Pré-filtro barato (comprimento + saco de caracteres, minorante da distância
        de edição) antes do Levenshtein completo, para escalar a planos grandes."""
        written_n = sim.compact(written)
        if not written_n:
            return []
        w_len = len(written_n)
        w_bag = Counter(written_n)
        if field_name not in self._bags:
            self._bags[field_name] = {v: Counter(v) for v in self._keys.get(field_name, ())}
        bags = self._bags[field_name]
        scored = []
        for value in self._keys.get(field_name, ()):
            if abs(len(value) - w_len) > 3:
                continue
            max_len = max(len(value), w_len)
            v_bag = bags[value]
            bag_diff = sum((w_bag - v_bag).values()) + sum((v_bag - w_bag).values())
            if bag_diff / 2 > 0.4 * max_len:
                continue
            r = sim.ratio(written_n, value)
            if r >= 0.6:
                scored.append((r, value))
        scored.sort(reverse=True)
        out: list[int] = []
        for _, value in scored[:top_k]:
            hits = self._lookup[field_name][value]
            if max_entries is not None and len(hits) > max_entries:
                continue
            out.extend(hits)
        return out

    def entries_within_tolerance(self, field_name: str, written: float, tolerance: float) -> frozenset[int]:
        """Conjunto de entradas cuja dimensão está dentro da tolerância do escrito.
        Cacheado: depende só do valor escrito, não do candidato."""
        key = (field_name, float(written), float(tolerance))
        cached = self._tol_cache.get(key)
        if cached is not None:
            return cached
        pairs = self._dims.get(field_name, [])
        lo = bisect.bisect_left(pairs, (written - tolerance, -1))
        hi = bisect.bisect_right(pairs, (written + tolerance, self.n + 1))
        out = frozenset(idx for _, idx in pairs[lo:hi])
        if len(self._tol_cache) > 4096:
            self._tol_cache.clear()
        self._tol_cache[key] = out
        return out

    def normalized(self, field_name: str, idx: int) -> str:
        return self._norm.get(field_name, [""] * self.n)[idx] if idx < self.n else ""
