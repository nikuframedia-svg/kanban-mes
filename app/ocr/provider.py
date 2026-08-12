"""OCR das folhas kanban.

Três providers:
- GeminiOcrProvider — lê a foto com a Gemini API e devolve {header, rows, footer}
  em JSON forçado por schema. Transcreve fielmente: o OCR NÃO corrige nada;
  correções são trabalho do motor de cruzamento + revisão humana.
- ClaudeOcrProvider — último recurso PAGO (API Claude), usado apenas quando a
  cadeia Gemini inteira falhou. Só existe se houver ANTHROPIC_API_KEY no
  ambiente; sem chave, o comportamento é exatamente o de sempre.
- ManualEntryProvider — sem chave configurada (ou sem foto), folha vazia para
  preenchimento manual. O resto da app funciona exatamente da mesma forma.

O prompt e a limpeza do resultado são partilhados pelos motores: o que muda de
um para o outro é o transporte, nunca as regras de transcrição.
"""

from __future__ import annotations

import base64
import http.client
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Protocol

from ..config import settings
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

def _extraction_prompt(template: KanbanTemplate) -> str:
    labels = template.field_labels or {}
    row_desc = "; ".join(f"{f} = «{labels.get(f, f)}»" for f in template.row_fields)
    return (
        "Estás a transcrever uma folha kanban manuscrita de uma "
        f"fábrica metalomecânica portuguesa ({template.label}).\n"
        "REGRAS ESTRITAS:\n"
        "1. Transcreve EXATAMENTE o que está escrito, mesmo que pareça errado. "
        "NÃO corrijas, NÃO completes, NÃO normalizes códigos nem datas.\n"
        "2. Célula vazia ou ilegível → null. Nunca inventes valores.\n"
        "3. Uma entrada em `rows` por cada linha da tabela COM ALGO escrito; "
        "ignora linhas totalmente vazias.\n"
        "4. Números: transcreve os dígitos tal como escritos (sem unidades). "
        "Um visto/cruz numa célula transcreve-se como «x».\n"
        "5. No cabeçalho, `turno` é a opção assinalada com cruz (M, R ou XM), se alguma; "
        "`n_operador` é o campo «N.º».\n"
        "6. Valores repetidos por linhas seguidas (ex.: cliente escrito uma vez para "
        "várias linhas) transcrevem-se só na linha onde estão escritos.\n"
        "7. Atenção às DUAS ÚLTIMAS colunas, que se confundem facilmente: o que "
        "estiver na coluna «QTD» vai para `qtd` e o que estiver na última coluna "
        "vai para `perf_comp`. Se uma delas estiver vazia na folha, deixa-a a null "
        "— não desloques valores de uma coluna para a outra.\n"
        f"Colunas da tabela, pela ordem da folha: {row_desc}.\n"
        "Devolve apenas o JSON pedido."
    )


def _auto_extraction_prompt(templates: dict[str, KanbanTemplate]) -> str:
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
        "3. Uma entrada em `rows` por cada linha da tabela COM ALGO escrito; "
        "ignora linhas totalmente vazias. Página sem nada manuscrito → `rows` vazio.\n"
        "4. Números: transcreve os dígitos tal como escritos (sem unidades). "
        "Um visto/cruz numa célula transcreve-se como «x».\n"
        "5. No cabeçalho, `turno` é a opção assinalada com cruz (M, R ou XM), se alguma; "
        "`n_operador` é o campo «N.º».\n"
        "6. Valores repetidos por linhas seguidas (ex.: cliente escrito uma vez para "
        "várias linhas) transcrevem-se só na linha onde estão escritos.\n"
        "7. Na face de produção, atenção às DUAS ÚLTIMAS colunas, que se confundem "
        "facilmente: o que estiver na coluna «QTD» vai para `qtd` e o que estiver na "
        "última coluna vai para `perf_comp`. Se uma delas estiver vazia na folha, "
        "deixa-a a null — não desloques valores de uma coluna para a outra.\n"
        "Devolve apenas o JSON pedido."
    )


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

    def _call(self, body: dict) -> dict:
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
                    return self._call_model(model, body)
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


