"""Testes da etapa de captação: ciclo de busca, download, envio ao Drive e os trabalhadores."""
import asyncio
import json
import math
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import yt_dlp

from canaldark.capture import download as dl
from canaldark.capture import sources, stage
from canaldark.capture.download import DownloadFailed, DownloadResult
from canaldark.capture.sources import SourceError
from canaldark.capture.stage import capture_cycle, download_one, upload_one
from canaldark.db import get_video, insert_video, update_fields
from canaldark.drive import DriveError, DriveUsage
from canaldark.models import Candidate, State

NOW = datetime.now(timezone.utc)


def cand(n, **kw) -> Candidate:
    base = dict(url=f"https://www.youtube.com/shorts/v{n}", titulo=f"Vídeo {n}", autor=f"@autor{n}", duracao=30.0)
    return Candidate(**{**base, **kw})


def lister(by_source: dict, calls: list | None = None):
    """list_candidates falso: devolve (ou lança) o que o teste preparou para o rótulo de cada fonte."""
    async def list_fn(spec, **kwargs):
        if calls is not None:
            calls.append((spec, kwargs))
        result = by_source[spec.rotulo()]
        if isinstance(result, Exception):
            raise result
        return list(result)
    return list_fn


async def no_trends(client, geo="BR", **kwargs):
    return []


async def cycle(ctx, list_fn, trends_fn=no_trends):
    return await capture_cycle(ctx, "teste", list_fn=list_fn, trends_fn=trends_fn, now=NOW)


async def rows(ctx, channel="teste"):
    async with ctx.db.execute("SELECT id FROM videos WHERE canal = ? ORDER BY id", (channel,)) as cur:
        ids = [r["id"] for r in await cur.fetchall()]
    return [await get_video(ctx.db, i) for i in ids]


def drain(sub) -> list[dict]:
    events = []
    while not sub.empty():
        events.append(sub.queue.get_nowait())
    return events


def warnings(events) -> list[str]:
    return [e["data"]["msg"] for e in events if e["type"] == "log" and e["data"]["level"] == "aviso"]


# ---- capture_cycle ------------------------------------------------------------


async def test_cycle_inserts_best_candidates(ctx, cfg_factory):
    ctx.cfgs["teste"] = cfg_factory(captar_por_dia=3, palavras_chave=["gta"], fontes=[
        {"tipo": "youtube_canal", "url": "https://www.youtube.com/@GaiaMemes", "tom": "meme", "autorizado": True},
        {"tipo": "youtube_busca", "busca": "gta 6", "so_creative_commons": True}])
    by_source = {
        "@GaiaMemes": [cand(1, views=10), cand(2, views=100_000, publicado_em=NOW - timedelta(hours=2))],
        "busca: gta 6": [cand(3, titulo="GTA 6 novidade"), cand(4),
                         cand(5, views=50, publicado_em=NOW - timedelta(hours=5), direto="https://cdn.example/5.mp4")],
    }
    geos = []

    async def trends_fn(client, geo="BR", **kwargs):
        geos.append(geo)
        return ["novidade"]

    sub = ctx.bus.subscribe()
    assert await cycle(ctx, lister(by_source), trends_fn) == 3
    got = {r.url_origem.rsplit("/", 1)[-1]: r for r in await rows(ctx)}
    assert set(got) == {"v2", "v3", "v5"}
    two, three, five = got["v2"], got["v3"], got["v5"]
    assert (two.autor, two.fonte, two.fonte_autorizada, two.tom, two.exige_cc, two.estado) == (
        "@autor2", "@GaiaMemes", True, "meme", False, State.descoberto)
    assert two.nota == pytest.approx(2 * math.log10(50_001) + 2 + 0.5, abs=1e-3)
    assert (three.fonte, three.tom, three.exige_cc, three.fonte_autorizada, three.nota) == (
        "busca: gta 6", "serio", True, False, 3.5)  # palavra-chave (+2) e termo em alta (+1.5)
    assert (five.url_midia, five.duracao, five.titulo_origem, five.autor) == (
        "https://cdn.example/5.mp4", 30.0, "Vídeo 5", "@autor5")
    assert geos == ["BR"]
    announced = [e["data"] for e in drain(sub) if e["type"] == "video.estado"]
    assert sorted(d["id"] for d in announced) == sorted(r.id for r in got.values())
    assert {d["estado"] for d in announced} == {"descoberto"}


