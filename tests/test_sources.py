"""Testes das fontes da captação: URLs de listagem, entradas do yt-dlp, Pexels e Pixabay."""
from datetime import datetime, timezone

import httpx
import pytest
import yt_dlp

from canaldark.capture import sources
from canaldark.capture.sources import (SourceError, candidate_from_entry, list_candidates, listing_url,
                                       ytdlp_extractor)
from canaldark.models import SourceSpec

PUBLISHED = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
ENTRY = {
    "_type": "url", "ie_key": "Youtube", "id": "abc123def45",
    "url": "https://www.youtube.com/shorts/abc123def45",
    "title": "GTA 6 trailer reação", "duration": 42, "view_count": 1000,
    "channel": "Gaia Memes", "uploader": "Gaia Memes", "uploader_id": "@GaiaMemes",
    "timestamp": PUBLISHED.timestamp(), "live_status": None,
    "thumbnails": [{"url": "https://i.ytimg.com/vi/abc/default.jpg"}, {"url": "https://i.ytimg.com/vi/abc/hq.jpg"}],
}


def channel(url="https://www.youtube.com/@GaiaMemes", **kw):
    return SourceSpec(tipo="youtube_canal", url=url, **kw)


def search(text="meme gta 6", **kw):
    return SourceSpec(tipo="youtube_busca", busca=text, **kw)


def offline():
    """Cliente HTTP que reprova o teste se alguém tentar usar a rede."""
    def handler(request):
        pytest.fail(f"acesso inesperado à rede: {request.url}")
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def fake_extractor(info, calls=None):
    async def extract(url, opts):
        if calls is not None:
            calls.append((url, opts))
        return info
    return extract


def test_listing_urls():
    assert listing_url(channel()) == "https://www.youtube.com/@GaiaMemes/shorts"
    # o yt-dlp atual não tem mais "ytsearchdate" (o YouTube tirou a ordem por data): busca filtrada por
    # "esta semana" + "vídeos"
    assert listing_url(search()) == "https://www.youtube.com/results?search_query=meme+gta+6&sp=EgQIAxAB"
    cc = listing_url(search("gatos fofos", so_creative_commons=True))
    assert cc == "https://www.youtube.com/results?search_query=gatos+fofos&sp=EgIwAQ%3D%3D"
    assert listing_url(SourceSpec(tipo="tiktok_perfil", url="https://www.tiktok.com/@perfil")) == \
        "https://www.tiktok.com/@perfil"
    assert listing_url(SourceSpec(tipo="instagram_perfil", url="https://www.instagram.com/perfil/")) == \
        "https://www.instagram.com/perfil/"


@pytest.mark.parametrize("pasted,expected", [
    ("https://www.youtube.com/@GaiaMemes/", "https://www.youtube.com/@GaiaMemes/shorts"),
    ("https://www.youtube.com/@GaiaMemes/shorts", "https://www.youtube.com/@GaiaMemes/shorts"),
    ("https://www.youtube.com/@GaiaMemes/videos?view=0&si=x", "https://www.youtube.com/@GaiaMemes/shorts"),
    ("https://www.youtube.com/channel/UC0123456789abcdefghijkl", "https://www.youtube.com/channel/UC0123456789abcdefghijkl/shorts"),
    ("youtube.com/@GaiaMemes", "https://youtube.com/@GaiaMemes/shorts"),
    ("@GaiaMemes", "https://www.youtube.com/@GaiaMemes/shorts"),
])
def test_channel_link_pasted_with_tab_query_or_without_scheme(pasted, expected):
    assert listing_url(channel(pasted)) == expected


