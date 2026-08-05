"""Interface do OCR — o modelo concreto fica por decidir (o Luís trata disso).

Qualquer implementação recebe o caminho da foto + o template da folha e devolve
o dicionário {header, rows, footer}. Enquanto não houver modelo configurado, a
app funciona em modo manual: a folha é criada vazia e preenchida no ecrã de
revisão; o motor de cruzamento funciona exatamente da mesma forma.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..templates_spec import KanbanTemplate


class OcrProvider(Protocol):
    name: str

    def extract(self, image_path: Path, template: KanbanTemplate) -> dict:
        """Devolve {"header": {...}, "rows": [{...}], "footer": {...}}."""
        ...


class ManualEntryProvider:
    """Sem OCR: devolve uma folha vazia com 10 linhas para preenchimento manual."""

    name = "manual"

    def extract(self, image_path: Path, template: KanbanTemplate) -> dict:
        return {
            "header": {f: None for f in template.header_fields},
            "rows": [{f: None for f in template.row_fields} for _ in range(10)],
            "footer": {f: None for f in template.footer_fields},
        }


def get_provider() -> OcrProvider:
    # Ponto único de troca quando o Luís escolher o modelo de OCR:
    # devolver aqui a implementação real (API de visão, modelo local, etc.).
    return ManualEntryProvider()
