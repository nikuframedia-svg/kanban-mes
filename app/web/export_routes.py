"""Workbook exports from the validated archive and optional local drafts."""
from __future__ import annotations

import io

from openpyxl import Workbook

from .. import db, pg_store, production_facts
from ..matching import similarity as sim
from ..templates_spec import get_template
from . import export as excel
from . import export_source, plan_review


def facts_for(sheet):
    facts = (production_facts.materialize_sheet(sheet) if plan_review.IS_MTG2
             else production_facts.materialize_sheet(sheet, get_template(sheet["template_name"]))["exports"])
    if plan_review.IS_MTG2:
        for parent in facts:
            for row, cross in parent["export_rows"]:
                yield parent["row_index"], row, cross
    else:
        for fact in facts:
            yield fact["row_index"], fact["row"], fact["cross"]


def export_sheets(conn_fn, de="", ate="", operador="", *, drafts=False):
    for value in (de, ate):
        if value:
            pg_store.normalize_sheet_date(value)
    sheets = export_source.load_validated_sheets(de, ate, operador)
    seen = {sheet["uid"] for sheet in sheets}
    if drafts:
        conn = conn_fn()
        try:
            for meta in db.list_sheets(conn):
                if meta["status"] == "validated" or meta["uid"] in seen or "paragens" in meta["template_name"]:
                    continue
                sheet = db.get_sheet(conn, meta["uid"])
                if not sheet or not sheet.get("sheet_data"):
                    continue
                header = sheet["sheet_data"].get("header") or {}
                if operador and str(header.get("operador") or "").strip() != operador:
                    continue
                try:
                    date = pg_store.normalize_sheet_date(header.get("data"))
                except pg_store.InvalidSheetDate:
                    date = None
                if (de and (not date or date < de)) or (ate and (not date or date > ate)):
                    continue
                sheets.append(sheet)
        finally:
            conn.close()
    return export_source.prepare_sheets(sheets) if drafts else sheets


def workbook(kind, sheets):
    rows = []
    converter = excel.basedados_row_for if kind == "basedados" else excel.cpis_row_for
    for sheet in sheets:
        header = sheet["sheet_data"].get("header") or {}
        try:
            date = pg_store.normalize_sheet_date(header.get("data")) or "9999"
        except pg_store.InvalidSheetDate:
            date = "9999"
        for i, row, cross in facts_for(sheet):
            rows.append(((date, str(header.get("operador") or ""), int(sheet.get("sheet_no") or 0),
                          sheet["uid"], i, str(cross.get("matched_plan_key") or "")),
                         converter(sheet, row, cross, None)))
    values = [row for _, row in sorted(rows, key=lambda item: item[0])]
    return excel.build_basedados_workbook(values) if kind == "basedados" else excel.build_cpis_workbook(values)


TECHNICAL_COLUMNS = (
    "source_app", "sheet_no", "sheet_uid", "row_index", "sheet_date", "family",
    "operator_name", "machine", "production_order", "sales_order", "customer_name",
    "profile_type", "model_ref", "quantity", "length_mm", "quantity_total_mm",
    "duration_text", "lot_ref", "plan_length_mm", "line_meters", "matched_plan_key",
    "match_confidence", "width_mm", "thickness_mm", "hours_worked", "validated_at",
    "profile_excel_o", "material_description",
)


def technical_workbook(sheets):
    wb = Workbook()
    ws = wb.active
    ws.title = "Producao"
    ws.append(list(TECHNICAL_COLUMNS))
    for sheet in sheets:
        data = sheet.get("sheet_data") or {}
        header, footer = data.get("header") or {}, data.get("footer") or {}
        for i, row, cross in facts_for(sheet):
            identity = cross.get("plan_identity") or {}
            values = {
                "source_app": pg_store.SOURCE_APP, "sheet_no": sheet.get("sheet_no"),
                "sheet_uid": sheet["uid"], "row_index": i, "sheet_date": header.get("data"),
                "family": get_template(sheet["template_name"]).family,
                "operator_name": header.get("operador"), "machine": header.get("setor_maquina"),
                "production_order": sim.strip_ref_prefix(row.get("of")),
                "sales_order": sim.strip_ref_prefix(row.get("ov")), "customer_name": row.get("cliente"),
                "profile_type": row.get("perfil"), "model_ref": row.get("modelo"),
                "quantity": sim.parse_number(row.get("qtd")), "length_mm": cross.get("plan_length_mm"),
                "quantity_total_mm": sim.parse_number(row.get("qtd_total_mm")),
                "duration_text": row.get("duracao"), "lot_ref": row.get("n_corte_lote") or row.get("lote"),
                "plan_length_mm": cross.get("plan_length_mm"), "line_meters": cross.get("plan_line_meters", cross.get("line_meters")),
                "matched_plan_key": cross.get("matched_plan_key"), "match_confidence": cross.get("p_correct"),
                "width_mm": sim.parse_number(row.get("larg_mm")), "thickness_mm": sim.parse_number(row.get("esp")),
                "hours_worked": sim.parse_number(footer.get("horas_trabalhadas")),
                "validated_at": str(sheet.get("validated_at") or "") or None,
                "profile_excel_o": identity.get("profile_excel_o"), "material_description": identity.get("material_description"),
            }
            ws.append([excel.neutralize_xlsx(values.get(key)) for key in TECHNICAL_COLUMNS])
    ws.freeze_panes = "A2"
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