async def test_cycle_skips_seen_and_failed_sources(ctx, cfg_factory):
    ctx.cfgs["teste"] = cfg_factory(palavras_proibidas=["vazado"], fontes=[
        {"tipo": "youtube_canal", "url": "https://www.youtube.com/@Quebrado"},
        {"tipo": "youtube_busca", "busca": "teste"}])
    await insert_video(ctx.db, "teste", "https://www.youtube.com/shorts/v1", "visto antes", estado=State.descartado)
    by_source = {"@Quebrado": SourceError("o canal sumiu"),
                 "busca: teste": [cand(1), cand(2), cand(3, duracao=300), cand(4, titulo="Trailer VAZADO"), cand(2)]}
    sub = ctx.bus.subscribe()
    assert await cycle(ctx, lister(by_source)) == 1
    assert [r.url_origem for r in await rows(ctx)] == ["https://www.youtube.com/shorts/v1",
                                                       "https://www.youtube.com/shorts/v2"]
    assert any("@Quebrado" in m and "o canal sumiu" in m for m in warnings(drain(sub)))


async def test_nothing_new_means_no_trends_request(ctx):
    await insert_video(ctx.db, "teste", "https://www.youtube.com/shorts/v1", "visto antes")
    asked = []

    async def trends_fn(client, geo="BR", **kwargs):
        asked.append(geo)
        return []

    assert await cycle(ctx, lister({"busca: teste": [cand(1)]}), trends_fn) == 0
    assert asked == []


async def test_cycle_stops_when_stock_full(ctx, cfg_factory):
    ctx.cfgs["teste"] = cfg_factory(estoque_max=2)
    await insert_video(ctx.db, "teste", "https://x.com/1", "a", estado=State.no_drive)
    await insert_video(ctx.db, "teste", "https://x.com/2", "b", estado=State.baixando)
    calls = []
    sub = ctx.bus.subscribe()
    assert await cycle(ctx, lister({"busca: teste": [cand(1)]}, calls)) == 0
    assert calls == []  # nem lê as fontes
    assert any("estoque cheio" in e["data"]["msg"] for e in drain(sub) if e["type"] == "log")


async def test_cycle_fills_only_the_free_stock(ctx, cfg_factory):
    ctx.cfgs["teste"] = cfg_factory(estoque_max=3)
    await insert_video(ctx.db, "teste", "https://x.com/1", "a", estado=State.no_drive)
    await insert_video(ctx.db, "teste", "https://x.com/2", "b", estado=State.arquivado)  # fora do estoque
    assert await cycle(ctx, lister({"busca: teste": [cand(i) for i in range(1, 6)]})) == 2


async def test_daily_cap_counts_videos_created_today(ctx, cfg_factory):
    ctx.cfgs["teste"] = cfg_factory(captar_por_dia=3)
    await insert_video(ctx.db, "teste", "https://x.com/hoje", "a", estado=State.descartado)
    await insert_video(ctx.db, "teste", "https://x.com/antigo", "b", estado=State.arquivado,
                       criado_em="2020-01-01T12:00:00+00:00")
    await insert_video(ctx.db, "outro", "https://x.com/outro", "c")  # outro canal não conta
    assert await cycle(ctx, lister({"busca: teste": [cand(i) for i in range(1, 6)]})) == 2


async def test_daily_cap_reached_skips_reading_sources(ctx, cfg_factory):
    ctx.cfgs["teste"] = cfg_factory(captar_por_dia=1)
    await insert_video(ctx.db, "teste", "https://x.com/hoje", "a", estado=State.descartado)
    calls = []
    assert await cycle(ctx, lister({"busca: teste": [cand(1)]}, calls)) == 0
    assert calls == []


