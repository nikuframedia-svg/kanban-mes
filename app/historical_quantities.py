"""Production balances frozen before the effective day (never nearest/future).

Independent of matching.history, whose observations only break matching ties.
The persisted basis is shared by review, validation, PG and exports.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from .matching import loaders
from .matching.angle_geometry import profile_key
from .matching.geometry import canonical_code
from .matching.full_profile import expand_entries
from .templates_spec import field_value, is_marked

VERSION = 1


def production_cutoff(value):
    from .pg_store import normalize_sheet_date
    day = normalize_sheet_date(value)
    return datetime.combine(datetime.fromisoformat(day).date(), time.min,
                            ZoneInfo("Europe/Lisbon")).astimezone(timezone.utc)


def select_snapshot(snapshots, value):
    cutoff = production_cutoff(value)
    candidates = []
    for item in snapshots:
        loaded = item.get("loaded_at") or item.get("snapshot_loaded_at")
        if isinstance(loaded, str):
            loaded = datetime.fromisoformat(loaded.replace("Z", "+00:00"))
        if loaded is not None and loaded.tzinfo is not None and loaded < cutoff:
            candidates.append((loaded, str(item["snapshot_id"])))
    if not candidates:
        raise ValueError("Não existe plano guardado antes do dia de produção.")
    loaded, sid = max(candidates)
    return {"snapshot_id": sid, "loaded_at": loaded.isoformat(),
            "cutoff": cutoff.isoformat(), "timezone": "Europe/Lisbon"}


def load_snapshot(value):
    from .pg_store import SOURCE_APP
    condition, params = (("dataset_id = %s", ("ds-met2-perfis",))
                         if SOURCE_APP == "kanban-mes-mtg2" else
                         ("snapshot_id LIKE %s", (loaders._CANTONEIRAS_LIKE,)))
    snapshots = loaders._fetch("SELECT snapshot_id, loaded_at FROM audit_mtg.snapshots WHERE " + condition, params)
    return select_snapshot(snapshots, value)


def load_order(snapshot_id, order):
    from .pg_store import SOURCE_APP
    extra, join = "", ""
    if SOURCE_APP == "kanban-mes-mtg2":
        extra = ", r.row_data ->> 'Ø Ext' AS profile_excel_o"
        join = " LEFT JOIN raw_mtg.plan_production_rows r ON r.snapshot_id=l.snapshot_id AND r.source_line_id=l.plan_key"
    return loaders._fetch(
        "SELECT l.*" + extra + " FROM analytics_mtg.kanban_plan_lines l" + join +
        " WHERE l.source_app=%s AND l.snapshot_id=%s AND l.production_order_no = ANY(%s) ORDER BY l.plan_key",
        (SOURCE_APP, snapshot_id, [order, "OF" + order]))


def context(data, row, decision_ids=()):
    from .pg_store import normalize_sheet_date
    return {"date": normalize_sheet_date((data.get("header") or {}).get("data")),
            "of": canonical_code(row.get("of"), prefix="OF"),
            "profile": profile_key(row.get("perfil")),
            "full": is_marked(field_value(row, "perf_comp")),
            "binding": row.get("_plan_binding"), "decisions": list(decision_ids),
            "unresolved": row.get("_identity_unresolved"), "version": VERSION}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def apply(sheet, data, cross, *, decisions=(), snapshot_loader=None, order_loader=None):
    if sheet.get("status") == "validated":
        return
    snapshot_loader = snapshot_loader or load_snapshot
    order_loader = order_loader or load_order
    previous = {r["row_index"]: r for r in (sheet.get("cross_check") or {}).get("rows", [])}
    selected, orders = None, {}
    for rc in cross.get("rows", []):
        i = rc["row_index"]
        row = data["rows"][i]
        if row.get("_deleted") is True or not is_marked(field_value(row, "perf_comp")):
            continue
        # Only decisions affecting this basis invalidate it, not later plan loads.
        relevant = [e["id"] for e in decisions if e.get("source") == "human" and
                    (e.get("field_path") == "header.data" or
                     str(e.get("field_path", "")).startswith(f"rows[{i}]"))]
        basis = {"version": VERSION, "status": "unavailable"}
        try:
            ctx = context(data, row, relevant)
            signature = fingerprint(ctx)
            basis.update(context=ctx, fingerprint=signature, date=ctx["date"])
            old = previous.get(i, {})
            frozen = old.get("quantity_basis") or {}
            if frozen.get("fingerprint") == signature and frozen.get("status") == "ready":
                for key in ("quantity_basis", "plan_refs", "plan_refs_valid", "plan_refs_error",
                            "full_profile_quantity", "plan_length_mm", "plan_line_meters",
                            "line_meters", "plan_meters_error"):
                    rc[key] = copy.deepcopy(old.get(key))
                continue
            if not ctx["of"] or not ctx["profile"] or ctx["unresolved"]:
                raise ValueError("OF e perfil físico precisam de identificação inequívoca.")
            if not rc.get("matched_plan_key") or rc.get("mode") in {"weak_guess", "no_match"} or rc.get("review_required"):
                raise ValueError("A correspondência da linha precisa de confirmação.")
            if selected is None:
                selected = snapshot_loader(ctx["date"])
            basis.update(selected)
            key = (selected["snapshot_id"], ctx["of"])
            if key not in orders:
                orders[key] = order_loader(*key)
            entries = [e for e in orders[key] if profile_key(e.get("profile_type")) == ctx["profile"]]
            identities = [(canonical_code(e.get("component_ref")), profile_key(e.get("profile_type")),
                           str(e.get("length_mm"))) for e in entries]
            for entry in entries:
                if not canonical_code(entry.get("component_ref")):
                    raise ValueError("Referência histórica sem identidade.")
                quantity = entry.get("remaining_quantity")
                if quantity is not None and not math.isfinite(float(quantity)):
                    raise ValueError("Quantidade histórica inválida.")
            if len(identities) != len(set(identities)):
                raise ValueError("Referências históricas repetidas: identidade ambígua.")
            expanded = expand_entries(entries, selected["snapshot_id"])
            if not expanded["plan_refs_valid"]:
                raise ValueError(expanded["plan_refs_error"])
            rc.update(expanded)
            basis.update(status="ready", diagnostic="last_snapshot_before_production_day")
        except Exception as exc:
            # Failure cannot silently reuse current-plan quantities or invent zero.
            basis["diagnostic"] = str(exc)[:300] if isinstance(exc, ValueError) else "Histórico do plano indisponível. Tenta verificar novamente."
            rc.update(plan_refs=[], plan_refs_valid=False, plan_refs_error=basis["diagnostic"],
                      full_profile_quantity=None, plan_length_mm=None,
                      plan_line_meters=None, line_meters=None)
        rc["quantity_basis"] = basis
    production = [r for r in cross.get("rows", []) if r.get("row_kind", r.get("mode"))
                  not in {"deleted", "empty", "activity"}]
    if any(r.get("quantity_basis") for r in production):
        meters = [r.get("plan_line_meters", r.get("line_meters")) for r in production]
        known = [m for m in meters if m is not None]
        summary = cross.setdefault("summary", {})
        total = round(sum(known), 3) if known else None
        summary.update(metros_teoricos=total, metros_parciais=any(m is None for m in meters))
        measured = summary.get("metros_produzidos")
        summary["desperdicio_m"] = (round(measured-total, 2) if measured is not None
            and total is not None and not summary["metros_parciais"] else None)


def needs_refresh(sheet):
    if sheet.get("status") not in {"extracted", "in_review"}:
        return False
    checks = {r["row_index"]: r for r in (sheet.get("cross_check") or {}).get("rows", [])}
    for i, row in enumerate((sheet.get("sheet_data") or {}).get("rows", [])):
        if row.get("_deleted") is not True and is_marked(field_value(row, "perf_comp")):
            if (checks.get(i, {}).get("quantity_basis") or {}).get("version") != VERSION:
                return True
    return False
