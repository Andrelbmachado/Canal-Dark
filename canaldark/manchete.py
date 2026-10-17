"""Texto da faixa: limpa o título original com regras fixas, sem IA."""
import re

from canaldark.edit.layout import _EMOJI

MAX_CHARS = 90
JUNK_WORDS = {"shorts", "short", "fyp", "foryou", "foryoupage", "viral", "reels", "tiktok"}

_URL = re.compile(r"https?://\S+")
_MENTION = re.compile(r"@\w[\w.]*")
_HASHTAG = re.compile(r"#\w+")
# além do _EMOJI do layout: junção (ZWJ), seletor de variação, tecla de número, tags de bandeira e o bloco ⌚⏰⏳
_EMOJI_EXTRA = re.compile("[\U0000200D\U0000FE0F\U000020E3\U000E0020-\U000E007F\U00002300-\U000023FF]")
_JUNK = re.compile(r"\b(?:" + "|".join(sorted(JUNK_WORDS, key=len, reverse=True)) + r")\b", re.IGNORECASE)
_EDGES = " |-–—•·:,;"
_TRAILING_PUNCT = " .,;:!?…|-–—•·"


def clean_title(title: str) -> str:
    """Aplica as regras do texto da faixa, na ordem do plano; maiúsculas e minúsculas ficam como vieram."""
    text = _URL.sub(" ", title)
    text = _MENTION.sub(" ", text)
    text = _HASHTAG.sub(" ", text)
    text = _EMOJI_EXTRA.sub(" ", _EMOJI.sub(" ", text))
    text = _JUNK.sub(" ", text)
    text = re.sub(r"\s+", " ", text)
    head, sep, _ = text.rpartition(" | ")
    if sep and len(head.strip()) >= 10:  # "Título | Canal": fica só o título
        text = head
    text = re.sub(r"!{2,}", "!", text)
    text = re.sub(r"\?{2,}", "?", text)
    text = re.sub(r"\.{2,}", "…", text)
    text = re.sub(r"\s+", " ", text).strip(_EDGES)
    if len(text) > MAX_CHARS:
        cut = text.rfind(" ", 0, MAX_CHARS)
        text = (text[:cut] if cut > 0 else text[:MAX_CHARS - 1]).rstrip(_TRAILING_PUNCT) + "…"
    return text


def headline_for(title: str, fallbacks: list[str], counter: int) -> str:
    """O título limpo; com menos de 3 letras, a próxima frase de `manchete_padrao` em rodízio."""
    text = clean_title(title)
    if sum(ch.isalpha() for ch in text) < 3:
        return fallbacks[counter % len(fallbacks)]
    return text
