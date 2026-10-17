"""Os 7 estilos: sorteio, disponibilidade e o render de verdade com FFmpeg em clipes gerados."""
import json
import random
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from canaldark.config import load_styles
from canaldark.edit.render import MediaInfo, extract_frame, probe, run_ffmpeg
from canaldark.edit.styles import (FALLBACK_STYLE, MAX_SECONDS, StyleInputs, available_styles, choose_style,
                                   prepare_job)
from canaldark.models import MemeTemplate, WordTiming

REPO = Path(__file__).resolve().parent.parent
STYLES = load_styles(REPO / "estilos")
WORDS = [WordTiming("Olá", 0.2, 0.8), WordTiming("pessoal", 0.8, 1.5)]
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg ausente")

BLUE, RED, GREEN = (0x30, 0x60, 0xC0), (0xC0, 0x30, 0x30), (0x30, 0xC0, 0x30)


def _ff(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *map(str, args)], check=True)


def _enc(audio: bool = True):
    return ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            *(["-c:a", "aac", "-shortest"] if audio else ["-an"])]


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    d = tmp_path_factory.mktemp("clips")
    _ff("-f", "lavfi", "-i", "color=c=0x3060C0:s=640x360:r=30:d=3", "-f", "lavfi", "-i",
        "sine=frequency=440:duration=3", "-ac", "2", *_enc(), d / "h.mp4")  # 16:9, estéreo
    _ff("-f", "lavfi", "-i", "testsrc2=size=360x640:rate=30:duration=3", "-f", "lavfi", "-i",
        "sine=frequency=330:duration=3", *_enc(), d / "v.mp4")  # 9:16, mono
    _ff("-f", "lavfi", "-i", "testsrc2=size=480x480:rate=30:duration=2", *_enc(audio=False), d / "mudo.mp4")
    _ff("-f", "lavfi", "-i", "color=c=0x30C030:s=320x240:r=30:d=2", "-f", "lavfi", "-i",
        "sine=frequency=220:duration=2", *_enc(), d / "meme.mp4")
    _ff("-f", "lavfi", "-i", "color=c=0xC03030:s=480x360", "-frames:v", "1", d / "capa.jpg")
    return d


async def _noop(pct):
    pass


async def _inputs(clips, src, work, preset, **over):
    path = clips / f"{src}.mp4"
    meme = preset.tipo == "meme"
    base = dict(src=path, info=await probe(path), text="Lula fala sobre economia no debate de hoje",
                credit="@GaiaMemes", work=work, texto_base="Siga para mais", words=WORDS,
                capa=clips / "capa.jpg" if meme else None, meme_clip=clips / "meme.mp4" if meme else None,
                meme=MemeTemplate(nome="shrek", clipe="meme.mp4") if meme else None,
                meme_info=await probe(clips / "meme.mp4") if meme else None)
    return StyleInputs(**{**base, **over})


async def _frame(video: Path, out: Path, at: float = 1.0) -> Image.Image:
    return Image.open(await extract_frame(video, out, at)).convert("RGB")


def _near(pixel, color, tol=40):
    return all(abs(a - b) <= tol for a, b in zip(pixel, color))


def test_choose_style_weights_and_no_triple_repeat():
    rng = random.Random(1)
    assert {choose_style({"a": .9, "b": .1}, ["a", "a"], {"a", "b"}, rng) for _ in range(50)} == {"b"}
    assert {choose_style({"a": .9, "b": .1}, [], {"b"}, rng) for _ in range(30)} == {"b"}  # indisponível nunca sai
    assert {choose_style({"a": 0.0, "b": 1.0}, [], {"a", "b"}, rng) for _ in range(30)} == {"b"}  # peso zero
    assert choose_style({"a": 1.0}, ["a", "a"], {"a"}, rng) == "a"  # único candidato pode repetir
    assert choose_style({"a": .5, "b": .5}, [], set(), rng) == FALLBACK_STYLE == "faixa_cima"
    picks = [choose_style({"a": .9, "b": .1}, ["a", "b"], {"a", "b"}, rng) for _ in range(300)]
    assert 230 < picks.count("a") < 295  # segue os pesos


