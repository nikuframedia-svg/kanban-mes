"""Read the validated archive and recover legacy facts without changing it."""
from __future__ import annotations

from copy import deepcopy

from .. import pg_store
from ..matching import carryover, loaders, similarity as sim
from ..matching.full_profile import expand_entries, plan_identity
from ..templates_spec import field_value, is_marked
from . import plan_review


class IncompleteExport(ValueError):
    def __init__(self, problems: list[dict]):
        self.problems = problems
        super().__init__("Não é possível reconstruir todas as referências da produção validada.")


def load_validated_sheets(de: str = "", ate: str = "", operador: str = "") -> list[dict]:
    conditions = ["source_app=%s", "template_name NOT LIKE '%%paragens%%'"]
    params: list = [pg_store.SOURCE_APP]
    for clause, value in (("sheet_date >= %s", de), ("sheet_date <= %s", ate),
                          ("operator_name = %s", operador)):
        if value:
            conditions.append(clause)
            params.append(value)
    sheets = loaders._fetch(
        "SELECT sheet_uid AS uid, sheet_no, template_name, sheet_data, cross_check, "
        "plan_snapshot_id, validated_at, 'validated' AS status FROM mes_kanban.validated_sheets WHERE "
        + " AND ".join(conditions) + " ORDER BY sheet_date, operator_name, sheet_no, sheet_uid",
        tuple(params),
    )
    for sheet in sheets:
        sheet["revision"] = (sheet.get("cross_check") or {}).get("materialized_revision", 0)
    return prepare_sheets(sheets)


def _chunks(values, size=400):
    values = list(values)
    for start in range(0, len(values), size):
        yield values[start:start + size]


def prepare_sheets(sheets: list[dict]) -> list[dict]:
    """Prefer frozen JSON, then PG children, then an explicitly saved snapshot.

    Missing historical quantities are a diagnostic, never today's remaining
    quantity. Existing zero quantities and metadata are immutable.
    """
    prepared = deepcopy(sheets)
    slots, missing_full, keys = [], [], set()
    for sheet in prepared:
        cross = sheet.setdefault("cross_check", {}) or {}
        sheet["cross_check"] = cross
        checks = {r["row_index"]: r for r in cross.setdefault("rows", [])}
        rows = (sheet.get("sheet_data") or {}).get("rows") or []
        inherited = carryover.resolve(rows, (), {})
        is_v3 = "cross-v3" in (cross.get("engine"), cross.get("engine_version"), cross.get("version"))
        for i, original_row in enumerate(rows):
            row = original_row if is_v3 else carryover.effective_row(original_row, inherited[i])
            if row.get("_deleted") is True:
                continue
            check = checks.get(i)
            if check is None:
                check = {"row_index": i}
                cross["rows"].append(check)
            full = is_marked(field_value(row, "perf_comp"))
            slots.append((sheet, i, row, check, full))
            if full and not check.get("plan_refs"):
                missing_full.append((sheet, i, row, check))
            if not check.get("plan_identity") and check.get("matched_plan_key"):
                keys.add(str(check["matched_plan_key"]))
            for ref in check.get("plan_refs") or []:
                if "material_description" not in ref or (plan_review.IS_MTG2 and "profile_excel_o" not in ref):
                    keys.add(str(ref.get("plan_key") or ""))
    children = {}
    for uids in _chunks({sheet["uid"] for sheet, *_ in missing_full}):
        for child in loaders._fetch(
            "SELECT c.* FROM mes_kanban.production_record_plan_refs c "
            "JOIN mes_kanban.validated_sheets v ON v.sheet_uid=c.sheet_uid "
            "WHERE v.source_app=%s AND c.sheet_uid = ANY(%s) ORDER BY c.row_index,c.plan_key",
            (pg_store.SOURCE_APP, uids),
        ):
            identity = (child.get("extra") or {}).get("plan_identity") or {}
            ref = {**identity, **child, "snapshot_id": child.get("plan_snapshot_id")}
            children.setdefault((child["sheet_uid"], child["row_index"]), []).append(ref)
            if "material_description" not in ref or (plan_review.IS_MTG2 and "profile_excel_o" not in ref):
                keys.add(str(ref.get("plan_key") or ""))
    matches = {}
    for chunk in _chunks(keys - {""}):
        for entry in plan_review.fetch_keys(chunk):
            matches.setdefault(str(entry["plan_key"]), []).append(entry)
    # A historical key without its snapshot is usable only if it is unique.
    by_key = {key: entries[0] for key, entries in matches.items() if len(entries) == 1}
    order_cache, problems = {}, []
    for sheet, i, row, check, full in slots:
        entry = by_key.get(str(check.get("matched_plan_key")))
        if not check.get("plan_identity") and entry:
            check["plan_identity"] = plan_identity(entry)
        if not full:
            continue
        refs = check.get("plan_refs") or children.get((sheet["uid"], i))
        if not refs:
            cross = sheet["cross_check"]
            snapshot = (cross.get("validation_snapshot_id") or cross.get("snapshot_id")
                        or sheet.get("plan_snapshot_id") or (cross.get("plan_reference") or {}).get("snapshot_id")
                        or (check.get("plan_identity") or {}).get("snapshot_id") or (entry or {}).get("snapshot_id"))
            of = (entry or {}).get("production_order_no") or row.get("of")
            profile = (entry or {}).get("profile_type") or row.get("perfil")
            reason = ("Snapshot da validação não identificado." if not snapshot else
                      "OF ou perfil físico da linha em falta.")
            if snapshot and of and profile:
                key = (str(snapshot), str(of))
                if key not in order_cache:
                    order_cache[key] = plan_review.fetch_order(*key)
                group = [r for r in order_cache[key] if plan_review.same_profile(r.get("profile_type"), profile)]
                expanded = expand_entries(group, str(snapshot), precision=3 if plan_review.IS_MTG2 else 2)
                reason = (expanded["plan_refs_error"] if group else
                          f"OF + perfil físico não encontrados no snapshot guardado ({snapshot}).")
                if expanded["plan_refs_valid"]:
                    check.update(expanded)
                    refs = check["plan_refs"]
        if not refs:
            problems.append({"uid": sheet["uid"], "sheet_no": sheet.get("sheet_no"), "row": i + 1,
                             "reason": reason})
            continue
        check["plan_refs"] = refs
        seen = set()
        for ref in refs:
            ref_key = ref.get("plan_key")
            if not ref_key or ref_key in seen:
                problems.append({"uid": sheet["uid"], "sheet_no": sheet.get("sheet_no"), "row": i + 1,
                                 "reason": "Referências guardadas sem chave estável ou com chave repetida."})
            seen.add(ref_key)
            known = by_key.get(str(ref.get("plan_key")))
            if known:
                for name, value in plan_identity(known).items():
                    ref.setdefault(name, value)
            quantity = sim.parse_number(ref.get("assumed_quantity"))
            if quantity is None or quantity < 0:
                problems.append({"uid": sheet["uid"], "sheet_no": sheet.get("sheet_no"), "row": i + 1,
                                 "reason": f"Referência {ref.get('component_ref') or ref.get('plan_key')}: quantidade produzida desconhecida."})
    if problems:
        # Nada bloqueia a exportação (25/09): as linhas sem saldo seguem sem
        # quantidade e ficam listadas no log para quem quiser rever.
        for problem in problems:
            print(f"[export] folha {problem.get('sheet_no') or problem.get('uid')} "
                  f"linha {problem.get('row')}: {problem.get('reason')}", flush=True)
    return prepared
