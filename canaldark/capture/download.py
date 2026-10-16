"""yt-dlp download in a worker thread, with progress bridged back to the event loop."""
import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import yt_dlp

RETRY_DELAYS = (5, 20, 60)
FORMAT = "bv*[height<=1080]+ba/b[height<=1080]"


class DownloadFailed(Exception):
    pass


def _options(out_dir: Path, hook, cookies_browser: str | None) -> dict:
    opts = {
        "format": FORMAT,
        "merge_output_format": "mp4",
        "outtmpl": str(out_dir / "%(id)s.%(ext)s"),
        "writeinfojson": True,
        "download_archive": str(out_dir.parent / "archive.txt"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
    }
    if cookies_browser:
        opts["cookiesfrombrowser"] = (cookies_browser,)
    return opts


def _fetch(url: str, opts: dict) -> Path:
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    if not info:
        raise yt_dlp.utils.DownloadError("yt-dlp não retornou informações (já baixado antes?)")
    downloads = info.get("requested_downloads") or []
    if downloads and downloads[0].get("filepath"):
        return Path(downloads[0]["filepath"])
    return Path(yt_dlp.YoutubeDL(opts).prepare_filename(info)).with_suffix(".mp4")


async def download(url: str, out_dir: Path, on_progress: Callable[[float], Awaitable[None]],
                   cookies_browser: str | None = None) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    loop = asyncio.get_running_loop()

    def hook(d: dict) -> None:
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                pct = min(99.0, 100.0 * d.get("downloaded_bytes", 0) / total)
                asyncio.run_coroutine_threadsafe(on_progress(pct), loop)

    last: Exception | None = None
    for attempt, delay in enumerate(RETRY_DELAYS):
        try:
            path = await asyncio.to_thread(_fetch, url, _options(out_dir, hook, cookies_browser))
            await on_progress(100.0)
            return path
        except yt_dlp.utils.DownloadError as e:
            last = e
            if attempt < len(RETRY_DELAYS) - 1:
                await asyncio.sleep(delay)
    raise DownloadFailed(f"download falhou após {len(RETRY_DELAYS)} tentativas: {last}")
