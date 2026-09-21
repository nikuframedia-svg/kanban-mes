"""Testes do scorer com um plano sintético que reproduz as situações-tipo:
valores raros vs comuns, erros de glifo, famílias de dimensões, H₀."""

import pytest

from app.matching.params import CrossParams
from app.matching.refs import FieldSpec, IndexSpec, PlanIndex
from app.matching.scorer import Scorer

SPEC = IndexSpec(
    identity_fields=(
        FieldSpec("of", "code", "of"),
        FieldSpec("ov", "code", "ov"),
        FieldSpec("cliente", "text", "cliente"),
    ),
    numeric_fields=(
        FieldSpec("comp_mm", "numeric", "comp_mm", tolerance=50.0),
    ),
    key_field="plan_key",
)


def make_index():
    entries = []
    # família de 50 linhas da mesma OF (dimensões diferentes) — cliente comum
    for i in range(50):
        entries.append({
            "plan_key": f"A{i}", "of": "OF250001", "ov": "OV2400001",
            "cliente": "PROEF EURICO FERREIRA", "comp_mm": 500 + i * 100,
        })
    # OF rara de outro cliente
    entries.append({
        "plan_key": "B0", "of": "OF259999", "ov": "OV2409999",
        "cliente": "SILVA & VINHA SA", "comp_mm": 1234,
    })
    # ruído: 100 OFs distintas do mesmo cliente comum
    for i in range(100):
        entries.append({
            "plan_key": f"C{i}", "of": f"OF26{i:04d}", "ov": "OV2400001",
            "cliente": "PROEF EURICO FERREIRA", "comp_mm": 999,
        })
    return PlanIndex(entries, SPEC, plan_age_days=2.0)


@pytest.fixture()
def scorer():
    return Scorer(make_index(), CrossParams())


def test_m_zero_medido_nao_rebenta(scorer):
    """O backtest pode medir m=0.0 num campo; log2(0) matava todos os matches."""
    scorer.params.score.m_by_field["of"] = 0.0
    row = {"of": "OF259999", "ov": "OV2409999", "cliente": "SILVA & VINHA"}
    m = scorer.match_row(row)          # não pode levantar
    assert m.winner is not None


def test_temperatura_zero_nao_rebenta(scorer):
    scorer.params.posterior.temperature_bits = 0.0
    row = {"of": "OF259999", "ov": "OV2409999", "cliente": "SILVA & VINHA"}
    m = scorer.match_row(row)          # OverflowError antes do clamp a 0.1
    assert 0.0 <= m.p_correct <= 1.0


def test_veto_dispara_com_prefixo_do_plano():
    """O plano guarda OF263323; o operador escreve 263323. O veto («escreveste
    um código que existe e não é este») tem de reconhecer a variante — antes a
    frequência do escrito sem prefixo era 0 e o veto nunca disparava."""
    from app.matching.loaders import CANTONEIRAS_SPEC

    entries = [
        {"plan_key": "A", "of": "OF263323", "ov": "OV1", "modelo": "M1", "perfil": "L60X60X5"},
        {"plan_key": "B", "of": "OF999999", "ov": "OV2", "modelo": "M2", "perfil": "L60X60X5"},
    ]
    s = Scorer(PlanIndex(entries, CANTONEIRAS_SPEC), CrossParams())
    fe = s._identity_evidence(CANTONEIRAS_SPEC.identity_fields[0], "263323", 1)
    assert fe.reason == "veto", "código válido contra entrada errada = veto"
    assert fe.bits == s.params.score.veto_valid_code_bits


def test_fuzzy_processa_o_campo_mais_seletivo_primeiro():
    """Nesting mal lido + máquina mal lida: o fuzzy da máquina (4 valores,
    centenas de linhas cada) enchia o teto de 300 candidatos e o nesting — o
    único campo que identifica — nunca chegava a gerar os dele."""
    spec = IndexSpec(
        identity_fields=(
            FieldSpec("nesting", "code", "nesting"),
            FieldSpec("maquina", "text", "maquina"),
        ),
        key_field="plan_key",
    )
    entries = []
    for i in range(2000):
        entries.append({"plan_key": f"N{i}", "nesting": f"S{i:05d}.CH5_1",
                        "maquina": f"LASER {i % 4 + 1}"})
    s = Scorer(PlanIndex(entries, spec), CrossParams())
    # nesting com um erro de OCR (l final em vez de 1), máquina com erro
    cands = s.candidates({"nesting": "S00123.CH5_l", "maquina": "LASE 3"})
    truth_idx = next(i for i, e in enumerate(entries) if e["plan_key"] == "N123")
    assert truth_idx in cands, "o dono do nesting tem de estar nos candidatos"


