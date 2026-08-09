"""OCR das folhas kanban.

Dois providers:
- GeminiOcrProvider — lê a foto com a Gemini API e devolve {header, rows, footer}
  em JSON forçado por schema. Transcreve fielmente: o OCR NÃO corrige nada;
  correções são trabalho do motor de cruzamento + revisão humana.
- ManualEntryProvider — sem chave configurada (ou sem foto), folha vazia para
  preenchimento manual. O resto da app funciona exatamente da mesma forma.
"""

from __future__ import annotations

import base64
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


class GeminiOcrProvider:
    name = "gemini"

    def __init__(self, api_key: str, model: str, timeout_s: float = 120.0):
        self.api_key = api_key
        self.models = [model] + [m for m in _FALLBACK_MODELS if m != model]
        self.timeout_s = timeout_s

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

    def _prompt(self, template: KanbanTemplate) -> str:
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

    def _request_body(self, image_bytes: bytes, mime: str, template: KanbanTemplate) -> dict:
        return {
            "contents": [{
                "parts": [
                    {"inline_data": {
                        "mime_type": mime,
                        "data": base64.b64encode(image_bytes).decode("ascii"),
                    }},
                    {"text": self._prompt(template)},
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
        429 (quota esgotada) salta logo para o modelo seguinte."""
        last_error: Exception | None = None
        for model in self.models:
            for attempt in (1, 2):
                try:
                    return self._call_model(model, body)
                except urllib.error.HTTPError as exc:
                    detail = exc.read().decode("utf-8", "ignore")[:300]
                    last_error = OcrError(f"Gemini [{model}] HTTP {exc.code}: {detail}")
                    if exc.code == 429:
                        break                      # quota deste modelo → próximo
                    if exc.code not in (500, 502, 503, 504) or attempt == 2:
                        raise last_error from exc
                except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                    last_error = OcrError(f"Gemini [{model}] indisponível: {exc}")
                    if attempt == 2:
                        break                      # rede instável → tenta outro modelo
                time.sleep(4.0)
        raise last_error or OcrError("Gemini: erro desconhecido")

    # ---- resposta → extração ----

    def _parse(self, response: dict, template: KanbanTemplate) -> dict:
        try:
            parts = response["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts)
            data = json.loads(text)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise OcrError(f"Resposta Gemini sem JSON válido: {exc}") from exc

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

    def extract(self, image_path: Path, template: KanbanTemplate) -> dict:
        if not image_path or not image_path.is_file():
            raise OcrError(f"Imagem não encontrada: {image_path}")
        mime = _MIME_BY_SUFFIX.get(image_path.suffix.lower(), "image/jpeg")
        body = self._request_body(image_path.read_bytes(), mime, template)
        return self._parse(self._call(body), template)

    def classify_page(self, image_path: Path) -> str:
        """A folha TPL102 tem frente (produção) e verso (paragens) — os PDFs
        digitalizados alternam as duas. Devolve 'producao' ou 'paragens'."""
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
                    {"text": (
                        "Esta página é uma folha kanban TPL102. Classifica-a:\n"
                        "- «producao» se a tabela tem colunas CLIENTE / OV / OF / PERFIL / MODELO / QTD;\n"
                        "- «paragens» se a tabela tem colunas MOTIVO DA PARAGEM / INÍCIO / FIM / DURAÇÃO / RESOLVIDO.\n"
                        "Devolve apenas o JSON pedido."
                    )},
                ],
            }],
            "generationConfig": {
                "temperature": 0,
                "response_mime_type": "application/json",
                "response_schema": {
                    "type": "OBJECT",
                    "properties": {"kind": {"type": "STRING", "enum": ["producao", "paragens"]}},
                    "required": ["kind"],
                },
            },
        }
        try:
            response = self._call(body)
            parts = response["candidates"][0]["content"]["parts"]
            data = json.loads("".join(p.get("text", "") for p in parts))
            kind = data.get("kind")
        except (OcrError, KeyError, IndexError, TypeError, json.JSONDecodeError):
            return "producao"          # em dúvida, comportamento antigo
        return kind if kind in ("producao", "paragens") else "producao"


def get_provider() -> OcrProvider:
    if settings.gemini_api_key:
        return GeminiOcrProvider(settings.gemini_api_key, settings.ocr_model)
    return ManualEntryProvider()
