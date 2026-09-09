"""Small, snapshot-bound plan queries for the review UI (no global index)."""
from __future__ import annotations

from .. import pg_store
from ..matching import carryover, loaders, similarity as sim
from ..matching.full_profile import expand_entries
from ..matching.angle_geometry import profile_key
from ..templates_spec import field_value, is_marked

SOURCE_APP = pg_store.SOURCE_APP
IS_MTG2 = SOURCE_APP == "kanban-mes-mtg2"


def _select() -> str:
    extra = ", r.row_data ->> 'Ø Ext' AS profile_excel_o" if IS_MTG2 else ", NULL AS profile_excel_o"
    join = (" JOIN raw_mtg.plan_production_rows r ON r.snapshot_id=l.snapshot_id "
            "AND r.source_line_id=l.plan_key") if IS_MTG2 else ""
    return """SELECT l.plan_key, l.snapshot_id, l.production_order_no,
        l.sales_order_no, l.customer_name, l.component_ref, l.profile_type,
        l.material_description, l.length_mm, l.quantity_planned, l.quantity_made,
        l.remaining_quantity, l.remaining_valid, l.remaining_rule,
        l.overproduction_quantity, l.closed_x, l.cutting_machine, l.planning_week
        """ + extra + " FROM analytics_mtg.kanban_plan_lines l" + join


def order_codes(value: str, prefix: str = "OF") -> list[str]:
    code = sim.normalize_code(sim.strip_ref_prefix(value))
    return list(dict.fromkeys([prefix + code, code])) if code else []


def fetch_order(snapshot_id: str, of: str) -> list[dict]:
    if not snapshot_id or not of:
        return []
    return loaders._fetch(
        _select() + " WHERE l.source_app=%s AND l.snapshot_id=%s "
        "AND l.production_order_no = ANY(%s) ORDER BY l.profile_type, l.component_ref, l.plan_key",
        (SOURCE_APP, snapshot_id, order_codes(of)),
    )


def fetch_keys(keys: list[str], snapshot_id: str | None = None) -> list[dict]:
    if not keys:
        return []
    sql = _select() + " WHERE l.source_app=%s AND l.plan_key = ANY(%s)"
    params: list = [SOURCE_APP, keys]
    if snapshot_id:
        sql += " AND l.snapshot_id=%s"
        params.append(snapshot_id)
    return loaders._fetch(sql, tuple(params))


def same_profile(left, right) -> bool:
    # Geometry keeps the wall thickness; the display value from Excel O is
    # deliberately never used as a grouping key.
    return bool(left and right and profile_key(left) == profile_key(right))


def lookup(snapshot_id: str, q: str, *, include_done: bool = False, offset: int = 0) -> dict:
    q = q.strip()[:160]
    result = {"snapshot_id": snapshot_id, "q": q, "mode": "none", "entries": [],
              "found": False, "offset": offset, "has_more": False}
    if not q:
        return result
    prefix = q.upper().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    for mode, condition, value in (
        ("of", "l.production_order_no = ANY(%s)", order_codes(q)),
        ("ov", "l.sales_order_no = ANY(%s)", order_codes(q, "OV")),
        ("modelo", "upper(l.component_ref) LIKE %s", prefix),
    ):
        base = " WHERE l.source_app=%s AND l.snapshot_id=%s AND " + condition
        params = (SOURCE_APP, snapshot_id, value)
        exists = loaders._fetch("SELECT 1 FROM analytics_mtg.kanban_plan_lines l" + base + " LIMIT 1", params)
        if not exists:
            continue
        active = "" if include_done else " AND (l.remaining_quantity > 0 OR NOT l.remaining_valid)"
        entries = loaders._fetch(
            _select() + base + active +
            " ORDER BY l.remaining_quantity ASC NULLS LAST, l.production_order_no, l.profile_type, l.component_ref, l.plan_key LIMIT 51 OFFSET %s",
            params + (offset,),
        )
        result.update(mode=mode, found=bool(entries), entries=entries[:50], has_more=len(entries) > 50)
        return result
    return result


