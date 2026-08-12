from app.matching import similarity as sim


def test_parse_number_milhares_europeus():
    """«1.200» é mil e duzentos (decimal em PT é vírgula) — lido como 1.2, uma
    quantidade acima do plano passava como confirmada e ia errada p/ Postgres."""
    assert sim.parse_number("1.200") == 1200.0
    assert sim.parse_number("12.500") == 12500.0
    assert sim.parse_number("6.000") == 6000.0
    assert sim.parse_number("-1.200") == -1200.0
    # decimais continuam decimais
    assert sim.parse_number("1,2") == 1.2
    assert sim.parse_number("1.2") == 1.2         # só 1 dígito após o ponto
    assert sim.parse_number("1.23") == 1.23       # 2 dígitos: não é milhar
    assert sim.parse_number("0.125") == 0.125     # milhares não começam por 0
    assert sim.parse_number("1.234,56") == 1234.56
    assert sim.parse_number("1,234.56") == 1234.56


def test_compact_normaliza_acentos():
    """CONCEIÇÃO tem de bater com CONCEICAO da lista SAP — antes o Ç e o Ã
    caíam fora e o nome certo ficava a distância 2."""
    assert sim.compact("Conceição") == "CONCEICAO"
    assert sim.compact("João Araújo") == "JOAOARAUJO"
    assert sim.compact("of 250002") == "OF250002"


def test_compact_and_code():
    assert sim.compact(" of 250002 ") == "OF250002"
    assert sim.code_similarity("OF25OOO2", "OF250002") == 1.0  # O↔0
    assert sim.code_similarity("OF250002", "OF250002") == 1.0
    assert sim.code_similarity("OF999999", "OF250002") < 0.8


def test_zero_o_variants():
    assert "OF250002" in sim.zero_o_variants("OF25OOO2")


def test_text_similarity_contains_and_tokens():
    assert sim.text_similarity("SILVA & VINHA", "SILVA & VINHA, S.A.") >= 0.9
    assert sim.text_similarity("PROEF", "PROEF EURICO FERREIRA SA") >= 0.9
    assert sim.text_similarity("METALOGALVA", "OMNINSTAL") < 0.6


def test_numeric_similarity():
    assert sim.numeric_similarity(1000, 1010, tolerance=50) == 1.0
    assert sim.numeric_similarity(1000, 1060, tolerance=50) < 1.0
    assert sim.numeric_similarity(1000, 5000, tolerance=50) == 0.0
    assert sim.parse_number(" 1.234,5 ") is not None
    assert sim.parse_number("6 mm") == 6.0
    assert sim.parse_number(None) is None


def test_year_prefix_is_not_identity():
    # o plano escreve '26_S07.CH5_1'; o operador escreve 'S07.CH5_1' — mesmo código
    assert sim.normalize_code("26_S07.CH5_1") == sim.normalize_code("S07.CH5_1")
    assert sim.normalize_code("25_S49.CH3_27") == "S49CH327"
    # sem o padrão ano+underscore nada muda
    assert sim.normalize_code("OF250001") == "OF250001"
    assert sim.normalize_code("2828-02") == "282802"
    # variantes O<->0 herdam a normalização
    assert "S07CH51" in sim.zero_o_variants("26_S07.CH5_1")


def test_code_similarity_ignores_year_prefix():
    assert sim.code_similarity("S07.CH5_1", "26_S07.CH5_1") == 1.0
    assert sim.code_similarity("26_S07.CH5_1", "S07.CH5_1") == 1.0
