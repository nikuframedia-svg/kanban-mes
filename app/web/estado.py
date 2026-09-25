"""Dados da página Estado — plano de produção vs produção validada, por OF.

Só SELECTs (padrão de app/matching/loaders.py: snapshot/lote mais recente).
Todas as funções degradam graciosamente: Postgres em baixo → {"available": False}.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .. import pg
from ..matching import loaders

_LATEST_CHAPA_BATCH = (
    "(SELECT batch_id FROM audit_mtg.chapa_batches ORDER BY loaded_at DESC LIMIT 1)"
)


def _fetch(sql: str, params: tuple = ()) -> list[dict]:
    return pg.fetch(sql, params if params else None)


def _current_snapshot_id() -> str | None:
    """O mesmo snapshot do índice do plano (uma linha de audit_mtg.snapshots).

    Perguntá-lo à vista kanban_plan_lines obrigava o Postgres a percorrer
    todas as cargas guardadas de perfis e cantoneiras (~1,2 s medidos).
    """
    return (loaders.plan_snapshot_info() or {}).get("snapshot_id")


def fetch_plan_rows(snapshot_id: str | None = None) -> list[dict]:
    """Plano agregado por OF, ambas as famílias, snapshot mais recente."""
    snapshot_id = snapshot_id or _current_snapshot_id()
    cantoneiras = _fetch("""
        SELECT max(customer_name) AS cliente, max(sales_order_no) AS ov,
               production_order_no AS of, 'cantoneiras' AS familia,
               CASE WHEN bool_and(quantity_planned IS NOT NULL)
                    THEN sum(quantity_planned) ELSE NULL END AS qtd_planeada,
               CASE WHEN bool_and(remaining_valid IS TRUE)
                    THEN sum(remaining_quantity) ELSE NULL END AS qtd_restante,
               string_agg(DISTINCT cutting_machine, ', '
                          ORDER BY cutting_machine) AS maquinas,
               min(planning_week) AS semana
        FROM analytics_mtg.kanban_plan_lines
        WHERE source_app = 'kanban-mes'
          AND snapshot_id = %s
        GROUP BY production_order_no
    """, (snapshot_id,)) if snapshot_id else []
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
        SELECT p.production_order AS of, p.family AS familia,
               max(p.customer_name) AS cliente, max(p.sales_order) AS ov,
               sum(p.quantity) AS qtd_validada, count(*) AS linhas,
               max(p.sheet_date) AS ultima_folha
        FROM mes_kanban.production_records p
        JOIN mes_kanban.validated_sheets s ON s.sheet_uid = p.sheet_uid
        WHERE s.source_app = 'kanban-mes'
          AND p.production_order IS NOT NULL
        GROUP BY p.production_order, p.family
    """)


def fetch_mes_kpis() -> dict:
    rows = _fetch("""
        SELECT (SELECT count(*) FROM mes_kanban.validated_sheets
                WHERE source_app = 'kanban-mes') AS folhas,
               (SELECT count(*) FROM mes_kanban.production_records p
                JOIN mes_kanban.validated_sheets s ON s.sheet_uid = p.sheet_uid
                WHERE s.source_app = 'kanban-mes') AS registos
    """)
    return rows[0] if rows else {"folhas": 0, "registos": 0}


