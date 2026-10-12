"""OCR das folhas kanban.

Providers:
- QwenOcrProvider — motor LOCAL (Ollama a servir qwen3.5:9b na GPU do PC da
  fábrica). Ativa-se com MES_QWEN_URL e passa a ser o principal: sem quotas e
  as imagens nunca saem da infraestrutura. Transporte e truques portados do
  sistema OCR da Metalogalva (ocr6.py), comprovado em produção.
- GeminiOcrProvider — lê a foto com a Gemini API e devolve {header, rows, footer}
  em JSON forçado por schema. Transcreve fielmente: o OCR NÃO corrige nada;
  correções são trabalho do motor de cruzamento + revisão humana.
- ClaudeOcrProvider — API da Anthropic (ANTHROPIC_API_KEY). Motor principal
  no servidor desde 12/10 (MES_OCR_PROVIDER=claude), com o Grok de reserva;
  sem MES_OCR_PROVIDER é o último recurso da cadeia Gemini, como sempre.
- GrokOcrProvider — xai.grok-4.6 no AWS Bedrock (MES_BEDROCK_API_KEY).
- ManualEntryProvider — sem chave configurada (ou sem foto), folha vazia para
  preenchimento manual. O resto da app funciona exatamente da mesma forma.

O prompt e a limpeza do resultado são partilhados pelos motores: o que muda de
um para o outro é o transporte, nunca as regras de transcrição.
"""

from __future__ import annotations

import base64
import http.client
import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Protocol

from ..config import settings
from . import sector_rules
from ..templates_spec import KanbanTemplate


class OcrError(Exception):
    """Falha de OCR (rede, quota, resposta inválida). O chamador decide o fallback."""


class OcrProvider(Protocol):
    name: str

    def extract(self, image_path: Path, template: KanbanTemplate) -> dict:
        """Devolve {"header": {...}, "rows": [{...}], "footer": {...}}."""
        ...


class ManualEntryProvider:
    """Sem OCR: devolve uma folha vazia com 10 linhas para preenchimento manual."""

    name = "manual"

    def extract(self, image_path: Path, template: KanbanTemplate) -> dict:
        return empty_extraction(template)


def empty_extraction(template: KanbanTemplate, n_rows: int = 10) -> dict:
    return {
        "header": {f: None for f in template.header_fields},
        "rows": [{f: None for f in template.row_fields} for _ in range(n_rows)],
        "footer": {f: None for f in template.footer_fields},
    }


_MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".heic": "image/heic", ".heif": "image/heif",
}

_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


# quotas do free tier variam por modelo e mudam sem aviso: se o primário esgotar
# (429), tenta-se o seguinte — o resultado é igual, só muda o motor
_FALLBACK_MODELS = ("gemini-3.1-flash-lite-preview", "gemini-flash-latest")


# ---- prompts e limpeza, partilhados pelos motores ----

def _json_shape(templates: dict[str, KanbanTemplate], with_kind: bool = False) -> str:
    """A estrutura JSON exata da resposta, como no OCR original
    (nikuframedia-svg/ocr, «Return this exact JSON structure»).

    O Gemini e o Claude recebem um schema no pedido; o Qwen só recebe este
    texto. Sem a estrutura escrita, o Qwen inventava chaves a partir do rótulo
    impresso («Modelo/Referência» → `modelo_referencia`) e a coluna inteira
    perdia-se (folha real, 26/09: 14 linhas, 0 modelos).
    """
    def obj(fields: tuple[str, ...]) -> str:
        return "{" + ", ".join(f'"{f}": null' for f in fields) + "}"
    header = obj(_union_fields(templates, lambda t: t.header_fields))
    footer = obj(_union_fields(templates, lambda t: t.footer_fields))
    kind = ('"kind": "' + '" ou "'.join(templates) + '",\n  ') if with_kind else ""
    if len(templates) == 1:
        rows = obj(next(iter(templates.values())).row_fields)
        per_face = ""
    else:
        rows = "…uma entrada por linha…"
        per_face = "\nCada entrada de `rows` usa as chaves da face identificada:\n" + "\n".join(
            f"- {k}: {obj(t.row_fields)}" for k, t in templates.items())
    return (
        "Devolve EXATAMENTE esta estrutura JSON, com estes nomes de chaves "
        "(não os rótulos impressos na folha):\n"
        f'{{\n  {kind}"header": {header},\n  "rows": [{rows}],\n  "footer": {footer}\n}}'
        f"{per_face}"
    )


def _compact_shape(templates: dict[str, KanbanTemplate], with_kind: bool = False) -> str:
    """Resposta curta: cada linha é uma lista pela ordem das colunas.

    O Grok escreve devagar (~7 tokens/s): sem repetir os nomes das chaves em
    cada linha a resposta cai para menos de metade (medido: 724 → 326 tokens,
    155 s → 72 s por folha) sem perder acerto."""
    def keys(fields):
        return "{" + ", ".join(f'"{f}": ...' for f in fields) + "}"
    header = keys(_union_fields(templates, lambda t: t.header_fields))
    footer = keys(_union_fields(templates, lambda t: t.footer_fields))
    kind = ('"kind": "' + '" ou "'.join(templates) + '", ') if with_kind else ""
    faces = "\n".join(f"- {k}: [" + ", ".join(t.row_fields) + "]" for k, t in templates.items())
    return (
        "Responde APENAS com JSON compacto, numa só linha, sem espaços nem indentação:\n"
        f'{{{kind}"header": {header}, "rows": [[...], [...]], "footer": {footer}}}\n'
        "Cada linha de `rows` é uma LISTA com um valor por coluna, nesta ordem fixa "
        "(null na posição de uma célula vazia; nunca saltes posições):\n" + faces
    )


