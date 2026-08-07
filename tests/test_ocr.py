"""Testes do provider Gemini — sem rede: a chamada HTTP é substituída."""

from __future__ import annotations

import json

import pytest

from app.ocr.provider import GeminiOcrProvider, ManualEntryProvider, OcrError
from app.templates_spec import CANTONEIRAS_KANBAN


def gemini_response(payload: dict) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]}


def make_provider() -> GeminiOcrProvider:
    return GeminiOcrProvider(api_key="test", model="gemini-flash-latest")


def test_parse_limpa_e_filtra_linhas_vazias():
    p = make_provider()
    resp = gemini_response({
        "header": {"operador": "  José ", "data": "2026-08-07", "turno": "M"},
        "rows": [
            {"cliente": "TECPOLES", "of": "OF251525", "perfil": "L60X60X4", "qtd": "120"},
            {"cliente": None, "of": "  ", "perfil": None},   # linha vazia → fora
        ],
        "footer": {"horas_trabalhadas": "8"},
    })
    out = p._parse(resp, CANTONEIRAS_KANBAN)
    assert out["header"]["operador"] == "José"
    assert out["header"]["n_operador"] is None          # campo em falta → None
    assert len(out["rows"]) == 1
    assert out["rows"][0]["of"] == "OF251525"
    assert set(out["rows"][0]) == set(CANTONEIRAS_KANBAN.row_fields)
    assert out["footer"]["horas_trabalhadas"] == "8"
    assert out["footer"]["metros_produzidos"] is None


def test_parse_sem_linhas_devolve_uma_linha_vazia():
    p = make_provider()
    out = p._parse(gemini_response({"header": {}, "rows": [], "footer": {}}),
                   CANTONEIRAS_KANBAN)
    assert len(out["rows"]) == 1
    assert all(v is None for v in out["rows"][0].values())


def test_resposta_invalida_lanca_ocr_error():
    p = make_provider()
    with pytest.raises(OcrError):
        p._parse({"candidates": []}, CANTONEIRAS_KANBAN)
    with pytest.raises(OcrError):
        p._parse(
            {"candidates": [{"content": {"parts": [{"text": "não é json"}]}}]},
            CANTONEIRAS_KANBAN,
        )


def test_imagem_inexistente_lanca_ocr_error(tmp_path):
    with pytest.raises(OcrError):
        make_provider().extract(tmp_path / "nao_existe.jpg", CANTONEIRAS_KANBAN)


def test_manual_provider_segue_o_template():
    out = ManualEntryProvider().extract(None, CANTONEIRAS_KANBAN)
    assert set(out["rows"][0]) == set(CANTONEIRAS_KANBAN.row_fields)
    assert "metros_produzidos" in out["footer"]


def test_schema_cobre_todos_os_campos():
    schema = make_provider()._schema(CANTONEIRAS_KANBAN)
    assert set(schema["properties"]["rows"]["items"]["properties"]) == set(
        CANTONEIRAS_KANBAN.row_fields
    )
