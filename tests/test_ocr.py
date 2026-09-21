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


def test_503_persistente_cai_para_o_modelo_seguinte(monkeypatch):
    """O caso real do lote de 11-08: o primário respondeu 503 («high demand»)
    duas vezes e a folha morreu sem os fallbacks alguma vez serem tentados."""
    import io
    import urllib.error

    p = make_provider()
    calls: list[str] = []

    def fake_call_model(model, body):
        calls.append(model)
        if model == p.models[0]:
            raise urllib.error.HTTPError(
                "url", 503, "Service Unavailable", {}, io.BytesIO(b"high demand")
            )
        return gemini_response({"header": {}, "rows": [], "footer": {}})

    monkeypatch.setattr(p, "_call_model", fake_call_model)
    monkeypatch.setattr("app.ocr.provider.time.sleep", lambda s: None)
    out = p._call({})
    assert "candidates" in out
    assert calls == [p.models[0], p.models[0], p.models[1]], \
        "2 tentativas no primário e depois o fallback — nunca desistir sem o tentar"


def test_404_salta_logo_e_401_rebenta(monkeypatch):
    import io
    import urllib.error

    p = make_provider()
    calls: list[str] = []

    def http_error(code, msg):
        return urllib.error.HTTPError("url", code, msg, {}, io.BytesIO(b""))

    def fake_404(model, body):
        calls.append(model)
        if model == p.models[0]:
            raise http_error(404, "Not Found")
        return gemini_response({"header": {}, "rows": [], "footer": {}})

    monkeypatch.setattr(p, "_call_model", fake_404)
    monkeypatch.setattr("app.ocr.provider.time.sleep", lambda s: None)
    p._call({})
    assert calls == [p.models[0], p.models[1]], "404 não gasta segunda tentativa"

    def fake_401(model, body):
        raise http_error(401, "Unauthorized")

    monkeypatch.setattr(p, "_call_model", fake_401)
    with pytest.raises(OcrError):
        p._call({})


def test_schema_cobre_todos_os_campos():
    schema = make_provider()._schema(CANTONEIRAS_KANBAN)
    assert set(schema["properties"]["rows"]["items"]["properties"]) == set(
        CANTONEIRAS_KANBAN.row_fields
    )


# ---- último recurso Claude ----

def test_gemini_esgotado_cai_para_o_claude(tmp_path, monkeypatch):
    """Toda a cadeia Gemini falhou → o trabalho passa ao motor de último
    recurso com os MESMOS argumentos; o resultado é indistinguível."""
    from app.ocr.provider import ClaudeOcrProvider, GeminiOcrProvider

    img = tmp_path / "f.png"
    from PIL import Image
    Image.new("RGB", (40, 40), "white").save(img)

    claude = ClaudeOcrProvider(api_key="t", model="m")
    seen = {}

    def fake_claude_call(image_path, prompt, schema):
        seen["prompt"] = prompt
        return {"header": {}, "rows": [{"of": "263323"}], "footer": {}}

    monkeypatch.setattr(claude, "_call", fake_claude_call)

    p = GeminiOcrProvider(api_key="t", model="m", fallback=claude)
    monkeypatch.setattr(p, "_call", lambda body: (_ for _ in ()).throw(
        OcrError("Gemini [m] HTTP 503: high demand")))

    out = p.extract(img, CANTONEIRAS_KANBAN)
    assert out["rows"][0]["of"] == "263323"
    assert "REGRAS ESTRITAS" in seen["prompt"], "mesmo prompt, outro transporte"


def test_erro_final_identifica_os_dois_motores(tmp_path, monkeypatch):
    from app.ocr.provider import ClaudeOcrProvider, GeminiOcrProvider

    img = tmp_path / "f.png"
    from PIL import Image
    Image.new("RGB", (40, 40), "white").save(img)

    claude = ClaudeOcrProvider(api_key="t", model="m")
    monkeypatch.setattr(claude, "_call", lambda *a: (_ for _ in ()).throw(
        OcrError("Claude [m]: overloaded")))
    p = GeminiOcrProvider(api_key="t", model="m", fallback=claude)
    monkeypatch.setattr(p, "_call", lambda body: (_ for _ in ()).throw(
        OcrError("Gemini [m] HTTP 503: high demand")))

    with pytest.raises(OcrError) as e:
        p.extract(img, CANTONEIRAS_KANBAN)
    assert "Gemini" in str(e.value) and "Claude" in str(e.value)