def test_available_styles_reports_missing_clip(tmp_path):
    memes_dir = tmp_path / "memes"
    memes_dir.mkdir()
    memes = {"shrek": MemeTemplate(nome="shrek", clipe="shrek.mp4"),
             "dicaprio": MemeTemplate(nome="dicaprio", clipe="dicaprio.mp4")}
    av = available_styles(STYLES, memes, memes_dir)
    assert set(av) == set(STYLES)
    assert av["meme_shrek"] == "falta o clipe memes/shrek.mp4"
    assert av["meme_dicaprio"] == "falta o clipe memes/dicaprio.mp4"
    assert all(av[s] is None for s in ("faixa_cima", "faixa_dupla", "comentario", "borrado", "karaoke"))
    (memes_dir / "shrek.mp4").write_bytes(b"x")
    assert available_styles(STYLES, memes, memes_dir)["meme_shrek"] is None
    assert "falta" in available_styles(STYLES, {}, memes_dir)["meme_shrek"]  # sem o memes/shrek.yaml


@needs_ffmpeg
@pytest.mark.parametrize("style", ["faixa_cima", "faixa_dupla", "comentario", "borrado", "karaoke", "meme_shrek"])
@pytest.mark.parametrize("src", ["h", "v", "mudo"])
async def test_every_style_renders_vertical_with_audio(style, src, clips, tmp_path):
    inp = await _inputs(clips, src, tmp_path / "work", STYLES[style])
    out = await run_ffmpeg(prepare_job(STYLES[style], inp), _noop)
    info = await probe(out)
    expected = inp.meme_info.duration if STYLES[style].tipo == "meme" else inp.info.duration
    assert (info.width, info.height) == (1080, 1920) and info.has_audio
    assert abs(info.duration - expected) < 0.35


@needs_ffmpeg
async def test_output_format_is_h264_aac_stereo_48k_30fps(clips, tmp_path):
    inp = await _inputs(clips, "v", tmp_path / "work", STYLES["faixa_cima"])  # fonte com áudio mono
    seen = []

    async def progress(pct):
        seen.append(pct)

    out = await run_ffmpeg(prepare_job(STYLES["faixa_cima"], inp), progress)
    streams = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(out)],
                                        capture_output=True, text=True, check=True).stdout)["streams"]
    v = next(s for s in streams if s["codec_type"] == "video")
    a = next(s for s in streams if s["codec_type"] == "audio")
    assert (v["codec_name"], v["pix_fmt"], v["r_frame_rate"]) == ("h264", "yuv420p", "30/1")
    assert (a["codec_name"], a["sample_rate"], a["channels"]) == ("aac", "48000", 2)
    assert seen and seen[-1] == 100 and all(0 <= p <= 100 for p in seen)
    assert any(0 < p < 100 for p in seen)  # o painel vê o progresso durante o render, não só o fim


def _stream_durations(path: Path) -> dict[str, float]:
    streams = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
                                        capture_output=True, text=True, check=True).stdout)["streams"]
    return {s["codec_type"]: float(s["duration"]) for s in streams}


