"""Templates das folhas kanban — que colunas tem cada folha e contra que índice
do plano se cruzam. Dois builtin: chapa (kanban diário de corte, cruza contra os
nestings) e cantoneiras (cruza contra o plano de perfis)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class KanbanTemplate:
    name: str
    family: str                       # chapa | cantoneiras
    label: str
    index_loader: str                 # nome da função em app.matching.loaders
    row_fields: tuple[str, ...]       # colunas da tabela, na ordem da folha
    header_fields: tuple[str, ...] = ("operador", "n_operador", "setor_maquina", "data", "turno")
    footer_fields: tuple[str, ...] = ("horas_trabalhadas",)
    field_labels: dict[str, str] | None = None


CHAPA_KANBAN = KanbanTemplate(
    name="chapa_kanban",
    family="chapa",
    label="Chapa — Kanban diário de corte",
    index_loader="load_nesting_index",
    row_fields=("nesting", "maquina", "esp", "comp_mm", "larg_mm", "repeticoes", "obs"),
    field_labels={
        "nesting": "Nesting", "maquina": "Máquina", "esp": "Esp. (mm)",
        "comp_mm": "Comp. chapa (mm)", "larg_mm": "Larg. chapa (mm)",
        "repeticoes": "Repetições", "obs": "Observações",
    },
)

CANTONEIRAS_KANBAN = KanbanTemplate(
    name="cantoneiras_kanban",
    family="cantoneiras",
    label="Cantoneiras — Kanban de corte",
    index_loader="load_cantoneiras_index",
    row_fields=("cliente", "ov", "of", "modelo", "qtd", "comp_mm", "obs"),
    field_labels={
        "cliente": "Cliente", "ov": "OV", "of": "OF", "modelo": "Referência",
        "qtd": "Qtd", "comp_mm": "Comp. (mm)", "obs": "Observações",
    },
)

TEMPLATES: dict[str, KanbanTemplate] = {
    t.name: t for t in (CHAPA_KANBAN, CANTONEIRAS_KANBAN)
}


def get_template(name: str) -> KanbanTemplate:
    return TEMPLATES[name]
