"""Verificação da leitura: uma folha lida às avessas não passa em silêncio.

Casos reais (26/09, Qwen 9B local): uma folha de 15 linhas lida como 4, com
várias linhas do papel empilhadas na mesma célula (QTD «2 4 1 2 2 2 2 2 2 2
3»); outra com as 14 linhas certas mas a coluna Modelo inteira vazia. A app
aceitava as duas e o cruzamento com o plano pintava de verde o que inventava
por cima.

Aqui a leitura é conferida por sinais que não dependem do plano. Se algum
aparecer, a página é relida com o motor seguinte da cadeia (Qwen → Gemini →
Claude) e fica a melhor leitura; a folha fica marcada «leitura suspeita» e o
operador vê porquê.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..templates_spec import KanbanTemplate, is_marked
from .provider import OcrError

META = "_ocr_check"

_MESSAGES = {
    "celulas_com_varias_linhas": "células com várias linhas do papel juntas (linhas coladas)",
    "qtd_absurda": "quantidades impossíveis (vários números colados)",
    "modelos_em_falta": "coluna Modelo por ler na maioria das linhas com quantidade",
    "resposta_cortada": "a resposta do OCR veio cortada (faltam as últimas linhas)",
    "linhas_em_falta": "menos linhas lidas do que linhas escritas na folha",
}


def _engine(extraction: dict) -> str:
    return str((extraction.get("_ocr") or {}).get("engine") or "?")


def _family(engine: str) -> str:
    return engine.split(":", 1)[0]


def problems_for(extraction: dict, template: KanbanTemplate,
                 image_path: Path | None = None) -> list[dict]:
    """Sinais de leitura estragada. Lista vazia = nada de suspeito."""
    rows = [r for r in extraction.get("rows") or []
            if any(v not in (None, "") for v in r.values())]
    found: dict[str, int] = {}

    def add(code: str, n: int = 1) -> None:
        found[code] = found.get(code, 0) + n

    for row in rows:
        if any(isinstance(v, str) and "\n" in v.strip() for v in row.values()):
            add("celulas_com_varias_linhas")
        qtd = re.sub(r"\s+", "", str(row.get("qtd") or ""))
        if qtd.isdigit() and len(qtd) >= 5:
            add("qtd_absurda")
    if "modelo" in template.row_fields:
        with_qtd = [r for r in rows if str(r.get("qtd") or "").strip()
                    and not is_marked(r.get("perf_comp")) and not is_marked(r.get("qtd"))]
        missing = [r for r in with_qtd if not str(r.get("modelo") or "").strip()]
        if len(with_qtd) >= 3 and len(missing) * 2 >= len(with_qtd):
            add("modelos_em_falta", len(missing))
    if (extraction.get("_ocr") or {}).get("truncated"):
        add("resposta_cortada")
    if image_path is not None and template.name == "cantoneiras_kanban":
        try:
            from .coverage import check_coverage, row_accounting
            expected = check_coverage(image_path, extraction).get("expected_rows")
            accounted = row_accounting(extraction)["accounted_rows"]
            if isinstance(expected, int) and expected > accounted:
                add("linhas_em_falta", expected - accounted)
        except Exception:
            pass  # a deteção de linhas é um sinal extra, nunca um bloqueio
    return [{"code": code, "count": n, "message": _MESSAGES[code]}
            for code, n in found.items()]


def _chain(provider) -> list:
    engines, current = [], provider
    while current is not None and all(current is not e for e in engines):
        engines.append(current)
        current = getattr(current, "fallback", None)
    return engines


def _read(engine, image_path: Path, template: KanbanTemplate,
          kinds: dict[str, KanbanTemplate] | None) -> tuple[KanbanTemplate, dict]:
    if kinds is not None and hasattr(engine, "extract_auto"):
        kind, extraction = engine.extract_auto(image_path, kinds)
        return kinds[kind], extraction
    return template, engine.extract(image_path, template)


def read_page(provider, image_path: Path, template: KanbanTemplate,
              kinds: dict[str, KanbanTemplate] | None = None) -> tuple[KanbanTemplate, dict]:
    """Lê a página, confere a leitura e relê com o motor seguinte se preciso.

    Devolve (template da face lida, extração). Um OcrError da primeira
    leitura sobe como antes (a folha abre vazia para preenchimento manual);
    falhas das releituras só ficam registadas.
    """
    chosen_template, extraction = _read(provider, image_path, template, kinds)
    first_problems = problems_for(extraction, chosen_template, image_path)
    problems = first_problems
    tried = [_engine(extraction)]
    if problems:
        for engine in _chain(provider)[1:]:
            if any(_family(t) == engine.name for t in tried):
                continue
            try:
                alt_template, alt = _read(engine, image_path, template, kinds)
            except OcrError as exc:
                tried.append(f"{engine.name}: erro ({str(exc)[:120]})")
                continue
            alt_problems = problems_for(alt, alt_template, image_path)
            tried.append(_engine(alt))
            if len(alt_problems) < len(problems):
                chosen_template, extraction, problems = alt_template, alt, alt_problems
            if not problems:
                break
    if first_problems:
        extraction[META] = {
            "suspect": True,
            "first_engine": tried[0],
            "first_problems": first_problems,
            "engine": _engine(extraction),
            "problems": problems,
            "engines_tried": tried,
        }
    return chosen_template, extraction


def summary(raw_extraction: dict | None) -> dict | None:
    """Para a interface: None se a leitura não levantou suspeitas."""
    meta = (raw_extraction or {}).get(META)
    return meta if isinstance(meta, dict) and meta.get("suspect") else None


def message(meta: dict) -> str:
    first = "; ".join(p["message"] for p in meta.get("first_problems") or [])
    left = meta.get("problems") or []
    if meta.get("engine") != meta.get("first_engine") and not left:
        return (f"Leitura suspeita pelo {_family(meta.get('first_engine', '?'))} ({first}); "
                f"relida pelo {_family(meta.get('engine', '?'))} sem esses problemas. Confere na mesma.")
    if meta.get("engine") != meta.get("first_engine"):
        return (f"Leitura suspeita ({first}); relida pelo {_family(meta.get('engine', '?'))}, "
                f"ainda com: {'; '.join(p['message'] for p in left)}. Confere linha a linha.")
    return f"Leitura suspeita ({first}); não houve outro motor que lesse melhor. Confere linha a linha."