class ClaudeOcrProvider:
    """Último recurso pago: a API Claude, só quando toda a cadeia Gemini falhou.

    Mesmo contrato, mesmos prompts, mesma disciplina de transcrição fiel — só
    muda o transporte (SDK `anthropic`) e a forma do schema (JSON Schema em vez
    do dialecto do Gemini). A ~0,6 cêntimos por folha, corre apenas nas falhas.
    """

    name = "claude"

    def __init__(self, api_key: str, model: str, timeout_s: float = 120.0):
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s
        self._client = None    # criado uma vez, na primeira chamada

    def _get_client(self):
        # um cliente por chamada abria um pool httpx novo de cada vez e
        # nenhum era fechado — num lote com o Gemini em baixo eram dezenas
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(
                api_key=self.api_key, timeout=self.timeout_s, max_retries=1)
        return self._client

    # ---- schema: JSON Schema standard (nullable = ["string", "null"]) ----

    @staticmethod
    def _obj(fields: tuple[str, ...]) -> dict:
        return {
            "type": "object",
            "properties": {f: {"type": ["string", "null"]} for f in fields},
            "required": list(fields),
            "additionalProperties": False,
        }

    def _schema(self, template: KanbanTemplate) -> dict:
        return {
            "type": "object",
            "properties": {
                "header": self._obj(template.header_fields),
                "rows": {"type": "array", "items": self._obj(template.row_fields)},
                "footer": self._obj(template.footer_fields),
            },
            "required": ["header", "rows", "footer"],
            "additionalProperties": False,
        }

    def _auto_schema(self, templates: dict[str, KanbanTemplate]) -> dict:
        return {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(templates)},
                "header": self._obj(_union_fields(templates, lambda t: t.header_fields)),
                "rows": {"type": "array",
                         "items": self._obj(_union_fields(templates, lambda t: t.row_fields))},
                "footer": self._obj(_union_fields(templates, lambda t: t.footer_fields)),
            },
            "required": ["kind", "header", "rows", "footer"],
            "additionalProperties": False,
        }

    # ---- pedido ----

    def _call(self, image_path: Path, prompt: str, schema: dict) -> dict:
        if not image_path or not image_path.is_file():
            raise OcrError(f"Imagem não encontrada: {image_path}")
        import anthropic

        mime = _MIME_BY_SUFFIX.get(image_path.suffix.lower(), "image/jpeg")
        client = self._get_client()
        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=8192,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {
                            "type": "base64", "media_type": mime,
                            "data": base64.b64encode(image_path.read_bytes()).decode("ascii"),
                        }},
                        {"type": "text", "text": prompt},
                    ],
                }],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
        except anthropic.APIError as exc:
            raise OcrError(f"Claude [{self.model}]: {exc}") from exc
        if response.stop_reason == "refusal":
            raise OcrError(f"Claude [{self.model}]: pedido recusado pelos classificadores")
        if response.stop_reason == "max_tokens":
            # sem isto o sintoma era «JSON inválido» e mandava quem depura
            # atrás de um bug de schema em vez do limite de output
            raise OcrError(f"Claude [{self.model}]: resposta truncada por max_tokens")
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise OcrError(f"Resposta Claude sem JSON válido: {exc}") from exc

    def extract(self, image_path: Path, template: KanbanTemplate) -> dict:
        data = self._call(image_path, _extraction_prompt(template),
                          self._schema(template))
        return _clean_extraction(data, template)

    def extract_auto(self, image_path: Path,
                     templates: dict[str, KanbanTemplate]) -> tuple[str, dict]:
        data = self._call(image_path, _auto_extraction_prompt(templates),
                          self._auto_schema(templates))
        kind = _resolve_kind(data, templates)
        return kind, _clean_extraction(data, templates[kind])


def get_provider() -> OcrProvider:
    claude = (ClaudeOcrProvider(settings.anthropic_api_key, settings.claude_ocr_model)
              if settings.anthropic_api_key else None)
    if settings.gemini_api_key:
        return GeminiOcrProvider(settings.gemini_api_key, settings.ocr_model,
                                 fallback=claude)
    if claude is not None:
        return claude          # só há chave Claude: passa a ser o motor
    return ManualEntryProvider()
