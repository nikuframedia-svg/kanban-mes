"""Carrega índices de referência do Postgres (dataresearchmtg).

Três referências:
- cantoneiras: linhas do plano (core_mtg.production_lines + production_orders);
- chapa: componentes reconciliados (core_mtg.chapa_components);
- nesting: programas de nesting da chapa (raw_mtg.chapa_nesting_rows) — é contra
  isto que o kanban diário do corte se cruza.

Só SELECTs. A escrita de validados vive em app/pg_store.py, não aqui.
"""

from __future__ import annotations

from datetime import datetime, timezone

import psycopg

from .. import pg
from .plan_values import clean_entry
from .refs import FieldSpec, IndexSpec, PlanIndex


def _dsn() -> str:
    return pg.dsn()


def _fetch(sql: str, params: tuple | None = None) -> list[dict]:
    # Ligação reaproveitada: pelo túnel da fábrica, abrir uma por consulta
    # custava ~0,5 s antes de o SELECT começar (ver app/pg.py).
    return pg.fetch(sql, params)


def _latest_snapshot_id() -> str | None:
    """Carga mais recente do plano de cantoneiras (uma linha de audit_mtg).

    Perguntá-lo à vista kanban_plan_lines obrigava o Postgres a percorrer
    todas as linhas de todas as cargas guardadas (~1,2 s medidos).
    """
    return (plan_snapshot_info() or {}).get("snapshot_id") or None


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
# `cliente` fica fora da escolha do candidato: é um resultado canónico da
# linha vencedora, não evidência para encontrar essa linha. Depois da escolha,
# cross_check materializa diretamente o customer_name dessa entry (inclusive
# quando a OF tem nomes diferentes), sem voltar a agregar por OF.
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


def load_cantoneiras_index(snapshot_id: str | None = None) -> PlanIndex:
    """Carrega uma fotografia coerente do plano MTG3.

    Quando ``snapshot_id`` é fornecido, nunca volta a resolver o ``latest``.
    Isto permite que o pop-up e a escolha da referência usem exatamente a
    mesma fotografia que o índice, mesmo que uma nova carga seja publicada a
    meio do pedido.
    """
    snapshot_id = snapshot_id or _latest_snapshot_id()
    if not snapshot_id:
        return PlanIndex([], CANTONEIRAS_SPEC, plan_age_days=30.0, snapshot_id=None)
    entries = _fetch(
        """
        SELECT snapshot_id, snapshot_loaded_at, plan_key,
               production_order_no AS of, sales_order_no AS ov,
               customer_name AS cliente, customer_name AS cliente_nome,
               component_ref AS modelo, profile_type AS perfil, material_description,
               length_mm AS comp_mm, quantity_planned AS qtd_planeada,
               quantity_made AS qtd_feita,
               remaining_quantity AS qtd_restante,
               overproduction_quantity AS excesso,
               remaining_valid AS falta_valida,
               cutting_machine AS maquina, planning_week AS semana,
               remaining_rule AS regra_calculo, closed_x
          FROM analytics_mtg.kanban_plan_lines
         WHERE source_app = 'kanban-mes'
           AND snapshot_id = %s
        """,
        (snapshot_id,),
    )
    # Erros de fórmula do Excel (ex.: perfil «#VALUE!» da OF265609) nunca
    # chegam às células nem servem de proposta.
    entries = [clean_entry(entry) for entry in entries]
    loaded = entries[0].get("snapshot_loaded_at") if entries else None
    age_days = (
        max(0.0, (datetime.now(timezone.utc) - loaded).total_seconds() / 86400.0)
        if loaded else 30.0
    )
    return PlanIndex(entries, CANTONEIRAS_SPEC, plan_age_days=age_days,
                     snapshot_id=snapshot_id)


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
        SELECT snapshot_id,
               nesting_code                AS plan_key,
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


def nesting_snapshot_id() -> str | None:
    """Snapshot dos nestings, independente do plano de perfis."""
    try:
        rows = _fetch(
            "SELECT plan_snapshot_id AS snapshot_id "
            "FROM audit_mtg.chapa_batches ORDER BY loaded_at DESC LIMIT 1"
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
        "SELECT snapshot_id, loaded_at, source_filename FROM audit_mtg.snapshots "
        "WHERE snapshot_id LIKE %s ORDER BY loaded_at DESC, snapshot_id DESC LIMIT 1",
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


def fetch_profile_lines(of: str, perfil: str, limit: int | None = None,
                        *, snapshot_id: str | None = None) -> list[dict]:
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
    return _fetch_canonical_lines(of, perfil, limit, snapshot_id=snapshot_id)


def fetch_of_lines(of: str, limit: int | None = None,
                   *, snapshot_id: str | None = None) -> list[dict]:
    """Todas as referências da OF, mesmo com perfil vazio/incorreto."""
    if not of:
        return []
    return _fetch_canonical_lines(of, None, limit, snapshot_id=snapshot_id)


def _fetch_canonical_lines(of: str, perfil: str | None,
                           limit: int | None, *, snapshot_id: str | None = None
                           ) -> list[dict]:
    profile_sql = (
        "AND upper(btrim(profile_type)) = upper(btrim(%s))" if perfil else ""
    )
    snapshot_id = snapshot_id or _latest_snapshot_id()
    if not snapshot_id:
        return []
    params: list[object] = [snapshot_id, of]
    if perfil:
        params.append(perfil)
    limit_sql = "LIMIT %s" if limit is not None else ""
    if limit is not None:
        params.append(limit)
    return _fetch(
        f"""
        SELECT snapshot_id, plan_key, component_ref, profile_type AS perfil,
               length_mm, quantity_planned, quantity_made,
               remaining_quantity, overproduction_quantity AS excesso,
               remaining_valid, cutting_machine, planning_week,
               cut_date, status, closed_x, material_description,
               remaining_rule
          FROM analytics_mtg.kanban_plan_lines
         WHERE source_app = 'kanban-mes'
           AND snapshot_id = %s
           AND production_order_no = %s
           {profile_sql}
         ORDER BY closed_x, remaining_quantity DESC NULLS LAST,
                  profile_type, component_ref, plan_key
         {limit_sql}
        """,
        tuple(params),
    )


def fetch_profiles_in_of(of: str, *, snapshot_id: str | None = None) -> list[dict]:
    """Perfis existentes numa obra, com quantas linhas tem cada um.

    Serve para quando o perfil escrito não casa nada: em vez de um vazio, a
    folha mostra o que a obra tem mesmo (casos reais de L200x100x12 escrito
    numa obra que só tem L200X100X10).
    """
    if not of:
        return []
    snapshot_id = snapshot_id or _latest_snapshot_id()
    if not snapshot_id:
        return []
    return _fetch(
        """
        SELECT btrim(profile_type) AS perfil, count(*) AS n_linhas
          FROM analytics_mtg.kanban_plan_lines
         WHERE source_app = 'kanban-mes'
           AND snapshot_id = %s
           AND production_order_no = %s
           AND profile_type IS NOT NULL AND btrim(profile_type) <> ''
         GROUP BY 1 ORDER BY 2 DESC, 1
         LIMIT 40
        """,
        (snapshot_id, of),
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
