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
    row_fields=("cliente", "ov", "of", "perfil", "modelo", "qtd", "perf_comp"),
    footer_fields=("metros_produzidos", "horas_trabalhadas"),
    field_labels={
        "cliente": "Cliente", "ov": "OV", "of": "OF", "perfil": "Perfil",
        "modelo": "Modelo/Referência", "qtd": "Qtd", "perf_comp": "Perf. Comp.",
        "metros_produzidos": "Metros produzidos",
        "horas_trabalhadas": "Horas trabalhadas",
    },
)

# «Perfil completo»: a última coluna da TPL102. Quando o operador põe um visto,
# aquela linha vale por todas as referências daquele perfil na OF — é por isso
# que nessas linhas não há modelo nem quantidade (medido: perfil em 100% delas,
# modelo em 16%). Esteve modelada como comprimento em milímetros, o que nunca
# correspondeu ao papel: em nenhuma folha real há milímetros nesta coluna.
_MARKS = frozenset({"x", "✓", "v", "sim", "ok"})


def is_marked(value: object) -> bool:
    """A célula tem um visto? (não confundir com ter um número escrito)"""
    return str(value or "").strip().lower() in _MARKS


# Folhas lidas antes da mudança de nome guardaram esta coluna como `comp_mm`.
# Ler pelos dois nomes evita reescrever `raw_extraction`, que é a transcrição
# original e não se falsifica para arrumar o schema.
_LEGACY_FIELD = {"perf_comp": "comp_mm"}
# o contrário: nome antigo -> nome de hoje, para quem percorre a linha guardada
LEGACY_FIELD_ALIASES = {old: new for new, old in _LEGACY_FIELD.items()}


def field_value(row: dict, field: str):
    value = row.get(field)
    if value in (None, "") and field in _LEGACY_FIELD:
        return row.get(_LEGACY_FIELD[field])
    return value

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