def display_ref(ref: dict) -> dict:
    line = dict(ref)
    before = sim.parse_number(ref.get("remaining_before"))
    made = sim.parse_number(ref.get("assumed_quantity"))
    line.update(quantity_made=ref.get("quantity_made_before"), remaining_quantity=before,
                made_in_sheet=made, remaining_after=max(before - made, 0) if before is not None and made is not None else None,
                remaining_valid=made is not None, overproduction_quantity=ref.get("overproduction_before"))
    return line


def totals(lines: list[dict]) -> dict:
    result = {"parcial": False}
    for name, key in (("planeada", "quantity_planned"), ("feita", "quantity_made"),
                      ("falta", "remaining_quantity"), ("excesso", "overproduction_quantity"),
                      ("nesta_folha", "made_in_sheet"), ("depois", "remaining_after")):
        values = [sim.parse_number(line.get(key)) for line in lines]
        known = [v for v in values if v is not None]
        result[name] = sum(known) if known else None
        if known and len(known) != len(lines):
            result["parcial"] = True
    return result


def context(sheet: dict, row_index: int, template, back: str) -> dict:
    rows = (sheet.get("sheet_data") or {}).get("rows") or []
    row = rows[row_index]
    full = is_marked(field_value(row, "perf_comp"))
    cross = sheet.get("cross_check") or {}
    check = next((r for r in cross.get("rows", []) if r.get("row_index") == row_index), {})
    ctx = {"uid": sheet["uid"], "row_index": row_index,
           "row_number": sum(r.get("_deleted") is not True for r in rows[:row_index + 1]),
           "readonly": sheet["status"] == "validated", "revision": sheet["revision"],
           "back_url": back, "origem_perf_comp": full, "all_of": False,
           "linhas": [], "erro": None, "plano": {}, "totais": None,
           "of": row.get("of"), "perfil": row.get("perfil"), "mtg2": IS_MTG2,
           "current_plan_key": (row.get("_plan_binding") or {}).get("plan_key") if not full else None}
    if not template.index_loader:
        ctx["erro"] = "Esta folha não cruza com o plano."
        return ctx
    if ctx["readonly"] and full:
        if not check.get("plan_refs"):
            from .export_source import prepare_sheets
            sheet = prepare_sheets([sheet])[0]
            check = next((r for r in sheet["cross_check"]["rows"] if r["row_index"] == row_index), {})
        ctx["linhas"] = [display_ref(ref) for ref in check.get("plan_refs", [])]
        ctx["plano"] = {"snapshot_id": cross.get("snapshot_id"), "frozen": True}
        ctx["totais"] = totals(ctx["linhas"])
        return ctx
    identity = check.get("plan_identity") or {}
    if ctx["readonly"] and identity:
        ctx["linhas"] = [dict(identity)]
        ctx["plano"] = {"snapshot_id": identity.get("snapshot_id"), "frozen": True}
        return ctx
    info = ({"snapshot_id": cross.get("validation_snapshot_id") or cross.get("snapshot_id")}
            if ctx["readonly"] else loaders.plan_snapshot_info())
    snapshot_id = str(info.get("snapshot_id") or "")
    ctx["plano"] = info
    if not snapshot_id:
        ctx["erro"] = "Não foi possível identificar o planeamento desta folha."
        return ctx
    inherited = carryover.resolve(rows, tuple(f for f in template.row_fields if f not in carryover.CARRY_FIELDS), {})
    effective = carryover.effective_row(row, inherited[row_index])
    of, profile = str(effective.get("of") or "").strip(), str(effective.get("perfil") or "").strip()
    ctx.update(of=of, perfil=profile, herdou_of=inherited[row_index].is_inherited("of"))
    if not of:
        ctx["erro"] = "Esta linha não tem OF. Usa Corrigir via OF para escolher uma referência."
        return ctx
    lines = fetch_order(snapshot_id, of)
    group = [line for line in lines if same_profile(line.get("profile_type"), profile)]
    ctx["all_of"] = not bool(group)
    if full and group:
        expanded = expand_entries(group, snapshot_id)
        ctx["linhas"] = [display_ref(ref) for ref in expanded["plan_refs"]]
    else:
        ctx["linhas"] = group or lines
        if full and not group:
            ctx["erro"] = "O perfil não pertence a esta OF. Usa Corrigir via OF para escolher o perfil completo."
    ctx["totais"] = totals(ctx["linhas"])
    return ctx
