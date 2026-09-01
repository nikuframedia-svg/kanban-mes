"""Parâmetros do motor de cruzamento.

Defaults conservadores escolhidos por ordem de grandeza; os valores afinados são
medidos nos NOSSOS dados pelo backtest (scripts/backtest.py) e gravados em
params/cross_params.json — nada é copiado de outros sistemas.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

PARAMS_PATH = Path(__file__).resolve().parent.parent.parent / "params" / "cross_params.json"

# Pares de glifos que qualquer OCR/caligrafia confunde. Conhecimento geral de
# domínio (não medido): serve de prior; o ciclo de aprendizagem refina com
# correções humanas reais.
GLYPH_CONFUSABLE: frozenset[tuple[str, str]] = frozenset(
    tuple(sorted(p))
    for p in [
        ("0", "O"), ("0", "Q"), ("0", "D"), ("1", "I"), ("1", "L"), ("1", "7"),
        ("5", "S"), ("8", "B"), ("6", "G"), ("2", "Z"), ("9", "G"), ("4", "A"),
        ("M", "H"), ("E", "F"), ("U", "V"), ("K", "R"), ("C", "G"), ("3", "8"),
    ]
)


@dataclass
class ChannelParams:
    """Canal de confusão de caracteres: custos em bits de cada operação de edição.
    Custo alto = substituição improvável. g = evidência em [0,1] de que a escrita
    é uma leitura plausível do valor verdadeiro."""

    sub_default_bits: float = 8.0     # substituição arbitrária
    glyph_pair_bits: float = 2.0      # par visualmente confundível (0↔O, 5↔S…)
    # Inserção/omissão de um carácter. Maior que sub/2 (8/2) de propósito:
    # com 4.0, del+ins custava o mesmo que UMA substituição e quase tudo era
    # «leitura plausível» — deu 8 irmãs empatadas no caso AT1T515/AT1T145.
    indel_bits: float = 5.0
    case_free: bool = True            # maiúsculas/minúsculas sem custo
    g_l0_bits: float = 12.0           # normalizador: g = 1 - custo/g_l0
    g_cap: float = 0.85               # evidência do canal nunca vale um match exato
    # custos aprendidos com correções humanas: {"A>B": bits}
    learned_subs: dict[str, float] = field(default_factory=dict)


@dataclass
class ScoreParams:
    """Fellegi–Sunter em bits: peso de um campo = log2(m/u).
    m = P(campo concorda | linha certa) — a medir no backtest.
    u = P(concordar por acaso) = frequência do VALOR no plano (calculada em vivo)."""

    # m por tipo de campo (default até o backtest medir por campo)
    m_default: float = 0.60
    m_by_field: dict[str, float] = field(default_factory=dict)
    u_min_corpus: int = 1000          # piso do denominador de u (planos pequenos)
    w_min_bits: float = 1.0           # peso mínimo de um acordo exato
    w_cap_bits: float = 14.0          # teto por campo (OF única não vale infinito)
    # ladder de acordo gradual (fração do peso consoante a semelhança)
    sim_full: float = 1.0             # sim >= 1.0  → peso total
    sim_near: float = 0.90            # sim >= 0.9  → metade do peso
    near_fraction: float = 0.5
    channel_floor_sim: float = 0.80   # abaixo disto nem o canal salva
    channel_g_floor: float = 0.30
    # penalizações por discordância
    disagree_code_bits: float = -1.5  # campos-código (OF, OV, lote, nesting)
    disagree_text_bits: float = -1.0  # campos-texto (cliente, modelo)
    veto_valid_code_bits: float = -3.0  # contradizer um código escrito que EXISTE no plano
    # dimensões numéricas — pontuadas em conjunto
    dim_disagree_bits: float = -2.5
    dim_disagree_cap_bits: float = -5.0
    dim_joint_cap_bits: float = 12.0
    dim_u_floor: float = 0.10         # colisão mínima assumida por dimensão
    # contexto (tetos < margem decisiva: desempata, nunca vence evidência direta)
    context_cap_bits: float = 2.0
    active_of_bonus_bits: float = 2.0
    active_of_days: int = 14
    margin_decisive_bits: float = 4.0


@dataclass
class PosteriorParams:
    """Confiança calibrada. P(certo) via softmax sobre candidatos + H0 explícito."""

    temperature_bits: float = 2.5     # a calibrar por Brier no backtest
    b_h0_raw_bits: float = 10.0       # evidência típica que uma linha verdadeira deve exceder
    # π(H0) = P(linha não está no plano), por idade do plano em dias.
    # Default monótono simples; o backtest/produção medem os valores reais.
    pi_h0_base: float = 0.05
    pi_h0_per_day: float = 0.01
    pi_h0_max: float = 0.50


@dataclass
class PolicyParams:
    """Política de escrita por perda esperada: escrever se P(certo) > 1 − C_rev/C_erro.
    Limiar por criticidade do campo — decisão de política, não constante medida."""

    write_threshold_identity: float = 0.95
    write_threshold_critical_dim: float = 0.98
    write_threshold_default: float = 0.90
    # Abaixo disto não há linha do plano credível: nada de propostas, e as
    # células passam a validar-se valor a valor contra o plano (existe ou não).
    propose_threshold: float = 0.50
    # Existindo candidato, o melhor vencedor determinístico substitui toda a
    # identidade pelo formato canónico do plano — strong/weak, vazios,
    # herdados e edições humanas incluídos. A transcrição OCR fica intacta.
    replace_with_plan: bool = True
    criticality: dict[str, int] = field(
        default_factory=lambda: {
            "esp": 5, "thickness_mm": 5, "comp_mm": 5, "length_mm": 5,
            "of": 3, "ov": 3, "cliente": 3, "modelo": 3, "lote": 3, "nesting": 3,
            # perfil e máquina são identidade: sem isto usavam o limiar de
            # escrita default (0.90) e pesavam o mínimo na fila de revisão
            "perfil": 3, "maquina": 3,
        }
    )
    criticality_default: int = 1


@dataclass
class CrossParams:
    channel: ChannelParams = field(default_factory=ChannelParams)
    score: ScoreParams = field(default_factory=ScoreParams)
    posterior: PosteriorParams = field(default_factory=PosteriorParams)
    policy: PolicyParams = field(default_factory=PolicyParams)
    fitted_from: str = "defaults"     # proveniência dos valores (defaults | backtest <data>)

    def save(self, path: Path = PARAMS_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False))
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path = PARAMS_PATH) -> "CrossParams":
        if not path.exists():
            return cls()
        data = json.loads(path.read_text())
        return cls(
            channel=ChannelParams(**data.get("channel", {})),
            score=ScoreParams(**data.get("score", {})),
            posterior=PosteriorParams(**data.get("posterior", {})),
            policy=PolicyParams(**data.get("policy", {})),
            fitted_from=data.get("fitted_from", "defaults"),
        )
