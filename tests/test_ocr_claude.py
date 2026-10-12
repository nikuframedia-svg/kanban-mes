"""Motor Claude (API Anthropic): mesmas instruções do Grok, repetição e reserva."""

from dataclasses import replace
from types import SimpleNamespace

import json

import pytest

from app.ocr import provider as p
from app.templates_spec import get_template

KINDS = {"producao": get_template("cantoneiras_kanban"),
         "paragens": get_template("cantoneiras_paragens")}


class FakeClient:
    """Faz de SDK `anthropic`: devolve as respostas por ordem e guarda os pedidos."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []
        self.messages = self

    def create(self, **kw):
        self.requests.append(kw)
        text, stop = self.answers.pop(0)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)],
                               stop_reason=stop,
                               usage=SimpleNamespace(input_tokens=4660, output_tokens=300))


def claude_with(*answers, **kw):
    claude = p.ClaudeOcrProvider("k", "claude-sonnet-5-5", **kw)
    claude._client = FakeClient(*answers)
    return claude


def image(tmp_path):
    from PIL import Image
    path = tmp_path / "folha.png"
    Image.new("RGB", (2400, 1700), "white").save(path)
    return path


READING = json.dumps({
    "kind": "producao", "header": {"operador": "RUI"},
    "rows": [["MGROUP", "2509290", "262750", "150x12", "ZG-101", "2", None], [None, None, None, None, "ZG-301", "2", None]],
    "footer": {"metros_produzidos": "220"}})


def test_leitura_em_lista_com_as_instrucoes_do_grok(tmp_path):
    claude = claude_with((READING, "end_turn"))
    kind, out = claude.extract_auto(image(tmp_path), KINDS)
    assert kind == "producao"
    assert [r["modelo"] for r in out["rows"]] == ["ZG-101", "ZG-301"]
    assert out["footer"]["metros_produzidos"] == "220"
    sent = claude._client.requests[0]
    assert sent["model"] == "claude-sonnet-5-5"
    img, prompt = sent["messages"][0]["content"]
    assert img["source"]["media_type"] == "image/jpeg"
    assert prompt["text"] == p._auto_extraction_prompt(KINDS, compact=True)
    assert "é a letra I" in prompt["text"]


def test_json_invalido_repete_uma_vez(tmp_path):
    claude = claude_with(("{\"kind\": \"producao\", \"rows\": [[", "end_turn"),
                         (READING, "end_turn"))
    assert claude.extract_auto(image(tmp_path), KINDS)[0] == "producao"
    assert len(claude._client.requests) == 2


def test_duas_respostas_invalidas_passam_ao_motor_de_reserva(tmp_path):
    class Reserva:
        name = "grok"

        def extract_auto(self, image_path, templates):
            return "producao", {"header": {}, "rows": [], "footer": {}}

    claude = claude_with(("sem json", "end_turn"), ("sem json", "end_turn"),
                         fallback=Reserva())
    assert claude.extract_auto(image(tmp_path), KINDS)[0] == "producao"
    assert len(claude._client.requests) == 2


def test_resposta_cortada_e_erro_sem_repetir(tmp_path):
    claude = claude_with((READING[:40], "max_tokens"))
    with pytest.raises(p.OcrError, match="max_tokens"):
        claude.extract_auto(image(tmp_path), KINDS)
    assert len(claude._client.requests) == 1


def test_claude_principal_com_o_grok_de_reserva(monkeypatch):
    monkeypatch.setattr(p, "settings", replace(
        p.settings, ocr_provider="claude", anthropic_api_key="a",
        claude_ocr_model="claude-sonnet-5-5", bedrock_api_key="b", gemini_api_key=""))
    engine = p.get_provider()
    assert engine.name == "claude" and engine.model == "claude-sonnet-5-5"
    assert engine.fallback.name == "grok" and engine.fallback.fallback is None
    monkeypatch.setattr(p, "settings", replace(p.settings, bedrock_api_key=""))
    assert p.get_provider().fallback is None
    monkeypatch.setattr(p, "settings", replace(p.settings, anthropic_api_key=""))
    with pytest.raises(RuntimeError):
        p.get_provider()
