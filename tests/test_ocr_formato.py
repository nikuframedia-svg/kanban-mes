"""Instruções do OCR como no OCR original: estrutura JSON exata e memória
de trabalho suficiente (casos reais das cantoneiras, 26/09)."""

import io
import json

from app.ocr import provider as p
from app.templates_spec import get_template

TEMPLATES = {"producao": "cantoneiras_kanban", "paragens": "cantoneiras_paragens"}


def test_instrucoes_mostram_a_estrutura_json_com_as_chaves_exatas():
    for name in TEMPLATES.values():
        t = get_template(name)
        fields = getattr(t, "ocr_row_fields", t.row_fields)
        prompt = p._extraction_prompt(t)
        assert "Devolve EXATAMENTE esta estrutura JSON" in prompt
        assert "{" + ", ".join(f'"{f}": null' for f in fields) + "}" in prompt
    auto = p._auto_extraction_prompt({k: get_template(v) for k, v in TEMPLATES.items()})
    assert '"kind": ' in auto and "Cada entrada de `rows` usa as chaves" in auto
    assert "não saltes nenhuma linha" in auto


def test_qwen_pede_memoria_de_trabalho_suficiente(monkeypatch):
    """Sem num_ctx vale o do Ollama (2048 nas versões antigas): medido numa
    folha real de 15 linhas, a resposta parava na primeira."""
    sent = {}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(req, timeout=None):
        sent.update(json.loads(req.data))
        return Response(json.dumps({"response": "{}"}).encode())

    monkeypatch.setattr(p.urllib.request, "urlopen", urlopen)
    p.QwenOcrProvider("http://x", "qwen3.5:9b", 5, True)._call("aW1n", "prompt")
    assert sent["options"]["num_ctx"] >= 8192
