"""Testes da nota dos candidatos, dos filtros e da lista de termos em alta do Google Trends."""
import math
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from canaldark.capture import trends
from canaldark.capture.scoring import matches_any, normalize, passes, score
from canaldark.capture.trends import cached_terms, clear_cache, trending_terms
from canaldark.models import Candidate

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:ht="https://trends.google.com/trending/rss"><channel><title>Daily Search Trends</title>
<item><title>Flamengo</title><ht:approx_traffic>200+</ht:approx_traffic></item>
<item><title>Lula</title><ht:approx_traffic>100+</ht:approx_traffic></item>
</channel></rss>"""


def plain(c: Candidate) -> float:
    return score(c, keywords=[], trending=[], authorized=False, now=NOW)


def test_score_components():
    c = Candidate(url="u", titulo="Lula no debate", views=3600, publicado_em=NOW - timedelta(hours=1))
    assert score(c, keywords=["lula"], trending=["Debate"], authorized=True, now=NOW) == pytest.approx(
        2 * math.log10(3601) + 2 + 2 + 1.5 + 0.5, abs=1e-3)


def test_unknown_views_and_date():
    c = Candidate(url="u", titulo="Lula no debate")
    assert score(c, keywords=["lula"], trending=["debate"], authorized=False, now=NOW) == 3.5
    assert plain(Candidate(url="u", titulo="Outro assunto")) == 0


def test_views_without_date_count_a_week_and_score_is_rounded():
    assert plain(Candidate(url="u", titulo="x", views=168 * 99)) == 4.0  # 99 views/h em 168 h → 2·log10(100)
    recent = Candidate(url="u", titulo="x", views=1000, publicado_em=NOW - timedelta(hours=10))
    assert plain(recent) == 6.009  # 2·log10(101) = 4.00864… arredondado, mais o bônus de 48 h


def test_recent_bonus_only_under_48h():
    assert plain(Candidate(url="u", titulo="x", views=0, publicado_em=NOW - timedelta(hours=47))) == 2
    assert plain(Candidate(url="u", titulo="x", views=0, publicado_em=NOW - timedelta(hours=49))) == 0
    naive = Candidate(url="u", titulo="x", views=0, publicado_em=datetime(2026, 10, 17, 11, 0))
    assert plain(naive) == 2  # data sem fuso conta como UTC


def test_normalize_and_whole_word_match():
    assert normalize("Eleição ÁGUA Coração") == "eleicao agua coracao"
    assert matches_any("Debate da ELEIÇÃO", ["eleicao"])
    assert matches_any("Nova IA do Google", ["IA"])
    assert not matches_any("Notícia boa", ["IA"])  # só palavra inteira
    assert matches_any("Segundo   turno hoje", ["segundo turno"])
    assert matches_any("GTA 6 chegou", ["GTA 6"]) and not matches_any("GTA 60 chegou", ["GTA 6"])
    assert not matches_any("Olá, mundo!", ["", "  "])  # termo vazio nunca casa
    assert not matches_any("Olá, mundo!", [])


def test_passes_filters():
    ok = Candidate(url="u", titulo="Trailer novo", duracao=30)
    assert passes(ok, banned=["vazado"])
    assert not passes(ok.model_copy(update={"duracao": 200}), banned=[])
    assert not passes(ok.model_copy(update={"duracao": 3}), banned=[])
    assert passes(ok.model_copy(update={"duracao": None}), banned=[])
    assert passes(ok.model_copy(update={"duracao": 5}), banned=[])
    assert passes(ok.model_copy(update={"duracao": 180}), banned=[])
    assert not passes(ok.model_copy(update={"titulo": "GTA 6 VAZADO hoje"}), banned=["vazado"])
    assert not passes(ok.model_copy(update={"duracao": 60}), banned=[], max_s=59)


@pytest.fixture(autouse=True)
def fresh_trends():
    clear_cache()
    yield
    clear_cache()


def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_trending_terms_cache():
    calls = []

    def ok(request):
        calls.append(str(request.url))
        return httpx.Response(200, text=RSS)

    async with client_for(ok) as client:
        assert await trending_terms(client, "BR") == ["Flamengo", "Lula"]
        assert await trending_terms(client, "BR") == ["Flamengo", "Lula"]
    assert calls == ["https://trends.google.com/trending/rss?geo=BR"]

    def down(request):
        raise httpx.ConnectError("sem rede", request=request)

    async with client_for(down) as client:
        assert await trending_terms(client, "BR", ttl=0) == ["Flamengo", "Lula"]  # falha devolve o cache
        assert await trending_terms(client, "US", ttl=0) == []  # sem cache: lista vazia
    assert cached_terms("BR") == ["Flamengo", "Lula"] and cached_terms("US") == []


async def test_cache_expires_after_ttl(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(trends, "_clock", lambda: clock[0])
    feeds = [RSS, RSS.replace("Flamengo", "Corinthians")]

    def handler(request):
        return httpx.Response(200, text=feeds.pop(0))

    async with client_for(handler) as client:
        assert await trending_terms(client) == ["Flamengo", "Lula"]
        clock[0] += 3599
        assert await trending_terms(client) == ["Flamengo", "Lula"]
        clock[0] += 2
        assert await trending_terms(client) == ["Corinthians", "Lula"]


async def test_failure_waits_before_trying_again(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(trends, "_clock", lambda: clock[0])
    calls = []

    def unavailable(request):
        calls.append(request)
        return httpx.Response(503)

    async with client_for(unavailable) as client:
        assert await trending_terms(client) == []
        clock[0] += 60
        assert await trending_terms(client) == []
        assert len(calls) == 1  # o painel pode perguntar a toda hora sem bater no Google a cada vez
        clock[0] += 300
        assert await trending_terms(client) == []
        assert len(calls) == 2


async def test_page_without_items_keeps_the_old_terms():
    responses = [httpx.Response(200, text=RSS), httpx.Response(200, text="<html>consentimento</html>")]

    async with client_for(lambda request: responses.pop(0)) as client:
        assert await trending_terms(client) == ["Flamengo", "Lula"]
        assert await trending_terms(client, ttl=0) == ["Flamengo", "Lula"]
