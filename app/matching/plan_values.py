"""Valores do plano que não são dados: erros do Excel e o «x» das procuras.

O Excel do planeamento tem fórmulas e o carregador lê o valor em cache: uma
fórmula partida chega como texto («#VALUE!», «#REF!»…). No Met2, o XLOOKUP de
cliente/OV devolve «x» quando não encontra nada. Nenhum destes valores pode ir
para uma célula da folha nem servir de proposta de substituição — foi assim que
um perfil «#VALUE!» apareceu numa folha de cantoneiras e voltava sempre que o
operador o corrigia.
"""

from __future__ import annotations

EXCEL_ERRORS = frozenset({
    "#VALUE!", "#REF!", "#N/A", "#DIV/0!", "#NAME?", "#NUM!", "#NULL!",
    "#SPILL!", "#CALC!", "#FIELD!", "#BLOCKED!", "#UNKNOWN!", "#GETTING_DATA",
})
# Campos preenchidos por XLOOKUP(…, "x") no Met2.
LOOKUP_FALLBACK_FIELDS = frozenset({
    "ov", "cliente", "cliente_nome", "customer_name", "sales_order_no",
})


def is_excel_error(value: object) -> bool:
    return isinstance(value, str) and value.strip().upper() in EXCEL_ERRORS


def clean(field: str, value: object) -> object:
    """O valor, ou None se for um erro do Excel / o «x» de uma procura falhada."""
    if is_excel_error(value):
        return None
    if (field in LOOKUP_FALLBACK_FIELDS and isinstance(value, str)
            and value.strip().lower() == "x"):
        return None
    return value


def clean_entry(entry: dict) -> dict:
    return {key: clean(key, value) for key, value in entry.items()}