def _expand_compact(data: dict, templates: dict[str, KanbanTemplate]) -> dict:
    """Linhas em lista (resposta curta) → dicionários pelas colunas da face."""
    rows = data.get("rows") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not any(isinstance(r, list) for r in rows):
        return data
    kind = data.get("kind") if data.get("kind") in templates else next(iter(templates))
    order = templates[kind].row_fields
    data["rows"] = [dict(zip(order, r)) if isinstance(r, list) else r for r in rows]
    return data


def _extraction_prompt(template: KanbanTemplate, compact: bool = False) -> str:
    labels = template.field_labels or {}
    row_desc = "; ".join(f"{f} = «{labels.get(f, f)}»" for f in template.row_fields)
    return (
        "Estás a transcrever uma folha kanban manuscrita de uma "
        f"fábrica metalomecânica portuguesa ({template.label}).\n"
        "REGRAS ESTRITAS:\n"
        "1. Transcreve EXATAMENTE o que está escrito, mesmo que pareça errado. "
        "NÃO corrijas, NÃO completes, NÃO normalizes códigos nem datas.\n"
        "2. Célula vazia ou ilegível → null. Nunca inventes valores.\n"
        "3. Uma entrada em `rows` por cada linha da tabela COM ALGO escrito, até "
        "à última — não saltes nenhuma linha nem juntes duas linhas numa só; "
        "ignora linhas totalmente vazias. Uma linha com apenas perfil e X em "
        "«Perf. Comp.» é uma linha independente: mantém modelo e quantidade "
        "a null. Nunca juntes esse perfil ou X à referência da linha anterior.\n"
        "4. Números: transcreve os dígitos tal como escritos (sem unidades). "
        "Um visto/cruz numa célula transcreve-se como «x».\n"
        "5. Cabeçalho impresso no topo da folha — transcreve TODOS os campos: "
        "`operador` = o nome manuscrito no campo «Operador»; `n_operador` = os "
        "dígitos do campo «N.º» (ao lado do nome); `setor_maquina` = o campo "
        "«Setor/Máquina» (ex.: «Rapid 20T - 2»); `data` = o campo «Data»; "
        "`turno` = a caixa assinalada com cruz entre «M», «R» e «XM», se alguma.\n"
        "6. Valores repetidos por linhas seguidas (ex.: cliente escrito uma vez para "
        "várias linhas) transcrevem-se só na linha onde estão escritos.\n"
        "7. Atenção às DUAS ÚLTIMAS colunas, que se confundem facilmente: o que "
        "estiver na coluna «QTD» vai para `qtd` e o que estiver na última coluna "
        "vai para `perf_comp`. Se uma delas estiver vazia na folha, deixa-a a null "
        "— não desloques valores de uma coluna para a outra.\n"
        f"Colunas da tabela, pela ordem da folha: {row_desc}.\n"
        + sector_rules.for_templates([template.name])
        + (_compact_shape({template.name: template}) if compact
           else _json_shape({template.name: template}))
    )


def _auto_extraction_prompt(templates: dict[str, KanbanTemplate], compact: bool = False) -> str:
    faces = []
    for kind, t in templates.items():
        labels = t.field_labels or {}
        cols = "; ".join(f"{f} = «{labels.get(f, f)}»" for f in t.row_fields)
        faces.append(f"- «{kind}» ({t.label}): {cols}")
    faces_desc = "\n".join(faces)
    return (
        "Estás a transcrever uma página digitalizada de uma folha kanban "
        "manuscrita de uma fábrica metalomecânica portuguesa (TPL102).\n"
        "A folha tem duas faces possíveis, com colunas IMPRESSAS diferentes:\n"
        f"{faces_desc}\n"
        "PRIMEIRO: identifica a face pelos cabeçalhos impressos da tabela e "
        "devolve-a em `kind`.\n"
        "DEPOIS transcreve APENAS as colunas dessa face; deixa os campos da "
        "outra face a null.\n"
        "REGRAS ESTRITAS:\n"
        "1. Transcreve EXATAMENTE o que está escrito, mesmo que pareça errado. "
        "NÃO corrijas, NÃO completes, NÃO normalizes códigos nem datas.\n"
        "2. Célula vazia ou ilegível → null. Nunca inventes valores.\n"
        "3. Uma entrada em `rows` por cada linha da tabela COM ALGO escrito, até "
        "à última — não saltes nenhuma linha nem juntes duas linhas numa só; "
        "ignora linhas totalmente vazias. Página sem nada manuscrito → `rows` vazio. "
        "Uma linha com apenas perfil e X em «Perf. Comp.» é independente: "
        "não juntes esse perfil ou X à referência da linha anterior.\n"
        "4. Números: transcreve os dígitos tal como escritos (sem unidades). "
        "Um visto/cruz numa célula transcreve-se como «x».\n"
        "5. Cabeçalho impresso no topo (igual nas duas faces) — transcreve TODOS "
        "os campos: `operador` = o nome manuscrito no campo «Operador»; "
        "`n_operador` = os dígitos do campo «N.º» (ao lado do nome); "
        "`setor_maquina` = o campo «Setor/Máquina» (ex.: «Rapid 20T - 2»); "
        "`data` = o campo «Data»; `turno` = a caixa assinalada com cruz entre "
        "«M», «R» e «XM», se alguma.\n"
        "6. Valores repetidos por linhas seguidas (ex.: cliente escrito uma vez para "
        "várias linhas) transcrevem-se só na linha onde estão escritos.\n"
        "7. Na face de produção, atenção às DUAS ÚLTIMAS colunas, que se confundem "
        "facilmente: o que estiver na coluna «QTD» vai para `qtd` e o que estiver na "
        "última coluna vai para `perf_comp`. Se uma delas estiver vazia na folha, "
        "deixa-a a null — não desloques valores de uma coluna para a outra.\n"
        + sector_rules.for_templates(t.name for t in templates.values())
        + (_compact_shape(templates, with_kind=True) if compact
           else _json_shape(templates, with_kind=True))
    )