async def test_youtube_flat_entries_become_candidates():
    second = {**ENTRY, "id": "x2", "url": "https://www.youtube.com/shorts/x2", "uploader_id": None,
              "timestamp": None, "upload_date": "20261015", "thumbnails": None,
              "thumbnail": "https://i.ytimg.com/vi/x2/hq.jpg", "view_count": None, "duration": None}
    calls = []
    async with offline() as client:
        found = await list_candidates(channel(tom="meme"), client=client,
                                      extract=fake_extractor({"entries": [ENTRY, None, second]}, calls))
    assert [url for url, _ in calls] == ["https://www.youtube.com/@GaiaMemes/shorts"]
    first, other = found
    assert (first.url, first.titulo, first.autor, first.fonte) == (
        "https://www.youtube.com/shorts/abc123def45", "GTA 6 trailer reação", "@GaiaMemes", "@GaiaMemes")
    assert (first.views, first.duracao, first.publicado_em) == (1000, 42, PUBLISHED)
    assert first.capa == "https://i.ytimg.com/vi/abc/hq.jpg"
    assert other.autor == "Gaia Memes"  # sem uploader_id: vale o nome do canal
    assert other.publicado_em == datetime(2026, 10, 15, tzinfo=timezone.utc)
    assert other.capa == "https://i.ytimg.com/vi/x2/hq.jpg"
    assert (other.views, other.duracao) == (None, None)
    async with offline() as client:
        capped = await list_candidates(channel(), client=client, limit=1,
                                       extract=fake_extractor({"entries": [ENTRY, second]}))
    assert [c.url for c in capped] == [ENTRY["url"]]


def test_author_fallbacks_and_handle_prefix():
    base = {"url": "https://www.youtube.com/shorts/z", "title": "Oi"}
    assert candidate_from_entry({**base, "uploader_id": "Fulano"}, search()).autor == "@Fulano"
    assert candidate_from_entry({**base, "uploader": "Fulano de Tal"}, search()).autor == "Fulano de Tal"
    assert candidate_from_entry(base, channel()).autor == "@GaiaMemes"  # o rótulo da fonte
    assert candidate_from_entry({"webpage_url": "https://www.youtube.com/shorts/w", "title": "Oi"}, search()).url == \
        "https://www.youtube.com/shorts/w"


def test_tiktok_and_instagram_credit_the_handle_not_the_numeric_id():
    tiktok = SourceSpec(tipo="tiktok_perfil", url="https://www.tiktok.com/@leenabhushan")
    entry = {"url": "https://www.tiktok.com/@leenabhushan/video/1", "title": "dança", "channel": "Leena B",
             "uploader": "leenabhushan", "uploader_id": "6691488002098119685"}
    assert candidate_from_entry(entry, tiktok).autor == "@leenabhushan"
    insta = SourceSpec(tipo="instagram_perfil", url="https://www.instagram.com/naomipq/")
    entry = {"url": "https://www.instagram.com/reel/C1/", "title": "Video by naomipq", "channel": "naomipq",
             "uploader": "B E A U T Y", "uploader_id": "2815873"}
    assert candidate_from_entry(entry, insta).autor == "@naomipq"


@pytest.mark.parametrize("status", ["is_live", "is_upcoming"])
def test_live_entries_are_dropped(status):
    assert candidate_from_entry({**ENTRY, "live_status": status}, search()) is None
    assert candidate_from_entry({**ENTRY, "live_status": "was_live"}, search()) is not None


def test_entries_without_url_or_title_and_channel_results_are_dropped():
    assert candidate_from_entry({**ENTRY, "url": None}, search()) is None
    assert candidate_from_entry({**ENTRY, "title": "  "}, search()) is None
    # a busca do YouTube também devolve canais e playlists: baixar um deles baixaria dezenas de vídeos
    result = {"_type": "url", "ie_key": "YoutubeTab", "url": "https://www.youtube.com/channel/UCx", "title": "Canal"}
    assert candidate_from_entry(result, search()) is None


async def test_default_extractor_runs_yt_dlp_flat(monkeypatch):
    seen = []

    class FakeYDL:
        def __init__(self, opts):
            seen.append(opts)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=True):
            seen.append((url, download))
            return {"entries": [ENTRY]}

    monkeypatch.setattr(sources.yt_dlp, "YoutubeDL", FakeYDL)
    async with offline() as client:
        found = await list_candidates(search(), client=client, limit=5)
        opts, call = seen
        assert call == ("https://www.youtube.com/results?search_query=meme+gta+6&sp=EgQIAxAB", False)
        assert (opts["extract_flat"], opts["playlistend"], opts["quiet"]) == ("in_playlist", 5, True)
        assert "cookiesfrombrowser" not in opts
        assert opts["extractor_args"]["youtubetab"]["approximate_date"]  # data aproximada nos resultados da busca
        assert [c.url for c in found] == [ENTRY["url"]]
        seen.clear()
        await list_candidates(search(), client=client, extract=ytdlp_extractor("chrome"))
    assert seen[0]["cookiesfrombrowser"] == ("chrome",)


