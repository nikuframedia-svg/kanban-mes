"""Recover full-profile identity from observations, never prior cross proposals."""
from .matching.evidence import build_evidence
from .matching.angle_geometry import parse_profile, profile_key
from .templates_spec import field_value, is_marked


def observations(sheet, events):
    if sheet.get("status") == "validated" or sheet.get("template_name") != "cantoneiras_kanban":
        return {}
    observed = build_evidence(sheet, events).data.get("rows", [])
    result = {}
    for i, row in enumerate((sheet.get("sheet_data") or {}).get("rows", [])):
        if row.get("_deleted") is True or not is_marked(field_value(row, "perf_comp")) or i >= len(observed):
            continue
        value = observed[i].get("perfil")
        parsed = parse_profile(value)
        if parsed.family == "L" and len(parsed.dimensions) == 3 and all(d > 0 for d in parsed.dimensions):
            result[i] = str(value).strip()
    return result


def corrections(sheet, events):
    rows = (sheet.get("sheet_data") or {}).get("rows", [])
    return {i: value for i, value in observations(sheet, events).items()
            if profile_key(rows[i].get("perfil")) != profile_key(value)}