@pytest.mark.parametrize("used,blocked", [(95, True), (90, True), (89, False)])
async def test_cycle_stops_when_drive_almost_full(ctx, used, blocked):
    ctx.drive.usage = DriveUsage(used, 100)
    calls = []
    sub = ctx.bus.subscribe()
    assert await cycle(ctx, lister({"busca: teste": [cand(1)]}, calls)) == (0 if blocked else 1)
    warned = warnings(drain(sub))
    assert bool(warned) == blocked and bool(calls) != blocked
    if blocked:
        assert "Drive" in warned[0]


@pytest.mark.parametrize("usage", [DriveUsage(10, None), None])
async def test_cycle_ignores_drive_without_total(ctx, usage):
    ctx.drive.usage = usage
    assert await cycle(ctx, lister({"busca: teste": [cand(1)]})) == 1


async def test_cycle_goes_on_when_drive_usage_fails(ctx):
    ctx.drive.fail["uso"] = DriveError("rclone about falhou")
    sub = ctx.bus.subscribe()
    assert await cycle(ctx, lister({"busca: teste": [cand(1)]})) == 1
    assert any("rclone about falhou" in m for m in warnings(drain(sub)))


async def test_cycle_survives_trends_failure(ctx):
    async def broken(client, geo="BR", **kwargs):
        raise RuntimeError("trends fora do ar")

    assert await cycle(ctx, lister({"busca: teste": [cand(1)]}), broken) == 1


async def test_cycle_passes_keys_and_channel_cookies_to_sources(ctx, cfg_factory, monkeypatch):
    ctx.keys = {"pexels": "CHAVE"}
    ctx.cfgs["teste"] = cfg_factory(cookies_navegador="chrome", fontes=[{"tipo": "pexels", "busca": "cute cat"}])
    calls = []
    assert await cycle(ctx, lister({"Pexels: cute cat": [cand(1)]}, calls)) == 1
    (spec, kwargs), = calls
    assert spec.busca == "cute cat" and kwargs["keys"] == {"pexels": "CHAVE"}
    assert isinstance(kwargs["client"], httpx.AsyncClient)
    seen = []

    class FakeYDL:
        def __init__(self, opts):
            seen.append(opts)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=True):
            return {"entries": []}

    monkeypatch.setattr(sources.yt_dlp, "YoutubeDL", FakeYDL)
    await kwargs["extract"]("ytsearchdate1:x", {"quiet": True})  # o extrator leva os cookies do canal
    assert seen[0]["cookiesfrombrowser"] == ("chrome",)


# ---- download (yt-dlp e arquivo direto) --------------------------------------


def ydl_factory(calls, *, license=None, error=None, skipped=False):
    """Imita o yt_dlp.YoutubeDL: grava o que o yt-dlp gravaria e devolve o info."""
    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts
            calls.append(opts)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=True):
            if error is not None:
                raise error
            if skipped:  # o match_filter recusou: nada foi baixado
                return {"id": "abc", "duration": 900}
            base = self.opts["outtmpl"].removesuffix(".%(ext)s")
            for hook in self.opts["progress_hooks"]:
                hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100})
            Path(base + ".mp4").write_bytes(b"video")
            Path(base + ".info.json").write_text('{"id": "abc"}')
            Path(base + ".jpg").write_bytes(b"jpg")
            return {"id": "abc", "title": "T", "duration": 33, "license": license,
                    "requested_downloads": [{"filepath": base + ".mp4"}]}
    return FakeYDL


def recorder(progress: list):
    async def on_progress(pct):
        progress.append(pct)
    return on_progress


@pytest.fixture
def no_wait(monkeypatch):
    monkeypatch.setattr(dl, "RETRY_DELAYS", (0, 0, 0))


