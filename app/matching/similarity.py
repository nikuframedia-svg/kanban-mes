"""Normalização e semelhança por campo.

Três tipos de campo:
- código (OF, OV, lote, nesting): comparação compacta alfanumérica com variantes 0↔O;
- texto (cliente, modelo): tokens normalizados sem sufixos societários;
- numérico (dimensões): dentro de tolerância = 1.0, decaimento linear fora.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

_NON_ALNUM = re.compile(r"[^A-Z0-9]+")
_WS = re.compile(r"\s+")

# Sufixos/palavras sem poder identificativo em nomes de clientes.
_CLIENT_STOPWORDS = frozenset(
    "SA LDA SL SAS GMBH AG BV SRL SPA LTD LTDA INC CO SOCIEDADE UNIPESSOAL E DE DO DA".split()
)


def compact(value: str | None) -> str:
    """Maiúsculas, só alfanumérico ASCII. 'of 250002' → 'OF250002'.

    Acentos decompõem-se primeiro (NFKD) para a letra base sobreviver:
    'CONCEIÇÃO' → 'CONCEICAO', não 'CONCEIO' — senão um nome corretamente
    lido pelo OCR ficava a 2 de distância da lista de colaboradores.
    """
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    return _NON_ALNUM.sub("", text.upper())


_YEAR_PREFIX = re.compile(r"^\s*\d{2}_")


def normalize_code(value: str | None) -> str:
    """O plano prefixa nestings com o ano ('26_S07.CH5_1'); os operadores nunca
    o escrevem ('S07.CH5_1'). O prefixo é formatação, não identidade — cai dos
    dois lados antes do lookup. Medido nos kanbans reais: cobertura exata sobe
    de 24,7% para 56,7%. OF/OV/referências não têm o padrão '\\d\\d_'."""
    if value is None:
        return ""
    return compact(_YEAR_PREFIX.sub("", str(value)))


def zero_o_variants(code: str) -> set[str]:
    """Variantes do código trocando O↔0 (a confusão mais comum de todas).
    Limitado a códigos curtos para não explodir combinatoriamente."""
    code = normalize_code(code)
    if not code or len(code) > 12:
        return {code} if code else set()
    variants = {code}
    for i, ch in enumerate(code):
        swap = {"O": "0", "0": "O"}.get(ch)
        if swap:
            variants |= {v[:i] + swap + v[i + 1 :] for v in list(variants)}
        if len(variants) > 64:
            break
    return variants


def code_variants(value: str | None, prefix: str = "") -> set[str]:
    """Formas sob as quais um código escrito à mão pode aparecer no plano.

    O plano guarda `OF263323`/`OV2504650` (todas as 64 mil linhas com prefixo)
    e o operador escreve `263323` — a folha já diz «OF» no cabeçalho da coluna,
    ninguém repete o prefixo. A variante prefixada é **adicional**: nem tudo o
    que está na coluna OV começa por OV, portanto a forma sem prefixo continua
    a valer.
    """
    base = normalize_code(value)
    if not base:
        return set()
    out = zero_o_variants(base)
    if prefix:
        pfx = compact(prefix)
        if pfx and not base.startswith(pfx):
            out = out | {pfx + v for v in out}
    return out


# Perfis de cantoneira: o plano escreve `L60X60X5`, o operador escreve `60x5`,
# `60 x 5` ou `L50x6`. Duas medidas querem dizer abas iguais (`60x5` = 60×60×5).
_PROFILE_PARTS = re.compile(r"^([A-Z]*)([0-9X]+)$")


def normalize_profile(value: str | None, known: object = None) -> str:
    """Forma canónica de um perfil, no formato do plano (`L a X b X c`).

    `known`, se vier, é um conjunto de perfis válidos: a expansão de duas para
    três medidas só se faz se o resultado existir mesmo no plano. Sem essa
    guarda estaríamos a adivinhar — a maioria das cantoneiras tem abas iguais,
    mas não todas.
    """
    base = compact(value)
    if not base:
        return ""
    m = _PROFILE_PARTS.match(base)
    if not m:
        return base
    letters, digits = m.group(1), m.group(2)
    parts = [p for p in digits.split("X") if p]
    if len(parts) == 3:
        cand = f"{letters or 'L'}{parts[0]}X{parts[1]}X{parts[2]}"
    elif len(parts) == 2:
        cand = f"{letters or 'L'}{parts[0]}X{parts[0]}X{parts[1]}"
    else:
        return base
    if known is not None and cand not in known:
        return base
    return cand


def client_tokens(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    tokens = [compact(t) for t in _WS.split(str(value).upper())]
    return tuple(t for t in tokens if t and t not in _CLIENT_STOPWORDS)


@lru_cache(maxsize=65536)
def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def ratio(a: str, b: str) -> float:
    """Semelhança em [0,1] baseada em distância de edição."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return 1.0 - levenshtein(a, b) / max(len(a), len(b))