@needs_ffmpeg
@pytest.mark.parametrize("video_s,audio_s", [(2, 3), (3, 2)])
async def test_short_stream_is_padded_to_full_duration(video_s, audio_s, clips, tmp_path):
    # trilhas de durações diferentes: o vídeo congela o último quadro, o áudio ganha silêncio
    src = tmp_path / "torto.mp4"
    _ff("-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=30:duration={video_s}", "-f", "lavfi", "-i",
        f"sine=frequency=440:duration={audio_s}", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", src)
    inp = StyleInputs(src=src, info=await probe(src), text="Texto", credit="@a", work=tmp_path / "work")
    assert abs(inp.info.duration - 3) < 0.1
    out = await run_ffmpeg(prepare_job(STYLES["faixa_cima"], inp), _noop)
    durations = _stream_durations(out)
    assert abs(durations["video"] - 3) < 0.1 and abs(durations["audio"] - 3) < 0.1


@needs_ffmpeg
async def test_faixa_cima_top_is_white(clips, tmp_path):
    inp = await _inputs(clips, "h", tmp_path / "work", STYLES["faixa_cima"])
    img = await _frame(await run_ffmpeg(prepare_job(STYLES["faixa_cima"], inp), _noop), tmp_path / "f.png")
    assert _near(img.getpixel((540, 20)), (255, 255, 255), 8)
    assert _near(img.getpixel((540, 1500)), BLUE)


@needs_ffmpeg
async def test_faixa_dupla_has_bars_above_and_below_the_video(clips, tmp_path):
    inp = await _inputs(clips, "h", tmp_path / "work", STYLES["faixa_dupla"])
    img = await _frame(await run_ffmpeg(prepare_job(STYLES["faixa_dupla"], inp), _noop), tmp_path / "f.png")
    assert _near(img.getpixel((540, 20)), (255, 255, 255), 8)  # faixa de cima (480 px)
    assert _near(img.getpixel((540, 500)), BLUE) and _near(img.getpixel((540, 1420)), BLUE)  # vídeo de 960 px
    assert _near(img.getpixel((540, 1460)), (255, 255, 255), 8)  # faixa de baixo (texto_base e crédito)
    assert min(img.crop((40, 1480, 1040, 1880)).convert("L").getextrema()) < 80  # o texto_base aparece


@needs_ffmpeg
async def test_extract_frame_of_clip_shorter_than_requested_time(tmp_path):
    _ff("-f", "lavfi", "-i", "color=c=0x3060C0:s=320x240:r=30:d=0.4", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        tmp_path / "curto.mp4")
    img = await _frame(tmp_path / "curto.mp4", tmp_path / "capa.jpg", at=1.0)
    assert img.size == (320, 240) and _near(img.getpixel((160, 120)), BLUE)


@needs_ffmpeg
async def test_faixa_cima_bar_grows_with_lines(clips, tmp_path):
    texts = {"a": "Game Dev", "b": "Lula fala sobre economia no debate",
             "c": "Lula fala sobre economia, juros e emprego no debate de hoje à noite na televisão aberta"}
    for name, text in texts.items():
        prepare_job(STYLES["faixa_cima"], await _inputs(clips, "h", tmp_path / name, STYLES["faixa_cima"], text=text))
    assert [Image.open(tmp_path / name / "faixa.png").size for name in texts] == [(1080, 300), (1080, 420),
                                                                                  (1080, 540)]


@needs_ffmpeg
@pytest.mark.parametrize("imagem_em,top,bottom", [("cima", RED, GREEN), ("baixo", GREEN, RED)])
async def test_meme_puts_capa_where_template_says(imagem_em, top, bottom, clips, tmp_path):
    preset = STYLES["meme_shrek"]
    inp = await _inputs(clips, "h", tmp_path / "work", preset,
                        meme=MemeTemplate(nome="shrek", clipe="meme.mp4", imagem_em=imagem_em))
    img = await _frame(await run_ffmpeg(prepare_job(preset, inp), _noop), tmp_path / "f.png")
    assert _near(img.getpixel((540, 20)), (255, 255, 255), 8)  # faixa do texto em cima
    assert _near(img.getpixel((540, 1000)), top) and _near(img.getpixel((540, 1700)), bottom)


@needs_ffmpeg
async def test_comentario_card_sits_over_video_over_blur(clips, tmp_path):
    from canaldark.edit.cards import comment_card

    inp = await _inputs(clips, "h", tmp_path / "work", STYLES["comentario"])
    img = await _frame(await run_ffmpeg(prepare_job(STYLES["comentario"], inp), _noop), tmp_path / "f.png")
    _, (x, y, w, h) = comment_card(inp.text, inp.credit, inp.font, width=960, video_size=(640, 360))
    x, y = x + 60, y + 160  # cartão centralizado, com topo em y=160
    assert _near(img.getpixel((x + w // 2, y + h // 2)), BLUE)  # o vídeo aparece no buraco
    assert _near(img.getpixel((x + 1, y + 1)), (255, 255, 255), 12)  # o canto arredondado do cartão cobre o vídeo
    assert _near(img.getpixel((540, 170)), (255, 255, 255), 12)  # corpo do cartão
    assert _near(img.getpixel((540, 1880)), BLUE)  # fundo: o próprio vídeo borrado


def test_long_source_capped_at_180s(tmp_path):
    inp = StyleInputs(src=tmp_path / "longo.mp4", info=MediaInfo(640, 360, 400.0, True), text="Texto",
                      credit="@a", work=tmp_path / "work")
    job = prepare_job(STYLES["faixa_cima"], inp)
    assert job.duration == MAX_SECONDS == 180
    durations = [float(job.cmd[i + 1]) for i, arg in enumerate(job.cmd) if arg == "-t"]
    assert durations and set(durations) == {180.0}


def test_karaoke_writes_ass_with_words(tmp_path):
    inp = StyleInputs(src=tmp_path / "v.mp4", info=MediaInfo(360, 640, 3.0, True), text="Texto", credit="@a",
                      work=tmp_path / "work", words=WORDS)
    job = prepare_job(STYLES["karaoke"], inp)
    ass = (tmp_path / "work" / "legendas.ass").read_text(encoding="utf-8")
    assert "OLÁ" in ass and r"{\c&H0000E0FF&}" in ass
    assert "ass=" in job.cmd[job.cmd.index("-filter_complex") + 1]
    no_words = prepare_job(STYLES["karaoke"], StyleInputs(src=tmp_path / "v.mp4", info=inp.info, text="Texto",
                                                          credit="@a", work=tmp_path / "w2"))
    assert "ass=" not in no_words.cmd[no_words.cmd.index("-filter_complex") + 1]
    assert not (tmp_path / "w2" / "legendas.ass").exists()


def test_karaoke_highlight_moves_word_by_word():
    from canaldark.edit.layout import build_ass

    words = [WordTiming("um", 0.0, 0.3), WordTiming("dois", 0.3, 0.6), WordTiming("três", 0.6, 0.9),
             WordTiming("quatro!", 1.0, 1.4), WordTiming("😂", 1.4, 1.5), WordTiming("cinco", 2.5, 2.9)]
    ass = build_ass(words, margin_v=360)
    hl, reset = "{\\c&H0000E0FF&}", "{\\r}"
    events = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
    assert events == [
        "Dialogue: 0,0:00:00.00,0:00:00.30,palavra,,0,0,0,," + hl + "UM" + reset + " DOIS TRÊS",
        "Dialogue: 0,0:00:00.30,0:00:00.60,palavra,,0,0,0,,UM " + hl + "DOIS" + reset + " TRÊS",
        "Dialogue: 0,0:00:00.60,0:00:00.90,palavra,,0,0,0,,UM DOIS " + hl + "TRÊS" + reset,
        "Dialogue: 0,0:00:01.00,0:00:01.40,palavra,,0,0,0,," + hl + "QUATRO!" + reset,
        "Dialogue: 0,0:00:02.50,0:00:02.90,palavra,,0,0,0,," + hl + "CINCO" + reset,
    ]
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    style = next(line for line in ass.splitlines() if line.startswith("Style: palavra,"))
    assert style.endswith(",360,1")  # margin_v
    assert "{\\c&H00FF0000&}UM" in build_ass(words[:1], margin_v=360, destaque="&H00FF0000&")


@needs_ffmpeg
async def test_karaoke_caption_survives_odd_paths(clips, tmp_path):
    work = tmp_path / "pasta com espaço, vírgula: e 'aspas' [x]"
    inp = await _inputs(clips, "h", work, STYLES["karaoke"], words=[WordTiming("Olá", 0.0, 2.0)])
    img = await _frame(await run_ffmpeg(prepare_job(STYLES["karaoke"], inp), _noop), tmp_path / "f.png")
    yellow = [p for p in img.crop((0, 1300, 1080, 1700)).get_flattened_data()
              if p[0] > 200 and p[1] > 170 and p[2] < 90]
    assert len(yellow) > 200  # a palavra atual aparece em amarelo


def test_meme_without_clip_is_refused(tmp_path):
    from canaldark.edit.render import RenderError

    inp = StyleInputs(src=tmp_path / "v.mp4", info=MediaInfo(360, 640, 3.0, True), text="Texto", credit="@a",
                      work=tmp_path / "work")
    with pytest.raises(RenderError):
        prepare_job(STYLES["meme_shrek"], inp)
