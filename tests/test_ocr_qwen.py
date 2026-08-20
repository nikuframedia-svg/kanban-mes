"""Testes do provider Qwen local (Ollama) — sem rede nem GPU: o transporte é
substituído. O parse tolerante e o redimensionamento são o código portado do
sistema OCR original da Metalogalva (ocr6.py), testados aqui contra os casos
que lá doeram: <think> sem fecho, fences, JSON truncado no teto de tokens e o
assert do GGML que exige dimensões múltiplas de 28."""

from __future__ import annotations

import base64
import io
import json

import pytest

from app.ocr.provider import (
    OcrError,
    QwenOcrProvider,
    _qwen_image_b64,
    _qwen_json,
)
from app.templates_spec import CANTONEIRAS_KANBAN


def make_provider(**kw) -> QwenOcrProvider:
    return QwenOcrProvider(url="http://ollama.local:11434", model="qwen3.5:9b", **kw)


def imagem(tmp_path, size=(1600, 1100)):
    from PIL import Image
    p = tmp_path / "folha.png"
    Image.new("RGB", size, "white").save(p)
    return p


# ---- parse tolerante -------------------------------------------------------

PAYLOAD = {"header": {"operador": "José"}, "rows": [{"of": "263323"}], "footer": {}}


def test_json_limpo_passa_direto():
    assert _qwen_json(json.dumps(PAYLOAD)) == PAYLOAD


def test_think_fechado_e_fences_sao_removidos():
    raw = ("<think>hmm, a coluna OF…</think>\n"
           "```json\n" + json.dumps(PAYLOAD) + "\n```")
    assert _qwen_json(raw) == PAYLOAD


def test_think_sem_fecho_salta_ate_ao_json():
    # o modelo divagou até ao teto sem fechar o bloco — caso real R224
    raw = "<think>vou analisar a folha " + json.dumps(PAYLOAD)
    assert _qwen_json(raw) == PAYLOAD


def test_json_truncado_recupera_linhas_completas():
    completo = {"header": {"operador": "José"},
                "rows": [{"of": "263323"}, {"of": "263324"}], "footer": {}}
    texto = json.dumps(completo)
    # corta a meio da última linha: recupera a primeira, não perde a folha
    truncado = texto[: texto.index("263324") - 10]
    out = _qwen_json(truncado)
    assert out["header"]["operador"] == "José"
    assert {"of": "263323"} in out["rows"]


def test_sem_json_lanca_value_error():
    with pytest.raises(ValueError):
        _qwen_json("não sei ler esta folha")


# ---- imagem: múltiplos de 28 (assert do GGML) ------------------------------

def test_imagem_redimensionada_a_multiplos_de_28(tmp_path):
    from PIL import Image
    b64 = _qwen_image_b64(imagem(tmp_path, (1600, 1100)), max_edge=1288)
    img = Image.open(io.BytesIO(base64.b64decode(b64)))
    w, h = img.size
    assert w % 28 == 0 and h % 28 == 0, "dimensões não múltiplas de 28 crasham o runner"
    assert max(w, h) <= 1288 + 14, "lado maior dentro do teto (± arredondamento ao stride)"


def test_imagem_pequena_tambem_arredonda(tmp_path):
    from PIL import Image
    b64 = _qwen_image_b64(imagem(tmp_path, (400, 300)), max_edge=1288)
    img = Image.open(io.BytesIO(base64.b64decode(b64)))
    assert img.size[0] % 28 == 0 and img.size[1] % 28 == 0


# ---- extração e cadeia -----------------------------------------------------

def test_extract_limpa_pelo_template(tmp_path, monkeypatch):
    p = make_provider()
    resposta = json.dumps({
        "header": {"operador": " José ", "campo_inventado": "x"},
        "rows": [{"of": "OF263323", "qtd": "4"}, {"of": None}],
        "footer": {"horas_trabalhadas": "8"},
    })
    monkeypatch.setattr(p, "_call", lambda b64, prompt: resposta)
    out = p.extract(imagem(tmp_path), CANTONEIRAS_KANBAN)
    assert out["header"]["operador"] == "José"
    assert "campo_inventado" not in out["header"], "sem schema no transporte, a limpeza é a guarda"
    assert len(out["rows"]) == 1
    assert set(out["rows"][0]) == set(CANTONEIRAS_KANBAN.row_fields)


def test_retry_na_mesma_imagem_e_escada_de_tamanhos(tmp_path, monkeypatch):
    """2 falhas → 3ª chamada (primeira tentativa do tamanho seguinte) sucede."""
    p = make_provider()
    monkeypatch.setattr("app.ocr.provider.time.sleep", lambda s: None)
    chamadas = []

    def flaky(b64, prompt):
        chamadas.append(len(b64))
        if len(chamadas) < 3:
            raise OcrError("Qwen [m]: resposta vazia")
        return json.dumps(PAYLOAD)

    monkeypatch.setattr(p, "_call", flaky)
    out = p.extract(imagem(tmp_path), CANTONEIRAS_KANBAN)
    assert out["rows"][0]["of"] == "263323"
    assert len(chamadas) == 3
    assert chamadas[2] < chamadas[0], "a 3ª chamada usa a imagem do tamanho seguinte (menor)"


def test_qwen_esgotado_cai_para_o_fallback(tmp_path, monkeypatch):
    """PC/GPU em baixo → a cadeia cloud apanha o trabalho, folha nunca fica por ler."""
    monkeypatch.setattr("app.ocr.provider.time.sleep", lambda s: None)

    class FakeGemini:
        name = "gemini"

        def extract(self, image_path, template):
            return {"header": {}, "rows": [{"of": "263323"}], "footer": {}}

    p = make_provider(fallback=FakeGemini())
    monkeypatch.setattr(p, "_call", lambda b64, prompt: (_ for _ in ()).throw(
        OcrError("Qwen [qwen3.5:9b] indisponível: connection refused")))
    out = p.extract(imagem(tmp_path), CANTONEIRAS_KANBAN)
    assert out["rows"][0]["of"] == "263323"


def test_erro_final_identifica_os_dois_motores(tmp_path, monkeypatch):
    monkeypatch.setattr("app.ocr.provider.time.sleep", lambda s: None)

    class FakeGemini:
        name = "gemini"

        def extract(self, image_path, template):
            raise OcrError("Gemini [m] HTTP 429: quota")

    p = make_provider(fallback=FakeGemini())
    monkeypatch.setattr(p, "_call", lambda b64, prompt: (_ for _ in ()).throw(
        OcrError("Qwen [qwen3.5:9b] indisponível: connection refused")))
    with pytest.raises(OcrError) as e:
        p.extract(imagem(tmp_path), CANTONEIRAS_KANBAN)
    assert "Qwen" in str(e.value) and "Gemini" in str(e.value)


def test_no_think_e_cinto_e_suspensorios(tmp_path, monkeypatch):
    """/no_think no prompt E think:false no payload — builds custom ignoram
    o segundo (lição do sistema original)."""
    p = make_provider(no_think=True)
    visto = {}

    def fake_urlopen(req, timeout):
        visto["payload"] = json.loads(req.data.decode("utf-8"))

        class R:
            def read(self):
                return json.dumps({"response": json.dumps(PAYLOAD)}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return R()

    monkeypatch.setattr("app.ocr.provider.urllib.request.urlopen", fake_urlopen)
    p.extract(imagem(tmp_path), CANTONEIRAS_KANBAN)
    assert visto["payload"]["think"] is False
    assert visto["payload"]["prompt"].endswith("/no_think")
    assert visto["payload"]["keep_alive"] == -1
    assert visto["payload"]["options"]["temperature"] == 0
