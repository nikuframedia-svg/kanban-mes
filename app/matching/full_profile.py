"""Preserve TPL102 aggregate/child provenance using one canonical snapshot."""
from .geometry import parse_decimal


def number(value):
    parsed = parse_decimal(value)
    return float(parsed) if parsed is not None else None


def expand_group(plan, winner):
    refs, invalid, seen = [], [], set()
    hits = plan.maps["of"].get(winner.of, set()) & plan.maps["perfil"].get(winner.profile, set())
    for idx in sorted(hits, key=lambda i: plan.semantic[i]):
        ref = plan.index.entries[idx]
        key = str(ref.get(plan.index.spec.key_field) or "")
        if not key or key in seen:
            continue
        seen.add(key)
        remaining = number(ref.get("qtd_restante"))
        rule = str(ref.get("regra_calculo") or "").strip()
        flag = ref.get("falta_valida")
        valid = bool(flag) if flag is not None else remaining is not None
        valid = valid and remaining is not None and remaining >= 0 and bool(rule)
        if not valid:
            invalid.append(key)
        refs.append({
            "plan_key": key, "component_ref": ref.get("modelo"),
            "profile_type": ref.get("perfil"), "length_mm": number(ref.get("comp_mm")),
            "quantity_planned": number(ref.get("qtd_planeada")),
            "quantity_made_before": number(ref.get("qtd_feita")),
            "remaining_before": remaining, "overproduction_before": number(ref.get("excesso")),
            "assumed_quantity": remaining if valid else None, "remaining_rule": rule or None,
        })
    valid = bool(refs) and not invalid
    error = ("Sem referências para a combinação OF + Perfil." if not refs else
             "Falta inválida ou desconhecida nas referências: " + ", ".join(invalid[:8]) if invalid else None)
    meters = None
    if valid:
        positive = [ref for ref in refs if (ref["assumed_quantity"] or 0) > 0]
        if all(ref["length_mm"] is not None for ref in positive):
            meters = round(sum(ref["assumed_quantity"] * ref["length_mm"] for ref in positive) / 1000, 2)
    return {"plan_refs": refs, "plan_refs_valid": valid, "plan_refs_error": error,
            "line_meters": meters, "plan_line_meters": meters, "plan_length_mm": None}