# ---- resgate do cabeçalho (segunda chamada focada) ----

# A data manuscrita é evidência; a data final mantém a regra do dia útil anterior.
# O gatilho automático continua a considerar apenas os quatro campos de identificação.
HEADER_RESCUE_FIELDS = ("operador", "n_operador", "setor_maquina", "data", "turno")


def _header_rescue_prompt(template: KanbanTemplate) -> str:
    return (
        "Estás a ver APENAS a faixa superior (o cabeçalho impresso) de uma "
        "folha kanban manuscrita de uma fábrica metalomecânica portuguesa "
        f"({template.label}).\n"
        "Transcreve SÓ estes campos, pelos nomes impressos na folha:\n"
        "- `operador` = o nome manuscrito no campo «Operador»;\n"
        "- `n_operador` = os dígitos do campo «N.º» (ao lado do nome);\n"
        "- `setor_maquina` = o campo «Setor/Máquina» (ex.: «Rapid 20T - 2»);\n"
        "- `data` = a data manuscrita no campo «Data», sem assumir a data da digitalização;\n"
        "- `turno` = a caixa assinalada com cruz entre «M», «R» e «XM», se alguma.\n"
        "REGRAS ESTRITAS: transcreve EXATAMENTE o que está escrito, sem corrigir "
        "nem completar; campo vazio ou ilegível → null; nunca inventes valores.\n"
        'Devolve apenas o JSON {"operador": …, "n_operador": …, '
        '"setor_maquina": …, "data": …, "turno": …}.'
    )


def _clean_header_fields(data: object) -> dict:
    """Resposta do resgate → só os 4 campos, com o mesmo strip do resto."""
    src = data if isinstance(data, dict) else {}
    # há modelos que embrulham na mesma em {"header": {...}} — aceita-se
    if isinstance(src.get("header"), dict):
        src = src["header"]
    out = {}
    for f in HEADER_RESCUE_FIELDS:
        v = src.get(f)
        out[f] = str(v).strip() or None if v is not None else None
    return out


# Fração da altura que cobre o cabeçalho da TPL102 com folga.
_HEADER_BAND_FRACTION = 0.30


def _crop_header_band(image_path: Path) -> Path:
    """Recorta a faixa superior da folha para a chamada de resgate.

    Escreve um JPEG temporário (quem chama apaga-o). As dimensões
    arredondam-se a múltiplos de 28 com a mesma regra do transporte Qwen
    (`_round_to_stride`): o modelo local rejeita outras dimensões e para os
    motores cloud o arredondamento é inócuo.
    """
    import tempfile

    from PIL import Image

    with Image.open(image_path) as raw:
        rgb = raw.convert("RGB")
    band = rgb.crop((0, 0, rgb.size[0],
                     max(1, round(rgb.size[1] * _HEADER_BAND_FRACTION))))
    target = (_round_to_stride(band.size[0]), _round_to_stride(band.size[1]))
    if target != band.size:
        band = band.resize(target, Image.Resampling.LANCZOS)
    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    try:
        band.save(tmp, format="JPEG", quality=90)
    finally:
        tmp.close()
    return Path(tmp.name)


def rescue_header(provider: "OcrProvider", image_path: Path | None,
                  template: KanbanTemplate, extraction: dict) -> dict:
    """Resgate do cabeçalho — corre DEPOIS do extract, para qualquer motor.

    Sintoma real: kanbans capturados com operador/n_operador/setor_maquina/
    turno vazios porque o modelo gasta a atenção na tabela. Se ≥2 destes 4
    campos vierem vazios e houver imagem, recorta-se a faixa superior e
    faz-se UMA segunda chamada focada só no cabeçalho. A fusão só preenche o
    que estava vazio — nunca pisa valores lidos, nunca toca nas linhas — e
    qualquer falha deixa a extração original intacta: o resgate é
    oportunista, não é caminho crítico.
    """
    header = extraction.get("header") or {}
    fields = [f for f in HEADER_RESCUE_FIELDS if f in template.header_fields]
    empty = [f for f in fields if f != "data" and not str(header.get(f) or "").strip()]
    extract_header = getattr(provider, "extract_header", None)
    if len(empty) < 2 or not image_path or extract_header is None:
        return extraction
    band: Path | None = None
    try:
        band = _crop_header_band(image_path)
        rescued = _clean_header_fields(extract_header(band, template))
    except (OcrError, OSError, ValueError):
        return extraction
    finally:
        if band is not None:
            try:
                band.unlink()
            except OSError:
                pass
    for f in fields:
        value = str(rescued.get(f) or "").strip()
        if value and not str(header.get(f) or "").strip():
            header[f] = value
    extraction["header"] = header
    extraction["_header_rescue"] = True
    return extraction


def _clean_extraction(data: dict, template: KanbanTemplate) -> dict:
    """Do JSON do modelo para a extração canónica do template."""
    def clean(section: object, fields: tuple[str, ...]) -> dict:
        src = section if isinstance(section, dict) else {}
        out = {}
        for f in fields:
            v = src.get(f)
            out[f] = str(v).strip() or None if v is not None else None
        return out

    rows_raw = data.get("rows") if isinstance(data.get("rows"), list) else []
    rows = [clean(r, template.row_fields) for r in rows_raw]
    rows = [r for r in rows if any(v is not None for v in r.values())]
    if not rows:
        rows = [{f: None for f in template.row_fields}]
    return {
        "header": clean(data.get("header"), template.header_fields),
        "rows": rows,
        "footer": clean(data.get("footer"), template.footer_fields),
    }


