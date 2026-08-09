"""As convenções que separam o que o operador escreve do que o plano guarda.

Foi aqui que o cruzamento esteve a 0%: o plano guarda `OF263323`/`L60X60X5` e
a folha diz `263323`/`60 x 5`. Cada teste deste ficheiro fixa uma dessas
diferenças, para não voltarem a passar despercebidas.
"""

import pytest

from app.matching import similarity as sim
from app.matching.params import CrossParams
from app.matching.refs import FieldSpec, IndexSpec, PlanIndex
from app.matching.scorer import Scorer

CANT_SPEC = IndexSpec(
    identity_fields=(
        FieldSpec("of", "code", "of", code_prefix="OF", max_candidate_entries=None),
        FieldSpec("ov", "code", "ov", code_prefix="OV"),
        FieldSpec("modelo", "code", "modelo"),
        FieldSpec("perfil", "profile", "perfil"),
    ),
    key_field="plan_key",
)


def make_index(n_irmas: int = 300):
    """Uma OF grande (como as reais) e uma pequena de outro perfil."""
    entries = [
        {"plan_key": f"A{i}", "of": "OF263323", "ov": "OV2504650",
         "modelo": f"AT1T{i:03d}", "perfil": "L60X60X5", "qtd_planeada": 4}
        for i in range(n_irmas)
    ]
    entries.append({"plan_key": "B0", "of": "OF260825", "ov": "OV2504651",
                    "modelo": "ZE-269", "perfil": "L90X90X8", "qtd_planeada": 2})
    return PlanIndex(entries, CANT_SPEC)


# ---- normalização do perfil ----

@pytest.mark.parametrize("escrito,esperado", [
    ("60x5", "L60X60X5"),        # duas medidas = abas iguais
    ("60 x 5", "L60X60X5"),      # espaços são ruído
    ("100 x 50 x 6", "L100X50X6"),
    ("L50x6", "L50X50X6"),       # já traz o L
    ("l40x40x3", "L40X40X3"),    # minúsculas
])
def test_normalize_profile(escrito, esperado):
    known = {"L60X60X5", "L100X50X6", "L50X50X6", "L40X40X3"}
    assert sim.normalize_profile(escrito, known) == esperado


def test_normalize_profile_nao_inventa_o_que_nao_existe_no_plano():
    """Expandir 2 medidas para 3 é uma inferência: só se o resultado existir.

    `PL8x60` é uma chapa, não uma cantoneira de abas iguais — expandir daria
    `PL8X8X60`, que não é nada.
    """
    assert sim.normalize_profile("PL8x60", {"PL8X60"}) == "PL8X60"
    assert sim.normalize_profile("99x9", {"L60X60X5"}) == "99X9"


# ---- prefixo dos códigos ----

def test_of_sem_prefixo_encontra_o_plano():
    index = make_index()
    assert index.exact_matches("of", "263323") == index.exact_matches("of", "OF263323")
    assert len(index.exact_matches("of", "263323")) == 300


def test_prefixo_e_variante_adicional_nao_substituta():
    """Nem tudo na coluna OV do plano começa por OV — a forma escrita continua a valer."""
    entries = [{"plan_key": "X", "of": "OF1", "ov": "No 4711", "modelo": "M", "perfil": "L1X1X1"}]
    index = PlanIndex(entries, CANT_SPEC)
    assert index.exact_matches("ov", "No 4711") == [0]


# ---- teto de candidatos ----

def test_of_grande_continua_visivel():
    """Uma OF de 300 linhas é na mesma uma OF; com o teto ficava invisível."""
    index = make_index(n_irmas=300)
    scorer = Scorer(index, CrossParams())
    assert scorer.candidates({"of": "263323"})


# ---- marginais ----

def test_marginal_da_of_sobrevive_a_diluicao_entre_irmas():
    """Com 300 irmãs, P(linha exacta) é sempre baixa mas P(OF) tem de ser alta.

    É esta a diferença que punha tudo a `unmatched`: perguntava-se «qual destas
    300 linhas é?» quando a pergunta útil é «é esta a OF?».
    """
    index = make_index(n_irmas=300)
    scorer = Scorer(index, CrossParams())
    match = scorer.match_row({"of": "263323", "perfil": "60 x 5"})
    assert match.winner is not None
    assert match.p_correct < 0.5, "a linha exacta não pode ser identificável"
    assert match.p_primary > 0.9, f"a OF tem de ser: {match.p_primary}"
    assert match.marginals["of"][0] == "OF263323"


def test_marginal_nao_inventa_confianca_quando_ha_duas_ofs_plausiveis():
    entries = [
        {"plan_key": "A", "of": "OF100001", "ov": "OV1", "modelo": "M1", "perfil": "L1X1X1"},
        {"plan_key": "B", "of": "OF100002", "ov": "OV2", "modelo": "M1", "perfil": "L1X1X1"},
    ]
    index = PlanIndex(entries, CANT_SPEC)
    scorer = Scorer(index, CrossParams())
    match = scorer.match_row({"modelo": "M1"})
    if match.winner is not None:
        assert match.p_primary < 0.9
