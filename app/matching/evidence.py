"""Reconstruct observations without reading identities written by the cross."""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import re
from dataclasses import dataclass


IDENTITY_FIELDS = frozenset({"of", "ov", "cliente", "perfil", "modelo"})
_ROW_FIELD = re.compile(r"rows\[(\d+)\]\.([a-zA-Z_][a-zA-Z_0-9]*)$")
_ROW = re.compile(r"rows\[(\d+)\]$")
_ALIASES = {"comp_mm": "perf_comp"}


@dataclass(frozen=True)
class Evidence:
    data: dict
    provenance: dict
    explicit_bindings: dict[int, dict]


def _structured(value):
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        try:
            # Legacy audit encoded dictionaries with str(dict).
            return ast.literal_eval(value)
        except (ValueError, SyntaxError):
            return None


def build_evidence(sheet: dict, human_events: list[dict]) -> Evidence:
    """Original OCR plus the latest human observation for each field.

    Current data supplies row structure and explicit decisions only. In
    particular neither an auto-filled identity nor length becomes evidence.
    The caller filters events by extraction generation before calling.
    """
    raw = sheet.get("raw_extraction") or {}
    data = copy.deepcopy(raw)
    current = sheet.get("sheet_data") or {}
    rows = data.setdefault("rows", [])
    current_rows = current.get("rows") or []
    while len(rows) < len(current_rows):
        rows.append({})
    # TPL102 used comp_mm as the historical name of the checkbox column.
    # Preserve the literal value, including an invalid numeric marker; it
    # cannot become a cut length or a second independent observation.
    for row in rows:
        if "perf_comp" not in row and "comp_mm" in row:
            row["perf_comp"] = row["comp_mm"]
        row.pop("comp_mm", None)
    sources = {}
    for section in ("header", "footer", "layout"):
        for key in data.get(section, {}):
            sources[f"{section}.{key}"] = {"source": "raw_extraction"}
    for i, row in enumerate(rows):
        for key in row:
            sources[f"rows[{i}].{key}"] = {"source": "raw_extraction"}
    from ..header_recovery import evidence_observations
    for key, value in evidence_observations(sheet).items():
        data.setdefault("header", {})[key] = value
        sources[f"header.{key}"] = {"source": "header_recovery"}
    recovery = current.get("_coverage_recovery") or {}
    from ..ocr.coverage import sheet_identity
    if recovery.get("context") == sheet_identity(sheet):
        for index, observation in recovery.get("anchor_observations", {}).items():
            i = int(index)
            if 0 <= i < len(rows):
                rows[i].update(copy.deepcopy(observation))
                for key in observation:
                    sources[f"rows[{i}].{key}"] = {"source": "row_recovery"}
        for index, observation in recovery.get("observations", {}).items():
            i = int(index)
            if 0 <= i < len(rows):
                rows[i] = copy.deepcopy(observation)
                for key in observation:
                    sources[f"rows[{i}].{key}"] = {"source": "row_recovery"}
    used_ids = []
    for event in sorted(human_events, key=lambda event: int(event["id"])):
        if event.get("source", "human") != "human":
            continue
        path = event["field_path"]
        value = event.get("new_value")
        match = _ROW_FIELD.fullmatch(path)
        if match:
            i, key = int(match[1]), _ALIASES.get(match[2], match[2])
            if i >= len(rows):
                continue
            if key.startswith("_"):
                if key not in {"_plan_binding", "_deleted"}:
                    continue
                value = _structured(value)
            rows[i][key] = value
            path = f"rows[{i}].{key}"
            # Editing identity cancels the previous explicit reference, just
            # as the review endpoint does. The binding audit event following
            # a reference selection reestablishes it in the same event order.
            if key in IDENTITY_FIELDS or key == "perf_comp":
                rows[i].pop("_plan_binding", None)
                sources[f"rows[{i}]._plan_binding"] = {"source": "human", "event_id": int(event["id"])}
        elif (match := _ROW.fullmatch(path)) and value == "<apagada>":
            i = int(match[1])
            if i >= len(rows):
                continue
            rows[i]["_deleted"] = True
        elif re.fullmatch(r"(?:header|footer|layout)\.[a-zA-Z_][a-zA-Z_0-9]*", path):
            section, key = path.split(".", 1)
            data.setdefault(section, {})[key] = value
        else:
            continue
        used_ids.append(int(event["id"]))
        sources[path] = {"source": "human", "event_id": int(event["id"])}
    explicit = {}
    for i, row in enumerate(rows):
        cur = current_rows[i] if i < len(current_rows) else {}
        if "_deleted" in cur:
            row["_deleted"] = cur["_deleted"]
        for marker in ("_paper_position", "_identity_unresolved"):
            if marker in cur:
                row[marker] = cur[marker]
        # These markers are operator decisions, never identity predictions.
        for key in ("perf_comp", "perfil_completo"):
            if key in cur and sources.get(f"rows[{i}].{key}", {}).get("source") != "human":
                row[key] = copy.deepcopy(cur[key])
        path = f"rows[{i}]._plan_binding"
        binding = row.get("_plan_binding") if path in sources else cur.get("_plan_binding")
        if isinstance(binding, dict) and binding.get("selected_explicitly"):
            explicit[i] = copy.deepcopy(binding)
        row.pop("_plan_binding", None)
    provenance = {
        "source": "raw_extraction+human_edits",
        "extraction_generation": int(sheet.get("extraction_generation") or 0),
        "event_floor": int(sheet.get("evidence_event_floor") or 0),
        "human_event_limit": max(used_ids, default=int(sheet.get("evidence_event_floor") or 0)),
        "human_event_ids": used_ids,
        "field_sources": sources,
    }
    if not sheet.get("extraction_generation"):
        provenance["legacy_boundary"] = sheet.get("extracted_at")
    payload = {"data": data, "explicit_bindings": explicit, "generation": provenance["extraction_generation"],
               "human_events": used_ids}
    provenance["fingerprint"] = hashlib.sha256(json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str,
    ).encode()).hexdigest()
    return Evidence(data, provenance, explicit)
