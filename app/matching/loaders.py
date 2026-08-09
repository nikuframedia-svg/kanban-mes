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


def _fetch(sql: str) -> list[dict]:
    with psycopg.connect(_dsn(), row_factory=dict_row) as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute(sql)
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
CANTONEIRAS_SPEC = IndexSpec(
    identity_fields=(
        FieldSpec("of", "code", "of", code_prefix="OF", max_candidate_entries=None),
        FieldSpec("ov", "code", "ov", code_prefix="OV"),
        FieldSpec("modelo", "code", "modelo"),
        FieldSpec("perfil", "profile", "perfil"),
    ),
    numeric_fields=(
        FieldSpec("comp_mm", "numeric", "comp_mm", tolerance=50.0),
    ),
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
