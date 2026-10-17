"""FFmpeg: leitura da mídia (ffprobe), execução do render com progresso, miniatura e quadro da fonte."""
import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

W, H, FPS = 1080, 1920, 30


class RenderError(Exception):
    """Falha do FFmpeg ou do ffprobe; a mensagem já vem em português para o log e o painel."""


@dataclass
class MediaInfo:
    width: int
    height: int
    duration: float
    has_audio: bool


@dataclass
class RenderJob:
    cmd: list[str]
    duration: float
    out: Path


def _tail(err: bytes, n: int) -> str:
    return err.decode(errors="replace").strip()[-n:]


async def probe(path: Path) -> MediaInfo:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path),
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RenderError(f"o ffprobe não conseguiu ler {Path(path).name}: {_tail(err, 300)}")
    data = json.loads(out)
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    w, h = int(v.get("width", 0)), int(v.get("height", 0))
    rot = abs(int((v.get("tags") or {}).get("rotate", 0) or 0))
    for sd in v.get("side_data_list", []) or []:
        rot = abs(int(sd.get("rotation", rot) or 0))
    if rot in (90, 270):
        w, h = h, w
    return MediaInfo(w, h, float(data.get("format", {}).get("duration", 0) or 0),
                     any(s.get("codec_type") == "audio" for s in streams))


async def run_ffmpeg(job: RenderJob, on_progress: Callable[[float], Awaitable[None]]) -> Path:
    """Roda o comando do estilo e informa o progresso (0–100) lendo o `-progress pipe:1` do FFmpeg."""
    job.out.parent.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(*job.cmd, stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stderr_task = asyncio.create_task(proc.stderr.read())
        async for raw in proc.stdout:
            key, _, val = raw.decode(errors="replace").strip().partition("=")
            if key == "out_time_us" and val.isdigit():
                await on_progress(min(99.0, int(val) / 1e6 / max(job.duration, 0.001) * 100))
        err = await stderr_task
        code = await proc.wait()
    except BaseException:  # cancelado (parada da plataforma) ou falha no progresso: não deixa o FFmpeg órfão
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
    if code != 0:
        raise RenderError(f"o FFmpeg falhou: {_tail(err, 800)}")
    await on_progress(100.0)
    return job.out


async def _grab(video: Path, out: Path, times: tuple[float, ...], vf: list[str], what: str) -> Path:
    """Grava um quadro em `out`, tentando cada instante; num vídeo mais curto, o seguinte (0 = o primeiro quadro)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    err = b""
    for at in dict.fromkeys(times):
        out.unlink(missing_ok=True)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-ss", f"{at:.2f}", "-i", str(video),
            "-frames:v", "1", *vf, str(out), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode == 0 and out.is_file() and out.stat().st_size > 0:
            return out
    raise RenderError(f"não consegui gerar {what} de {Path(video).name}: {_tail(err, 300)}".rstrip(": "))


async def make_thumb(video: Path, out: Path, at: float = 0.5) -> Path:
    """Miniatura de 270 px de largura (a capa do card no painel)."""
    return await _grab(video, out, (at, 0.0), ["-vf", "scale=270:-2"], "a miniatura")


async def extract_frame(video: Path, out: Path, at: float = 1.0) -> Path:
    """Um quadro em tamanho original (a capa do meme quando a fonte não trouxe uma)."""
    return await _grab(video, out, (at, 0.0), ["-q:v", "2"], "um quadro")