def test_sem_fallback_o_erro_gemini_propaga(tmp_path, monkeypatch):
    p = make_provider()
    monkeypatch.setattr(p, "_call", lambda body: (_ for _ in ()).throw(
        OcrError("Gemini [m] HTTP 429: quota")))
    img = tmp_path / "f.png"
    from PIL import Image
    Image.new("RGB", (40, 40), "white").save(img)
    with pytest.raises(OcrError) as e:
        p.extract(img, CANTONEIRAS_KANBAN)
    assert "último recurso" not in str(e.value)


def test_claude_schema_e_json_schema_valido():
    """O dialecto do Claude é JSON Schema: nullable via ["string","null"],
    additionalProperties fechado, required completo."""
    from app.ocr.provider import ClaudeOcrProvider
    from app.templates_spec import CANTONEIRAS_PARAGENS

    c = ClaudeOcrProvider(api_key="t", model="m")
    schema = c._auto_schema({"producao": CANTONEIRAS_KANBAN,
                             "paragens": CANTONEIRAS_PARAGENS})
    rows = schema["properties"]["rows"]["items"]
    assert rows["additionalProperties"] is False
    assert set(rows["required"]) == set(rows["properties"])
    assert rows["properties"]["of"]["type"] == ["string", "null"]
    assert schema["properties"]["kind"]["enum"] == ["producao", "paragens"]


# ---- resgate do cabeçalho ----

def _folha_img(tmp_path):
    from PIL import Image
    img = tmp_path / "folha.png"
    Image.new("RGB", (400, 600), "white").save(img)
    return img


def _extraction(**header):
    base = {"operador": None, "n_operador": None, "setor_maquina": None,
            "data": None, "turno": None} | header
    return {"header": base, "rows": [{"of": "263323", "qtd": "4"}], "footer": {}}


class _RescueProvider:
    """Motor falso: só o método de resgate, com registo do que recebeu."""

    name = "fake"

    def __init__(self, answer=None, fail=False):
        self.answer = answer or {}
        self.fail = fail
        self.calls: list = []

    def extract_header(self, image_path, template):
        from app.ocr.provider import OcrError
        self.calls.append(image_path)
        if self.fail:
            raise OcrError("resgate rebentou")
        return self.answer


def test_resgate_dispara_com_cabecalho_vazio_e_funde(tmp_path):
    from app.ocr.provider import rescue_header

    p = _RescueProvider(answer={"operador": "Zé Manel", "n_operador": "47",
                                "setor_maquina": "Rapid 20T - 2", "turno": "M"})
    out = rescue_header(p, _folha_img(tmp_path), CANTONEIRAS_KANBAN, _extraction())
    assert len(p.calls) == 1, "≥2 campos vazios + imagem → UMA segunda chamada"
    assert p.calls[0] != _folha_img(tmp_path), "a chamada leva o recorte, não a folha"
    assert out["header"]["operador"] == "Zé Manel"
    assert out["header"]["setor_maquina"] == "Rapid 20T - 2"
    assert out["header"]["turno"] == "M"
    assert out["_header_rescue"] is True
    assert out["rows"] == [{"of": "263323", "qtd": "4"}], "as linhas nunca se tocam"
    assert not p.calls[0].exists(), "o recorte temporário é apagado"


def test_resgate_nao_dispara_com_cabecalho_preenchido(tmp_path):
    from app.ocr.provider import rescue_header

    p = _RescueProvider(answer={"operador": "OUTRO"})
    cheio = _extraction(operador="Zé", n_operador="47", setor_maquina="Rapid 20T - 2")
    out = rescue_header(p, _folha_img(tmp_path), CANTONEIRAS_KANBAN, cheio)
    assert p.calls == [], "só 1 campo vazio (turno) → sem segunda chamada"
    assert "_header_rescue" not in out

    # sem imagem também não há resgate possível
    out2 = rescue_header(p, None, CANTONEIRAS_KANBAN, _extraction())
    assert p.calls == [] and "_header_rescue" not in out2


def test_resgate_nunca_pisa_valores_lidos(tmp_path):
    from app.ocr.provider import rescue_header

    p = _RescueProvider(answer={"operador": "IMPOSTOR", "n_operador": "99",
                                "setor_maquina": "Rapid 20T - 2", "turno": "M"})
    out = rescue_header(p, _folha_img(tmp_path), CANTONEIRAS_KANBAN,
                        _extraction(operador="Zé Manel"))
    assert out["header"]["operador"] == "Zé Manel", "o lido no extract manda"
    assert out["header"]["n_operador"] == "99", "os vazios preenchem-se"
    assert out["_header_rescue"] is True