def _resolve_kind(data: dict, templates: dict[str, KanbanTemplate]) -> str:
    """Kind em falta/inválido → primeira face (produção), o comportamento
    antigo do classificador em dúvida."""
    kind = data.get("kind")
    return kind if kind in templates else next(iter(templates))


def _union_fields(templates: dict[str, KanbanTemplate], getter) -> tuple[str, ...]:
    out: list[str] = []
    for t in templates.values():
        out.extend(f for f in getter(t) if f not in out)
    return tuple(out)


# ── Qwen local (Ollama) ──────────────────────────────────────────────────────
# Transporte e parse portados do sistema OCR original da Metalogalva
# (nikuframedia-svg/ocr, ocr6.py) — lições pagas em fábrica que não vale a
# pena reaprender:
# - endpoint NATIVO /api/generate (a camada OpenAI-compat e o json mode do
#   Ollama crashavam o runner com estes modelos): JSON só por prompt + parse
#   tolerante;
# - o Qwen2.5/3.5-VL agrupa patches 14x14 em blocos 2x2 → AMBAS as dimensões
#   da imagem têm de ser múltiplas de 28, senão o GGML dá assert e mata o
#   runner; e como o assert dispara de forma intermitente perto de 1288,
#   há uma escada de tamanhos 1288→1120→1008;
# - "think": false nem sempre é honrado por builds custom → reforça-se com
#   a convenção Qwen «/no_think» no fim do prompt (cinto e suspensórios).

_QWEN_STRIDE = 28
_QWEN_EDGES = (1288, 1120, 1008)   # 46/40/36 × 28
_QWEN_NUM_PREDICT = 8192