def fetch_of_detail(of: str, snapshot_id: str | None = None) -> dict:
    """Drill-down de uma OF: componentes do plano + folhas validadas."""
    snapshot_id = snapshot_id or _current_snapshot_id()
    plan = _fetch("""
        SELECT component_ref AS modelo, profile_type AS perfil,
               length_mm AS comp_mm, quantity_planned AS qtd_planeada,
               CASE WHEN remaining_valid IS TRUE THEN remaining_quantity
                    ELSE NULL END AS qtd_restante,
               overproduction_quantity AS excesso,
               quantity_made AS qtd_feita, remaining_valid,
               cutting_machine AS maquina, planning_week AS semana,
               plan_key, snapshot_id
        FROM analytics_mtg.kanban_plan_lines
        WHERE source_app = 'kanban-mes'
          AND snapshot_id = %s
          AND production_order_no = %s
        ORDER BY component_ref, plan_key
        LIMIT 200
    """, (snapshot_id, of)) if snapshot_id else []
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
        SELECT p.sheet_uid, s.sheet_no, p.row_index, p.sheet_date,
               p.operator_name, p.machine, p.model_ref, p.quantity,
               p.match_confidence
        FROM mes_kanban.production_records p
        JOIN mes_kanban.validated_sheets s ON s.sheet_uid = p.sheet_uid
        WHERE s.source_app = 'kanban-mes' AND p.production_order = %s
        ORDER BY p.sheet_date DESC, p.sheet_uid, p.row_index
        LIMIT 200
    """, (of,))
    return {"plan": plan, "produced": produced}


def merge_by_of(plan_rows: list[dict], validated_rows: list[dict]) -> list[dict]:
    """Full outer join por OF. OFs validadas sem plano ficam assinaladas."""
    out: dict[str, dict] = {}
    for p in plan_rows:
        of = str(p["of"])
        planeada = (float(p["qtd_planeada"])
                    if p.get("qtd_planeada") is not None else None)
        restante = (float(p["qtd_restante"])
                    if p.get("qtd_restante") is not None else None)
        feita = (
            max(planeada - restante, 0.0)
            if planeada is not None and restante is not None else None
        )
        out[of] = {
            "of": of,
            "cliente": p.get("cliente"),
            "ov": p.get("ov"),
            "familia": p.get("familia"),
            "qtd_planeada": planeada,
            "qtd_restante": restante,
            "progresso": (
                feita / planeada
                if feita is not None and planeada is not None and planeada > 0
                else None
            ),
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


# Acima disto o plano está a ficar velho. 36 h = duas passagens do cron
# falhadas; 72 h = ninguém grava o ficheiro no Drive há três dias.
FRESCURA_AVISO_HORAS = 36
FRESCURA_ALERTA_HORAS = 72

_FONTES = (
    ("plano de produção", "^mtg_", None),
    ("lista de colaboradores", None, "ds-colaboradores"),
)


def fetch_fontes() -> list[dict]:
    """Idade de cada referência que a app consome.

    O ponto 7 do cliente — «o planeamento é actualizado diariamente» — não é
    código nosso: o cron já corre duas vezes por dia e recarrega minutos depois
    de o ficheiro mudar. O que nos cabe é dizer quando isso não aconteceu.
    """
    out = []
    for nome, like, dataset in _FONTES:
        if like:
            sql = ("SELECT snapshot_id, loaded_at FROM audit_mtg.snapshots "
                   "WHERE snapshot_id ~ %s ORDER BY loaded_at DESC LIMIT 1")
            params = (like,)
        else:
            sql = ("SELECT snapshot_id, loaded_at FROM audit_mtg.snapshots "
                   "WHERE dataset_id = %s ORDER BY loaded_at DESC LIMIT 1")
            params = (dataset,)
        try:
            rows = _fetch(sql, params)
        except Exception:
            rows = []
        if not rows:
            out.append({"nome": nome, "horas": None, "estado": "ausente"})
            continue
        loaded = rows[0]["loaded_at"]
        horas = max(0.0, (datetime.now(timezone.utc) - loaded).total_seconds() / 3600.0)
        estado = ("crit" if horas > FRESCURA_ALERTA_HORAS
                  else "warn" if horas > FRESCURA_AVISO_HORAS else "ok")
        out.append({"nome": nome, "horas": horas, "estado": estado,
                    "snapshot": rows[0]["snapshot_id"]})
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
            "fontes": fetch_fontes(),
        }
    except Exception as exc:  # Postgres em baixo, schema ausente — mostrar, não rebentar
        msg = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
        return {"available": False, "error": msg, "rows": [], "total": 0,
                "kpis": {"folhas": 0, "registos": 0}, "detail": None, "fontes": []}
