import json
import shutil
import subprocess
from pathlib import Path

import pytest

from canaldark.edit.layout import FONT, build_ass
from canaldark.edit.render import RenderSpec, probe, render
from canaldark.edit.tts import WordTiming

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg ausente")


def _ff(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("media")
    _ff("-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30:duration=10", "-f", "lavfi", "-i",
        "sine=frequency=440:duration=10", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-shortest", str(d / "clip10s.mp4"))
    _ff("-f", "lavfi", "-i", "testsrc=size=1080x1920:rate=30:duration=4", "-c:v", "libx264",
        "-pix_fmt", "yuv420p", "-an", str(d / "mute_vertical.mp4"))
    _ff("-f", "lavfi", "-i", "sine=frequency=220:duration=3", str(d / "narr.wav"))
    return d


def _ffprobe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def _spec(tmp_path, src, **kw):
    ass = tmp_path / "leg.ass"
    ass.write_text(build_ass([WordTiming("Olá", 0.2, 0.8), WordTiming("pessoal", 0.8, 1.5)]))
    base = dict(src=src, cut=None, headline_lines=["Rockstar confirma:", "GTA 6 no Brasil?"], font=FONT,
                font_size=72, bar=656, bottom_text="Fonte: Tecnoblog", ass_path=ass, narration=None,
                sound=None, sound_at=None, out=tmp_path / "out.mp4", image_layers=[], duration=3.0,
                src_has_audio=True)
    base.update(kw)
    return RenderSpec(**base)


async def _noop(p):
    pass


async def test_render_output_is_1080x1920_with_audio(media, tmp_path):
    out = await render(_spec(tmp_path, media / "clip10s.mp4", narration=media / "narr.wav"), _noop)
    streams = _ffprobe(out)["streams"]
    v = next(s for s in streams if s["codec_type"] == "video")
    assert (v["width"], v["height"], v["codec_name"]) == (1080, 1920, "h264")
    assert [s["codec_name"] for s in streams if s["codec_type"] == "audio"] == ["aac"]


async def test_render_vertical_source(media, tmp_path):
    info = await probe(media / "mute_vertical.mp4")
    assert not info.has_audio
    out = await render(_spec(tmp_path, media / "mute_vertical.mp4", bar=420, src_has_audio=False), _noop)
    v = next(s for s in _ffprobe(out)["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (1080, 1920)


async def test_render_without_source_audio(media, tmp_path):
    out = await render(_spec(tmp_path, media / "mute_vertical.mp4", bar=420, src_has_audio=False,
                             narration=media / "narr.wav"), _noop)
    assert len([s for s in _ffprobe(out)["streams"] if s["codec_type"] == "audio"]) == 1


async def test_render_dialogo_without_source(media, tmp_path):
    out = await render(_spec(tmp_path, None, bar=420, src_has_audio=False, narration=media / "narr.wav"), _noop)
    assert (await probe(out)).width == 1080


async def test_progress_reaches_100(media, tmp_path):
    seen = []

    async def rec(p):
        seen.append(p)
    await render(_spec(tmp_path, media / "clip10s.mp4"), rec)
    assert seen[-1] == 100


async def test_headline_with_percent_and_backslash_is_drawn(media, tmp_path):
    out = await render(_spec(tmp_path, media / "clip10s.mp4", headline_lines=["Juros sobem 0,5%", "C:\\x 100%"],
                             bottom_text="Fonte: 50% G1"), _noop)
    r = subprocess.run(["ffmpeg", "-hide_banner", "-ss", "1", "-i", str(out), "-frames:v", "1", "-vf",
                        "crop=1080:600:0:20,signalstats,metadata=print:key=lavfi.signalstats.YMIN", "-f", "null", "-"],
                       capture_output=True, text=True)
    ymin = int(r.stderr.split("lavfi.signalstats.YMIN=")[1].split()[0])
    assert ymin < 60  # black headline pixels exist in the white top bar
