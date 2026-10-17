"""Download de um vídeo captado: yt-dlp numa thread ou, no Pexels e no Pixabay, o arquivo direto via httpx."""
from __future__ import annotations

import asyncio
import glob
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
import yt_dlp

from canaldark.capture.sources import BOT_MESSAGE, is_bot_check

RETRY_DELAYS = (5, 20, 60)
MAX_SECONDS = 600  # mais longo que isso (lives, episódios) nem baixa
# Até Full HD nas duas orientações: o "[height<=1080]" do v1 baixava um Short (1080×1920) em 480×854
FORMAT = "bv*[height<=?1920][width<=?1920]+ba/b[height<=?1920][width<=?1920]/b"
META_KEYS = ("id", "title", "duration", "uploader", "uploader_id", "channel", "license", "webpage_url", "width",
             "height", "timestamp", "view_count")
HTTP_TIMEOUT = httpx.Timeout(30.0, read=120.0)
FRAME_TIMEOUT = 60.0

OnProgress = Callable[[float], Awaitable[None]]


class DownloadFailed(Exception):
    pass


class _Skipped(Exception):
    pass


@dataclass
class DownloadResult:
    video: Path
    info: Path | None
    capa: Path | None
    meta: dict


def _options(out_dir: Path, name: str, hook, cookies_browser: str | None) -> dict:
    opts = {
        "format": FORMAT,
        "merge_output_format": "mp4",
        "outtmpl": str(out_dir / f"{name.replace('%', '%%')}.%(ext)s"),
        "writeinfojson": True,
        "writethumbnail": True,
        "postprocessors": [{"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"}],
        # "<=?" deixa passar duração desconhecida; o "| !duration" do v1 não é OU no yt-dlp e quebrava o filtro
        "match_filter": yt_dlp.utils.match_filter_func(f"duration <=? {MAX_SECONDS}"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
    }
    if cookies_browser:
        opts["cookiesfrombrowser"] = (cookies_browser,)
    return opts


def _fetch(url: str, opts: dict) -> tuple[Path, dict]:
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    if not info:
        raise yt_dlp.utils.DownloadError("yt-dlp não retornou informações")
    downloads = info.get("requested_downloads") or []
    if not downloads or not downloads[0].get("filepath"):
        raise _Skipped(f"vídeo mais longo que {MAX_SECONDS // 60} min ou indisponível; ignorado")
    return Path(downloads[0]["filepath"]), info


async def _direct(url: str, direct_url: str, out_dir: Path, name: str, on_progress: OnProgress,
                  client: httpx.AsyncClient) -> tuple[Path, dict]:
    video, part = out_dir / f"{name}.mp4", out_dir / f"{name}.mp4.part"
    async with client.stream("GET", direct_url, follow_redirects=True, timeout=HTTP_TIMEOUT) as resp:
        resp.raise_for_status()
        total, done = int(resp.headers.get("content-length") or 0), 0
        with part.open("wb") as f:
            async for chunk in resp.aiter_bytes(1 << 16):
                f.write(chunk)
                done += len(chunk)
                if total:
                    await on_progress(min(99.0, 100.0 * done / total))
    part.replace(video)
    return video, {"url": url, "direct_url": direct_url}


def _describe(e: BaseException | None) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code}"
    text = str(e).removeprefix("ERROR: ").strip() if e else ""
    text = text or type(e).__name__
    return text if len(text) <= 300 else text[:299] + "…"


async def _with_retries(attempt: Callable[[], Awaitable]):
    last: BaseException | None = None
    for i, delay in enumerate(RETRY_DELAYS):
        try:
            return await attempt()
        except _Skipped as e:
            raise DownloadFailed(str(e)) from e
        except yt_dlp.utils.DownloadError as e:
            if is_bot_check(str(e)):  # tentar de novo sem cookies só piora o bloqueio
                raise DownloadFailed(BOT_MESSAGE) from e
            last = e
        except httpx.HTTPError as e:
            last = e
        if i < len(RETRY_DELAYS) - 1:
            await asyncio.sleep(delay)
    raise DownloadFailed(f"download falhou após {len(RETRY_DELAYS)} tentativas: {_describe(last)}")


def _remove(out_dir: Path, name: str) -> None:
    for f in out_dir.glob(f"{glob.escape(name)}.*"):
        f.unlink(missing_ok=True)


async def _frame(video: Path, out: Path) -> Path | None:
    """Capa de reserva: um quadro do próprio vídeo, quando a fonte não trouxe miniatura."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-v", "error", "-i", str(video), "-vf", "thumbnail=30,scale=480:-2", "-frames:v", "1",
            str(out), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.wait_for(proc.wait(), FRAME_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return None
    except OSError:  # ffmpeg ausente
        return None
    if proc.returncode == 0 and out.is_file() and out.stat().st_size > 0:
        return out
    out.unlink(missing_ok=True)
    return None


async def download(url: str, out_dir: Path, on_progress: OnProgress, cookies_browser: str | None = None, *,
                   name: str, require_cc: bool = False, direct_url: str | None = None,
                   client: httpx.AsyncClient | None = None) -> DownloadResult:
    """Baixa para out_dir/<name>.mp4, com <name>.info.json e a capa <name>.jpg ao lado."""
    out_dir.mkdir(parents=True, exist_ok=True)
    if direct_url:
        own = client is None
        http = client or httpx.AsyncClient()
        try:
            video, meta = await _with_retries(lambda: _direct(url, direct_url, out_dir, name, on_progress, http))
        finally:
            if own:
                await http.aclose()
        info: Path | None = out_dir / f"{name}.info.json"
        info.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    else:
        loop = asyncio.get_running_loop()

        def hook(d: dict) -> None:
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                if total:
                    pct = min(99.0, 100.0 * d.get("downloaded_bytes", 0) / total)
                    asyncio.run_coroutine_threadsafe(on_progress(pct), loop)

        opts = _options(out_dir, name, hook, cookies_browser)
        video, full = await _with_retries(lambda: asyncio.to_thread(_fetch, url, opts))
        if require_cc and "Creative Commons" not in str(full.get("license") or ""):
            _remove(out_dir, name)
            raise DownloadFailed("o vídeo não é Creative Commons; ignorado")
        meta = {k: full[k] for k in META_KEYS if full.get(k) is not None}
        info = out_dir / f"{name}.info.json"
        if not info.is_file():
            info = None
    capa = out_dir / f"{name}.jpg"
    if not capa.is_file():
        capa = await _frame(video, capa)
    await on_progress(100.0)
    return DownloadResult(video=video, info=info, capa=capa, meta=meta)
