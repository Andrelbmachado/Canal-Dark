"""FFmpeg render of the 9:16 template: white bars, headline, captions, narration and sound mix."""
import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from canaldark.models import Cut

W, H = 1080, 1920
FPS = 30
ORIGINAL_UNDER_NARRATION_DB = -14


class RenderError(Exception):
    pass


@dataclass
class MediaInfo:
    width: int
    height: int
    duration: float
    has_audio: bool


@dataclass
class ImageLayer:
    path: Path
    start: float
    end: float


@dataclass
class RenderSpec:
    src: Path | None
    cut: Cut | None
    headline_lines: list[str]
    font: Path
    font_size: int
    bar: int
    bottom_text: str
    ass_path: Path
    narration: Path | None
    sound: Path | None
    sound_at: float | None
    out: Path
    image_layers: list[ImageLayer] = field(default_factory=list)
    duration: float = 15.0
    src_has_audio: bool = True


async def probe(path: Path) -> MediaInfo:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RenderError(f"ffprobe não conseguiu ler {path.name}: {err.decode(errors='replace')[-300:]}")
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


def _q(value) -> str:
    """Quote a filter option value (paths with spaces, colons or quotes)."""
    return "'" + str(value).replace("\\", "/").replace("'", r"'\''") + "'"


def headline_file(spec: RenderSpec) -> Path:
    return spec.out.with_suffix(".manchete.txt")


def bottom_file(spec: RenderSpec) -> Path:
    return spec.out.with_suffix(".rodape.txt")


def build_ffmpeg_cmd(spec: RenderSpec) -> list[str]:
    d = f"{spec.duration:.3f}"
    cmd = ["ffmpeg", "-hide_banner", "-y", "-nostats", "-progress", "pipe:1"]
    inputs = 0
    if spec.src is not None:
        if spec.cut and spec.cut.inicio:
            cmd += ["-ss", f"{spec.cut.inicio:.3f}"]
        if spec.cut and spec.cut.fim:
            cmd += ["-t", f"{max(0.5, spec.cut.fim - (spec.cut.inicio or 0)):.3f}"]
        cmd += ["-i", str(spec.src)]
        video_chain = (f"[0:v]scale={W}:{W}:force_original_aspect_ratio=decrease,"
                       f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:white,setsar=1,fps={FPS},"
                       f"tpad=stop_mode=clone:stop_duration={d}")
    else:
        cmd += ["-f", "lavfi", "-i", f"color=c=white:s={W}x{H}:r={FPS}:d={d}"]
        video_chain = "[0:v]setsar=1"
    inputs += 1

    audio_parts: list[str] = []
    filters: list[str] = []
    if spec.src is not None and spec.src_has_audio:
        vol = f"volume={ORIGINAL_UNDER_NARRATION_DB}dB," if spec.narration else ""
        filters.append(f"[0:a]{vol}aresample=48000[a_src]")
        audio_parts.append("[a_src]")
    if spec.narration is not None:
        cmd += ["-i", str(spec.narration)]
        filters.append(f"[{inputs}:a]aresample=48000[a_narr]")
        audio_parts.append("[a_narr]")
        inputs += 1
    if spec.sound is not None:
        cmd += ["-i", str(spec.sound)]
        delay = max(0, round((spec.sound_at or 0) * 1000))
        filters.append(f"[{inputs}:a]aresample=48000,adelay={delay}:all=1[a_snd]")
        audio_parts.append("[a_snd]")
        inputs += 1
    cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    audio_parts.append(f"[{inputs}:a]")
    inputs += 1

    layer_inputs = []
    for layer in spec.image_layers:
        cmd += ["-loop", "1", "-t", d, "-i", str(layer.path)]
        layer_inputs.append((inputs, layer))
        inputs += 1

    chain = video_chain + "[base]"
    filters.insert(0, chain)
    last = "base"
    for n, (idx, layer) in enumerate(layer_inputs):
        filters.append(f"[{idx}:v]scale=-2:{min(900, H - 2 * spec.bar)}[img{n}]")
        filters.append(f"[{last}][img{n}]overlay=(W-w)/2:(H-h)/2:"
                       f"enable='between(t,{layer.start:.3f},{layer.end:.3f})'[ov{n}]")
        last = f"ov{n}"

    font = _q(spec.font)
    text = (f"[{last}]drawtext=fontfile={font}:textfile={_q(headline_file(spec))}:fontsize={spec.font_size}"
            f":fontcolor=black:text_align=C:line_spacing={round(spec.font_size * 0.25)}"
            f":x=(w-text_w)/2:y=({spec.bar}-text_h)/2")
    if spec.bottom_text:
        text += (f",drawtext=fontfile={font}:textfile={_q(bottom_file(spec))}:fontsize=38"
                 f":fontcolor=0x555555:x=(w-text_w)/2:y=h-200")
    text += f",ass={_q(spec.ass_path)}:fontsdir={_q(spec.font.parent)},format=yuv420p[v]"
    filters.append(text)
    filters.append(f"{''.join(audio_parts)}amix=inputs={len(audio_parts)}:duration=longest:normalize=0,"
                   f"alimiter=limit=0.95[a]")

    cmd += ["-filter_complex", ";".join(filters), "-map", "[v]", "-map", "[a]", "-t", d,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(FPS),
            "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart", str(spec.out)]
    return cmd


async def render(spec: RenderSpec, on_progress: Callable[[float], Awaitable[None]]) -> Path:
    spec.out.parent.mkdir(parents=True, exist_ok=True)
    headline_file(spec).write_text("\n".join(spec.headline_lines), encoding="utf-8")
    bottom_file(spec).write_text(spec.bottom_text, encoding="utf-8")
    proc = await asyncio.create_subprocess_exec(*build_ffmpeg_cmd(spec), stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    stderr_task = asyncio.create_task(proc.stderr.read())
    async for raw in proc.stdout:
        key, _, val = raw.decode(errors="replace").strip().partition("=")
        if key == "out_time_us" and val.isdigit():
            await on_progress(min(99.0, int(val) / 1e6 / spec.duration * 100))
    err = await stderr_task
    if await proc.wait() != 0:
        raise RenderError(f"FFmpeg falhou: {err.decode(errors='replace')[-800:]}")
    await on_progress(100.0)
    return spec.out