def test_valor_certo_com_mais_de_500_linhas_gera_candidatos():
    """OV de obra grande (>500 linhas): o teto de candidatos escondia-a por
    completo e a linha dava no_match — agora, sem nada dentro dos tetos,
    repete-se o exato sem teto."""
    spec = IndexSpec(
        identity_fields=(FieldSpec("ov", "code", "ov", code_prefix="OV"),),
        key_field="plan_key",
    )
    entries = [{"plan_key": f"E{i}", "ov": "OV900001"} for i in range(601)]
    s = Scorer(PlanIndex(entries, spec), CrossParams())
    cands = s.candidates({"ov": "900001"})
    assert len(cands) == 601


def test_exact_match_wins_with_high_confidence(scorer):
    row = {"of": "OF259999", "ov": "OV2409999", "cliente": "SILVA & VINHA", "comp_mm": 1230}
    m = scorer.match_row(row)
    assert m.winner is not None
    assert m.winner.plan_key == "B0"
    assert m.mode == "strong"
    assert m.p_correct > 0.9


def test_glyph_error_is_recovered(scorer):
    # operador escreveu OF2599S9 (S em vez de 9): o canal deve recuperar
    row = {"of": "OF25999S", "ov": "OV2409999", "cliente": "SILVA E VINHA", "comp_mm": 1234}
    m = scorer.match_row(row)
    assert m.winner is not None
    assert m.winner.plan_key == "B0"


def test_rare_value_weighs_more_than_common(scorer):
    # peso de uma OF única > peso de uma OV partilhada por 150 linhas
    f_of = SPEC.identity_fields[0]
    f_ov = SPEC.identity_fields[1]
    w_of = scorer.value_weight(f_of, "OF259999")
    w_ov = scorer.value_weight(f_ov, "OV2400001")
    assert w_of > w_ov


def test_sibling_dims_disambiguate(scorer):
    # mesma OF, 50 irmãos: só a dimensão distingue; margem entre irmãos não conta
    # para o modo, mas o vencedor deve ser o comprimento certo
    row = {"of": "OF250001", "ov": "OV2400001", "cliente": "PROEF", "comp_mm": 2500}
    m = scorer.match_row(row)
    assert m.winner is not None
    assert m.winner.plan_key == "A20"  # 500 + 20*100 = 2500


def test_h0_wins_when_row_not_in_plan(scorer):
    # linha que não existe no plano: identidade desconhecida, dims sem família
    row = {"of": "OF990000", "ov": "OV9900000", "cliente": "CLIENTE FANTASMA", "comp_mm": 77777}
    m = scorer.match_row(row)
    assert m.winner is None or m.p_correct < 0.5


def test_empty_row_no_crash(scorer):
    m = scorer.match_row({})
    assert m.winner is None
    assert m.mode == "no_match"


def test_veto_written_valid_code(scorer):
    # OF escrita existe no plano e não é a do candidato → veto mais forte
    # que uma simples discordância
    entry_idx_b0 = next(
        i for i, e in enumerate(scorer.index.entries) if e["plan_key"] == "B0"
    )
    fe = scorer._identity_evidence(SPEC.identity_fields[0], "OF250001", entry_idx_b0)
    assert fe.reason == "veto"
    assert fe.bits == scorer.params.score.veto_valid_code_bits


def test_context_bonus_capped():
    idx = make_index()
    s = Scorer(idx, CrossParams(), active_primary={"OF259999"})
    row = {"of": "OF259999", "ov": "OV2409999", "cliente": "SILVA & VINHA", "comp_mm": 1234}
    m = s.match_row(row)
    assert m.winner.context_bits <= s.params.score.context_cap_bits


def test_normalized_handles_unknown_field_and_bounds_without_large_default():
    index = make_index()
    assert index.normalized("of", 0) == "OF250001"
    assert index.normalized("missing", 0) == ""
    assert index.normalized("of", -1) == ""
    assert index.normalized("of", index.n) == ""
