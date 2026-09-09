"""Materialização única dos factos finais de uma folha.

Postgres e os dois exports consomem esta estrutura. Assim uma linha física de
``Perf. Comp. = X`` nunca é contada simultaneamente como agregado e como cada
referência: o Postgres recebe o pai agregado + filhos de proveniência; exports
recebem apenas filhos com produção positiva.
"""

from __future__ import annotations

from copy import deepcopy

from .matching.full_profile import plan_identity
from .matching import carryover
from .matching import similarity as sim
from .templates_spec import KanbanTemplate, field_value, is_marked


def row_has_content(row: dict) -> bool:
    return any(
        value is not None and str(value).strip()
        for key, value in row.items() if not str(key).startswith("_")
    )


def materialize_sheet(sheet: dict, template: KanbanTemplate) -> dict:
    data = sheet.get("sheet_data") or {}
    rows = data.get("rows") or []
    cross = sheet.get("cross_check") or {}
    is_v3 = cross.get("engine") == "cross-v3"
    cross_rows = {row.get("row_index"): row for row in cross.get("rows", [])}
    content_fields = tuple(
        field for field in template.row_fields
        if field not in carryover.CARRY_FIELDS
    )
    identities = carryover.resolve(rows, content_fields, {})
    parents: list[dict] = []
    exports: list[dict] = []
    plan_refs: list[dict] = []

    for row_index, source_row in enumerate(rows):
        if (not isinstance(source_row, dict)
                or source_row.get("_deleted") is True
                or not row_has_content(source_row)):
            continue
        if is_v3 and cross_rows.get(row_index, {}).get("row_kind") in {"activity", "empty", "deleted"}:
            continue
        effective = source_row if is_v3 else carryover.effective_row(source_row, identities[row_index])
        row = {key: value for key, value in effective.items()
               if not str(key).startswith("_")}
        row_cross = deepcopy(cross_rows.get(row_index) or {})
        full_profile = (
            template.name == "cantoneiras_kanban"
            and is_marked(field_value(source_row, "perf_comp"))
        )
        refs = list(row_cross.get("plan_refs") or []) if full_profile else []

        if full_profile and refs:
            assumed = [sim.parse_number(ref.get("assumed_quantity")) for ref in refs]
            aggregate_qtd = sum(value or 0.0 for value in assumed)
            parent_row = {**row, "modelo": None, "qtd": aggregate_qtd}
            parent_cross = {
                **row_cross,
                "matched_plan_key": None,
                "plan_length_mm": None,
                # ``line_meters`` já é a soma de todos os filhos calculada
                # pelo cross; não identifica uma referência única.
            }
            parent = {
                "row_index": row_index,
                "row": parent_row,
                "cross": parent_cross,
                "aggregate": True,
            }
            parents.append(parent)
            for ref in refs:
                quantity = sim.parse_number(ref.get("assumed_quantity"))
                ref_fact = {
                    **ref,
                    "row_index": row_index,
                    "plan_snapshot_id": cross.get("snapshot_id"),
                }
                plan_refs.append(ref_fact)
                # Zero fica na auditoria/filho PG, mas não é uma linha de
                # produção positiva num ficheiro operacional.
                if quantity is None or quantity <= 0:
                    continue
                length = sim.parse_number(ref.get("length_mm"))
                child_row = {
                    **row,
                    "modelo": ref.get("component_ref"),
                    "perfil": ref.get("profile_type") or row.get("perfil"),
                    "qtd": quantity,
                    "perf_comp": None,
                }
                for field, key in (("of", "production_order_no"), ("ov", "sales_order_no"), ("cliente", "customer_name")):
                    if key in ref:
                        child_row[field] = ref[key]
                child_cross = {
                    **row_cross,
                    "matched_plan_key": ref.get("plan_key"),
                    "plan_identity": plan_identity(ref, ref.get("snapshot_id") or cross.get("snapshot_id")),
                    "plan_length_mm": length,
                    "line_meters": (
                        round(quantity * length / 1000.0, 2)
                        if length is not None else None
                    ),
                }
                exports.append({
                    "row_index": row_index,
                    "row": child_row,
                    "cross": child_cross,
                    "aggregate": False,
                    "plan_key": ref.get("plan_key"),
                })
        else:
            fact = {
                "row_index": row_index,
                "row": row,
                "cross": row_cross,
                "aggregate": False,
            }
            parents.append(fact)
            exports.append(fact)

    return {"parents": parents, "exports": exports, "plan_refs": plan_refs}
