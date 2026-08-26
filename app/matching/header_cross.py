"""Cruzamento determinístico dos campos do cabeçalho da folha.

Ao contrário das linhas, o cabeçalho não precisa de um scorer probabilístico:
ou existe uma referência inequívoca (número + nome do colaborador, máquina do
catálogo, data válida, uma única caixa de turno), ou a decisão fica para o
revisor.  Uma proposta nunca é confundida com o valor final.

O módulo é deliberadamente independente da web e da base de dados. As fontes
disponíveis são passadas pelo chamador, para uma falha no plano/SAP não impedir
o resto da folha de ser revisto.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Iterable

from . import operador


HEADER_LABELS = {
    "operador": "Operador",
    "n_operador": "N.º operador",
    "setor_maquina": "Setor/Máquina",
    "data": "Data",
    "turno": "Turno",
}


@dataclass
class HeaderCellCheck:
    field: str
    written: str | None
    proposal: str | None
    status: str                 # confirmed|corrected|review|ambiguous|missing|no_reference
    reason: str
    message: str
    sources: list[dict] = field(default_factory=list)
    auto_write: bool = False
    applied: bool = False
    human_protected: bool = False
    candidates: list[str] = field(default_factory=list)
    actor: str | None = None


_DATE_DMY = re.compile(r"^\s*(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})\s*$")
_DATE_ISO = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*$")


def canonical_date(value: object) -> str | None:
    """Uma data inequívoca da folha em formato visual português.

    Não se completam datas sem ano e não se corrigem dígitos por semelhança:
    isso já seria inventar uma data, não canonizá-la.
    """
    text = str(value or "").strip()
    match = _DATE_ISO.match(text)
    if match:
        year, month, day = map(int, match.groups())
    else:
        match = _DATE_DMY.match(text)
        if not match:
            return None
        day, month, year = map(int, match.groups())
        if year < 100:
            year += 2000
    try:
        parsed = date(year, month, day)
    except ValueError:
        return None
    return parsed.strftime("%d/%m/%Y")


def previous_business_day(base: date) -> date:
    """Dia útil anterior a `base`: salta sábados e domingos.

    Feriados não se saltam de propósito: não existe aqui um calendário de
    feriados fiável (nacionais + municipais + pontes da fábrica), e a fábrica
    trabalha em vários — assumir o dia é menos errado do que inventar folgas.
    """
    day = base - timedelta(days=1)
    while day.weekday() >= 5:          # 5 = sábado, 6 = domingo
        day -= timedelta(days=1)
    return day


def _compact(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return "".join(ch for ch in text.upper() if ch.isalnum())


def _written(value: object) -> str | None:
    return str(value or "").strip() or None


def _employee_number_is_exact(value: object, expected: int | None) -> bool:
    """Aceita só algarismos do próprio número; O→0 é sempre uma proposta."""
    text = "".join(ch for ch in str(value or "") if not ch.isspace())
    return bool(expected is not None and text.isdigit() and int(text) == expected)


def _cell(field_name: str, written: object, *, proposal: str | None = None,
          status: str, reason: str, message: str,
          sources: list[dict] | None = None, auto_write: bool = False,
          protected: bool = False, candidates: Iterable[str] = (),
          actor: str | None = None) -> HeaderCellCheck:
    return HeaderCellCheck(
        field=field_name,
        written=_written(written),
        proposal=proposal,
        status=status,
        reason=reason,
        message=message,
        sources=list(sources or ()),
        auto_write=bool(auto_write and not protected),
        human_protected=protected,
        candidates=list(candidates),
        actor=actor,
    )


def _operator_cells(header: dict, employees: dict | None,
                    protected: set[str]) -> tuple[list[HeaderCellCheck], dict]:
    name = _written(header.get("operador"))
    number = _written(header.get("n_operador"))

    if not isinstance(employees, dict) or not employees:
        cells = [
            _cell(
                f, header.get(f), status="missing" if not _written(header.get(f)) else "no_reference",
                reason="employees_unavailable",
                message=("Campo por preencher." if not _written(header.get(f))
                         else "Lista de colaboradores indisponível; valor mantido."),
                protected=f in protected,
            )
            for f in ("operador", "n_operador")
        ]
        return cells, {
            "cod": None, "pernr": None, "name": name,
            "rule": "sem_ref", "confident": False, "accepted": False,
            "written_name": name,
        }

    match = operador.resolve(name, number, employees)
    exact_number = _employee_number_is_exact(number, match.cod)
    confident = bool(
        match.confident and match.rule in ("exact", "token") and exact_number
    )
    source = [{"kind": "employees", "value": str(match.cod)}] if match.cod else []
    cells: list[HeaderCellCheck] = []

    targets = {
        "operador": match.name,
        "n_operador": str(match.cod) if match.cod is not None else None,
    }
    for field_name, current in (("operador", name), ("n_operador", number)):
        target = targets[field_name]
        is_protected = field_name in protected
        # ``resolve`` já provou a identidade (incluindo O↔0 no número e
        # acentos/caixa no nome). Aqui pergunta-se uma coisa diferente: o
        # valor está já na forma canónica da referência? Se não estiver, um
        # match inequívoco deve também limpar a grafia do OCR.
        same = current == target
        if confident:
            if same:
                cells.append(_cell(
                    field_name, current, status="confirmed", reason=match.rule,
                    message="Confere com a lista de colaboradores.",
                    sources=source, protected=is_protected,
                ))
            elif is_protected:
                # A decisão humana é inviolável, mesmo quando só falta pôr o
                # nome na forma canónica da lista.
                cells.append(_cell(
                    field_name, current, proposal=target, status="confirmed",
                    reason=f"{match.rule}_human",
                    message="Identidade confirmada; edição manual mantida.",
                    sources=source, protected=True,
                ))
            else:
                cells.append(_cell(
                    field_name, current, proposal=target, status="corrected",
                    reason=match.rule,
                    message="Forma canónica da lista de colaboradores.",
                    sources=source, auto_write=True, actor="header:employees",
                ))
        elif target:
            reason = "corrected_number" if match.confident and not exact_number else match.rule
            cells.append(_cell(
                field_name, current, proposal=target, status="review",
                reason=reason,
                message=(
                    "O número exige correção; confirma a identidade sugerida."
                    if reason == "corrected_number" else
                    "Há uma sugestão, mas nome e número não confirmam a mesma pessoa."
                ),
                sources=source, protected=is_protected,
            ))
        else:
            cells.append(_cell(
                field_name, current,
                status="missing" if not current else "ambiguous",
                reason=match.rule,
                message=("Campo por preencher." if not current
                         else "Sem correspondência inequívoca nos colaboradores."),
                protected=is_protected,
            ))

    operator_result = asdict(match)
    operator_result["accepted"] = confident
    operator_result["changed_name"] = match.changed_name
    if not confident and match.pernr:
        # Um PERNR sugerido é útil para revisão, mas não deve parecer a
        # identidade aceite no contrato consumido pela persistência/export.
        operator_result.update({
            "candidate_pernr": match.pernr,
            "candidate_cod": match.cod,
            "candidate_name": match.name,
            "pernr": None,
        })
    return cells, operator_result


# Nomes do mestre atual (core_mtg.machines) e as formas que aparecem escritas
# nas folhas TPL102 — o operador escreve «Rapid 20T - 2», o SAP guarda
# «Ficep Rapid 20T -2». Um alias explícito pode ser canonizado; fuzzy matching
# só gera sugestões.
_MACHINE_ALIASES = {
    "RAPID20T": "Ficep Rapid 20T",
    "RAPID20T1": "Ficep Rapid 20T -1",
    "RAPID20T2": "Ficep Rapid 20T -2",
    "RAPID25": "Ficep Rapid 25T",
    "RAPID25T": "Ficep Rapid 25T",
    "XPT4": "Ficep XP T4",
    "XPT6": "Ficep XP T6",
    "CORTEMANUAL": "Máquina de Corte Manual",
}

# Um nome de família sozinho («FICEP» cobre Rapid e XP; «RAPID» cobre três
# máquinas; «PEDDI» duas) nunca escolhe uma máquina — como o «SERROTE» do
# setor de perfis, identifica a família e pede confirmação.
_GENERIC_FAMILIES = ("FICEP", "RAPID", "PEDDI")


def _catalog_map(machines: Iterable[object] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for machine in machines or ():
        if isinstance(machine, dict):
            value = machine.get("display_name") or machine.get("machine_name")
        else:
            value = (
                getattr(machine, "display_name", None)
                or getattr(machine, "machine_name", None)
                or machine
            )
        value_s = _written(value)
        if value_s:
            out[_compact(value_s)] = value_s
    return out


def _catalog_for_template(catalog: dict[str, str], template) -> dict[str, str]:
    """Impede uma máquina de outro setor de confirmar o cabeçalho.

    O catálogo core_mtg.machines cobre a fábrica inteira: as folhas de
    cantoneiras (frente e verso da TPL102) só podem ter saído das máquinas
    deste setor (Rapid/Ficep/Peddi…), nunca do serrote ou da Vanguard do setor
    de perfis (app irmã, porta 8101). Outras famílias (ex.: chapa) não têm
    catálogo aqui — sem referência, nada se confirma nem se corrige.
    """
    if getattr(template, "family", "") != "cantoneiras":
        return {}
    return {
        key: value for key, value in catalog.items()
        if "SERROTE" not in key and "VANGUARD" not in key
    }


def _catalog_target(target: str, catalog: dict[str, str]) -> str | None:
    return catalog.get(_compact(target))


def _resolve_machine(value: object, catalog: dict[str, str],
                     *, fixed: str | None = None) -> str | None:
    compact = _compact(value)
    if not compact:
        return None
    if compact in catalog:
        return catalog[compact]
    alias = _MACHINE_ALIASES.get(compact)
    if alias:
        return _catalog_target(alias, catalog) or (
            fixed if fixed and _compact(fixed) == _compact(alias) else None
        )
    if fixed and compact == _compact(fixed):
        return fixed
    return None


def _machine_suggestions(value: object, catalog: dict[str, str]) -> list[str]:
    """Sugestões tolerantes, sem qualquer poder de escrita."""
    from . import similarity as sim

    text = _written(value)
    if not text:
        return []
    scored = sorted(
        ((sim.text_similarity(text, name), name) for name in catalog.values()),
        reverse=True,
    )
    return [name for score, name in scored[:3] if score >= 0.5]


def template_machine(template) -> str | None:
    """Máquina fixa declarada pelo contrato do template.

    No setor de cantoneiras não existe: a MESMA TPL102 serve todas as máquinas
    (Rapid 20T-1/2, Rapid 25, Ficep XP T4/T6, Peddi 8) e o verso idem — a
    máquina valida-se contra o catálogo, nunca contra o impresso. O loop fica
    para um template futuro que declare a sua máquina explicitamente.
    """
    for attr in ("canonical_machine", "machine_value", "header_machine"):
        explicit = _written(getattr(template, attr, None))
        if explicit:
            return explicit
    return None


def _machine_cell(header: dict, template, protected: set[str],
                  machines: Iterable[object] | None,
                  plan_machines: Iterable[object] | None) -> HeaderCellCheck:
    field_name = "setor_maquina"
    written = _written(header.get(field_name))
    is_protected = field_name in protected
    fixed = template_machine(template)
    catalog = _catalog_for_template(_catalog_map(machines), template)
    # A máquina fixa do template é uma referência mesmo antes de a lista do
    # Postgres estar disponível.
    if fixed:
        fixed = _catalog_target(fixed, catalog) or fixed
    resolved = _resolve_machine(written, catalog, fixed=fixed)

    plan_values = []
    for value in plan_machines or ():
        canonical = _resolve_machine(value, catalog, fixed=fixed)
        if canonical and canonical not in plan_values:
            plan_values.append(canonical)
    plan_value = plan_values[0] if len(plan_values) == 1 else None

    sources: list[dict] = []
    if fixed:
        sources.append({"kind": "template", "value": fixed})
    sources.extend({"kind": "plan", "value": value} for value in plan_values)

    if fixed:
        if not written:
            return _cell(
                field_name, written, proposal=fixed, status="corrected",
                reason="fixed_template", message="Máquina impressa neste template.",
                sources=sources, auto_write=True, protected=is_protected,
                actor="header:machine",
            )
        if resolved == fixed:
            if written == fixed or is_protected:
                return _cell(
                    field_name, written, proposal=fixed if written != fixed else None,
                    status="confirmed", reason="fixed_template",
                    message=("Confere com o template; edição manual mantida."
                             if is_protected and written != fixed else "Confere com o template."),
                    sources=sources, protected=is_protected,
                )
            return _cell(
                field_name, written, proposal=fixed, status="corrected",
                reason="machine_alias", message="Nome canónico da máquina.",
                sources=sources, auto_write=True, actor="header:machine",
            )
        return _cell(
            field_name, written, proposal=fixed, status="review",
            reason="template_conflict",
            message="O valor lido não confere com a máquina impressa no template.",
            sources=sources, protected=is_protected,
        )

    if resolved:
        if plan_value and plan_value != resolved:
            return _cell(
                field_name, written, proposal=plan_value, status="review",
                reason="plan_conflict",
                message="A máquina escrita e a planeada não coincidem; confirma a usada.",
                sources=sources + [{"kind": "machine_catalog", "value": resolved}],
                protected=is_protected,
            )
        source = sources + [{"kind": "machine_catalog", "value": resolved}]
        if written == resolved or is_protected:
            return _cell(
                field_name, written, proposal=resolved if written != resolved else None,
                status="confirmed", reason="machine_catalog",
                message=("Máquina reconhecida; edição manual mantida."
                         if is_protected and written != resolved else "Máquina reconhecida."),
                sources=source, protected=is_protected,
            )
        return _cell(
            field_name, written, proposal=resolved, status="corrected",
            reason="machine_alias", message="Nome canónico da máquina.",
            sources=source, auto_write=True, actor="header:machine",
        )

    if not written:
        if plan_value:
            return _cell(
                field_name, written, proposal=plan_value, status="review",
                reason="plan_only",
                message="O plano sugere uma máquina, mas não prova onde foi produzido.",
                sources=sources, protected=is_protected,
            )
        if len(plan_values) > 1:
            return _cell(
                field_name, written, status="ambiguous", reason="multiple_plan_machines",
                message="As linhas ligadas ao plano apontam para máquinas diferentes.",
                sources=sources, protected=is_protected, candidates=plan_values,
            )
        return _cell(
            field_name, written, status="missing", reason="empty_machine",
            message="Máquina por preencher.", sources=sources,
            protected=is_protected,
        )

    # «FICEP»/«RAPID»/«PEDDI» identificam apenas a família: nunca escolhem
    # uma das máquinas.
    if _compact(written) in _GENERIC_FAMILIES:
        candidates = [
            value for key, value in catalog.items() if _compact(written) in key
        ] or list(dict.fromkeys(catalog.values()))
        return _cell(
            field_name, written, status="ambiguous", reason="generic_family",
            message=f"«{written}» não identifica uma máquina única; confirma.",
            sources=sources, protected=is_protected, candidates=candidates,
        )

    suggestions = _machine_suggestions(written, catalog)
    if suggestions:
        return _cell(
            field_name, written,
            proposal=suggestions[0] if len(suggestions) == 1 else None,
            status="review" if len(suggestions) == 1 else "ambiguous",
            reason="fuzzy_machine",
            message=("Máquina parecida encontrada; confirma."
                     if len(suggestions) == 1 else "Há várias máquinas possíveis; confirma."),
            sources=sources, protected=is_protected, candidates=suggestions,
        )
    return _cell(
        field_name, written, status="no_reference" if not catalog else "ambiguous",
        reason="machine_not_found",
        message=("Catálogo de máquinas indisponível; valor mantido."
                 if not catalog else "Máquina não reconhecida no catálogo."),
        sources=sources, protected=is_protected,
    )


def _date_cell(header: dict, protected: set[str],
               assumed_date: str | None = None) -> HeaderCellCheck:
    field_name = "data"
    written = _written(header.get(field_name))
    is_protected = field_name in protected
    # Regra da fábrica (26/08): a folha entregue à digitalização é SEMPRE do
    # dia útil anterior — a data assumida manda sobre o que o OCR leu ou o
    # operador escreveu no papel. Só a edição humana no sistema a desativa
    # (cai para a validação normal, lá em baixo).
    if assumed_date and not is_protected:
        sources = [{"kind": "assumed_date", "value": assumed_date}]
        if written == assumed_date:
            return _cell(
                field_name, written, status="confirmed",
                reason="assumed_prev_business_day",
                message="Data assumida: dia útil anterior à digitalização.",
                sources=sources,
            )
        return _cell(
            field_name, written, proposal=assumed_date, status="corrected",
            reason="assumed_prev_business_day",
            message="Data assumida: dia útil anterior à digitalização.",
            sources=sources, auto_write=True, actor="header:date",
        )
    if not written:
        return _cell(
            field_name, written, status="missing", reason="empty_date",
            message="Data obrigatória por preencher.", protected=is_protected,
        )
    canonical = canonical_date(written)
    if canonical is None:
        return _cell(
            field_name, written, status="review", reason="invalid_date",
            message="Data inválida; escreve dia/mês/ano.", protected=is_protected,
        )
    source = [{"kind": "written_date", "value": canonical}]
    if written == canonical or is_protected:
        return _cell(
            field_name, written, proposal=canonical if written != canonical else None,
            status="confirmed", reason="valid_date",
            message=("Data válida; formato manual mantido."
                     if is_protected and written != canonical else "Data válida."),
            sources=source, protected=is_protected,
        )
    return _cell(
        field_name, written, proposal=canonical, status="corrected",
        reason="date_format", message="Data normalizada para dia/mês/ano.",
        sources=source, auto_write=True, actor="header:date",
    )


def _allowed_shifts(template) -> tuple[str, ...]:
    explicit = (
        getattr(template, "allowed_shifts", None)
        or getattr(template, "turno_options", None)
    )
    if explicit:
        return tuple(str(v).upper() for v in explicit)
    # A TPL102 imprime três caixas (frente e verso): M / R / XM.
    return ("M", "R", "XM")


def _shift_cell(header: dict, template, protected: set[str]) -> HeaderCellCheck:
    field_name = "turno"
    written = _written(header.get(field_name))
    is_protected = field_name in protected
    allowed = _allowed_shifts(template)
    sources = [{"kind": "template", "value": "/".join(allowed)}]
    if not written:
        return _cell(
            field_name, written, status="missing", reason="empty_shift",
            message="Nenhum turno assinalado.", sources=sources,
            protected=is_protected,
        )
    parts = [p for p in re.split(r"[+,;/|]+", written) if p.strip()]
    canonical_parts = [_compact(part) for part in parts]
    valid = [part for part in canonical_parts if part in allowed]
    if len(parts) > 1:
        return _cell(
            field_name, written, status="ambiguous", reason="multiple_shifts",
            message="Mais de uma caixa de turno foi lida; confirma.",
            sources=sources, protected=is_protected, candidates=valid,
        )
    canonical = canonical_parts[0] if canonical_parts else ""
    if canonical not in allowed:
        return _cell(
            field_name, written, status="review", reason="unknown_shift",
            message=f"Turno desconhecido; opções: {', '.join(allowed)}.",
            sources=sources, protected=is_protected, candidates=allowed,
        )
    if written == canonical or is_protected:
        return _cell(
            field_name, written, proposal=canonical if written != canonical else None,
            status="confirmed", reason="valid_shift",
            message=("Turno válido; edição manual mantida."
                     if is_protected and written != canonical else "Turno reconhecido."),
            sources=sources, protected=is_protected,
        )
    return _cell(
        field_name, written, proposal=canonical, status="corrected",
        reason="shift_format", message="Turno normalizado.", sources=sources,
        auto_write=True, actor="header:shift",
    )


def check_header(header: dict, template, *, human_fields: set[str] | None = None,
                 employees: dict | None = None,
                 machines: Iterable[object] | None = None,
                 plan_machines: Iterable[object] | None = None,
                 source_document: dict | None = None,
                 assumed_date: str | None = None) -> dict:
    """Cruza os cinco campos e devolve apenas estruturas JSON-serializáveis.

    `assumed_date` (dd/mm/aaaa) é a data assumida da folha — o dia útil
    anterior à data-base calculada pelo chamador; ver `_date_cell`.
    """
    human_fields = human_fields or set()
    operator_cells, operator_result = _operator_cells(header, employees, human_fields)
    cells = operator_cells + [
        _machine_cell(header, template, human_fields, machines, plan_machines),
        _date_cell(header, human_fields, assumed_date),
        _shift_cell(header, template, human_fields),
    ]
    return {
        "cells": {cell.field: asdict(cell) for cell in cells},
        "operator": operator_result,
        # Nome/página são auditoria de origem. Nunca entram em _date_cell.
        "source_document": dict(source_document or {}),
    }
