"""Normalização das datas manuscritas antes do Postgres.

O caso que motivou isto é real: «06/08/2026» (6 de agosto) foi gravado como
2026-06-08 (8 de junho) porque a data ia crua para a coluna `date` e o
Postgres estava em DateStyle MDY. Cá dentro não há ambiguidade: dia/mês/ano.
"""

import pytest

from app.pg_store import InvalidSheetDate, normalize_sheet_date


def test_formatos_reais_das_folhas():
    # todos os formatos observados no staging
    assert normalize_sheet_date("06/08/2026") == "2026-08-06"
    assert normalize_sheet_date("10/08/2026") == "2026-08-10"
    assert normalize_sheet_date("06-08-2026") == "2026-08-06"
    assert normalize_sheet_date("10/8/26") == "2026-08-10"
    assert normalize_sheet_date("7/8/26") == "2026-08-07"
    assert normalize_sheet_date("14/05/24") == "2024-05-14"
    assert normalize_sheet_date(" 6.8.2026 ") == "2026-08-06"
    assert normalize_sheet_date("2026-08-06") == "2026-08-06"


def test_dia_e_mes_nunca_trocam():
    """A regra é sempre dia/mês — 10/08 é 10 de agosto, nunca 8 de outubro."""
    assert normalize_sheet_date("10/08/2026") == "2026-08-10"
    assert normalize_sheet_date("01/02/2026") == "2026-02-01"


def test_data_ininterpretavel_levanta():
    for raw in ("6/08", "agosto", "32/01/2026", "10/13/2026", "", "x"):
        with pytest.raises(InvalidSheetDate):
            normalize_sheet_date(raw)
