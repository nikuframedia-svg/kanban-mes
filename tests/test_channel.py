from app.matching.channel import CharChannel
from app.matching.params import ChannelParams


def make_channel():
    return CharChannel(ChannelParams())


def test_identical_is_free():
    ch = make_channel()
    assert ch.align_cost_bits("OF250002", "OF250002") == 0.0
    assert ch.evidence("OF250002", "OF250002") == ChannelParams().g_cap


def test_glyph_confusion_is_cheap():
    ch = make_channel()
    # 5↔S: um glifo confundível deve custar pouco e dar evidência alta
    cost = ch.align_cost_bits("OF2S0002", "OF250002")
    assert cost == ChannelParams().glyph_pair_bits
    assert ch.evidence("OF2S0002", "OF250002") > 0.7


def test_arbitrary_substitution_is_expensive():
    ch = make_channel()
    # X↔5 não é confusão visual: custo alto
    assert ch.align_cost_bits("OFX50002", "OF250002") >= ChannelParams().sub_default_bits - 4.0
    # código completamente diferente: evidência zero
    assert ch.evidence("ZZZZZZ", "OF250002") == 0.0


def test_indel():
    ch = make_channel()
    assert ch.align_cost_bits("OF25002", "OF250002") == ChannelParams().indel_bits


def test_learned_subs_override():
    p = ChannelParams(learned_subs={"7>1": 1.0})
    ch = CharChannel(p)
    assert ch.sub_cost("7", "1") == 1.0
