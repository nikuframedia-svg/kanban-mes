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
    index_loader: str | None          # função em app.matching.loaders; None = sem cruzamento
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
    label="Cantoneiras — Kanban de produção (TPL102)",
    index_loader="load_cantoneiras_index",
    # colunas na ordem exata da folha física TPL102 (Rapid 20T):
    # CLIENTE | OV | OF | PERFIL | MODELO | QTD | PERF. COMP.
    row_fields=("cliente", "ov", "of", "perfil", "modelo", "qtd", "comp_mm"),
    footer_fields=("metros_produzidos", "horas_trabalhadas"),
    field_labels={
        "cliente": "Cliente", "ov": "OV", "of": "OF", "perfil": "Perfil",
        "modelo": "Modelo", "qtd": "Qtd", "comp_mm": "Perf. Comp. (mm)",
        "metros_produzidos": "Metros produzidos",
        "horas_trabalhadas": "Horas trabalhadas",
    },
)

CANTONEIRAS_PARAGENS = KanbanTemplate(
    name="cantoneiras_paragens",
    family="cantoneiras",
    label="Cantoneiras — Paragens (TPL102 verso)",
    index_loader=None,                # paragens não se cruzam com o plano
    row_fields=("motivo", "inicio", "fim", "duracao", "resolvido"),
    footer_fields=(),
    field_labels={
        "motivo": "Motivo da paragem", "inicio": "Início", "fim": "Fim",
        "duracao": "Duração", "resolvido": "Resolvido",
    },
)

TEMPLATES: dict[str, KanbanTemplate] = {
    t.name: t for t in (CHAPA_KANBAN, CANTONEIRAS_KANBAN, CANTONEIRAS_PARAGENS)
}


def get_template(name: str) -> KanbanTemplate:
    return TEMPLATES[name]
