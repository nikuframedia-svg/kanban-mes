"""Resolver quem assinou a folha, contra a lista de colaboradores do SAP.

Nos kanbans o **número** vem escrito de forma fiável e o nome não. Medido nas
folhas reais: o operador 2105 é MARCO LOPES no SAP e o OCR leu «Flavio Lopes»
na frente e «Mauro Lopes» no verso — o número certo nas duas, o nome errado nas
duas. Por isso a chave é o número; o nome serve para mostrar e para confirmar.

Casar por nome não é opção: 60 dos 720 nomes da lista estão repetidos (há dois
ANTONIO SOUSA).
"""

from __future__ import annotations

from dataclasses import dataclass

from . import similarity as sim


@dataclass(frozen=True)
class Employee:
    cod: int
    pernr: str
    full_name: str


@dataclass(frozen=True)
class OperatorMatch:
    """Como se decidiu a identidade — guardar o *como* é o que a torna auditável."""

    cod: int | None
    pernr: str | None
    name: str | None          # nome canónico a mostrar
    rule: str                 # exact | token | corrigido | so_numero | sem_ref | sem_numero
    confident: bool           # False = mostrar a amarelo, pedir olho humano
    written_name: str | None = None

    @property
    def changed_name(self) -> bool:
        return bool(self.name and self.written_name
                    and _norm(self.name) != _norm(self.written_name))


def _norm(value: str | None) -> str:
    return sim.compact(value)


def _tokens(value: str | None) -> set[str]:
    """Palavras do nome, normalizadas uma a uma.

    Normalizar o nome inteiro não serve: `compact` cola tudo («FABIO ARAUJO» →
    «FABIOARAUJO») e deixava de haver tokens para comparar.
    """
    return {sim.compact(part) for part in str(value or "").split() if sim.compact(part)}


def _name_is_compatible(written: str | None, canonical: str) -> bool:
    """O nome escrito é uma versão maltratada do canónico?

    Exige que cada palavra escrita tenha correspondência no nome do SAP, com
    tolerância de uma letra. Assim «Fabio» passa por FABIO ARAUJO (abreviou) e
    «harwinder» por HARVINDER (letra trocada), mas «Flavio Lopes» não passa por
    MARCO LOPES — partilhar só o apelido não chega para trocar o nome próprio.
    Palavras de uma ou duas letras são ruído do OCR e ignoram-se.
    """
    escritos = {t for t in _tokens(written) if len(t) > 2}
    canonicos = _tokens(canonical)
    if not escritos or not canonicos:
        return False
    return all(
        any(sim.levenshtein(t, c) <= 1 for c in canonicos)
        for t in escritos
    )


def _parse_cod(value: object) -> int | None:
    text = "".join(ch for ch in str(value or "") if ch.isdigit())
    return int(text) if text else None


def resolve(written_name: str | None, written_cod: object,
            employees: dict[int, Employee]) -> OperatorMatch:
    """Identidade do operador a partir do que está escrito na folha."""
    name = str(written_name or "").strip() or None
    cod = _parse_cod(written_cod)

    if not employees:
        return OperatorMatch(cod, None, name, "sem_ref", confident=False, written_name=name)

    if cod is not None and cod in employees:
        emp = employees[cod]
        if _norm(name) == _norm(emp.full_name):
            rule, confident = "exact", True
        elif _name_is_compatible(name, emp.full_name):
            # o número bate e o nome escrito cabe no do SAP (abreviado ou com
            # letras trocadas): é a mesma pessoa mal transcrita
            rule, confident = "token", True
        else:
            # O número bate mas o nome escrito tem partes que não existem no do
            # SAP — «Flavio Lopes» para MARCO LOPES. O número manda, mas trocar
            # um nome legível por outro nome legível nunca pode ser silencioso.
            rule, confident = "so_numero", False
        return OperatorMatch(emp.cod, emp.pernr, emp.full_name, rule, confident, name)

    if cod is not None and name:
        # Número que não existe na lista: procurar um dígito trocado, exigindo
        # que o nome quase bata. A regra da app da MTG2 — «partilhar um token» —
        # não serve aqui: SINGH aparece em 186 dos 720 nomes, e no caso real
        # (2480 «Gurbinder Singh») deixava dois candidatos empatados. Com a
        # distância sobre o nome inteiro, GURPINDER SINGH ganha sozinho.
        candidatos = [
            emp for emp in employees.values()
            if abs(emp.cod - cod) < 10000
            and sim.levenshtein(str(emp.cod), str(cod)) <= 1
            and sim.levenshtein(_norm(name), _norm(emp.full_name)) <= 2
        ]
        if len(candidatos) == 1:
            emp = candidatos[0]
            return OperatorMatch(emp.cod, emp.pernr, emp.full_name, "corrigido",
                                 confident=False, written_name=name)

    # Sem correspondência: fica como está. Não se inventa um número de pessoal
    # para quem não está na lista — pode ser um contratado recente, e uma
    # identidade fabricada numa tabela de auditoria é pior do que um vazio.
    return OperatorMatch(cod, None, name, "sem_numero" if cod is None else "sem_ref",
                         confident=False, written_name=name)
