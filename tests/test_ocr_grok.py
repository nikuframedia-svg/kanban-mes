"""Motor Grok (Bedrock Mantle): instruções, resposta curta, novas tentativas."""

import io
import json

import pytest

from app.ocr import provider as p
from app.templates_spec import get_template

KINDS = {"producao": get_template("cantoneiras_kanban"),
         "paragens": get_template("cantoneiras_paragens")}


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def answer(payload, status="completed"):
    return {"status": status, "output": [{"content": [
        {"type": "output_text", "text": json.dumps(payload)}]}]}


def image(tmp_path):
    from PIL import Image
    path = tmp_path / "folha.png"
    Image.new("RGB", (2400, 1700), "white").save(path)
    return path


def test_instrucoes_levam_regras_do_setor_e_resposta_curta():
    prompt = p._auto_extraction_prompt(KINDS, compact=True)
    assert "ALINHAMENTO DAS COLUNAS" in prompt and "é a letra I" in prompt
    assert "- producao: [cliente, ov, of, perfil, modelo, qtd, perf_comp]" in prompt
    # os outros motores mantêm as chaves completas, mas também com as regras
    full = p._auto_extraction_prompt(KINDS)
    assert "ALINHAMENTO DAS COLUNAS" in full and '"modelo": null' in full
    assert "ALINHAMENTO" not in p._extraction_prompt(get_template("chapa_kanban"))


def test_leitura_em_lista_vira_linhas_da_folha(tmp_path, monkeypatch):
    sent = {}

    def urlopen(req, timeout=None):
        sent.update(json.loads(req.data))
        return Response(json.dumps(answer({
            "kind": "producao", "header": {"operador": "RAHUL"},
            "rows": [["MGROUP", "2509290", "262750", "150x12", "ZG-101", "2", None],
                     [None, None, None, None, "ZG-301", "2", None]],
            "footer": {"metros_produzidos": "220"}})).encode())

    monkeypatch.setattr(p.urllib.request, "urlopen", urlopen)
    grok = p.GrokOcrProvider("k", "https://mantle/openai/v1", "xai.grok-4.6")
    kind, out = grok.extract_auto(image(tmp_path), KINDS)
    assert kind == "producao"
    assert [r["modelo"] for r in out["rows"]] == ["ZG-101", "ZG-301"]
    assert out["footer"]["metros_produzidos"] == "220"
    assert sent["model"] == "xai.grok-4.6" and sent["reasoning"] == {"effort": "low"}
    url = sent["input"][0]["content"][1]["image_url"]
    assert url.startswith("data:image/jpeg;base64,")


def test_erro_500_repete_e_depois_responde(tmp_path, monkeypatch):
    calls = []

    def urlopen(req, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            raise p.urllib.error.HTTPError(req.full_url, 500, "boom", {}, io.BytesIO(b"x"))
        return Response(json.dumps(answer({"kind": "producao", "header": {}, "rows": [],
                                           "footer": {}})).encode())

    monkeypatch.setattr(p.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(p.time, "sleep", lambda s: None)
    grok = p.GrokOcrProvider("k", "https://mantle", "m")
    assert grok.extract_auto(image(tmp_path), KINDS)[0] == "producao"
    assert len(calls) == 2


def test_erro_de_pedido_nao_repete_e_passa_ao_motor_seguinte(tmp_path, monkeypatch):
    calls = []

    def urlopen(req, timeout=None):
        calls.append(1)
        raise p.urllib.error.HTTPError(req.full_url, 400, "bad", {}, io.BytesIO(b"x"))

    class Reserva:
        name = "gemini"

        def extract_auto(self, image_path, templates):
            return "producao", {"header": {}, "rows": [], "footer": {}}

    monkeypatch.setattr(p.urllib.request, "urlopen", urlopen)
    grok = p.GrokOcrProvider("k", "https://mantle", "m", fallback=Reserva())
    assert grok.extract_auto(image(tmp_path), KINDS)[0] == "producao"
    assert len(calls) == 1


def test_resposta_cortada_e_erro(tmp_path, monkeypatch):
    monkeypatch.setattr(p.urllib.request, "urlopen", lambda req, timeout=None: Response(
        json.dumps(answer({"rows": []}, status="incomplete")).encode()))
    with pytest.raises(p.OcrError, match="incomplete"):
        p.GrokOcrProvider("k", "https://mantle", "m").extract_auto(image(tmp_path), KINDS)


def test_grok_e_o_motor_quando_ha_chave(monkeypatch):
    from dataclasses import replace
    monkeypatch.setattr(p, "settings", replace(p.settings, bedrock_api_key="k",
                                               ocr_provider="", qwen_url=""))
    assert p.get_provider().name == "grok"
    monkeypatch.setattr(p, "settings", replace(p.settings, bedrock_api_key="",
                                               ocr_provider="grok"))
    with pytest.raises(RuntimeError):
        p.get_provider()


def test_lote_le_varias_folhas_ao_mesmo_tempo_sem_pausa(monkeypatch):
    import threading
    from dataclasses import replace
    from app.web import main
    monkeypatch.setattr(main, "settings", replace(main.settings, ocr_parallel=3))
    monkeypatch.setattr(main.time, "sleep", lambda s: pytest.fail("sem pausa com API paga"))
    active, peak, seen = [0], [0], []
    lock = threading.Lock()
    pause = threading.Event()

    def process(uid):
        with lock:
            active[0] += 1; peak[0] = max(peak[0], active[0]); seen.append(uid)
        pause.wait(0.05)
        with lock:
            active[0] -= 1

    monkeypatch.setattr(main, "_process_sheet", process)
    main._run_sheets(["a", "b", "c", "d"])
    assert sorted(seen) == ["a", "b", "c", "d"] and peak[0] == 3
