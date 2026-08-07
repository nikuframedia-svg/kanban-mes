"""A ÚNICA porta de escrita para o Postgres.

Chamado exclusivamente no ato de validação humana: insere a folha validada e as
suas linhas em mes_kanban (append-only). Tudo o resto da app só lê do Postgres.
"""

from __future__ import annotations

import json
import os

import psycopg

from .config import settings
from .matching import similarity as sim
from .templates_spec import KanbanTemplate

APP_VERSION = "kanban-mes 0.1.0"

# campos da linha kanban → colunas de mes_kanban.production_records
_FIELD_TO_COLUMN = {
    "of": "production_order",
    "ov": "sales_order",
    "cliente": "customer_name",
    "modelo": "model_ref",
    "nesting": "model_ref",
    "qtd": "quantity",
    "repeticoes": "quantity",
    "comp_mm": "length_mm",
    "larg_mm": "width_mm",
    "esp": "thickness_mm",
    "lote": "lot_ref",
    "sucata": "scrap",
}
_NUMERIC_COLUMNS = {"quantity", "length_mm", "width_mm", "thickness_mm"}


def _dsn() -> str:
    return os.environ.get("MES_PG_DSN") or settings.pg_dsn


def _store_stoppages(cur, sheet: dict, header: dict, filled: list,
                     sheet_date: str | None, operator: str) -> int:
    """Linhas do verso da folha (paragens) → mes_kanban.stoppage_records."""
    machine = str(header.get("setor_maquina") or "").strip() or None
    n = 0
    for i, row in filled:
        cur.execute(
            """
            INSERT INTO mes_kanban.stoppage_records
                (sheet_uid, row_index, sheet_date, machine, operator_name,
                 motivo, inicio, fim, duracao_horas, resolvido, validated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            """,
            (
                sheet["uid"], i, sheet_date, machine,
                operator or "(desconhecido)",
                str(row.get("motivo") or "").strip() or None,
                str(row.get("inicio") or "").strip() or None,
                str(row.get("fim") or "").strip() or None,
                sim.parse_number(row.get("duracao")),
                str(row.get("resolvido") or "").strip() or None,
            ),
        )
        n += 1
    return n


def store_validated_sheet(sheet: dict, template: KanbanTemplate,
                          edit_count: int, actor: str) -> int:
    """Insere a folha + linhas. Devolve o nº de linhas de produção gravadas.
    Lança em caso de erro — o chamador não deve marcar a folha como validada
    sem este INSERT ter sido confirmado."""
    data = sheet["sheet_data"] or {}
    header = data.get("header") or {}
    rows = data.get("rows") or []
    footer = data.get("footer") or {}
    cross = sheet.get("cross_check") or {}
    cross_rows = {r["row_index"]: r for r in cross.get("rows", [])}

    sheet_date = str(header.get("data") or "")[:10] or None
    operator = str(header.get("operador") or "").strip()
    # valor de folha (rodapé), desnormalizado para cada linha — é a única
    # coluna de horas no schema
    hours_worked = sim.parse_number(footer.get("horas_trabalhadas"))

    # linhas com conteúdo (pelo menos um campo preenchido)
    filled = [
        (i, row) for i, row in enumerate(rows)
        if any(v is not None and str(v).strip() != "" for v in row.values())
    ]

    with psycopg.connect(_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO mes_kanban.validated_sheets
                    (sheet_uid, sheet_date, template_name, family, operator_name,
                     operator_no, sector_machine, shift, image_sha256,
                     raw_extraction, sheet_data, cross_check, edit_count,
                     validated_by, app_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    sheet["uid"], sheet_date, template.name, template.family,
                    operator or "(desconhecido)",
                    str(header.get("n_operador") or "") or None,
                    str(header.get("setor_maquina") or "") or None,
                    str(header.get("turno") or "") or None,
                    sheet.get("image_sha256") or "",
                    json.dumps(sheet.get("raw_extraction") or {}, ensure_ascii=False, default=str),
                    json.dumps(data, ensure_ascii=False, default=str),
                    json.dumps(cross, ensure_ascii=False, default=str),
                    edit_count, actor, APP_VERSION,
                ),
            )
            if template.name == "cantoneiras_paragens":
                n = _store_stoppages(cur, sheet, header, filled, sheet_date, operator)
                conn.commit()
                return n
            n = 0
            for i, row in filled:
                cr = cross_rows.get(i) or {}
                cols: dict[str, object] = {}
                extra: dict[str, object] = {}
                for f, value in row.items():
                    if value is None or str(value).strip() == "":
                        continue
                    col = _FIELD_TO_COLUMN.get(f)
                    if col is None:
                        extra[f] = value
                    elif col in _NUMERIC_COLUMNS:
                        cols[col] = sim.parse_number(value)
                    else:
                        cols.setdefault(col, str(value).strip())
                machine = row.get("maquina") or header.get("setor_maquina")
                cur.execute(
                    """
                    INSERT INTO mes_kanban.production_records
                        (sheet_uid, row_index, sheet_date, family, operator_name,
                         machine, production_order, sales_order, customer_name,
                         model_ref, matched_plan_key, match_confidence,
                         quantity, length_mm, width_mm, thickness_mm, lot_ref,
                         scrap, hours_worked, extra, validated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, now())
                    """,
                    (
                        sheet["uid"], i, sheet_date, template.family,
                        operator or "(desconhecido)",
                        str(machine).strip() if machine else None,
                        cols.get("production_order"), cols.get("sales_order"),
                        cols.get("customer_name"), cols.get("model_ref"),
                        cr.get("matched_plan_key"), cr.get("p_correct"),
                        cols.get("quantity"), cols.get("length_mm"),
                        cols.get("width_mm"), cols.get("thickness_mm"),
                        cols.get("lot_ref"), cols.get("scrap"),
                        hours_worked,
                        json.dumps(extra, ensure_ascii=False, default=str) if extra else None,
                    ),
                )
                n += 1
        conn.commit()
    return n
