"""Dados da página Estado — plano de produção vs produção validada, por OF.

Só SELECTs (padrão de app/matching/loaders.py: snapshot/lote mais recente).
Todas as funções degradam graciosamente: Postgres em baixo → {"available": False}.
"""

from __future__ import annotations

import os

from ..config import settings

# regex em vez de LIKE: um '%' literal no SQL colide com os placeholders do psycopg
_LATEST_MTG = (
    "(SELECT snapshot_id FROM audit_mtg.snapshots "
    "WHERE snapshot_id ~ '^mtg_' ORDER BY loaded_at DESC LIMIT 1)"
)
_LATEST_CHAPA_BATCH = (
    "(SELECT batch_id FROM audit_mtg.chapa_batches ORDER BY loaded_at DESC LIMIT 1)"
)


def _dsn() -> str:
    return os.environ.get("MES_PG_DSN") or settings.pg_dsn


def _fetch(sql: str, params: tuple = ()) -> list[dict]:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(_dsn(), row_factory=dict_row, connect_timeout=5) as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute(sql, params if params else None)
            return cur.fetchall()


def fetch_plan_rows() -> list[dict]:
    """Plano agregado por OF, ambas as famílias, snapshot mais recente."""
    cantoneiras = _fetch(f"""
        SELECT o.customer_key AS cliente, o.sales_order_no AS ov,
               l.production_order_no AS of, 'cantoneiras' AS familia,
               sum(l.quantity_planned)   AS qtd_planeada,
               sum(l.remaining_quantity) AS qtd_restante,
               string_agg(DISTINCT l.cutting_machine, ', ') AS maquinas,
               min(l.planning_week) AS semana
        FROM core_mtg.production_lines l
        JOIN core_mtg.production_orders o
          ON o.snapshot_id = l.snapshot_id
         AND o.production_order_no = l.production_order_no
        WHERE l.snapshot_id = {_LATEST_MTG}
        GROUP BY 1, 2, 3
    """)
    chapa = _fetch(f"""
        SELECT max(customer_name) AS cliente, max(sales_order_no) AS ov,
               production_order_no AS of, 'chapa' AS familia,
               sum(quantity_plan)           AS qtd_planeada,
               sum(cut_remaining_quantity)  AS qtd_restante,
               string_agg(DISTINCT cutting_machine, ', ') AS maquinas,
               NULL::integer AS semana
        FROM core_mtg.chapa_components
        WHERE batch_id = {_LATEST_CHAPA_BATCH}
        GROUP BY production_order_no
    """)
    return cantoneiras + chapa


def fetch_validated_rows() -> list[dict]:
    """Produção validada no MES, agregada por OF."""
    return _fetch("""
        SELECT production_order AS of, family AS familia,
               max(customer_name) AS cliente, max(sales_order) AS ov,
               sum(quantity) AS qtd_validada, count(*) AS linhas,
               max(sheet_date) AS ultima_folha
        FROM mes_kanban.production_records
        WHERE production_order IS NOT NULL
        GROUP BY 1, 2
    """)


def fetch_mes_kpis() -> dict:
    rows = _fetch("""
        SELECT (SELECT count(*) FROM mes_kanban.validated_sheets)   AS folhas,
               (SELECT count(*) FROM mes_kanban.production_records) AS registos
    """)
    return rows[0] if rows else {"folhas": 0, "registos": 0}


def fetch_of_detail(of: str) -> dict:
    """Drill-down de uma OF: componentes do plano + folhas validadas."""
    plan = _fetch(f"""
        SELECT l.component_ref AS modelo, l.profile_type AS perfil,
               l.length_mm AS comp_mm, l.quantity_planned AS qtd_planeada,
               l.remaining_quantity AS qtd_restante, l.cutting_machine AS maquina
        FROM core_mtg.production_lines l
        WHERE l.snapshot_id = {_LATEST_MTG} AND l.production_order_no = %s
        ORDER BY l.component_ref
        LIMIT 200
    """, (of,))
    if not plan:
        plan = _fetch(f"""
            SELECT component_ref AS modelo, material_quality AS perfil,
                   thickness_mm AS comp_mm, quantity_plan AS qtd_planeada,
                   cut_remaining_quantity AS qtd_restante, cutting_machine AS maquina
            FROM core_mtg.chapa_components
            WHERE batch_id = {_LATEST_CHAPA_BATCH} AND production_order_no = %s
            ORDER BY component_ref
            LIMIT 200
        """, (of,))
    produced = _fetch("""
        SELECT sheet_uid, row_index, sheet_date, operator_name, machine,
               model_ref, quantity, match_confidence
        FROM mes_kanban.production_records
        WHERE production_order = %s
        ORDER BY sheet_date DESC, sheet_uid, row_index
        LIMIT 200
    """, (of,))
    return {"plan": plan, "produced": produced}


