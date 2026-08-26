"""Normalização das datas manuscritas e da proveniência antes do Postgres.

O caso que motivou as datas é real: «06/08/2026» (6 de agosto) foi gravado
como 2026-06-08 (8 de junho) porque a data ia crua para a coluna `date` e o
Postgres estava em DateStyle MDY. Cá dentro não há ambiguidade: dia/mês/ano.
"""

import pytest

from app.pg_store import (
    InvalidSheetDate,
    normalize_sheet_date,
    source_from_image_path,
)


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


def test_proveniencia_derivada_do_nome_do_render():
    """Páginas de PDF gravam-se como {sha16}_{stem}_pNN.png (upload/ingest);
    é daí que a validação recupera o ficheiro e a página de origem."""
    assert source_from_image_path(
        "data/images/0123456789abcdef_18-08-2026_p3.png"
    ) == ("18-08-2026.pdf", 3)
    # o stem pode conter os próprios underscores/hífens do scanner
    assert source_from_image_path(
        "/x/aaaaaaaaaaaaaaaa_06-08-2026 - Rapid20T 1_p12.png"
    ) == ("06-08-2026 - Rapid20T 1.pdf", 12)


def test_proveniencia_de_fotos_e_manuais_fica_nula():
    # foto com nome livre: não se inventa um PDF de origem
    assert source_from_image_path(
        "data/images/0123456789abcdef_IMG_1234.jpg"
    ) == (None, None)
    # sem prefixo sha (não veio do _save_image) e sem imagem de todo
    assert source_from_image_path("18-08-2026_p3.png") == (None, None)
    assert source_from_image_path(None) == (None, None)
