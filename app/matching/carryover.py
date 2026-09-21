"""Herança «idem»: a identidade escrita uma vez vale para as linhas seguintes.

Nas folhas reais o operador escreve cliente/OV/OF na primeira linha do bloco e
deixa as seguintes em branco — é a convenção de qualquer impresso em papel.
Medido: só 33% das linhas trazem OF escrita; com herança, 85%. Sem isto o
motor tenta cruzar linhas que não têm a chave mais forte e falha.

A herança é uma leitura, não uma escrita: devolve a identidade *efectiva* para
efeitos de cruzamento e diz de que linha veio, mas nunca toca nos dados. Uma
inferência gravada é indistinguível do que o operador escreveu, e isso não se
desfaz.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import similarity as sim

# Campos que se transportam em bloco. A OF é a âncora; cliente/OV costumam ser
# escritos uma vez por obra e o perfil vale até aparecer outro perfil escrito.
# Modelo e quantidade descrevem sempre uma linha concreta e nunca se herdam.
CARRY_FIELDS = ("of", "ov", "cliente", "perfil")

# Marcas de «idem» que os operadores usam em vez de deixar em branco: aspas
# (nas várias grafias que o OCR devolve), vírgulas duplas (aspas rentes à
# linha — folha real 6c9c634e, transcritas «,,»), o sinal de igual, a palavra
# escrita. Sem isto, uma célula com «"» contava como valor — não batia com a
# OF do bloco, CORTAVA a herança, e as linhas seguintes herdavam aspas
# literais (folha real fd88081e: 5 linhas boas perdidas).
_DITTO_MARKS = frozenset({'"', "''", "”", "“", "„", "〃", "=", ",", ",,", ", ,"})


def is_ditto(value: object) -> bool:
    """A célula diz «o mesmo que em cima»? (aspas, =, «idem»)"""
    text = str(value or "").strip()
    return bool(text) and (text in _DITTO_MARKS or text.lower() == "idem")


@dataclass(frozen=True)
class RowIdentity:
    """Identidade efectiva de uma linha e de onde veio cada campo."""

    values: dict[str, str]          # campo -> valor efectivo
    inherited_from: dict[str, int]  # campo -> índice da linha que o forneceu

    def is_inherited(self, field: str) -> bool:
        return field in self.inherited_from


def _written(row: dict, field: str) -> str:
    """O que o operador escreveu de facto — uma marca de «idem» não é um
    valor, é um pedido de herança, e trata-se como a célula em branco."""
    value = row.get(field)
    if is_ditto(value):
        return ""
    return str(value).strip() if value is not None else ""


def _has_content(row: dict, content_fields: tuple[str, ...]) -> bool:
    """A linha diz alguma coisa? Linhas totalmente vazias não herdam nada.

    Uma folha criada à mão nasce com 10 linhas em branco; sem esta regra
    ficavam todas com a identidade da última linha preenchida e apareciam a
    cruzar com o plano. Uma linha só com marcas de «idem» também não é
    conteúdo — aspas sem produção à frente são lixo de OCR.

    Conta TUDO o que não é identidade da obra, não só os campos que o motor
    cruza: uma linha com aspas + qtd + visto de perfil completo é produção
    real (qtd/perf_comp não estão no IndexSpec) — tratá-la como muda deixava-a
    sem OF e ainda cortava o bloco às linhas seguintes.
    """
    extra = tuple(
        f for f in row
        if f not in content_fields and f not in CARRY_FIELDS
        and not str(f).startswith("_")
    )
    return any(_written(row, f) for f in (*content_fields, *extra))


def is_deleted(row: object) -> bool:
    """Uma linha eliminada conserva o OCR/auditoria, mas não existe na folha."""
    return isinstance(row, dict) and row.get("_deleted") is True


def resolve(rows: list[dict], content_fields: tuple[str, ...],
            human_fields_by_row: dict[int, set[str]] | None = None) -> list[RowIdentity]:
    """Identidade efectiva de cada linha, com a proveniência de cada campo."""
    human_fields_by_row = human_fields_by_row or {}
    out = {}
    block: dict[str, str] = {}      # último valor visto de cada campo
    block_source: dict[str, int] = {}  # linha de onde veio

    for i, row in sorted(enumerate(rows), key=lambda item: item[1].get("_display_order", item[1].get("_paper_position", item[0]+1))):
        if is_deleted(row):
            # Uma eliminação aproxima visualmente as linhas vizinhas: não
            # fornece identidade e também não corta o bloco entre elas.
            out[i] = RowIdentity(values={}, inherited_from={})
            continue
        human = human_fields_by_row.get(i, set())
        if row.get("_manual_entry"):
            human = human | set(CARRY_FIELDS)
        written = {f: _written(row, f) for f in CARRY_FIELDS}

        if not _has_content(row, content_fields) and not any(written.values()):
            # linha muda: não herda e corta o bloco, para o que vier a seguir
            # não colar à identidade de antes de um espaço em branco
            out[i] = RowIdentity(values={}, inherited_from={})
            block, block_source = {}, {}
            continue

        if written["of"]:
            same_block = bool(block.get("of")) and sim.code_similarity(written["of"], block["of"]) >= 0.9
            if not same_block:
                # Uma OF nova inicia uma nova obra e corta toda a identidade
                # que vinha da obra anterior.
                block, block_source = {}, {}

        values: dict[str, str] = {}
        inherited: dict[str, int] = {}
        for f in CARRY_FIELDS:
            if written[f]:
                values[f] = written[f]
                block[f] = written[f]
                block_source[f] = i
            elif f in human:
                # o humano apagou o campo de propósito — é uma decisão, não uma
                # omissão; herdar por cima seria desfazê-la
                continue
            elif block.get(f):
                values[f] = block[f]
                inherited[f] = block_source.get(f, i)
        out[i] = RowIdentity(values=values, inherited_from=inherited)
    return [out[i] for i in range(len(rows))]


def effective_row(row: dict, identity: RowIdentity) -> dict:
    """A linha como o motor a deve ver: o escrito, mais o herdado."""
    if not identity.inherited_from:
        return row
    merged = dict(row)
    for field, value in identity.values.items():
        if not _written(row, field):
            merged[field] = value
    return merged