async def test_bot_check_asks_for_cookies():
    async def extract(url, opts):
        raise yt_dlp.utils.DownloadError(
            "ERROR: [youtube] abc123def45: Sign in to confirm you’re not a bot. Use --cookies-from-browser")
    async with offline() as client:
        with pytest.raises(SourceError, match="cookies_navegador"):
            await list_candidates(search(), client=client, extract=extract)


def failing_filtered_search(calls, error):
    async def extract(url, opts):
        calls.append(url)
        if url.startswith("https://www.youtube.com/results"):
            raise error
        return {"entries": [ENTRY]}
    return extract


async def test_search_falls_back_to_plain_search_when_filter_is_refused():
    calls = []
    refused = yt_dlp.utils.DownloadError("ERROR: [youtube:search_url] meme gta 6: HTTP Error 400: Bad Request")
    async with offline() as client:
        found = await list_candidates(search(), client=client, limit=10,
                                      extract=failing_filtered_search(calls, refused))
    assert calls == ["https://www.youtube.com/results?search_query=meme+gta+6&sp=EgQIAxAB", "ytsearch10:meme gta 6"]
    assert [c.url for c in found] == [ENTRY["url"]]


async def test_no_fallback_for_bot_check_or_creative_commons_search():
    bot = yt_dlp.utils.DownloadError("ERROR: [youtube:search_url] x: Sign in to confirm you’re not a bot")
    refused = yt_dlp.utils.DownloadError("ERROR: [youtube:search_url] x: HTTP Error 400: Bad Request")
    for spec, error, match in [(search(), bot, "cookies_navegador"),
                               (search(so_creative_commons=True), refused, "HTTP Error 400")]:
        calls = []
        async with offline() as client:
            with pytest.raises(SourceError, match=match):
                await list_candidates(spec, client=client, extract=failing_filtered_search(calls, error))
        assert len(calls) == 1  # sem filtro de licença a busca traria vídeos que o download recusaria


async def test_other_extractor_failures_become_source_errors():
    async def extract(url, opts):
        raise yt_dlp.utils.DownloadError("ERROR: [youtube:tab] @GaiaMemes: This channel does not have a shorts tab")
    async with offline() as client:
        with pytest.raises(SourceError, match="does not have a shorts tab") as info:
            await list_candidates(channel(), client=client, extract=extract)
    assert "cookies" not in str(info.value)


PEXELS = {"videos": [
    {"id": 1, "url": "https://www.pexels.com/video/gato-1/", "image": "https://images.pexels.com/1.jpg",
     "duration": 12, "user": {"name": "Ana"},
     "video_files": [
         {"width": 1920, "height": 1080, "link": "https://v.pexels.com/1-deitado.mp4", "file_type": "video/mp4"},
         {"width": 720, "height": 1280, "link": "https://v.pexels.com/1-720.mp4", "file_type": "video/mp4"},
         {"width": 1080, "height": 1920, "link": "https://v.pexels.com/1-1080.mp4", "file_type": "video/mp4"},
         {"width": 2160, "height": 3840, "link": "https://v.pexels.com/1-4k.mp4", "file_type": "video/mp4"},
         {"width": None, "height": None, "link": "https://v.pexels.com/1.m3u8", "file_type": "video/hls"}]},
    {"id": 2, "url": "https://www.pexels.com/video/2/", "image": "https://images.pexels.com/2.jpg", "duration": 8,
     "user": {"name": "Bia"},
     "video_files": [{"width": 1920, "height": 1080, "link": "https://v.pexels.com/2.mp4"}]},  # nada em pé
]}


async def test_pexels_picks_portrait_file():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=PEXELS)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        found = await list_candidates(SourceSpec(tipo="pexels", busca="cute cat"), client=client,
                                      keys={"pexels": "CHAVE"})
    request, = requests
    assert (request.url.host, request.url.path) == ("api.pexels.com", "/videos/search")
    assert dict(request.url.params) == {"query": "cute cat", "orientation": "portrait", "per_page": "30"}
    assert request.headers["Authorization"] == "CHAVE"
    video, = found
    assert video.direto == "https://v.pexels.com/1-1080.mp4" and video.autor == "Ana · Pexels"
    assert (video.url, video.licenca, video.capa, video.duracao, video.fonte) == (
        "https://www.pexels.com/video/gato-1/", "Pexels", "https://images.pexels.com/1.jpg", 12, "Pexels: cute cat")
    assert video.titulo == ""  # a API não tem título: o texto da faixa cai na manchete_padrao do canal