async def test_download_via_yt_dlp_writes_video_info_and_cover(tmp_path, monkeypatch):
    calls, progress = [], []
    monkeypatch.setattr(dl.yt_dlp, "YoutubeDL", ydl_factory(calls))
    out = tmp_path / "captados"
    res = await dl.download("https://www.youtube.com/shorts/abc", out, recorder(progress), "chrome", name="7")
    assert (res.video, res.info, res.capa) == (out / "7.mp4", out / "7.info.json", out / "7.jpg")
    assert res.meta["duration"] == 33 and progress[-1] == 100
    opts, = calls
    assert opts["outtmpl"] == str(out / "7.%(ext)s")
    assert opts["writeinfojson"] and opts["writethumbnail"] and opts["merge_output_format"] == "mp4"
    assert {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"} in opts["postprocessors"]
    assert opts["cookiesfrombrowser"] == ("chrome",)
    accepts = opts["match_filter"]
    assert accepts({"id": "abc", "title": "T", "duration": 601}, incomplete=False)  # texto = motivo da recusa
    assert accepts({"duration": 600}, incomplete=False) is None
    assert accepts({}, incomplete=False) is None  # duração desconhecida passa


@pytest.mark.parametrize("sizes,expected", [
    ([(360, 640), (480, 854), (720, 1280), (1080, 1920), (2160, 3840)], "1080x1920"),  # Short em pé
    ([(640, 360), (1280, 720), (1920, 1080), (3840, 2160)], "1920x1080"),  # vídeo deitado
    ([(None, None)], "NonexNone"),  # formato sem medidas ainda serve
])
def test_format_picks_full_hd_in_any_orientation(sizes, expected):
    """O seletor de formatos real do yt-dlp, com a lista de formatos do pior para o melhor."""
    audio = {"format_id": "audio", "vcodec": "none", "acodec": "mp4a.40.2", "ext": "m4a", "url": "https://x/a"}
    videos = [{"format_id": f"{w}x{h}", "width": w, "height": h, "vcodec": "avc1", "acodec": "none", "ext": "mp4",
               "url": f"https://x/{w}x{h}"} for w, h in sizes]
    pick = yt_dlp.YoutubeDL({"quiet": True}).build_format_selector(dl.FORMAT)
    chosen = next(iter(pick({"formats": [audio, *videos], "has_merged_format": False, "incomplete_formats": False})))
    assert chosen["format_id"] == f"{expected}+audio"


async def test_download_retries_then_raises(tmp_path, monkeypatch, no_wait):
    calls = []
    monkeypatch.setattr(dl.yt_dlp, "YoutubeDL",
                        ydl_factory(calls, error=yt_dlp.utils.DownloadError("ERROR: HTTP Error 503")))
    with pytest.raises(DownloadFailed, match="3 tentativas"):
        await dl.download("https://www.youtube.com/shorts/abc", tmp_path, recorder([]), name="7")
    assert len(calls) == 3


async def test_bot_check_is_not_retried(tmp_path, monkeypatch, no_wait):
    calls = []
    error = yt_dlp.utils.DownloadError("ERROR: [youtube] abc: Sign in to confirm you’re not a bot.")
    monkeypatch.setattr(dl.yt_dlp, "YoutubeDL", ydl_factory(calls, error=error))
    with pytest.raises(DownloadFailed, match="cookies_navegador"):
        await dl.download("https://www.youtube.com/shorts/abc", tmp_path, recorder([]), name="7")
    assert len(calls) == 1


async def test_skipped_long_video_fails_without_retry(tmp_path, monkeypatch, no_wait):
    calls = []
    monkeypatch.setattr(dl.yt_dlp, "YoutubeDL", ydl_factory(calls, skipped=True))
    with pytest.raises(DownloadFailed, match="longo"):
        await dl.download("https://www.youtube.com/watch?v=abc", tmp_path, recorder([]), name="7")
    assert len(calls) == 1


async def test_require_cc_rejects_and_deletes_files(tmp_path, monkeypatch, no_wait):
    calls = []
    monkeypatch.setattr(dl.yt_dlp, "YoutubeDL", ydl_factory(calls, license=None))
    out = tmp_path / "captados"
    (out / "77.mp4").parent.mkdir(parents=True)
    (out / "77.mp4").write_bytes(b"outro")  # outro vídeo na mesma pasta fica
    with pytest.raises(DownloadFailed, match="Creative Commons"):
        await dl.download("https://www.youtube.com/shorts/abc", out, recorder([]), name="7", require_cc=True)
    assert sorted(p.name for p in out.iterdir()) == ["77.mp4"] and len(calls) == 1


async def test_require_cc_accepts_creative_commons(tmp_path, monkeypatch):
    monkeypatch.setattr(dl.yt_dlp, "YoutubeDL",
                        ydl_factory([], license="Creative Commons Attribution license (reuse allowed)"))
    res = await dl.download("https://www.youtube.com/shorts/abc", tmp_path, recorder([]), name="7", require_cc=True)
    assert res.video.read_bytes() == b"video"


async def test_direct_download_uses_http_not_yt_dlp(tmp_path, monkeypatch):
    class Forbidden:
        def __init__(self, opts):
            pytest.fail("o download direto não usa o yt-dlp")

    monkeypatch.setattr(dl.yt_dlp, "YoutubeDL", Forbidden)
    body = b"x" * 200_000
    requested = []

    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(200, content=body)

    progress = []
    out = tmp_path / "captados"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        res = await dl.download("https://pixabay.com/videos/id-10/", out, recorder(progress), name="9",
                                require_cc=True, direct_url="https://cdn.pixabay.com/10_large.mp4", client=client)
    assert requested == ["https://cdn.pixabay.com/10_large.mp4"]
    assert res.video == out / "9.mp4" and res.video.read_bytes() == body
    expected = {"url": "https://pixabay.com/videos/id-10/", "direct_url": "https://cdn.pixabay.com/10_large.mp4"}
    assert json.loads(res.info.read_text()) == expected and res.meta == expected
    assert res.capa is None  # bytes que não são vídeo: sem quadro para a capa
    assert progress[-1] == 100 and sorted(p.name for p in out.iterdir()) == ["9.info.json", "9.mp4"]


async def test_direct_download_retries_server_errors(tmp_path, no_wait):
    statuses = [503, 503, 200]

    def handler(request):
        code = statuses.pop(0)
        return httpx.Response(code, content=b"ok" if code == 200 else b"")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        res = await dl.download("https://www.pexels.com/video/1/", tmp_path, recorder([]), name="9",
                                direct_url="https://cdn.example/1.mp4", client=client)
    assert res.video.read_bytes() == b"ok" and statuses == []

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))) as client:
        with pytest.raises(DownloadFailed, match="404"):
            await dl.download("https://www.pexels.com/video/2/", tmp_path, recorder([]), name="10",
                              direct_url="https://cdn.example/2.mp4", client=client)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="precisa do ffmpeg")