def _round_to_stride(value: int) -> int:
    rounded = ((value + _QWEN_STRIDE // 2) // _QWEN_STRIDE) * _QWEN_STRIDE
    return max(_QWEN_STRIDE, rounded)


def _qwen_image_b64(image_path: Path, max_edge: int) -> str:
    """Imagem → JPEG base64 com ambas as dimensões múltiplas de 28."""
    import io

    from PIL import Image

    with Image.open(image_path) as raw:
        rgb = raw.convert("RGB")
    scale = min(1.0, max_edge / max(rgb.size))
    target = (_round_to_stride(round(rgb.size[0] * scale)),
              _round_to_stride(round(rgb.size[1] * scale)))
    if target != rgb.size:
        rgb = rgb.resize(target, Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    rgb.save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _salvage_truncated_json(snippet: str) -> dict | None:
    """Recupera um JSON cortado a meio (modelo bateu no teto de tokens a meio
    das linhas). Fecha os containers ainda abertos e tenta parsar; se falhar,
    recua até ao `}` anterior (descarta a linha incompleta) e repete. Devolve
    o JSON com o máximo de linhas completas, ou None."""
    best = snippet
    while best:
        stack: list[str] = []
        in_str = esc = False
        balanced = True
        for ch in best:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = not in_str
            elif not in_str:
                if ch in "{[":
                    stack.append("}" if ch == "{" else "]")
                elif ch in "}]":
                    if not stack:
                        balanced = False
                        break
                    stack.pop()
        if balanced and not in_str:
            candidate = best.rstrip().rstrip(",") + "".join(reversed(stack))
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
        cut = best.rfind("}")
        if cut == -1:
            return None
        best = best[:cut]
    return None


def _qwen_json(raw: str) -> dict:
    """Remove <think>…</think> e fences de markdown, e extrai o JSON.
    Levanta ValueError se não houver JSON recuperável."""
    import re

    text = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    # bloco <think> SEM fecho (o modelo divagou até ao teto sem o fechar):
    # o regex acima não o apanha; salta o prefixo até ao primeiro '{'
    if "<think>" in text and "{" in text:
        text = text[text.index("{"):]
    if "```" in text:
        for part in text.split("```"):
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                text = part
                break
    s, e = text.find("{"), text.rfind("}") + 1
    if s == -1 or e <= s:
        raise ValueError("resposta sem delimitadores JSON")
    try:
        return json.loads(text[s:e])
    except json.JSONDecodeError:
        salvaged = _salvage_truncated_json(text[s:])
        if salvaged is not None:
            return salvaged
        raise ValueError("JSON irrecuperável na resposta") from None


class QwenOcrProvider:
    """Motor local: Ollama a servir um modelo de visão Qwen na GPU da fábrica.

    Sem schema no transporte — a resposta é texto que se limpa com
    `_qwen_json` e depois passa pelo mesmo `_clean_extraction` dos outros
    motores (campos a mais caem, campos em falta ficam None, `kind` inválido
    resolve para a primeira face).
    """

    name = "qwen"

    def __init__(self, url: str, model: str, timeout_s: float = 600.0,
                 no_think: bool = True, fallback: "OcrProvider | None" = None):
        self.url = url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self.no_think = no_think
        self.fallback = fallback

    def _with_fallback(self, method: str, fn, *args):
        try:
            return fn(*args)
        except OcrError as exc:
            if self.fallback is None:
                raise
            try:
                return getattr(self.fallback, method)(*args)
            except OcrError as exc2:
                raise OcrError(f"{exc}; fallback: {exc2}") from exc2

    def _call(self, image_b64: str, prompt: str) -> str:
        prompt_text = f"{prompt}\n/no_think" if self.no_think else prompt
        payload: dict = {
            "model": self.model,
            "prompt": prompt_text,
            "images": [image_b64],
            "stream": False,
            "keep_alive": -1,      # modelo residente na GPU entre folhas
            "options": {"temperature": 0, "num_predict": _QWEN_NUM_PREDICT,
                        "num_ctx": settings.qwen_num_ctx},
        }
        if self.no_think:
            payload["think"] = False
        req = urllib.request.Request(
            f"{self.url}/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "ignore")[:300]
            raise OcrError(f"Qwen [{self.model}] HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, OSError, http.client.HTTPException,
                TimeoutError, json.JSONDecodeError) as exc:
            raise OcrError(f"Qwen [{self.model}] indisponível: {exc}") from exc
        text = data.get("response")
        if not text:
            raise OcrError(f"Qwen [{self.model}]: resposta vazia")
        return text

    def _request_json(self, image_path: Path, prompt: str) -> dict:
        """Escada de tamanhos × 2 tentativas cada (máx. 6 chamadas): o retry
        com a MESMA imagem recupera JSON malformado ocasional (estado de
        sampling), e o tamanho menor contorna o assert intermitente do GGML."""
        if not image_path or not image_path.is_file():
            raise OcrError(f"Imagem não encontrada: {image_path}")
        last_error: Exception | None = None
        for edge in _QWEN_EDGES:
            image_b64 = _qwen_image_b64(image_path, edge)
            for _attempt in (1, 2):
                try:
                    return _qwen_json(self._call(image_b64, prompt))
                except OcrError as exc:
                    last_error = exc
                except ValueError as exc:
                    last_error = OcrError(f"Qwen [{self.model}]: {exc}")
                time.sleep(1.0)
        raise last_error or OcrError("Qwen: erro desconhecido")

    def extract(self, image_path: Path, template: KanbanTemplate) -> dict:
        return self._with_fallback("extract", self._extract, image_path, template)

    def _extract(self, image_path: Path, template: KanbanTemplate) -> dict:
        data = self._request_json(image_path, _extraction_prompt(template))
        return _clean_extraction(data, template)

    def extract_auto(self, image_path: Path,
                     templates: dict[str, KanbanTemplate]) -> tuple[str, dict]:
        return self._with_fallback("extract_auto", self._extract_auto,
                                   image_path, templates)

    def _extract_auto(self, image_path: Path,
                      templates: dict[str, KanbanTemplate]) -> tuple[str, dict]:
        data = self._request_json(image_path, _auto_extraction_prompt(templates))
        kind = _resolve_kind(data, templates)
        return kind, _clean_extraction(data, templates[kind])

    def extract_header(self, image_path: Path, template: KanbanTemplate) -> dict:
        """Chamada de resgate: só os 4 campos do cabeçalho (ver rescue_header)."""
        return self._with_fallback("extract_header", self._extract_header,
                                   image_path, template)

    def _extract_header(self, image_path: Path, template: KanbanTemplate) -> dict:
        data = self._request_json(image_path, _header_rescue_prompt(template))
        return _clean_header_fields(data)


class GeminiOcrProvider:
    name = "gemini"

    def __init__(self, api_key: str, model: str, timeout_s: float = 120.0,
                 fallback: "OcrProvider | None" = None):
        self.api_key = api_key
        self.models = [model] + [m for m in _FALLBACK_MODELS if m != model]
        self.timeout_s = timeout_s
        # último recurso quando a cadeia Gemini INTEIRA falha (ex.: Claude)
        self.fallback = fallback

    def _with_fallback(self, method: str, fn, *args):
        """Corre `fn`; se toda a cadeia Gemini falhar e houver um motor de
        último recurso, passa-lhe o trabalho (mesmo método, mesmos args). O
        erro final identifica os dois motores — quem lê o `_ocr_error` fica a
        saber o que foi tentado."""
        try:
            return fn(*args)
        except OcrError as exc:
            if self.fallback is None:
                raise
            try:
                return getattr(self.fallback, method)(*args)
            except OcrError as exc2:
                raise OcrError(f"{exc}; último recurso: {exc2}") from exc2

    # ---- pedido ----

    def _schema(self, template: KanbanTemplate) -> dict:
        def obj(fields: tuple[str, ...]) -> dict:
            return {
                "type": "OBJECT",
                "properties": {f: {"type": "STRING", "nullable": True} for f in fields},
            }
        return {
            "type": "OBJECT",
            "properties": {
                "header": obj(template.header_fields),
                "rows": {"type": "ARRAY", "items": obj(template.row_fields)},
                "footer": obj(template.footer_fields),
            },
            "required": ["header", "rows", "footer"],
        }

    def _request_body(self, image_bytes: bytes, mime: str, template: KanbanTemplate) -> dict:
        return {
            "contents": [{
                "parts": [
                    {"inline_data": {
                        "mime_type": mime,
                        "data": base64.b64encode(image_bytes).decode("ascii"),
                    }},
                    {"text": _extraction_prompt(template)},
                ],
            }],
            "generationConfig": {
                "temperature": 0,
                "response_mime_type": "application/json",
                "response_schema": self._schema(template),
            },
        }

    def _call_model(self, model: str, body: dict) -> dict:
        req = urllib.request.Request(
            f"{_API_BASE}/{model}:generateContent",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": self.api_key,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _call(self, body: dict, validate=None) -> dict:
        """Tenta cada modelo da cadeia; por modelo, uma repetição em erro transitório.

        429 (quota) e 404 (modelo desconhecido) saltam logo para o modelo
        seguinte; 5xx esgota as duas tentativas e só então passa ao seguinte —
        um 503 de «high demand» é do modelo, não da conta, e os fallbacks
        existem exatamente para isso. Erros de pedido (400/401/403) rebentam
        já: tentar outro modelo com a mesma chave inválida não resolve nada.
        """
        last_error: Exception | None = None
        for model in self.models:
            for attempt in (1, 2):
                try:
                    response = self._call_model(model, body)
                    if validate is not None:
                        try:
                            validate(response)
                        except (OcrError, ValueError) as exc:
                            last_error = OcrError(f"Gemini [{model}]: {exc}")
                            break  # A successful HTTP response can still omit physical rows.
                    return response
                except urllib.error.HTTPError as exc:
                    detail = exc.read().decode("utf-8", "ignore")[:300]
                    last_error = OcrError(f"Gemini [{model}] HTTP {exc.code}: {detail}")
                    if exc.code in (429, 404):
                        break                      # quota/modelo inexistente → próximo
                    if exc.code not in (500, 502, 503, 504):
                        raise last_error from exc
                    if attempt == 2:
                        break                      # 5xx persistente → próximo modelo
                except (urllib.error.URLError, OSError, http.client.HTTPException,
                        TimeoutError, json.JSONDecodeError) as exc:
                    # OSError apanha ConnectionResetError e afins: uma ligação
                    # cortada a meio da resposta não é URLError e furava os
                    # retries E o fallback pago, deixando a folha pendurada.
                    last_error = OcrError(f"Gemini [{model}] indisponível: {exc}")
                    if attempt == 2:
                        break                      # rede instável → tenta outro modelo
                time.sleep(4.0)
        raise last_error or OcrError("Gemini: erro desconhecido")

    # ---- resposta → extração ----

    @staticmethod
    def _response_json(response: dict, engine: str = "Gemini") -> dict:
        try:
            parts = response["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts)
            return json.loads(text)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise OcrError(f"Resposta {engine} sem JSON válido: {exc}") from exc

    def _parse(self, response: dict, template: KanbanTemplate) -> dict:
        return _clean_extraction(self._response_json(response), template)

    def extract(self, image_path: Path, template: KanbanTemplate) -> dict:
        return self._with_fallback("extract", self._extract, image_path, template)

    def _extract(self, image_path: Path, template: KanbanTemplate) -> dict:
        if not image_path or not image_path.is_file():
            raise OcrError(f"Imagem não encontrada: {image_path}")
        mime = _MIME_BY_SUFFIX.get(image_path.suffix.lower(), "image/jpeg")
        body = self._request_body(image_path.read_bytes(), mime, template)
        return self._parse(self._call(body), template)

    # ---- frente/verso numa só chamada ----

    def _auto_schema(self, templates: dict[str, KanbanTemplate]) -> dict:
        """Schema união: o modelo diz que face é (`kind`) E transcreve-a, num
        só pedido. Antes eram duas chamadas por página — a classificação
        gastava metade da quota do free tier numa pergunta trivial."""
        def obj(fields: tuple[str, ...]) -> dict:
            return {
                "type": "OBJECT",
                "properties": {f: {"type": "STRING", "nullable": True} for f in fields},
            }
        return {
            "type": "OBJECT",
            "properties": {
                "kind": {"type": "STRING", "enum": list(templates)},
                "header": obj(_union_fields(templates, lambda t: t.header_fields)),
                "rows": {"type": "ARRAY",
                         "items": obj(_union_fields(templates, lambda t: t.row_fields))},
                "footer": obj(_union_fields(templates, lambda t: t.footer_fields)),
            },
            "required": ["kind", "header", "rows", "footer"],
        }

    def extract_auto(self, image_path: Path,
                     templates: dict[str, KanbanTemplate]) -> tuple[str, dict]:
        """Classifica a face E transcreve numa só chamada.

        `templates` = {"producao": …, "paragens": …}. Devolve (kind, extração
        limpa pelo template dessa face).
        """
        return self._with_fallback("extract_auto", self._extract_auto,
                                   image_path, templates)

    def _extract_auto(self, image_path: Path,
                      templates: dict[str, KanbanTemplate]) -> tuple[str, dict]:
        if not image_path or not image_path.is_file():
            raise OcrError(f"Imagem não encontrada: {image_path}")
        mime = _MIME_BY_SUFFIX.get(image_path.suffix.lower(), "image/jpeg")
        body = {
            "contents": [{
                "parts": [
                    {"inline_data": {
                        "mime_type": mime,
                        "data": base64.b64encode(image_path.read_bytes()).decode("ascii"),
                    }},
                    {"text": _auto_extraction_prompt(templates)},
                ],
            }],
            "generationConfig": {
                "temperature": 0,
                "response_mime_type": "application/json",
                "response_schema": self._auto_schema(templates),
            },
        }
        data = self._response_json(self._call(body))
        kind = _resolve_kind(data, templates)
        return kind, _clean_extraction(data, templates[kind])

    def extract_header(self, image_path: Path, template: KanbanTemplate) -> dict:
        """Chamada de resgate: só os 4 campos do cabeçalho (ver rescue_header)."""
        return self._with_fallback("extract_header", self._extract_header,
                                   image_path, template)

    def _extract_header(self, image_path: Path, template: KanbanTemplate) -> dict:
        if not image_path or not image_path.is_file():
            raise OcrError(f"Imagem não encontrada: {image_path}")
        mime = _MIME_BY_SUFFIX.get(image_path.suffix.lower(), "image/jpeg")
        body = {
            "contents": [{
                "parts": [
                    {"inline_data": {
                        "mime_type": mime,
                        "data": base64.b64encode(image_path.read_bytes()).decode("ascii"),
                    }},
                    {"text": _header_rescue_prompt(template)},
                ],
            }],
            "generationConfig": {
                "temperature": 0,
                "response_mime_type": "application/json",
                "response_schema": {
                    "type": "OBJECT",
                    "properties": {
                        f: {"type": "STRING", "nullable": True}
                        for f in HEADER_RESCUE_FIELDS
                    },
                },
            },
        }
        return _clean_header_fields(self._response_json(self._call(body)))


_ocr_log = logging.getLogger("uvicorn.error.ocr")


class _ApiVisionProvider:
    """Motor por API com as instruções curtas (linhas em lista) e as regras do setor.

    Partilhado pelo Claude e pelo Grok: o que muda de um para o outro é só o
    transporte (``_call``, que devolve o JSON lido). Se o motor falhar, o
    trabalho passa ao seguinte (``fallback``) com os mesmos argumentos.
    """

    fallback: "OcrProvider | None" = None
    max_edge = 1700

    def _call(self, image_path: Path, prompt: str) -> dict:
        raise NotImplementedError

    def _with_fallback(self, method: str, fn, *args):
        try:
            return fn(*args)
        except OcrError as exc:
            if self.fallback is None:
                raise
            try:
                return getattr(self.fallback, method)(*args)
            except OcrError as exc2:
                raise OcrError(f"{exc}; fallback: {exc2}") from exc2

    def _jpeg_b64(self, image_path: Path) -> str:
        """Foto em JPEG (lado maior ≤ max_edge), em base64."""
        import io

        from PIL import Image
        if not image_path or not image_path.is_file():
            raise OcrError(f"Imagem não encontrada: {image_path}")
        with Image.open(image_path) as raw:
            im = raw.convert("RGB")
        scale = min(1.0, self.max_edge / max(im.size))
        if scale < 1:
            im = im.resize((round(im.size[0] * scale), round(im.size[1] * scale)),
                           Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=90)
        return base64.b64encode(buf.getvalue()).decode("ascii")

    def extract(self, image_path: Path, template: KanbanTemplate) -> dict:
        return self._with_fallback("extract", self._extract, image_path, template)

    def _extract(self, image_path: Path, template: KanbanTemplate) -> dict:
        data = self._call(image_path, _extraction_prompt(template, compact=True))
        return _clean_extraction(_expand_compact(data, {template.name: template}), template)

    def extract_auto(self, image_path: Path,
                     templates: dict[str, KanbanTemplate]) -> tuple[str, dict]:
        return self._with_fallback("extract_auto", self._extract_auto, image_path, templates)

    def _extract_auto(self, image_path: Path,
                      templates: dict[str, KanbanTemplate]) -> tuple[str, dict]:
        data = self._call(image_path, _auto_extraction_prompt(templates, compact=True))
        data = _expand_compact(data, templates)
        kind = _resolve_kind(data, templates)
        return kind, _clean_extraction(data, templates[kind])

    def extract_header(self, image_path: Path, template: KanbanTemplate) -> dict:
        return self._with_fallback("extract_header", self._extract_header, image_path, template)

    def _extract_header(self, image_path: Path, template: KanbanTemplate) -> dict:
        return _clean_header_fields(self._call(image_path, _header_rescue_prompt(template)))


class ClaudeOcrProvider(_ApiVisionProvider):
    """OCR pela API da Anthropic (Claude), com as mesmas instruções do Grok.

    12/10: o Grok no Bedrock passou a responder a ~1,5 tokens/s (até 50 min de
    espera por folha) e os outros modelos do Bedrock estão travados pela quota
    diária da conta. Teste nas 25 folhas validadas (12/10), Sonnet 5.5: 84% dos
    modelos e 84% das OF certas (Grok 84%/86%, Qwen 51%/60%), ~5 s e ~2,5
    cêntimos por folha. Sem schema forçado: o das faces todas passa o limite de
    campos opcionais da API; o JSON vem das instruções e uma resposta inválida
    (1 em 25 no teste) repete uma vez.
    """

    name = "claude"

    def __init__(self, api_key: str, model: str, timeout_s: float = 120.0,
                 fallback: "OcrProvider | None" = None, max_edge: int = 1700,
                 attempts: int = 2):
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s
        self.fallback = fallback
        self.max_edge = max_edge
        self.attempts = attempts
        self._client = None    # criado uma vez, na primeira chamada

    def _get_client(self):
        # um cliente por chamada abria um pool httpx novo de cada vez e nenhum
        # era fechado; o SDK já repete os 429/5xx/529 com espera
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(
                api_key=self.api_key, timeout=self.timeout_s, max_retries=2)
        return self._client

    def _call(self, image_path: Path, prompt: str) -> dict:
        import anthropic

        content = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": self._jpeg_b64(image_path)}},
            {"type": "text", "text": prompt},
        ]
        client = self._get_client()
        last: OcrError | None = None
        for _ in range(self.attempts):
            started = time.monotonic()
            try:
                response = client.messages.create(
                    model=self.model, max_tokens=8000,
                    messages=[{"role": "user", "content": content}])
            except anthropic.APIError as exc:
                _ocr_log.warning("[ocr] claude falhou ao fim de %.1f s: %s",
                                 time.monotonic() - started, exc)
                raise OcrError(f"Claude [{self.model}]: {exc}") from exc
            _ocr_log.info("[ocr] claude %.1f s, tokens %s/%s, %s",
                          time.monotonic() - started, response.usage.input_tokens,
                          response.usage.output_tokens, response.stop_reason)
            if response.stop_reason == "refusal":
                raise OcrError(f"Claude [{self.model}]: pedido recusado pelos classificadores")
            if response.stop_reason == "max_tokens":
                # sem isto o sintoma era «JSON inválido» e mandava quem depura
                # atrás de um bug de instruções em vez do limite de output
                raise OcrError(f"Claude [{self.model}]: resposta truncada por max_tokens")
            text = "".join(b.text for b in response.content if b.type == "text")
            try:
                return _qwen_json(text)
            except ValueError as exc:   # JSON cortado ou inválido: repete uma vez
                last = OcrError(f"Claude [{self.model}]: {exc}")
        raise last or OcrError(f"Claude [{self.model}]: sem resposta")


class GrokOcrProvider(_ApiVisionProvider):
    """OCR pela API do AWS Bedrock (Mantle, compatível com a OpenAI): xai.grok-4.6.

    Decisão do Luís (12/10): as apps passam para o servidor e o OCR para uma
    API de modelo. Testado em folhas validadas (12/10): com as regras do setor
    e a resposta curta, 81% dos modelos certos (Qwen no PC: 52%), ~72 s por
    folha, ~1 cêntimo. O Mantle devolve 500 de vez em quando (2 em ~60
    pedidos): repete-se com espera antes de desistir para o motor seguinte.
    À tarde de 12/10 passou a responder a ~1,5 tokens/s (10 min por folha):
    ficou como reserva do Claude.
    """

    name = "grok"
    _RETRY_STATUS = {429, 500, 502, 503, 504}

    def __init__(self, api_key: str, endpoint: str, model: str,
                 timeout_s: float = 300.0, fallback: "OcrProvider | None" = None,
                 max_edge: int = 1700, attempts: int = 3):
        self.api_key = api_key
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self.fallback = fallback
        self.max_edge = max_edge
        self.attempts = attempts

    def _image_url(self, image_path: Path) -> str:
        return "data:image/jpeg;base64," + self._jpeg_b64(image_path)

    def _call(self, image_path: Path, prompt: str) -> dict:
        body = json.dumps({
            "model": self.model,
            "reasoning": {"effort": "low"},
            "max_output_tokens": 8000,
            "input": [{"role": "user", "content": [
                {"type": "input_text", "text": prompt},
                {"type": "input_image", "image_url": self._image_url(image_path)},
            ]}],
        }).encode("utf-8")
        last: Exception | None = None
        for attempt in range(self.attempts):
            req = urllib.request.Request(
                f"{self.endpoint}/responses", data=body, method="POST",
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"})
            started = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                usage = data.get("usage") or {}
                _ocr_log.info("[ocr] grok %.1f s, tokens %s/%s", time.monotonic() - started,
                              usage.get("input_tokens"), usage.get("output_tokens"))
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "ignore")[:300]
                last = OcrError(f"Grok [{self.model}] HTTP {exc.code}: {detail}")
                if exc.code not in self._RETRY_STATUS:
                    raise last from exc
            except (urllib.error.URLError, OSError, http.client.HTTPException,
                    TimeoutError, json.JSONDecodeError) as exc:
                last = OcrError(f"Grok [{self.model}] indisponível: {exc}")
            _ocr_log.warning("[ocr] grok tentativa %d falhou ao fim de %.1f s: %s",
                             attempt + 1, time.monotonic() - started, last)
            if attempt + 1 < self.attempts:
                time.sleep(5 * (attempt + 1))
        else:
            raise last or OcrError("Grok: erro desconhecido")
        if data.get("status") != "completed":
            raise OcrError(f"Grok [{self.model}]: resposta {data.get('status')} "
                           f"({(data.get('incomplete_details') or {}).get('reason')})")
        text = "".join(c.get("text", "") for o in data.get("output", [])
                       for c in (o.get("content") or []) if c.get("type") == "output_text")
        try:
            return _qwen_json(text)
        except ValueError as exc:
            raise OcrError(f"Grok [{self.model}]: {exc}") from exc



def extract_checked(provider, image_path, template, validate):
    """Bounded fallback when a strip contradicts independently observed rows.

    The callback checks count AND physical anchors; no expected transcription
    or plan reference is supplied to the OCR model.
    """
    try:
        if isinstance(provider, GeminiOcrProvider):
            body = provider._request_body(image_path.read_bytes(),
                _MIME_BY_SUFFIX.get(image_path.suffix.lower(), "image/png"), template)
            response = provider._call(body, lambda r: validate(provider._parse(r, template)))
            return provider._parse(response, template)
        result = provider.extract(image_path, template)
        validate(result)
        return result
    except (OcrError, ValueError):
        fallback = getattr(provider, "fallback", None)
        if fallback is None:
            raise
        return extract_checked(fallback, image_path, template, validate)


def get_provider() -> OcrProvider:
    claude = (ClaudeOcrProvider(settings.anthropic_api_key, settings.claude_ocr_model)
              if settings.anthropic_api_key else None)
    gemini = (GeminiOcrProvider(settings.gemini_api_key, settings.ocr_model,
                                fallback=claude)
              if settings.gemini_api_key else None)
    if settings.ocr_provider == "claude":
        if claude is None:
            raise RuntimeError("MES_OCR_PROVIDER=claude sem ANTHROPIC_API_KEY")
        # reserva noutro fornecedor: o Grok (Bedrock) e, sem ele, o Gemini
        claude.fallback = (
            GrokOcrProvider(settings.bedrock_api_key, settings.bedrock_endpoint,
                            settings.grok_model, settings.grok_timeout_s)
            if settings.bedrock_api_key else
            GeminiOcrProvider(settings.gemini_api_key, settings.ocr_model)
            if settings.gemini_api_key else None)
        return claude
    if settings.ocr_provider == "grok" or (
            not settings.ocr_provider and settings.bedrock_api_key and not settings.qwen_url):
        if not settings.bedrock_api_key:
            raise RuntimeError("MES_OCR_PROVIDER=grok sem MES_BEDROCK_API_KEY")
        return GrokOcrProvider(settings.bedrock_api_key, settings.bedrock_endpoint,
                               settings.grok_model, settings.grok_timeout_s,
                               fallback=gemini or claude)
    if settings.qwen_url:
        # motor local primário; se o PC/GPU estiver em baixo, a cadeia cloud
        # (Gemini→Claude) apanha o trabalho e as folhas nunca ficam por ler
        return QwenOcrProvider(settings.qwen_url, settings.qwen_model,
                               settings.qwen_timeout_s, settings.qwen_no_think,
                               fallback=gemini or claude)
    if gemini is not None:
        return gemini
    if claude is not None:
        return claude          # só há chave Claude: passa a ser o motor
    return ManualEntryProvider()
