"""Um único materializador alimenta PG e exports sem dupla contagem."""

from app.production_facts import materialize_sheet
from app.templates_spec import CANTONEIRAS_KANBAN


def test_perf_comp_cria_pai_agregado_filhos_e_exporta_so_positivos():
    sheet = {
        "sheet_data": {
            "header": {},
            "rows": [{
                "of": "263323", "ov": "2500001", "cliente": "CLIENTE",
                "perfil": "L40X40X4", "modelo": "NÃO-DEVE-SAIR",
                "qtd": "999", "perf_comp": "X",
                "_plan_binding": {"snapshot_id": "snap", "plan_key": "K1"},
            }],
            "footer": {},
        },
        "cross_check": {
            "snapshot_id": "snap",
            "rows": [{
                "row_index": 0, "matched_plan_key": "K1",
                "plan_length_mm": 1500, "line_meters": 12.0,
                "plan_refs": [
                    {
                        "plan_key": "K1", "component_ref": "REF-1",
                        "profile_type": "L40X40X4", "length_mm": 2000,
                        "remaining_before": 6, "assumed_quantity": 6,
                        "remaining_rule": "max(QTD - Maq., 0)",
                    },
                    {
                        "plan_key": "K2", "component_ref": "REF-2",
                        "profile_type": "L40X40X4", "length_mm": 1500,
                        "remaining_before": 0, "assumed_quantity": 0,
                        "remaining_rule": "max(QTD - Maq., 0)",
                    },
                ],
                "cells": [],
            }],
        },
    }

    facts = materialize_sheet(sheet, CANTONEIRAS_KANBAN)
    assert len(facts["parents"]) == 1
    parent = facts["parents"][0]
    assert parent["aggregate"] is True
    assert parent["row"]["qtd"] == 6
    assert parent["row"]["modelo"] is None
    assert parent["cross"]["matched_plan_key"] is None
    assert parent["cross"]["plan_length_mm"] is None
    assert "_plan_binding" not in parent["row"]

    assert [ref["plan_key"] for ref in facts["plan_refs"]] == ["K1", "K2"]
    assert len(facts["exports"]) == 1, "a referência com falta zero é só auditoria"
    exported = facts["exports"][0]
    assert exported["row"]["modelo"] == "REF-1"
    assert exported["row"]["qtd"] == 6
    assert exported["cross"]["line_meters"] == 12.0
    assert sum(f["row"]["qtd"] for f in facts["exports"]) == parent["row"]["qtd"]


def test_linha_eliminada_nao_materializa_factos():
    sheet = {
        "sheet_data": {"rows": [
            {"of": "1", "modelo": "OK", "qtd": 1},
            {"of": "2", "modelo": "APAGADA", "qtd": 99, "_deleted": True},
        ]},
        "cross_check": {"rows": [
            {"row_index": 0, "cells": []},
            {"row_index": 1, "cells": []},
        ]},
    }
    facts = materialize_sheet(sheet, CANTONEIRAS_KANBAN)
    assert [fact["row_index"] for fact in facts["parents"]] == [0]
    assert [fact["row"]["modelo"] for fact in facts["exports"]] == ["OK"]


def test_materializacao_usa_identidade_herdada_sem_herdar_modelo_ou_qtd():
    sheet = {
        "sheet_data": {"rows": [
            {"of": "263323", "ov": "2508335", "cliente": "MG GROUP ENERGY",
             "perfil": "100x12", "modelo": "ZE-242", "qtd": 4},
            {"perfil": "110x8", "modelo": "ZE-329", "qtd": 2},
            {"modelo": "ZE-330"},
        ]},
        "cross_check": {"rows": [
            {"row_index": index, "cells": []} for index in range(3)
        ]},
    }
    rows = [fact["row"] for fact in materialize_sheet(
        sheet, CANTONEIRAS_KANBAN,
    )["exports"]]
    assert rows[2]["of"] == "263323"
    assert rows[2]["ov"] == "2508335"
    assert rows[2]["cliente"] == "MG GROUP ENERGY"
    assert rows[2]["perfil"] == "110x8"
    assert rows[2]["modelo"] == "ZE-330"
    assert rows[2].get("qtd") is None
