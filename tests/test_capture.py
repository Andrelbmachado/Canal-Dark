from pathlib import Path

import httpx
import pytest
import respx

from canaldark.capture import download as dl
from canaldark.capture.sources import read_source
from canaldark.capture.stage import capture_cycle
from canaldark.events import EventBus
from canaldark.models import ChannelConfig, SourceItem, SourceSpec, State, TriageResult
from canaldark.db import get_video

FIX = Path(__file__).parent / "fixtures" / "feeds"
CFG = ChannelConfig(id="gta6", nome="GTA", fontes=[{"tipo": "google_news", "busca": "GTA 6"}],
                    formatos={"noticia": 0.6, "meme": 0.25, "dialogo": 0.15}, estilo_manchete="a",
                    persona_roteiro="b", voz={"id": "pm_alex"}, horarios=["09:00"], contas={"youtube": "gta6"},
                    max_por_dia=4)


async def test_parse_google_news_fixture():
    with respx.mock() as m:
        m.get(url__startswith="https://news.google.com/rss/search").mock(
            return_value=httpx.Response(200, text=(FIX / "google_news.xml").read_text()))
        async with httpx.AsyncClient() as c:
            items = await read_source(SourceSpec(tipo="google_news", busca="GTA 6"), c)
    assert len(items) == 2
    assert items[0].source_name == "Tecnoblog" and items[0].video_url is None


async def test_parse_youtube_feed_sets_video_url():
    with respx.mock() as m:
        m.get(url__startswith="https://www.youtube.com/feeds/videos.xml").mock(
            return_value=httpx.Response(200, text=(FIX / "youtube.xml").read_text()))
        async with httpx.AsyncClient() as c:
            items = await read_source(SourceSpec(tipo="youtube_canal", id="UCx"), c)
    assert items[0].video_url == "https://www.youtube.com/watch?v=VID123"
    assert items[0].source_name == "Rockstar Games"


def _items():
    return [SourceItem(url="https://a.com/1", title="A", source_name="s", video_url="https://youtu.be/1"),
            SourceItem(url="https://a.com/2", title="B", source_name="s", video_url="https://youtu.be/2"),
            SourceItem(url="https://a.com/3", title="C", source_name="s")]


async def test_cycle_keeps_best_of_group(db, monkeypatch):
    async def fake_read(spec, client):
        return _items()[:2]

    async def fake_triage(cfg, items):
        return [TriageResult(url="https://a.com/1", nota=7, grupo="g"),
                TriageResult(url="https://a.com/2", nota=9, grupo="g")]
    monkeypatch.setattr("canaldark.capture.stage.read_source", fake_read)
    monkeypatch.setattr("canaldark.capture.stage.triage", fake_triage)
    n = await capture_cycle(db, EventBus(db), CFG)
    assert n == 1
    async with db.execute("SELECT url_origem, nota FROM videos") as cur:
        rows = await cur.fetchall()
    assert rows[0]["url_origem"] == "https://youtu.be/2" and rows[0]["nota"] == 9


async def test_low_score_discarded_and_daily_cap(db, monkeypatch):
    items = [SourceItem(url=f"https://a.com/{i}", title=str(i), source_name="s", video_url=f"https://v/{i}")
             for i in range(8)]

    async def fake_read(spec, client):
        return items

    async def fake_triage(cfg, its):
        return [TriageResult(url=i.url, nota=(2 if n == 0 else 8), grupo=str(n)) for n, i in enumerate(its)]
    monkeypatch.setattr("canaldark.capture.stage.read_source", fake_read)
    monkeypatch.setattr("canaldark.capture.stage.triage", fake_triage)
    assert await capture_cycle(db, EventBus(db), CFG) == 4


async def test_item_without_video_goes_to_dialogo(db, monkeypatch):
    async def fake_read(spec, client):
        return [_items()[2]]

    async def fake_triage(cfg, items):
        return [TriageResult(url="https://a.com/3", nota=8, grupo="x")]
    monkeypatch.setattr("canaldark.capture.stage.read_source", fake_read)
    monkeypatch.setattr("canaldark.capture.stage.triage", fake_triage)
    await capture_cycle(db, EventBus(db), CFG)
    row = await get_video(db, 1)
    assert row.estado == State.baixado and row.formato == "dialogo"


async def test_download_progress_reaches_100(tmp_path, monkeypatch):
    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=True):
            for done in (0, 500, 1000):
                for h in self.opts["progress_hooks"]:
                    h({"status": "downloading", "downloaded_bytes": done, "total_bytes": 1000})
            out = tmp_path / "v1.mp4"
            out.write_bytes(b"x")
            for h in self.opts["progress_hooks"]:
                h({"status": "finished", "filename": str(out)})
            return {"id": "v1", "ext": "mp4", "requested_downloads": [{"filepath": str(out)}]}
    monkeypatch.setattr(dl.yt_dlp, "YoutubeDL", FakeYDL)
    seen = []

    async def on_progress(p):
        seen.append(p)
    path = await dl.download("https://youtu.be/x", tmp_path, on_progress, cookies_browser=None)
    assert path.exists() and seen[-1] == 100


async def test_download_retries_then_raises(tmp_path, monkeypatch):
    calls = []

    class Boom:
        def __init__(self, opts): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

        def extract_info(self, url, download=True):
            calls.append(1)
            raise dl.yt_dlp.utils.DownloadError("bloqueado")
    monkeypatch.setattr(dl.yt_dlp, "YoutubeDL", Boom)
    monkeypatch.setattr(dl, "RETRY_DELAYS", (0, 0, 0))

    async def noop(p): pass
    with pytest.raises(dl.DownloadFailed):
        await dl.download("https://youtu.be/x", tmp_path, noop, cookies_browser=None)
    assert len(calls) == 3