def test_resgate_falhado_deixa_o_extract_intacto(tmp_path):
    import copy

    from app.ocr.provider import rescue_header

    p = _RescueProvider(fail=True)
    original = _extraction()
    antes = copy.deepcopy(original)
    out = rescue_header(p, _folha_img(tmp_path), CANTONEIRAS_KANBAN, original)
    assert out == antes, "falha no resgate não pode mudar nada"
    assert "_header_rescue" not in out

    # motor sem método de resgate (ex.: manual) → simplesmente não corre
    out2 = rescue_header(ManualEntryProvider(), _folha_img(tmp_path),
                         CANTONEIRAS_KANBAN, _extraction())
    assert "_header_rescue" not in out2


def test_recorte_do_cabecalho_multiplo_de_28(tmp_path):
    """O Qwen exige AMBAS as dimensões múltiplas de 28 (assert do GGML)."""
    from PIL import Image

    from app.ocr.provider import _crop_header_band

    img = tmp_path / "f.png"
    Image.new("RGB", (413, 601), "white").save(img)
    band = _crop_header_band(img)
    try:
        with Image.open(band) as b:
            w, h = b.size
        assert w % 28 == 0 and h % 28 == 0
        assert h <= 601 * 0.30 + 28, "só a faixa superior, não a folha inteira"
    finally:
        band.unlink()


def test_get_provider_encadeia_pelas_chaves(monkeypatch):
    from app.ocr import provider as mod

    class S:
        qwen_url = ""
        qwen_model = "qwen3.5:9b"
        qwen_timeout_s = 600.0
        qwen_no_think = True
        gemini_api_key = "g"
        ocr_model = "m"
        anthropic_api_key = "a"
        claude_ocr_model = "claude-haiku-4-5"

    monkeypatch.setattr(mod, "settings", S())
    p = mod.get_provider()
    assert p.name == "gemini" and p.fallback is not None and p.fallback.name == "claude"

    S.anthropic_api_key = ""
    p = mod.get_provider()
    assert p.name == "gemini" and p.fallback is None

    S.gemini_api_key = ""
    S.anthropic_api_key = "a"
    assert mod.get_provider().name == "claude"

    S.anthropic_api_key = ""
    assert mod.get_provider().name == "manual"

    # Qwen local definido → primário, com a cadeia cloud atrás
    S.qwen_url = "http://localhost:11434"
    S.gemini_api_key = "g"
    S.anthropic_api_key = "a"
    p = mod.get_provider()
    assert p.name == "qwen"
    assert p.fallback.name == "gemini" and p.fallback.fallback.name == "claude"

    # Qwen sem nenhuma chave cloud: primário sem rede, sem fallback
    S.gemini_api_key = ""
    S.anthropic_api_key = ""
    p = mod.get_provider()
    assert p.name == "qwen" and p.fallback is None


def test_checked_strip_uses_next_model_for_missing_or_misaligned_rows(tmp_path, monkeypatch):
    from app.ocr.provider import GeminiOcrProvider, extract_checked, OcrError
    from app.row_recovery import align_strip
    from app.templates_spec import get_template
    from tests.test_physical_rows import specimen_686
    paper,sheet=specimen_686()
    p=GeminiOcrProvider('test','first');p.models=['first','second','third']
    called=[]
    def response(model,body):
        called.append(model)
        rows=paper[1:] if model=='third' else (paper[1:2] if model=='first' else list(reversed(paper[1:])))
        return {'candidates':[{'content':{'parts':[{'text':__import__('json').dumps({'rows':rows})}]}}]}
    monkeypatch.setattr(p,'_call_model',response)
    image=tmp_path/'strip.png';image.write_bytes(b'fake')
    def validate(result):
        rows=result['rows']
        if len(rows)!=6:raise ValueError('Wrong count')
        align_strip(rows,list(range(2,8)),sheet['raw_extraction']['rows'],sheet['sheet_data']['rows'])
    result=extract_checked(p,image,get_template('cantoneiras_kanban'),validate)
    assert called==['first','second','third'] and len(result['rows'])==6
    called.clear();p.models=['first','second']
    with pytest.raises(OcrError):extract_checked(p,image,get_template('cantoneiras_kanban'),validate)
    assert called==['first','second']
