"""Small, snapshot-bound plan queries for the review UI (no global index)."""
from __future__ import annotations

from .. import pg_store
from ..matching import carryover, loaders, similarity as sim
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
    length = sim.parse_number(ref.get("length_mm"))
    meters = (
        round(made * length / 1000.0, 3)
        if made is not None and made >= 0 and length is not None
        else None
    )
    meters_error = None
    if made is None:
        meters_error = "Quantidade em falta desconhecida"
    elif made > 0 and length is None:
        meters_error = (
            "Comprimento em falta em "
            + str(ref.get("component_ref") or ref.get("plan_key") or "referência")
        )
    line.update(quantity_made=ref.get("quantity_made_before"), remaining_quantity=before,
                made_in_sheet=made, remaining_after=max(before - made, 0) if before is not None and made is not None else None,
                remaining_valid=made is not None,
                overproduction_quantity=ref.get("overproduction_before"),
                made_meters=meters, meters_error=meters_error)
    return line


def totals(lines: list[dict]) -> dict:
    result = {"parcial": False, "meters_errors": []}
    for name, key in (("planeada", "quantity_planned"), ("feita", "quantity_made"),
                      ("falta", "remaining_quantity"), ("excesso", "overproduction_quantity"),
                      ("nesta_folha", "made_in_sheet"), ("depois", "remaining_after")):
        values = [sim.parse_number(line.get(key)) for line in lines]
        known = [v for v in values if v is not None]
        result[name] = sum(known) if known else None
        if known and len(known) != len(lines):
            result["parcial"] = True
    meter_values = [sim.parse_number(line.get("made_meters")) for line in lines]
    positive = [line for line in lines if (sim.parse_number(line.get("made_in_sheet")) or 0) > 0]
    missing_positive = [line for line in positive if line.get("made_meters") is None]
    known_meters = [value for value in meter_values if value is not None]
    result["metros"] = (
        round(sum(known_meters), 3)
        if known_meters and not missing_positive
        else (0.0 if not positive else None)
    )
    result["meters_errors"] = list(dict.fromkeys(
        line.get("meters_error") for line in lines if line.get("meters_error")
    ))
    if missing_positive:
        result["parcial"] = True
    return result


def context(sheet: dict, row_index: int, template, back: str, scope: str = "profile", *, running=False) -> dict:
    if scope not in {"profile", "of"}:
        raise ValueError("Âmbito de referências inválido.")
    rows = (sheet.get("sheet_data") or {}).get("rows") or []
    row = rows[row_index]
    full = is_marked(field_value(row, "perf_comp"))
    cross = sheet.get("cross_check") or {}
    check = next((r for r in cross.get("rows", []) if r.get("row_index") == row_index), {})
    inherited = carryover.resolve(rows, tuple(f for f in template.row_fields if f not in carryover.CARRY_FIELDS), {})
    effective = carryover.effective_row(row, inherited[row_index])
    of, profile = str(effective.get("of") or "").strip(), str(effective.get("perfil") or "").strip()
    ordered = sorted((i for i, r in enumerate(rows) if r.get("_deleted") is not True),
                     key=lambda i: rows[i].get("_paper_position", i + 1))
    ctx = {"uid": sheet["uid"], "row_index": row_index, "row_number": ordered.index(row_index) + 1,
           "readonly": sheet["status"] == "validated", "revision": sheet["revision"],
           "back_url": back, "origem_perf_comp": full, "all_of": scope == "of", "scope": scope,
           "linhas": [], "erro": None, "plano": {}, "totais": None,
           "of": of, "perfil": profile, "mtg2": IS_MTG2,
           "herdou_of": inherited[row_index].is_inherited("of"),
           "production_values": False, "consultation_only": False,
           "current_plan_key": (row.get("_plan_binding") or {}).get("plan_key") if not full else None}
    if not template.index_loader:
        ctx["erro"] = "Esta folha não cruza com o plano."
        return ctx
    if not of:
        ctx["erro"] = "Esta linha não tem OF. Usa Corrigir via OF para escolher uma referência."
        return ctx
    saved = []
    if full and ctx["readonly"]:
        if not check.get("plan_refs"):
            from .export_source import prepare_sheets
            archived = prepare_sheets([sheet])[0]
            check = next((r for r in archived["cross_check"]["rows"] if r["row_index"] == row_index), {})
        saved = [display_ref(ref) for ref in check.get("plan_refs", [])]
        ctx["plano"] = {"snapshot_id": cross.get("validation_snapshot_id") or cross.get("snapshot_id"),
                        **(check.get("quantity_basis") or {}), "frozen": True}
        ctx["production_values"] = True
    elif full:
        basis = check.get("quantity_basis") or {}
        # A corrected physical profile must not display facts for the old profile.
        basis_profile = (basis.get("context") or {}).get("profile")
        ready = basis.get("status") == "ready" and (not basis_profile or basis_profile == profile_key(profile))
        if ready:
            saved = [display_ref(ref) for ref in check.get("plan_refs", [])]
            ctx["plano"] = {**basis, "frozen": True}
            ctx["production_values"] = True
        else:
            ctx["consultation_only"] = True
            ctx["erro"] = ("O saldo histórico está a ser verificado automaticamente." if running else
                           check.get("plan_refs_error") or "Saldo histórico indisponível; a produção deste perfil ainda não pode ser validada.")
            ctx["plano"] = {**loaders.plan_snapshot_info(), "consultation": True}
    elif ctx["readonly"]:
        identity = check.get("plan_identity") or {}
        ctx["plano"] = {"snapshot_id": identity.get("snapshot_id") or cross.get("validation_snapshot_id") or cross.get("snapshot_id"), "frozen": True}
    else:
        ctx["plano"] = loaders.plan_snapshot_info()

    # Frozen profile groups are complete and remain usable when PG is offline.
    if ctx["production_values"] and scope == "profile":
        ctx["linhas"] = saved
    else:
        snapshot = str(ctx["plano"].get("snapshot_id") or "")
        if not snapshot:
            ctx["erro"] = "Não foi possível identificar o planeamento desta folha."
            return ctx
        lines = fetch_order(snapshot, of)
        ctx["linhas"] = lines if scope == "of" else [line for line in lines if same_profile(line.get("profile_type"), profile)]
        if saved:
            by_key = {line["plan_key"]: line for line in saved}
            ctx["linhas"] = [by_key.get(line.get("plan_key"), line) for line in ctx["linhas"]]
        if scope == "profile" and not ctx["linhas"]:
            ctx["erro"] = (ctx["erro"] + " " if ctx["erro"] else "") + "Não há referências deste perfil nesta OF. Podes consultar toda a OF."
    ctx["totais"] = totals(saved if ctx["production_values"] else ctx["linhas"])
    return ctx