def code_similarity(written: str | None, truth: str | None) -> float:
    """1.0 se alguma variante O↔0 bate certo; senão ratio de edição compacta.
    Normaliza o prefixo de ano dos dois lados (ver normalize_code)."""
    w, t = normalize_code(written), normalize_code(truth)
    if not w or not t:
        return 0.0
    if w == t or t in zero_o_variants(w):
        return 1.0
    return ratio(w, t)


def text_similarity(written: str | None, truth: str | None) -> float:
    """Clientes/modelos: igualdade compacta, contenção de tokens, ou fuzzy."""
    w_comp, t_comp = compact(written), compact(truth)
    if not w_comp or not t_comp:
        return 0.0
    if w_comp == t_comp:
        return 1.0
    # o escrito está contido na designação completa (operador abrevia)
    if len(w_comp) >= 4 and (w_comp in t_comp or t_comp in w_comp):
        return 0.97
    w_tokens, t_tokens = client_tokens(written), client_tokens(truth)
    if w_tokens and t_tokens:
        if set(w_tokens) & set(t_tokens):
            common = len(set(w_tokens) & set(t_tokens))
            return min(0.97, 0.6 + 0.2 * common)
        best = max(
            (ratio(a, b) for a in w_tokens for b in t_tokens[:8]),
            default=0.0,
        )
        return max(best, ratio(w_comp, t_comp))
    return ratio(w_comp, t_comp)


def numeric_similarity(written: float | None, truth: float | None, tolerance: float) -> float:
    """1.0 dentro da tolerância; decaimento linear até 0 a 4× tolerância."""
    if written is None or truth is None:
        return 0.0
    diff = abs(float(written) - float(truth))
    if diff <= tolerance:
        return 1.0
    span = max(tolerance * 3.0, 1e-9)
    return max(0.0, 1.0 - (diff - tolerance) / span)


# «1.200» é milhar; «0.125» não (milhares não começam por zero)
_THOUSANDS_DOT = re.compile(r"^-?[1-9]\d{0,2}(\.\d{3})+$")

_REF_PREFIX = re.compile(r"^\s*(OF|OV)\s*(\d+)\s*$", re.IGNORECASE)


def strip_ref_prefix(value: object) -> str:
    """«OF263323» → «263323». No planeamento e na Metalogalva 2 as ordens são
    números puros; o prefixo é convenção interna do Excel do plano. Tudo o que
    se MOSTRA e GRAVA fica nu — o matching continua a usar variantes por
    dentro. Só OF/OV: um modelo «QS122» não pode perder o Q."""
    text = str(value or "").strip()
    m = _REF_PREFIX.match(text)
    return m.group(2) if m else text


def parse_number(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = re.sub(r"[^0-9.,\-]", "", str(value).strip())
    if "." in text and "," in text:
        if text.rindex(",") > text.rindex("."):
            # europeu: ponto de milhares, vírgula decimal (1.234,56)
            text = text.replace(".", "").replace(",", ".")
        else:
            # americano: vírgula de milhares, ponto decimal (1,234.56)
            text = text.replace(",", "")
    elif _THOUSANDS_DOT.match(text):
        # Só pontos, em grupos de 3: «1.200» é mil e duzentos, não 1,2 — em
        # português o decimal escreve-se com vírgula. Lido como 1.2, uma
        # quantidade de 1.200 passava no limite do plano e ia para o Postgres
        # como um.
        text = text.replace(".", "")
    else:
        text = text.replace(",", ".")
    if not text or text in {"-", "."}:
        return None
    try:
        return float(text)
    except ValueError:
        return None