async def test_direct_download_makes_cover_from_a_frame(tmp_path):
    clip = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=180x320:rate=10:duration=1",
                    "-pix_fmt", "yuv420p", str(clip)], check=True)

    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=clip.read_bytes()))) as client:
        res = await dl.download("https://www.pexels.com/video/1/", tmp_path / "out", recorder([]), name="9",
                                direct_url="https://cdn.example/1.mp4", client=client)
    assert res.capa == tmp_path / "out" / "9.jpg" and res.capa.read_bytes()[:2] == b"\xff\xd8"


# ---- download_one --------------------------------------------------------------


def fake_download(got: dict, *, meta=None, cover=True, error=None):
    async def download(url, out_dir, on_progress, cookies_browser=None, *, name, require_cc=False, direct_url=None,
                       client=None):
        got.update(url=url, out_dir=out_dir, cookies=cookies_browser, name=name, require_cc=require_cc,
                   direct_url=direct_url)
        if error is not None:
            raise error
        out_dir.mkdir(parents=True, exist_ok=True)
        video = out_dir / f"{name}.mp4"
        video.write_bytes(b"video")
        info = out_dir / f"{name}.info.json"
        info.write_text("{}")
        capa = None
        if cover:
            capa = out_dir / f"{name}.jpg"
            capa.write_bytes(b"jpg")
        await on_progress(100.0)
        return DownloadResult(video=video, info=info, capa=capa, meta=meta or {})
    return download


