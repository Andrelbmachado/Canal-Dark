"""Nota e filtros dos candidatos da captação, sem IA: views por hora, novidade e palavras do título."""
from __future__ import annotations

import math
import re
import unicodedata
from datetime import datetime, timezone

from canaldark.models import Candidate

UNKNOWN_AGE_HOURS = 168.0  # sem data de publicação, conta como uma semana
RECENT_HOURS = 48.0


def normalize(text: str) -> str:
    """Minúsculas (casefold) e sem acentos: "Eleição" → "eleicao"."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def _words(text: str) -> str:
    return " ".join(normalize(text).split())


def matches_any(title: str, terms: list[str]) -> bool:
    """True se algum termo aparece no título como palavra inteira, sem diferenciar maiúsculas e acentos."""
    text = _words(title)
    for term in terms:
        t = _words(term)
        if t and re.search(rf"(?<!\w){re.escape(t)}(?!\w)", text):
            return True
    return False


def passes(c: Candidate, *, banned: list[str], min_s: float = 5, max_s: float = 180) -> bool:
    if c.duracao is not None and not min_s <= c.duracao <= max_s:
        return False
    return not matches_any(c.titulo, banned)


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def score(c: Candidate, *, keywords: list[str], trending: list[str], authorized: bool, now: datetime) -> float:
    hours = UNKNOWN_AGE_HOURS
    if c.publicado_em is not None:
        hours = (_utc(now) - _utc(c.publicado_em)).total_seconds() / 3600
    vph = c.views / max(1.0, hours) if c.views else 0.0
    s = 2 * math.log10(1 + vph)
    if c.publicado_em is not None and hours < RECENT_HOURS:
        s += 2
    if matches_any(c.titulo, keywords):
        s += 2
    if matches_any(c.titulo, trending):
        s += 1.5
    if authorized:
        s += 0.5
    return round(s, 3)