def merge_by_of(plan_rows: list[dict], validated_rows: list[dict]) -> list[dict]:
    """Full outer join por OF. OFs validadas sem plano ficam assinaladas."""
    out: dict[str, dict] = {}
    for p in plan_rows:
        of = str(p["of"])
        planeada = float(p.get("qtd_planeada") or 0)
        restante = float(p.get("qtd_restante") or 0)
        feita = max(planeada - restante, 0.0)
        out[of] = {
            "of": of,
            "cliente": p.get("cliente"),
            "ov": p.get("ov"),
            "familia": p.get("familia"),
            "qtd_planeada": planeada,
            "qtd_restante": restante,
            "progresso": (feita / planeada) if planeada > 0 else None,
            "qtd_validada": 0.0,
            "linhas_validadas": 0,
            "ultima_folha": None,
            "maquinas": p.get("maquinas"),
            "semana": p.get("semana"),
            "sem_plano": False,
        }
    for v in validated_rows:
        of = str(v["of"])
        row = out.get(of)
        if row is None:
            row = out[of] = {
                "of": of,
                "cliente": v.get("cliente"),
                "ov": v.get("ov"),
                "familia": v.get("familia"),
                "qtd_planeada": None,
                "qtd_restante": None,
                "progresso": None,
                "qtd_validada": 0.0,
                "linhas_validadas": 0,
                "ultima_folha": None,
                "maquinas": None,
                "semana": None,
                "sem_plano": True,
            }
        row["qtd_validada"] = float(v.get("qtd_validada") or 0)
        row["linhas_validadas"] = int(v.get("linhas") or 0)
        row["ultima_folha"] = v.get("ultima_folha")
    rows = list(out.values())
    rows.sort(key=lambda r: (r["ultima_folha"] is None,
                             str(r["ultima_folha"] or ""),
                             str(r["cliente"] or "")), reverse=False)
    # atividade recente primeiro; sem atividade no fim, por cliente
    with_activity = sorted((r for r in rows if r["ultima_folha"]),
                           key=lambda r: str(r["ultima_folha"]), reverse=True)
    without = sorted((r for r in rows if not r["ultima_folha"]),
                     key=lambda r: (str(r["cliente"] or "~"), r["of"]))
    return with_activity + without


def filter_rows(rows: list[dict], q: str = "", familia: str = "") -> list[dict]:
    q = (q or "").strip().lower()
    familia = (familia or "").strip().lower()
    out = rows
    if familia in ("chapa", "cantoneiras"):
        out = [r for r in out if (r.get("familia") or "").lower() == familia]
    if q:
        out = [
            r for r in out
            if q in str(r.get("cliente") or "").lower()
            or q in str(r.get("ov") or "").lower()
            or q in str(r.get("of") or "").lower()
        ]
    return out


def load_estado(q: str = "", familia: str = "", of: str = "") -> dict:
    """Ponto de entrada da página. Nunca lança — devolve available/error."""
    try:
        merged = merge_by_of(fetch_plan_rows(), fetch_validated_rows())
        kpis = fetch_mes_kpis()
        detail = fetch_of_detail(of) if of else None
        return {
            "available": True, "error": None,
            "rows": filter_rows(merged, q, familia),
            "total": len(merged),
            "kpis": kpis,
            "detail": detail,
        }
    except Exception as exc:  # Postgres em baixo, schema ausente — mostrar, não rebentar
        msg = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
        return {"available": False, "error": msg, "rows": [], "total": 0,
                "kpis": {"folhas": 0, "registos": 0}, "detail": None}
