"""Carrega índices de referência do Postgres (dataresearchmtg).

Três referências:
- cantoneiras: linhas do plano (core_mtg.production_lines + production_orders);
- chapa: componentes reconciliados (core_mtg.chapa_components);
- nesting: programas de nesting da chapa (raw_mtg.chapa_nesting_rows) — é contra
  isto que o kanban diário do corte se cruza.

Só SELECTs. A escrita de validados vive em app/pg_store.py, não aqui.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

from ..config import settings
from .refs import FieldSpec, IndexSpec, PlanIndex


def _dsn() -> str:
    return os.environ.get("MES_PG_DSN") or settings.pg_dsn


def _fetch(sql: str, params: tuple | None = None) -> list[dict]:
    with psycopg.connect(_dsn(), row_factory=dict_row) as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def _plan_age_days(snapshot_like: str) -> float:
    rows = _fetch(
        "SELECT max(loaded_at) AS loaded_at FROM audit_mtg.snapshots "
        f"WHERE snapshot_id LIKE '{snapshot_like}'"
    )
    loaded = rows[0]["loaded_at"] if rows else None
    if not loaded:
        return 30.0
    return max(0.0, (datetime.now(timezone.utc) - loaded).total_seconds() / 86400.0)


# Convenções que separam o que o operador escreve do que o plano guarda:
# - o plano prefixa todas as OF com "OF" e as OV com "OV"; a folha não (a
#   coluna já se chama OF, ninguém repete o prefixo);
# - o plano escreve perfis como L60X60X5, a folha como "60 x 5";
# - a OF é o campo que identifica de verdade, por isso não leva teto de
#   candidatos: há OFs com mais de 600 linhas e ficavam invisíveis.
#
# `cliente` ficou de fora de propósito: o plano guarda o cliente interno da
# Metalogalva ("c.m.e.-const. e") e o operador escreve o cliente final ("CMF")
# ou uma nota. Como discorda sempre, era evidência negativa uniforme — não
# ajudava a escolher candidato nenhum e mantinha a célula vermelha para sempre.
# A validação do cliente existe, mas à parte do scorer: `plan_customer_for`
# em cross_check.py resolve-o pela OF (que o determina por construção) e as
# entries levam `cliente_nome`/`n_clientes` só para esse check.
CANTONEIRAS_SPEC = IndexSpec(
    identity_fields=(
        FieldSpec("of", "code", "of", code_prefix="OF", max_candidate_entries=None),
        FieldSpec("ov", "code", "ov", code_prefix="OV"),
        FieldSpec("modelo", "code", "modelo"),
        FieldSpec("perfil", "profile", "perfil"),
    ),
    # Sem dimensões: a última coluna da folha é PERF. COMP. (um visto), não um
    # comprimento. Enquanto esteve modelada como comprimento, o motor propunha
    # escrever milímetros do plano por cima do visto do operador.
    numeric_fields=(),
    key_field="plan_key",
)

CHAPA_SPEC = IndexSpec(
    identity_fields=(
        FieldSpec("of", "code", "of"),
        FieldSpec("ov", "code", "ov"),
        FieldSpec("cliente", "text", "cliente"),
        FieldSpec("modelo", "code", "modelo"),
    ),
    numeric_fields=(
        FieldSpec("esp", "numeric", "esp", tolerance=0.05),
    ),
    key_field="plan_key",
)

NESTING_SPEC = IndexSpec(
    identity_fields=(
        FieldSpec("nesting", "code", "nesting"),
        FieldSpec("maquina", "text", "maquina"),
    ),
    numeric_fields=(
        FieldSpec("esp", "numeric", "esp", tolerance=0.05),
        FieldSpec("comp_mm", "numeric", "comp_mm", tolerance=10.0),
        FieldSpec("larg_mm", "numeric", "larg_mm", tolerance=10.0),
    ),
    key_field="plan_key",
)


def load_cantoneiras_index() -> PlanIndex:
    entries = _fetch(
        """
        SELECT l.source_line_id            AS plan_key,
               l.production_order_no       AS of,
               o.sales_order_no            AS ov,
               o.customer_key              AS cliente,
               o.distinct_customers        AS n_clientes,
               c.customer_name             AS cliente_nome,
               l.component_ref             AS modelo,
               l.profile_type              AS perfil,
               l.length_mm                 AS comp_mm,
               l.quantity_planned          AS qtd_planeada,
               l.remaining_quantity        AS qtd_restante,
               l.cutting_machine           AS maquina,
               l.planning_week             AS semana,
               l.included_in_backlog       AS em_backlog
        FROM core_mtg.production_lines l
        JOIN core_mtg.production_orders o
          ON o.snapshot_id = l.snapshot_id
         AND o.production_order_no = l.production_order_no
        LEFT JOIN core_mtg.customers c
          ON c.snapshot_id = o.snapshot_id
         AND c.customer_key = o.customer_key
        WHERE l.snapshot_id = (
            SELECT snapshot_id FROM audit_mtg.snapshots
            WHERE snapshot_id LIKE 'mtg\\_%'
            ORDER BY loaded_at DESC LIMIT 1
        )
        """
    )
    return PlanIndex(entries, CANTONEIRAS_SPEC, plan_age_days=_plan_age_days("mtg\\_%"))


def load_chapa_index() -> PlanIndex:
    entries = _fetch(
        """
        SELECT business_key                AS plan_key,
               production_order_no         AS of,
               sales_order_no              AS ov,
               customer_name               AS cliente,
               component_ref               AS modelo,
               thickness_mm                AS esp,
               material_quality            AS material,
               cutting_machine             AS maquina,
               quantity_plan               AS qtd_planeada,
               cut_remaining_quantity      AS qtd_restante,
               production_deadline         AS prazo
        FROM core_mtg.chapa_components
        WHERE batch_id = (
            SELECT batch_id FROM audit_mtg.chapa_batches
            ORDER BY loaded_at DESC LIMIT 1
        )
        """
    )
    return PlanIndex(entries, CHAPA_SPEC, plan_age_days=_plan_age_days("chapa\\_%"))


def load_nesting_index() -> PlanIndex:
    entries = _fetch(
        """
        SELECT nesting_code                AS plan_key,
               nesting_code                AS nesting,
               machine_name                AS maquina,
               thickness_mm                AS esp,
               sheet_length_mm             AS comp_mm,
               sheet_width_mm              AS larg_mm,
               repetitions_planned         AS repeticoes_planeadas,
               manufacturing_date          AS data_fabrico
        FROM raw_mtg.chapa_nesting_rows
        WHERE snapshot_id = (
            SELECT plan_snapshot_id FROM audit_mtg.chapa_batches
            ORDER BY loaded_at DESC LIMIT 1
        )
        """
    )
    return PlanIndex(entries, NESTING_SPEC, plan_age_days=_plan_age_days("chapa\\_%"))


def load_machines() -> list[dict]:
    """Catálogo canónico de máquinas do mesmo snapshot Met3 usado no cross."""
    return _fetch(
        """
        SELECT machine_code, display_name
        FROM core_mtg.machines
        WHERE snapshot_id = (
            SELECT snapshot_id FROM audit_mtg.snapshots
            WHERE snapshot_id LIKE %s
            ORDER BY loaded_at DESC LIMIT 1
        )
        ORDER BY display_name
        """,
        (_CANTONEIRAS_LIKE,),
    )


def employees_snapshot_id() -> str | None:
    """Fingerprint independente da carga de colaboradores.

    A cache dos colaboradores era invalidada pelo snapshot do PLANO: uma carga
    nova de colaboradores sem plano novo ficava invisível até ao restart.
    """
    try:
        rows = _fetch(
            "SELECT snapshot_id FROM audit_mtg.snapshots "
            "WHERE dataset_id = 'ds-colaboradores' "
            "ORDER BY loaded_at DESC LIMIT 1"
        )
    except psycopg.Error:
        return None
    return str(rows[0]["snapshot_id"]) if rows else None


def load_active_ofs(days: int = 14) -> set[str]:
    """OFs com atividade recente nos registos validados do MES (contexto D1).
    Enquanto mes_kanban não existir/estiver vazio, devolve vazio — sem contexto."""
    try:
        rows = _fetch(
            "SELECT DISTINCT production_order FROM mes_kanban.production_records "
            f"WHERE sheet_date >= current_date - interval '{int(days)} days'"
        )
        return {r["production_order"] for r in rows if r["production_order"]}
    except psycopg.Error:
        return set()


# O padrão vai como parâmetro: num SQL com placeholders, um `%` literal seria
# lido como início de placeholder.
_CANTONEIRAS_LIKE = "mtg\\_%"
_CANTONEIRAS_SNAPSHOT = (
    "SELECT snapshot_id FROM audit_mtg.snapshots "
    "WHERE snapshot_id LIKE %s ORDER BY loaded_at DESC LIMIT 1"
)


def plan_snapshot_info() -> dict:
    """Identidade e idade do plano de cantoneiras em uso."""
    rows = _fetch(
        "SELECT snapshot_id, source_filename, loaded_at FROM audit_mtg.snapshots "
        "WHERE snapshot_id LIKE %s ORDER BY loaded_at DESC LIMIT 1",
        (_CANTONEIRAS_LIKE,),
    )
    if not rows:
        return {}
    info = dict(rows[0])
    loaded = info.get("loaded_at")
    info["age_hours"] = (
        max(0.0, (datetime.now(timezone.utc) - loaded).total_seconds() / 3600.0)
        if loaded else None
    )
    return info


def fetch_profile_lines(of: str, perfil: str, limit: int = 500) -> list[dict]:
    """Todas as referências do plano para a chave OF + Perfil.

    É o que uma linha marcada com PERF. COMP. representa: o operador escreveu o
    perfil e não listou modelo a modelo, portanto para conferir é preciso ver
    quais são as referências que aquele perfil tem naquela obra.

    `upper()` no perfil não é decorativo: o plano tem `l40X40X3` e `L40X40X3`
    como valores distintos, e uma comparação sensível a maiúsculas perdia
    linhas.
    """
    if not of or not perfil:
        return []
    return _fetch(
        f"""
        SELECT l.component_ref, l.length_mm, l.quantity_planned, l.quantity_made,
               l.remaining_quantity, l.cutting_machine, l.planning_week,
               l.cut_date::date AS cut_date, l.status, l.closed_x,
               l.material_description
          FROM core_mtg.production_lines l
         WHERE l.snapshot_id = ({_CANTONEIRAS_SNAPSHOT})
           AND l.production_order_no = %s
           AND upper(btrim(l.profile_type)) = upper(btrim(%s))
         ORDER BY l.closed_x, l.remaining_quantity DESC, l.component_ref
         LIMIT %s
        """,
        (_CANTONEIRAS_LIKE, of, perfil, limit),
    )


def fetch_profiles_in_of(of: str) -> list[dict]:
    """Perfis existentes numa obra, com quantas linhas tem cada um.

    Serve para quando o perfil escrito não casa nada: em vez de um vazio, a
    folha mostra o que a obra tem mesmo (casos reais de L200x100x12 escrito
    numa obra que só tem L200X100X10).
    """
    if not of:
        return []
    return _fetch(
        f"""
        SELECT btrim(l.profile_type) AS perfil, count(*) AS n_linhas
          FROM core_mtg.production_lines l
         WHERE l.snapshot_id = ({_CANTONEIRAS_SNAPSHOT})
           AND l.production_order_no = %s
           AND l.profile_type IS NOT NULL AND btrim(l.profile_type) <> ''
         GROUP BY 1 ORDER BY 2 DESC, 1
         LIMIT 40
        """,
        (_CANTONEIRAS_LIKE, of),
    )


def load_employees() -> dict[int, "Employee"]:
    """Colaboradores do snapshot mais recente, indexados pelo número escrito.

    Devolve vazio (em vez de rebentar) se a tabela ainda não existir: a lista é
    uma referência para melhorar a leitura, não uma dependência da revisão.
    """
    from .operador import Employee

    try:
        rows = _fetch(
            """
            SELECT cod, pernr, full_name FROM core_mtg.employees
             WHERE snapshot_id = (
                SELECT snapshot_id FROM audit_mtg.snapshots
                 WHERE dataset_id = 'ds-colaboradores'
                 ORDER BY loaded_at DESC LIMIT 1
             )
            """
        )
    except psycopg.Error:
        return {}
    return {
        int(r["cod"]): Employee(int(r["cod"]), str(r["pernr"]).strip(), str(r["full_name"]).strip())
        for r in rows
    }