async def test_download_one_sets_paths(ctx, monkeypatch):
    vid = await insert_video(ctx.db, "teste", "https://www.youtube.com/shorts/a", "T", estado=State.baixando,
                             exige_cc=True)
    got = {}
    monkeypatch.setattr(stage, "download", fake_download(got, meta={"duration": 42.5}))
    assert await download_one(ctx, await get_video(ctx.db, vid)) == State.baixado
    row = await get_video(ctx.db, vid)
    folder = ctx.data / "captados" / "teste"
    assert got == {"url": "https://www.youtube.com/shorts/a", "out_dir": folder, "cookies": None, "name": str(vid),
                   "require_cc": True, "direct_url": None}
    assert (row.estado, row.duracao, row.caminho_local) == (State.baixado, 42.5, str(folder / f"{vid}.mp4"))
    assert row.caminho_capa == str(ctx.data / "capas" / f"{vid}-origem.jpg")
    assert Path(row.caminho_capa).read_bytes() == b"jpg" and not (folder / f"{vid}.jpg").exists()


async def test_download_one_direct_file_keeps_listed_duration(ctx, cfg_factory, monkeypatch):
    ctx.cfgs["teste"] = cfg_factory(cookies_navegador="firefox")
    vid = await insert_video(ctx.db, "teste", "https://www.pexels.com/video/1/", "", estado=State.baixando,
                             url_midia="https://cdn.example/1.mp4", duracao=12.0)
    got = {}
    monkeypatch.setattr(stage, "download", fake_download(got, cover=False))
    assert await download_one(ctx, await get_video(ctx.db, vid)) == State.baixado
    row = await get_video(ctx.db, vid)
    assert (got["direct_url"], got["cookies"]) == ("https://cdn.example/1.mp4", "firefox")
    assert (row.duracao, row.caminho_capa, row.estado) == (12.0, None, State.baixado)


async def test_download_failure_goes_to_erro_descoberto(ctx, monkeypatch):
    vid = await insert_video(ctx.db, "teste", "https://www.youtube.com/shorts/a", "T", estado=State.baixando)
    monkeypatch.setattr(stage, "download",
                        fake_download({}, error=DownloadFailed("o vídeo não é Creative Commons; ignorado")))
    sub = ctx.bus.subscribe()
    assert await download_one(ctx, await get_video(ctx.db, vid)) == State.erro
    row = await get_video(ctx.db, vid)
    assert (row.fila_erro, row.erro) == (State.descoberto, "o vídeo não é Creative Commons; ignorado")
    assert {"id": vid, "estado": "erro", "erro": row.erro} in [e["data"] for e in drain(sub)
                                                               if e["type"] == "video.estado"]


# ---- upload_one ----------------------------------------------------------------


async def local_video(ctx, estado=State.enviando, with_info=True):
    folder = ctx.data / "captados" / "teste"
    folder.mkdir(parents=True, exist_ok=True)
    vid = await insert_video(ctx.db, "teste", f"https://www.youtube.com/shorts/{estado.value}", "T", estado=estado)
    video = folder / f"{vid}.mp4"
    video.write_bytes(b"video")
    if with_info:
        (folder / f"{vid}.info.json").write_text('{"id": "x"}')
    await update_fields(ctx.db, vid, caminho_local=video)
    return await get_video(ctx.db, vid)


async def test_upload_one_moves_to_drive(ctx):
    row = await local_video(ctx)
    assert await upload_one(ctx, row) == State.no_drive
    after = await get_video(ctx.db, row.id)
    assert (after.estado, after.remoto_bruto, after.caminho_local) == (
        State.no_drive, f"teste/1-captados/{row.id}.mp4", None)
    assert ctx.drive.files == {f"teste/1-captados/{row.id}.mp4": b"video",
                               f"teste/1-captados/{row.id}.info.json": b'{"id": "x"}'}
    assert not Path(row.caminho_local).exists()


async def test_upload_failure_goes_to_erro_baixado(ctx):
    row = await local_video(ctx)
    ctx.drive.fail["enviar"] = DriveError("rclone: sem rede")
    assert await upload_one(ctx, row) == State.erro
    after = await get_video(ctx.db, row.id)
    assert after.fila_erro == State.baixado and "sem rede" in after.erro
    assert after.remoto_bruto == f"teste/1-captados/{row.id}.mp4"  # gravado antes de enviar
    assert Path(row.caminho_local).exists() and after.caminho_local == row.caminho_local


