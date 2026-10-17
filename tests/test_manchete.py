"""Texto da faixa a partir do título original (regras fixas, sem IA)."""
import pytest

from canaldark.manchete import MAX_CHARS, clean_title, headline_for


@pytest.mark.parametrize("title,expected", [
    ("Bolacha #engraçado #humor #memesbr", "Bolacha"),
    ("ARTHUR CONVIDOU A HATER PRO PODCAST! 😳 #mbl #partidomissao", "ARTHUR CONVIDOU A HATER PRO PODCAST!"),
    ("Starbucks em choque com o que viu 👀💀", "Starbucks em choque com o que viu"),
    ("Épico 😂 #memesjurídicos #advogado", "Épico"),
    ("O Primeiro Video Game da História 🕹️ #curiosidades", "O Primeiro Video Game da História"),
    ("A Samsung ENGANOU seus usuários? 😭📱", "A Samsung ENGANOU seus usuários?"),
    ("Game Dev ☠️", "Game Dev"),
    ("Real", "Real"),
    ("Can't stop skating the new park🔥 #gaming", "Can't stop skating the new park"),
    ("Essa Cena Horripilante Foi Inspirada na Arte de Damien Hirst — The Cell (2000)",
     "Essa Cena Horripilante Foi Inspirada na Arte de Damien Hirst — The Cell (2000)"),
    ("The Average Guy..", "The Average Guy…"),
    ("How Rockstar convinced GTA 6 Fans 😭🕹️", "How Rockstar convinced GTA 6 Fans"),
    ("Tesla Roadster Flying Car 🚀", "Tesla Roadster Flying Car"),
    ("usa o cinto 😂 #comedyeffect", "usa o cinto"),
    ("A mão mais rápido do oeste...🤠", "A mão mais rápido do oeste…"),
])
def test_examples_from_reference_shorts(title, expected):
    assert clean_title(title) == expected


def test_cuts_channel_suffix_after_pipe():
    assert clean_title("Lula fala sobre economia | Jornal da Band") == "Lula fala sobre economia"


def test_keeps_pipe_when_the_part_before_it_is_short():
    assert clean_title("Bolacha | Canal") == "Bolacha | Canal"
    assert clean_title("A | B | Notícias de hoje | Canal") == "A | B | Notícias de hoje"


def test_long_title_truncated_at_word():
    words = ["palavra", "comprida", "outra", "coisa", "Brasil", "eleição"]
    title = " ".join(words[i % len(words)] for i in range(40))
    assert len(title) > 200
    out = clean_title(title)
    assert len(out) <= MAX_CHARS and out.endswith("…")
    assert out[:-1].split(" ")[-1] in words  # a última palavra continua inteira
    assert title.startswith(out[:-1])


def test_long_word_without_spaces_is_cut_hard():
    out = clean_title("A" * 200)
    assert out == "A" * 89 + "…"


def test_truncation_drops_trailing_punctuation():
    title = "x" * 85 + ", " + "y" * 20  # o último espaço antes da posição 90 vem logo depois da vírgula
    assert clean_title(title) == "x" * 85 + "…"


def test_junk_words_case_insensitive_and_whole_words_only():
    assert clean_title("Gato fofo SHORTS Viral fyp") == "Gato fofo"
    assert clean_title("Shortcut do TikTok") == "Shortcut do"


def test_repeated_punctuation_and_edge_separators():
    assert clean_title("- Que isso?!!! ??? Incrível!!!! •") == "Que isso?! ? Incrível!"
    assert clean_title("| Notícia: ,;") == "Notícia"


def test_mentions_and_links_removed():
    assert clean_title("Veja isso @fulano https://x.com/y") == "Veja isso"
    assert clean_title("Olha https://www.youtube.com/@GaiaMemes aqui") == "Olha aqui"


def test_emoji_between_words_does_not_glue_them():
    assert clean_title("Olá😂mundo") == "Olá mundo"


def test_fallback_rotation():
    assert headline_for("#shorts #fyp 😂", ["A", "B"], 3) == "B"
    assert headline_for("Ok!", ["A", "B"], 0) == "A" and headline_for("Real", ["A"], 0) == "Real"


def test_weird_titles_fall_back_or_fit():
    fallbacks = ["Olha isso", "Você viu?"]
    assert headline_for("#lula #bolsonaro #eleicoes2026", fallbacks, 0) == "Olha isso"
    assert headline_for("😂😂😂🔥🔥", fallbacks, 1) == "Você viu?"
    assert headline_for("https://youtu.be/abc @canal", fallbacks, 2) == "Olha isso"
    assert headline_for("TUDO EM MAIÚSCULAS AQUI", fallbacks, 0) == "TUDO EM MAIÚSCULAS AQUI"
    assert len(headline_for("Notícia " * 40, fallbacks, 0)) <= MAX_CHARS
