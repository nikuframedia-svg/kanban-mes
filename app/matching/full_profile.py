"""Frozen plan identity and full-profile production, independent of I/O."""
from __future__ import annotations

from . import similarity as sim
from ..templates_spec import field_value, is_marked


def _value(entry: dict, canonical: str, indexed: str):
    return entry[canonical] if canonical in entry else entry.get(indexed)


def plan_identity(entry: dict, snapshot_id: str | None = None) -> dict:
    return {
        "snapshot_id": snapshot_id or entry.get("snapshot_id"),
        "plan_key": str(entry.get("plan_key") or ""),
        "production_order_no": _value(entry, "production_order_no", "of"),
        "sales_order_no": _value(entry, "sales_order_no", "ov"),
        "customer_name": _value(entry, "customer_name", "cliente"),
        "component_ref": _value(entry, "component_ref", "modelo"),
        "profile_type": _value(entry, "profile_type", "perfil"),
        "profile_excel_o": entry.get("profile_excel_o"),
        "material_description": entry.get("material_description"),
        "length_mm": sim.parse_number(_value(entry, "length_mm", "comp_mm")),
    }


def expand_entries(entries: list[dict], snapshot_id: str | None, *, precision: int = 3) -> dict:
    refs, invalid, seen = [], [], set()
    invalid_keys = False
    for entry in sorted(entries, key=lambda e: str(e.get("plan_key") or "")):
        identity = plan_identity(entry, snapshot_id)
        key = identity["plan_key"]
        if not key or key in seen:
            invalid_keys = True
            continue
        seen.add(key)
        remaining = sim.parse_number(_value(entry, "remaining_quantity", "qtd_restante"))
        rule = _value(entry, "remaining_rule", "regra_calculo")
        valid = (_value(entry, "remaining_valid", "falta_valida") is True
                 and remaining is not None and remaining >= 0 and bool(rule))
        if not valid:
            invalid.append(key)
        refs.append({
            **identity,
            "quantity_planned": sim.parse_number(_value(entry, "quantity_planned", "qtd_planeada")),
            "quantity_made_before": sim.parse_number(_value(entry, "quantity_made", "qtd_feita")),
            "remaining_before": remaining,
            "overproduction_before": sim.parse_number(_value(entry, "overproduction_quantity", "excesso")),
            "assumed_quantity": remaining if valid else None,
            "remaining_rule": rule,
            "closed_x": entry.get("closed_x"),
            "cutting_machine": _value(entry, "cutting_machine", "maquina"),
            "planning_week": _value(entry, "planning_week", "semana"),
        })
    valid = bool(refs) and not invalid and not invalid_keys
    positive = [ref for ref in refs if (ref["assumed_quantity"] or 0) > 0]
    missing_lengths = [
        str(ref.get("component_ref") or ref.get("plan_key") or "referência")
        for ref in positive if ref["length_mm"] is None
    ]
    meters = None
    if valid and all(ref["length_mm"] is not None for ref in positive):
        meters = round(sum(ref["assumed_quantity"] * ref["length_mm"] for ref in positive) / 1000, precision)
    return {
        "plan_refs": refs, "plan_refs_valid": valid,
        "plan_refs_error": ("Referências sem chave estável ou com chave repetida." if invalid_keys else
                            "Sem referências para OF + perfil." if not refs else
                            "Qtd em Falta inválida/desconhecida: " + ", ".join(invalid[:8]) if invalid else None),
        "full_profile_quantity": sum(ref["assumed_quantity"] or 0 for ref in refs) if valid else None,
        "plan_length_mm": None, "plan_line_meters": meters, "line_meters": meters,
        "plan_meters_error": (
            "Comprimento em falta em " + ", ".join(missing_lengths[:8])
            if valid and missing_lengths else None
        ),
        # Marca interna persistível: estes factos foram calculados nesta mesma
        # execução e attach_plan_facts pode reutilizá-los sem expandir o grupo.
        "plan_refs_expanded": True,
    }


def attach_plan_facts(cross: dict, index, rows: list[dict], *, precision: int = 3,
                      reuse_expanded: bool = False) -> None:
    """Attach only the chosen identity; never an alternative or an OCR guess."""
    by_key = {str(e.get(index.spec.key_field)): e for e in index.entries} if index else {}
    for check in cross.get("rows", []):
        check.pop("plan_identity", None)
        entry = by_key.get(str(check.get("matched_plan_key")))
        i = check.get("row_index", -1)
        if entry is None or not 0 <= i < len(rows):
            continue
        check["plan_identity"] = plan_identity(entry, index.snapshot_id)
        if is_marked(field_value(rows[i], "perf_comp")):
            if not (reuse_expanded and check.get("plan_refs_expanded") is True):
                hits = set(index.exact_matches("of", entry.get("of")))
                hits &= set(index.exact_matches("perfil", entry.get("perfil")))
                check.update(expand_entries(
                    [index.entries[j] for j in sorted(hits)],
                    index.snapshot_id,
                    precision=precision,
                ))
    production = [r for r in cross.get("rows", [])
                  if r.get("row_kind", r.get("mode")) not in {"empty", "activity", "deleted"}]
    if any(r.get("plan_refs") for r in production):
        summary = cross.setdefault("summary", {})
        meters = [r.get("plan_line_meters", r.get("line_meters")) for r in production]
        known_meters = [m for m in meters if m is not None]
        total = round(sum(known_meters), precision) if known_meters else None
        partial = any(m is None for m in meters)
        summary.update(metros_teoricos=total, metros_parciais=partial)
        measured = summary.get("metros_produzidos")
        summary["desperdicio_m"] = round(measured - total, 2) if measured is not None and total is not None and not partial else None


def expand_group(plan, winner):
    hits = plan.maps["of"].get(winner.of, set()) & plan.maps["perfil"].get(winner.profile, set())
    return expand_entries([plan.index.entries[i] for i in hits], plan.index.snapshot_id, precision=2)
