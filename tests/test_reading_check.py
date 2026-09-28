"""Verificação da leitura do OCR (casos reais de 26/09 com o Qwen 9B)."""

from pathlib import Path

from app.ocr import provider as p
from app.ocr import reading_check as rc
from app.templates_spec import get_template

TPL = get_template("cantoneiras_kanban")
KINDS = {"producao": TPL, "paragens": get_template("cantoneiras_paragens")}


def rows(*items):
    keys = ("cliente", "ov", "of", "perfil", "modelo", "qtd", "perf_comp")
    return [dict(zip(keys, item)) for item in items]


def extraction(row_list, engine="qwen:qwen3.5:9b", **meta):
    return {"header": {}, "rows": row_list, "footer": {},
            "_ocr": {"engine": engine, **meta}}


GOOD = rows(*[("MGROUP", "2509290", "262750", "150x15", f"ZG-{n}", "2", None)
              for n in (416, 426, 436, 522)])


def test_coluna_com_o_rotulo_impresso_nao_se_perde():
    """Folha 872: o Qwen devolveu «modelo_referencia» e o rodapé solto."""
    data = {"kind": "producao", "header": {"operador": "RAHUL JASWAL"},
            "rows": [{"cliente": "MGROUP", "modelo_referencia": "ZG-101", "qtd": "2"},
                     {"Modelo/Referência": "ZG-301", "QTD": "2"}],
            "metros_produzidos": "220", "horas_trabalhadas": "7:30"}
    out = p._clean_extraction(data, TPL)
    assert [r["modelo"] for r in out["rows"]] == ["ZG-101", "ZG-301"]
    assert out["rows"][1]["qtd"] == "2"
    assert out["footer"] == {"metros_produzidos": "220", "horas_trabalhadas": "7:30"}


def test_nome_exato_ganha_e_chaves_desconhecidas_caem():
    out = p._clean_extraction({"rows": [{"modelo": "A", "referencia": "B", "lixo": "x"}]}, TPL)
    assert out["rows"][0]["modelo"] == "A" and "lixo" not in out["rows"][0]


def test_instrucoes_dizem_as_chaves_exatas():
    for prompt in (p._extraction_prompt(TPL), p._auto_extraction_prompt(KINDS)):
        assert '"modelo": null' in prompt and '"footer": {"metros_produzidos": null' in prompt
    assert '"kind": "producao" | "paragens"' in p._auto_extraction_prompt(KINDS)


def test_resposta_cortada_fica_marcada():
    data = p._qwen_json('{"rows": [{"modelo": "A", "qtd": "2"}, {"modelo": "B", "q')
    assert data["_ocr"]["truncated"] is True and data["rows"][0]["modelo"] == "A"


def test_leitura_boa_nao_levanta_suspeitas():
    assert rc.problems_for(extraction(GOOD), TPL) == []


def test_linhas_coladas_e_qtd_absurda():
    """Folha 863: 15 linhas do papel lidas como 4."""
    bad = rows(("", "25041290\n2503573", "263210\n268?", "L75x5", None, "6\n2", None),
               (None, None, None, "L60x 6", None, "24122222", None))
    codes = {x["code"] for x in rc.problems_for(extraction(bad), TPL)}
    assert codes == {"celulas_com_varias_linhas", "qtd_absurda"}


def test_coluna_modelo_vazia_com_quantidades():
    empty = rows(*[("MGROUP", None, "262750", "150x15", None, "2", None)] * 5)
    assert [x["code"] for x in rc.problems_for(extraction(empty), TPL)] == ["modelos_em_falta"]
    # linhas de perfil completo não têm modelo: não contam
    full = rows(*[(None, None, "262750", "L70x6", None, None, "x")] * 5)
    assert rc.problems_for(extraction(full), TPL) == []


def test_resposta_cortada_e_suspeita():
    assert [x["code"] for x in rc.problems_for(extraction(GOOD, truncated=True), TPL)] == [
        "resposta_cortada"]


class Engine:
    def __init__(self, name, result=None, error=None, fallback=None):
        self.name, self.result, self.error, self.fallback = name, result, error, fallback
        self.calls = 0

    def extract_auto(self, image_path, kinds):
        self.calls += 1
        if self.error:
            raise p.OcrError(self.error)
        return "producao", self.result

    def extract(self, image_path, template):
        return self.extract_auto(image_path, None)[1]


BAD = extraction(rows(*[("MGROUP", None, "262750", "150x15", None, "2", None)] * 5))


def test_leitura_estragada_e_relida_pelo_motor_seguinte():
    claude = Engine("claude", extraction(GOOD, engine="claude:x"))
    gemini = Engine("gemini", extraction(GOOD, engine="gemini"), fallback=claude)
    qwen = Engine("qwen", BAD, fallback=gemini)
    template, out = rc.read_page(qwen, Path("x.png"), TPL, KINDS)
    assert template is TPL and out["rows"] == GOOD
    check = out[rc.META]
    assert check["suspect"] and check["reread"] and check["problems"] == []
    assert claude.calls == 0 and "relida por outro motor" in rc.message(check)
    assert "_ocr" not in out and "gemini" not in str(check), "quem leu não fica gravado"


def test_sem_motor_melhor_fica_a_primeira_marcada():
    gemini = Engine("gemini", error="quota")
    qwen = Engine("qwen", BAD, fallback=gemini)
    _, out = rc.read_page(qwen, Path("x.png"), TPL, KINDS)
    assert out["rows"] == BAD["rows"]
    assert out[rc.META]["problems"][0]["code"] == "modelos_em_falta"
    assert out[rc.META]["reread"] is False


def test_leitura_boa_nao_gasta_outro_motor():
    gemini = Engine("gemini", extraction(GOOD, engine="gemini"))
    qwen = Engine("qwen", extraction(GOOD), fallback=gemini)
    _, out = rc.read_page(qwen, Path("x.png"), TPL, KINDS)
    assert rc.META not in out and "_ocr" not in out and gemini.calls == 0


def test_motor_que_ja_leu_por_fallback_nao_e_repetido():
    """O Qwen caiu e o Gemini respondeu por ele: não se pergunta ao Gemini outra vez."""
    claude = Engine("claude", extraction(GOOD, engine="claude:x"))
    gemini = Engine("gemini", extraction(GOOD, engine="gemini"), fallback=claude)
    qwen = Engine("qwen", extraction(BAD["rows"], engine="gemini"), fallback=gemini)
    _, out = rc.read_page(qwen, Path("x.png"), TPL, KINDS)
    assert gemini.calls == 0 and claude.calls == 1 and out["rows"] == GOOD


def test_qwen_pede_memoria_de_trabalho_suficiente(monkeypatch):
    """Sem num_ctx vale o do Ollama (2048/4096): uma folha cheia não cabe."""
    import io
    import json
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