PIXABAY = {"hits": [
    {"id": 10, "pageURL": "https://pixabay.com/videos/id-10/", "duration": 9, "views": 500, "user": "Joao",
     "videos": {"large": {"url": "https://cdn.pixabay.com/10_large.mp4", "width": 1080, "height": 1920,
                          "thumbnail": "https://cdn.pixabay.com/10_large.jpg"},
                "medium": {"url": "https://cdn.pixabay.com/10_medium.mp4", "width": 720, "height": 1280,
                           "thumbnail": "https://cdn.pixabay.com/10_medium.jpg"}}},
    {"id": 11, "pageURL": "https://pixabay.com/videos/id-11/", "duration": 7, "views": 50, "user": "Lia",
     "videos": {"large": {"url": "https://cdn.pixabay.com/11_large.mp4", "width": 1920, "height": 1080,
                          "thumbnail": "https://cdn.pixabay.com/11_large.jpg"},
                "medium": {"url": "https://cdn.pixabay.com/11_medium.mp4", "width": 720, "height": 1280}}},
    {"id": 12, "pageURL": "https://pixabay.com/videos/id-12/", "duration": 6, "views": 5, "user": "Rui",
     "videos": {"large": {"url": "", "width": 0, "height": 0, "thumbnail": ""},
                "medium": {"url": "https://cdn.pixabay.com/12_medium.mp4", "width": 720, "height": 1280,
                           "thumbnail": "https://cdn.pixabay.com/12_medium.jpg"}}},
]}


async def test_pixabay_parses_hits():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=PIXABAY)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        found = await list_candidates(SourceSpec(tipo="pixabay", busca="kitten"), client=client,
                                      keys={"pixabay": "CHAVE"})
    request, = requests
    assert (request.url.host, request.url.path) == ("pixabay.com", "/api/videos/")
    assert dict(request.url.params) == {"key": "CHAVE", "q": "kitten", "per_page": "30", "safesearch": "true"}
    assert [c.url for c in found] == ["https://pixabay.com/videos/id-10/", "https://pixabay.com/videos/id-12/"]
    big, medium_only = found
    assert (big.direto, big.autor, big.views, big.capa, big.duracao, big.licenca, big.fonte) == (
        "https://cdn.pixabay.com/10_large.mp4", "Joao · Pixabay", 500, "https://cdn.pixabay.com/10_large.jpg", 9,
        "Pixabay", "Pixabay: kitten")
    assert (medium_only.direto, medium_only.capa) == (
        "https://cdn.pixabay.com/12_medium.mp4", "https://cdn.pixabay.com/12_medium.jpg")


async def test_pixabay_small_limit_still_sends_a_valid_page_size():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=PIXABAY)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        found = await list_candidates(SourceSpec(tipo="pixabay", busca="kitten"), client=client,
                                      keys={"pixabay": "CHAVE"}, limit=1)
    assert requests[0].url.params["per_page"] == "3"  # a API recusa menos de 3
    assert len(found) == 1


async def test_pixabay_error_does_not_leak_the_key():
    def handler(request):
        return httpx.Response(400, text="[ERROR 400] Invalid or missing API key")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SourceError) as info:
            await list_candidates(SourceSpec(tipo="pixabay", busca="kitten"), client=client,
                                  keys={"pixabay": "SEGREDO123"})
    assert "400" in str(info.value) and "SEGREDO123" not in str(info.value)


@pytest.mark.parametrize("tipo,name", [("pexels", "Pexels"), ("pixabay", "Pixabay")])
@pytest.mark.parametrize("keys", [None, {}, {"pexels": "", "pixabay": ""}])
async def test_missing_key_raises_source_error(tipo, name, keys):
    async with offline() as client:
        with pytest.raises(SourceError) as info:
            await list_candidates(SourceSpec(tipo=tipo, busca="cat"), client=client, keys=keys)
    assert str(info.value) == f"chave do {name} não configurada: rode `canaldark auth {tipo}`"
