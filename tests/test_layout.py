import pytest

from canaldark.edit.layout import bar_height, build_ass, font_size_for, wrap_headline
from canaldark.edit.tts import WordTiming, estimate_timings


def test_bar_for_16x9():
    assert bar_height(1920, 1080) == 656


def test_bar_for_vertical_never_below_420():
    assert bar_height(1080, 1920) == 420


def test_bar_for_square():
    assert bar_height(1000, 1000) == 420


def test_wrap_keeps_words_and_accents():
    lines = wrap_headline("Rockstar confirma: GTA 6 terá ação no Brasil? 🔥")
    assert "".join(lines).replace(" ", "") == "Rockstarconfirma:GTA6teráaçãonoBrasil?"
    assert all(len(line) <= 22 for line in lines)


def test_wrap_never_cuts_long_word():
    lines = wrap_headline("Inconstitucionalissimamente aprovado")
    assert lines[0] == "Inconstitucionalissimamente"


def test_wrap_more_than_three_lines_raises():
    with pytest.raises(ValueError):
        wrap_headline("palavra " * 15)


def test_font_size_shrinks_to_fit():
    assert font_size_for(["a"], 656) == 76
    small = font_size_for(["Rockstar confirma: GTA", "6 terá ação no Brasil?"], 420)
    assert 44 <= small < 76 and 22 * 0.62 * small <= 1000


def test_estimate_timings_cover_duration_in_order():
    words = estimate_timings("Olá pessoal, hoje tem novidade", 3.0)
    assert [w.word for w in words] == ["Olá", "pessoal,", "hoje", "tem", "novidade"]
    assert words[0].start == 0 and words[-1].end == pytest.approx(3.0)
    assert all(a.end <= b.start + 1e-9 for a, b in zip(words, words[1:]))


def test_build_ass_groups_up_to_three_words():
    words = [WordTiming(w, i * 0.3, i * 0.3 + 0.3) for i, w in enumerate("um dois três quatro cinco".split())]
    ass = build_ass(words, "palavra")
    events = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
    assert len(events) == 2
    assert events[0].endswith("UM DOIS TRÊS") and "0:00:00.00" in events[0]
    assert "PlayResY: 1920" in ass
