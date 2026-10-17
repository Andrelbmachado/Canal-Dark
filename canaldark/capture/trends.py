"""Termos em alta no Google Trends (RSS), com cache por país; usados na nota e na aba Captação."""
from __future__ import annotations

import time

import feedparser
import httpx

TRENDS_RSS = "https://trends.google.com/trending/rss?geo={geo}"
USER_AGENT = "Mozilla/5.0 (CanalDark; +https://github.com/Andrelbmachado/Canal-Dark)"
RETRY_AFTER_FAILURE = 300.0  # depois de uma falha, espera 5 min antes de tentar a rede de novo
TIMEOUT = 15.0

_clock = time.monotonic
_cache: dict[str, tuple[float, list[str]]] = {}  # geo → (quando buscou, termos)
_failed: dict[str, float] = {}  # geo → quando a última busca falhou


async def trending_terms(client: httpx.AsyncClient, geo: str = "BR", *, ttl: float = 3600) -> list[str]:
    """Títulos do RSS do Google Trends; nunca lança: se a rede falhar, devolve o último cache ou []."""
    now = _clock()
    hit = _cache.get(geo)
    if hit and now - hit[0] < ttl:
        return list(hit[1])
    failed = _failed.get(geo)
    if failed is not None and now - failed < min(ttl, RETRY_AFTER_FAILURE):
        return list(hit[1]) if hit else []
    try:
        resp = await client.get(TRENDS_RSS.format(geo=geo), headers={"User-Agent": USER_AGENT},
                                follow_redirects=True, timeout=TIMEOUT)
        resp.raise_for_status()
        terms = _titles(feedparser.parse(resp.content))
        if not terms:  # página de consentimento ou feed vazio: não troca o cache por nada
            raise ValueError("RSS do Google Trends sem itens")
    except Exception:
        _failed[geo] = now
        return list(hit[1]) if hit else []
    _cache[geo] = (now, terms)
    _failed.pop(geo, None)
    return list(terms)


def _titles(feed) -> list[str]:
    terms: list[str] = []
    for entry in feed.entries:
        title = (entry.get("title") or "").strip()
        if title and title not in terms:
            terms.append(title)
    return terms


def cached_terms(geo: str = "BR") -> list[str]:
    """O que já está no cache, sem tocar na rede (para o painel)."""
    hit = _cache.get(geo)
    return list(hit[1]) if hit else []


def clear_cache() -> None:
    _cache.clear()
    _failed.clear()
