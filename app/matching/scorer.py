"""O scorer Fellegi–Sunter em bits.

Para cada linha manuscrita, pergunta: "de que linha do plano é que o operador
estava a falar?" — e responde com o argmax da evidência somada em bits, uma
margem honesta face ao melhor rival, e uma probabilidade calibrada que inclui
a hipótese H₀ de "não está no plano".
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import similarity as sim
from .channel import CharChannel
from .params import CrossParams
from .refs import FieldSpec, PlanIndex


@dataclass
class FieldEvidence:
    name: str
    written: str | None
    truth: str | None
    similarity: float
    bits: float
    reason: str          # exact | near | channel | disagree | veto | empty


@dataclass
class EntryScore:
    idx: int
    plan_key: str
    bits: float
    agree_count: int
    fields: list[FieldEvidence] = field(default_factory=list)
    dim_bits: float = 0.0
    dim_agree: tuple[str, ...] = ()
    dim_disagree: tuple[str, ...] = ()
    context_bits: float = 0.0


@dataclass
class RowMatch:
    winner: EntryScore | None
    p_correct: float               # P(é esta a linha exacta do plano)
    margin_bits: float
    mode: str                      # strong | weak_guess | no_match
    rivals: list[EntryScore] = field(default_factory=list)
    candidates_evaluated: int = 0
    # P(o valor do campo de identidade primário é o do vencedor). Soma as linhas
    # irmãs em vez de as opor — é esta a confiança que interessa na revisão.
    p_primary: float = 0.0
    # campo -> (valor mais provável, probabilidade)
    marginals: dict[str, tuple[str, float]] = field(default_factory=dict)


class Scorer:
    def __init__(self, index: PlanIndex, params: CrossParams | None = None,
                 active_primary: set[str] | None = None):
        self.index = index
        self.params = params or CrossParams()
        self.channel = CharChannel(self.params.channel)
        # valores do campo de identidade primário com atividade recente (contexto)
        self.active_primary = active_primary or set()
        self._primary = index.spec.identity_fields[0].name

    # ---- peso de um campo: log2(m/u), com u por VALOR ----

    def value_weight(self, f: FieldSpec, value: str) -> float:
        p = self.params.score
        n = max(self.index.n, p.u_min_corpus)
        u = max(self.index.value_frequency(f.name, value), 1) / n
        m = p.m_by_field.get(f.name, p.m_default)
        w = math.log2(m / u)
        return max(p.w_min_bits, min(p.w_cap_bits, w))

    # ---- evidência de um campo de identidade ----

    def _identity_evidence(self, f: FieldSpec, written_raw: object, idx: int) -> FieldEvidence:
        p = self.params.score
        # Normalizar pelo índice: é ele que sabe as convenções do plano (prefixo
        # das OF, forma canónica dos perfis).
        written = self.index.normalize_written(
            f.name, written_raw if written_raw is None else str(written_raw)
        )
        truth = self.index.normalized(f.name, idx)
        if not written or not truth:
            return FieldEvidence(f.name, written or None, truth or None, 0.0, 0.0, "empty")

        if f.kind == "code":
            # O escrito pode vir sem o prefixo que o plano usa; qualquer variante
            # que bata certo é uma concordância exacta, não uma parecença.
            similarity = (
                1.0 if truth in self.index.variants_for(f.name, written_raw)
                else sim.code_similarity(written, truth)
            )
        elif f.kind == "profile":
            similarity = 1.0 if written == truth else sim.code_similarity(written, truth)
        else:
            similarity = sim.text_similarity(written_raw and str(written_raw), truth)
        w = self.value_weight(f, truth)

        if similarity >= p.sim_full:
            return FieldEvidence(f.name, written, truth, similarity, w, "exact")
        if similarity >= p.sim_near:
            return FieldEvidence(f.name, written, truth, similarity, p.near_fraction * w, "near")

        g = self.channel.evidence(written, truth)
        if similarity >= p.channel_floor_sim:
            g = max(g, p.channel_g_floor)
        if g > 0.0:
            return FieldEvidence(f.name, written, truth, similarity, g * w, "channel")

        # discordância franca
        if f.kind == "code" and self.index.value_frequency(f.name, written) > 0:
            # o operador escreveu um código que EXISTE no plano e não é este:
            # contradizer evidência escrita válida custa mais
            return FieldEvidence(f.name, written, truth, similarity, p.veto_valid_code_bits, "veto")
        bits = p.disagree_code_bits if f.kind == "code" else p.disagree_text_bits
        return FieldEvidence(f.name, written, truth, similarity, bits, "disagree")

    # ---- dimensões: pontuadas em conjunto ----

    def _dims_evidence(self, row: dict, idx: int,
                       memo: dict | None = None) -> tuple[float, tuple[str, ...], tuple[str, ...]]:
        p = self.params.score
        agree: list[str] = []
        disagree: list[str] = []

        for f in self.index.spec.numeric_fields:
            written = sim.parse_number(row.get(f.name))
            if written is None:
                continue
            truth = sim.parse_number(self.index.entries[idx].get(f.entry_key))
            if truth is None:
                continue
            s = sim.numeric_similarity(written, truth, f.tolerance)
            if s >= 1.0:
                agree.append(f.name)
            elif s <= 0.0:
                disagree.append(f.name)
            # zona intermédia: nem prova nem contradiz

        bits = 0.0
        if agree:
            # |interseção| depende só (linha, subconjunto de dims que concordam):
            # memoizado por match_row — os candidatos partilham o cálculo
            agree_key = tuple(agree)
            joint_count = None if memo is None else memo.get(agree_key)
            if joint_count is None:
                compatible: frozenset[int] | None = None
                for name in agree:
                    f = next(x for x in self.index.spec.numeric_fields if x.name == name)
                    written = sim.parse_number(row.get(name))
                    within = self.index.entries_within_tolerance(name, written, f.tolerance)
                    compatible = within if compatible is None else (compatible & within)
                joint_count = len(compatible or ())
                if memo is not None:
                    memo[agree_key] = joint_count
            floor_product = p.dim_u_floor ** len(agree)
            u_joint = max(joint_count / max(self.index.n, 1), floor_product, 1e-9)
            bits += min(-math.log2(u_joint), p.dim_joint_cap_bits)
        if disagree:
            bits += max(p.dim_disagree_cap_bits, len(disagree) * p.dim_disagree_bits)
        return bits, tuple(agree), tuple(disagree)

    # ---- pontuar uma entrada ----

    def score_entry(self, row: dict, idx: int, _dim_memo: dict | None = None,
                    _id_memo: dict | None = None) -> EntryScore:
        p = self.params.score
        entry = self.index.entries[idx]
        # evidência de identidade depende só (campo, valor da entrada): os irmãos
        # da mesma OF partilham-na — memoizada por match_row
        fields: list[FieldEvidence] = []
        for f in self.index.spec.identity_fields:
            key = (f.name, self.index.normalized(f.name, idx))
            fe = None if _id_memo is None else _id_memo.get(key)
            if fe is None:
                fe = self._identity_evidence(f, row.get(f.name), idx)
                if _id_memo is not None:
                    _id_memo[key] = fe
            fields.append(fe)
        dim_bits, dim_agree, dim_disagree = self._dims_evidence(row, idx, _dim_memo)

        context = 0.0
        if self.active_primary:
            primary_value = self.index.normalized(self._primary, idx)
            if primary_value in self.active_primary:
                context = min(p.active_of_bonus_bits, p.context_cap_bits)

        total = sum(fe.bits for fe in fields) + dim_bits + context
        agree_count = sum(1 for fe in fields if fe.similarity >= 0.55 and fe.written)
        return EntryScore(
            idx=idx,
            plan_key=str(entry.get(self.index.spec.key_field, idx)),
            bits=total,
            agree_count=agree_count,
            fields=fields,
            dim_bits=dim_bits,
            dim_agree=dim_agree,
            dim_disagree=dim_disagree,
            context_bits=context,
        )

    # ---- candidatos ----

    # Teto por omissão: valores com mais entradas do que isto não geram
    # candidatos sozinhos (continuam a contar como evidência na pontuação).
    # Campos que identificam mesmo — a OF — levantam-no no seu FieldSpec: uma
    # OF de 600 linhas continua a ser uma OF, e com o teto ficava invisível.
    MAX_VALUE_ENTRIES = 500

    def candidates(self, row: dict, top_k: int = 10) -> list[int]:
        out: set[int] = set()
        fuzzy_pending: list = []
        for f in self.index.spec.identity_fields:
            written = row.get(f.name)
            if written is None or str(written).strip() == "":
                continue
            exact = self.index.exact_matches(f.name, str(written),
                                             max_entries=f.max_candidate_entries)
            out.update(exact)
            if not exact:
                fuzzy_pending.append((f, str(written)))
        # fuzzy só quando preciso, do campo mais seletivo para o menos, com
        # paragem antecipada — evita varrer dezenas de milhares de referências
        # quando já há candidatos suficientes de campos mais fortes
        fuzzy_pending.sort(key=lambda fw: len(self.index._keys.get(fw[0].name, ())))
        for f, written in fuzzy_pending:
            if len(out) >= 300:
                break
            out.update(self.index.fuzzy_candidates(f.name, written, top_k=top_k,
                                                   max_entries=f.max_candidate_entries))
        if not out and self.index.spec.numeric_fields:
            # sem pistas de identidade: restringir por dimensões
            compatible: set[int] | None = None
            for f in self.index.spec.numeric_fields:
                written = sim.parse_number(row.get(f.name))
                if written is None:
                    continue
                within = self.index.entries_within_tolerance(f.name, written, f.tolerance)
                compatible = within if compatible is None else (compatible & within)
            if compatible:
                out = compatible if len(compatible) <= 500 else set(list(compatible)[:500])
        return sorted(out)

    # ---- decisão + posterior ----

    def match_row(self, row: dict, top_k: int = 10) -> RowMatch:
        dim_memo: dict = {}
        id_memo: dict = {}
        pool = [
            self.score_entry(row, idx, dim_memo, id_memo)
            for idx in self.candidates(row, top_k=top_k)
        ]
        if not pool:
            return RowMatch(winner=None, p_correct=0.0, margin_bits=0.0,
                            mode="no_match", candidates_evaluated=0)

        pool.sort(key=lambda s: (-s.bits, -s.agree_count, s.plan_key))
        winner = pool[0]

        # margem face ao melhor rival com identidade primária DIFERENTE
        # (irmãos da mesma OF não são rivais de identidade)
        winner_primary = self.index.normalized(self._primary, winner.idx)
        margin = math.inf
        for s in pool[1:]:
            if self.index.normalized(self._primary, s.idx) != winner_primary:
                margin = winner.bits - s.bits
                break

        p_correct = self._posterior(pool, winner)
        marginals = self._marginals(pool)
        pp = self.params.score
        mode = "strong" if (margin >= pp.margin_decisive_bits and winner.bits > 0) else "weak_guess"
        rivals = [s for s in pool[1:6] if winner.bits - s.bits <= 2.0]
        return RowMatch(
            winner=winner,
            p_correct=p_correct,
            p_primary=marginals.get(self._primary, (None, 0.0))[1],
            marginals=marginals,
            margin_bits=margin if margin != math.inf else winner.bits,
            mode=mode,
            rivals=rivals,
            candidates_evaluated=len(pool),
        )

    def _marginals(self, pool: list[EntryScore]) -> dict[str, tuple[str, float]]:
        """Probabilidade do VALOR de cada campo, somada sobre as linhas do pool.

        `p_correct` responde a «é esta a linha exacta do plano?» — e numa OF com
        300 linhas irmãs essa pergunta não tem resposta possível: a massa
        divide-se por todas e nenhuma passa de 0,3. A pergunta útil na revisão é
        «de que OF é esta linha?», e essa soma as irmãs em vez de as opor.
        """
        if not pool:
            return {}
        p = self.params.posterior
        t = max(p.temperature_bits, 1e-6)
        b_max = max(s.bits for s in pool)
        weights = [math.pow(2.0, (s.bits - b_max) / t) for s in pool]

        pi = min(p.pi_h0_max, p.pi_h0_base + p.pi_h0_per_day * max(self.index.plan_age_days, 0.0))
        pi = max(pi, 1e-4)
        b_h0 = p.b_h0_raw_bits + math.log2(max(self.index.n, 2))
        w_h0 = (pi / (1.0 - pi)) * math.pow(2.0, (b_h0 - b_max) / t)
        total = sum(weights) + w_h0
        if total <= 0:
            return {}

        out: dict[str, tuple[str, float]] = {}
        for f in self.index.spec.identity_fields:
            by_value: dict[str, float] = {}
            for s, w in zip(pool, weights):
                value = self.index.normalized(f.name, s.idx)
                if value:
                    by_value[value] = by_value.get(value, 0.0) + w
            if by_value:
                best = max(by_value.items(), key=lambda kv: kv[1])
                out[f.name] = (best[0], best[1] / total)
        return out

    def _posterior(self, pool: list[EntryScore], winner: EntryScore) -> float:
        """Softmax sobre o pool avaliado + H₀ explícito.

        H₀ = "a linha não está no plano". O seu prior π cresce com a idade do
        plano; a sua evidência base b_H0 inclui log2(N) para o limiar não
        derivar silenciosamente quando o plano cresce.
        """
        p = self.params.posterior
        t = max(p.temperature_bits, 1e-6)
        b_max = winner.bits

        z_entries = sum(math.pow(2.0, (s.bits - b_max) / t) for s in pool)

        pi = min(p.pi_h0_max, p.pi_h0_base + p.pi_h0_per_day * max(self.index.plan_age_days, 0.0))
        pi = max(pi, 1e-4)
        odds_h0 = pi / (1.0 - pi)
        # log2(N) sem temperatura: compensa o facto de os pesos u-por-valor
        # crescerem com o tamanho do plano; b_h0_raw é a exigência adicional.
        b_h0 = p.b_h0_raw_bits + math.log2(max(self.index.n, 2))
        w_h0 = odds_h0 * math.pow(2.0, (b_h0 - b_max) / t)

        w_winner = 1.0  # 2^((b_max - b_max)/t)
        return w_winner / (z_entries + w_h0)
