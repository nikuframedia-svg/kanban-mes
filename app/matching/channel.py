"""Canal de confusão de caracteres.

Pergunta que responde: "quão plausível é que o operador/OCR tenha escrito W
quando a verdade era T?" — alinhamento de programação dinâmica (Needleman–Wunsch)
onde cada substituição tem um custo em bits. Pares visualmente confundíveis
(0↔O, 5↔S, 8↔B…) custam pouco; substituições arbitrárias custam muito.

Devolve g ∈ [0,1]: evidência de "leitura plausível", usada pelo scorer como
fração do peso do campo quando não há acordo exato.
"""

from __future__ import annotations

from functools import lru_cache

from .params import ChannelParams, GLYPH_CONFUSABLE


class CharChannel:
    def __init__(self, params: ChannelParams | None = None):
        self.p = params or ChannelParams()

    def sub_cost(self, a: str, b: str) -> float:
        if a == b:
            return 0.0
        if self.p.case_free and a.upper() == b.upper():
            return 0.0
        a_u, b_u = a.upper(), b.upper()
        learned = self.p.learned_subs.get(f"{a_u}>{b_u}") or self.p.learned_subs.get(f"{b_u}>{a_u}")
        if learned is not None:
            return learned
        if tuple(sorted((a_u, b_u))) in GLYPH_CONFUSABLE:
            return self.p.glyph_pair_bits
        return self.p.sub_default_bits

    def align_cost_bits(self, written: str, truth: str) -> float:
        """Custo mínimo em bits para transformar `truth` no que foi escrito."""
        return self._align_cached(written.upper(), truth.upper())

    @lru_cache(maxsize=32768)
    def _align_cached(self, w: str, t: str) -> float:
        if w == t:
            return 0.0
        indel = self.p.indel_bits
        prev = [j * indel for j in range(len(t) + 1)]
        for i, cw in enumerate(w, 1):
            cur = [i * indel]
            for j, ct in enumerate(t, 1):
                cur.append(
                    min(
                        prev[j] + indel,
                        cur[j - 1] + indel,
                        prev[j - 1] + self.sub_cost(cw, ct),
                    )
                )
            prev = cur
        return prev[-1]

    def evidence(self, written: str | None, truth: str | None) -> float:
        """g ∈ [0, g_cap]: 1 − custo/g_l0, truncado. 0 quando não há nada escrito."""
        if not written or not truth:
            return 0.0
        cost = self.align_cost_bits(str(written), str(truth))
        g = 1.0 - cost / self.p.g_l0_bits
        return max(0.0, min(self.p.g_cap, g))