async def test_upload_resumes_after_crash_mid_move(ctx):
    row = await local_video(ctx, with_info=False)
    remote = f"teste/1-captados/{row.id}.mp4"
    ctx.drive.files[remote] = b"video"
    Path(row.caminho_local).unlink()  # o rclone moveu o arquivo e a plataforma caiu antes de gravar no_drive
    await update_fields(ctx.db, row.id, remoto_bruto=remote)
    assert await upload_one(ctx, await get_video(ctx.db, row.id)) == State.no_drive
    assert "enviar" not in [c[0] for c in ctx.drive.calls]
    after = await get_video(ctx.db, row.id)
    assert (after.caminho_local, after.remoto_bruto) == (None, remote)


async def test_upload_without_local_file_or_remote_goes_to_erro(ctx):
    row = await local_video(ctx)
    Path(row.caminho_local).unlink()
    assert await upload_one(ctx, row) == State.erro
    after = await get_video(ctx.db, row.id)
    assert after.fila_erro == State.baixado and after.remoto_bruto is None
    assert "enviar" not in [c[0] for c in ctx.drive.calls]


# ---- trabalhadores -------------------------------------------------------------


async def wait_for_state(ctx, vid, state, timeout=5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    row = await get_video(ctx.db, vid)
    while row.estado != state:
        if loop.time() > end:
            raise AssertionError(f"vídeo {vid} ficou em {row.estado.value}, esperava {state.value}")
        await asyncio.sleep(0.01)
        row = await get_video(ctx.db, vid)
    return row


async def stop(ctx, *tasks):
    ctx.stop.set()
    await asyncio.wait_for(asyncio.gather(*tasks), 2)


async def test_download_worker_takes_active_channels_and_survives_errors(ctx, monkeypatch):
    monkeypatch.setattr(stage, "IDLE_SLEEP", 0.01)
    bad = await insert_video(ctx.db, "teste", "https://x.com/ruim", "ruim")
    good = await insert_video(ctx.db, "teste", "https://x.com/bom", "bom")
    other = await insert_video(ctx.db, "outro", "https://x.com/outro", "outro")  # canal fora de channels_for
    ok = fake_download({})

    async def download(url, **kwargs):
        if url.endswith("ruim"):
            raise OSError("disco cheio")
        return await ok(url, **kwargs)

    monkeypatch.setattr(stage, "download", download)
    task = asyncio.create_task(stage.download_worker(ctx))
    try:
        await wait_for_state(ctx, good, State.baixado)
        failed = await wait_for_state(ctx, bad, State.erro)
    finally:
        await stop(ctx, task)
    assert "disco cheio" in failed.erro
    assert (await get_video(ctx.db, other)).estado == State.descoberto


async def test_drive_upload_worker_sends_downloaded_videos(ctx, monkeypatch):
    monkeypatch.setattr(stage, "IDLE_SLEEP", 0.01)
    row = await local_video(ctx, estado=State.baixado)
    task = asyncio.create_task(stage.drive_upload_worker(ctx))
    try:
        await wait_for_state(ctx, row.id, State.no_drive)
    finally:
        await stop(ctx, task)
    assert f"teste/1-captados/{row.id}.mp4" in ctx.drive.files


async def test_paused_capture_claims_nothing(ctx, monkeypatch):
    monkeypatch.setattr(stage, "IDLE_SLEEP", 0.01)
    ctx.active = lambda tab: [] if tab == "captacao" else ["teste"]
    waiting = await insert_video(ctx.db, "teste", "https://x.com/a", "a")
    ready = await local_video(ctx, estado=State.baixado)
    tasks = [asyncio.create_task(stage.download_worker(ctx)), asyncio.create_task(stage.drive_upload_worker(ctx))]
    await asyncio.sleep(0.1)
    await stop(ctx, *tasks)
    assert (await get_video(ctx.db, waiting)).estado == State.descoberto
    assert (await get_video(ctx.db, ready.id)).estado == State.baixado and ctx.drive.calls == []
