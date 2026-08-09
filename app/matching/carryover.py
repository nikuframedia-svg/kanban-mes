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

# Campos que se transportam em bloco. A ordem importa para as regras abaixo:
# a OF é a âncora, o resto acompanha-a.
CARRY_FIELDS = ("of", "ov", "cliente")


@dataclass(frozen=True)
class RowIdentity:
    """Identidade efectiva de uma linha e de onde veio cada campo."""

    values: dict[str, str]          # campo -> valor efectivo
    inherited_from: dict[str, int]  # campo -> índice da linha que o forneceu

    def is_inherited(self, field: str) -> bool:
        return field in self.inherited_from


def _written(row: dict, field: str) -> str:
    value = row.get(field)
    return str(value).strip() if value is not None else ""


def _has_content(row: dict, content_fields: tuple[str, ...]) -> bool:
    """A linha diz alguma coisa? Linhas totalmente vazias não herdam nada.

    Uma folha criada à mão nasce com 10 linhas em branco; sem esta regra
    ficavam todas com a identidade da última linha preenchida e apareciam a
    cruzar com o plano.
    """
    return any(_written(row, f) for f in content_fields)


def resolve(rows: list[dict], content_fields: tuple[str, ...],
            human_fields_by_row: dict[int, set[str]] | None = None) -> list[RowIdentity]:
    """Identidade efectiva de cada linha, com a proveniência de cada campo."""
    human_fields_by_row = human_fields_by_row or {}
    out: list[RowIdentity] = []
    block: dict[str, str] = {}      # último valor visto de cada campo
    block_source: dict[str, int] = {}  # linha de onde veio

    for i, row in enumerate(rows):
        human = human_fields_by_row.get(i, set())
        written = {f: _written(row, f) for f in CARRY_FIELDS}

        if not _has_content(row, content_fields) and not any(written.values()):
            # linha muda: não herda e corta o bloco, para o que vier a seguir
            # não colar à identidade de antes de um espaço em branco
            out.append(RowIdentity(values={}, inherited_from={}))
            block, block_source = {}, {}
            continue

        if written["of"]:
            same_block = bool(block.get("of")) and sim.code_similarity(written["of"], block["of"]) >= 0.9
            if not same_block:
                # OF nova a meio da folha: começa bloco, não arrasta nada do anterior
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
        out.append(RowIdentity(values=values, inherited_from=inherited))
    return out


def effective_row(row: dict, identity: RowIdentity) -> dict:
    """A linha como o motor a deve ver: o escrito, mais o herdado."""
    if not identity.inherited_from:
        return row
    merged = dict(row)
    for field, value in identity.values.items():
        if not _written(row, field):
            merged[field] = value
    return merged
