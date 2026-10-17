"""Etapa de edição: texto e estilo do vídeo, render, capa final e a transição para editado/revisar/erro."""
import asyncio
import itertools
import random
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from canaldark.db import claim, get_video, insert_video, update_fields
from canaldark.edit import stage
from canaldark.edit.render import RenderError, probe
from canaldark.edit.stage import edit_video, edit_worker
from canaldark.edit.transcribe import TranscribeUnavailable, Transcript
from canaldark.models import State, WordTiming

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg ausente")
_urls = itertools.count()


def _ff(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *map(str, args)], check=True)


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    d = tmp_path_factory.mktemp("clip")
    enc = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest"]
    _ff("-f", "lavfi", "-i", "testsrc2=size=360x640:rate=30:duration=2", "-f", "lavfi", "-i",
        "sine=frequency=440:duration=2", *enc, d / "fonte.mp4")
    _ff("-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=3", "-f", "lavfi", "-i",
        "sine=frequency=220:duration=3", *enc, d / "meme.mp4")
    return d


async def _editing(ctx, clip, title="Bolacha #engraçado #humor 😂", channel="teste", **fields):
    """Grava um vídeo em `pronto` com a fonte no pacote e o pega como o trabalhador faria (→ editando)."""
    vid = await insert_video(ctx.db, channel, f"https://youtu.be/v{next(_urls)}", title, estado=State.pronto,
                             autor="@GaiaMemes", **fields)
    src = ctx.data / "pacote" / channel / f"{vid}.mp4"
    src.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(clip / "fonte.mp4", src)
    await update_fields(ctx.db, vid, caminho_local=str(src))
    return await claim(ctx.db, State.pronto, State.editando, video_id=vid)


def _drain(sub) -> list[dict]:
    events = []
    while not sub.empty():
        events.append(sub.queue.get_nowait())
    return events


@pytest.fixture(autouse=True)
def no_whisper(monkeypatch):
    """Nunca roda o faster-whisper de verdade (ele baixaria o modelo da internet); o karaokê sai sem legenda."""
    async def unavailable(path):
        raise TranscribeUnavailable("faster-whisper não instalado: rode `pip install -e .[ml]`")

    monkeypatch.setattr(stage, "transcribe", unavailable)


@pytest.fixture
def fast_render(monkeypatch):
    """Troca o FFmpeg do estilo por uma cópia da fonte: para testes da lógica da etapa, não do render."""
    async def copy(job, on_progress):
        src = Path(job.cmd[job.cmd.index("-i") + 1])
        job.out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, job.out)
        await on_progress(100.0)
        return job.out

    monkeypatch.setattr(stage, "run_ffmpeg", copy)


async def test_edit_video_renders_and_marks_editado(ctx, clip):
    row = await _editing(ctx, clip)
    sub = ctx.bus.subscribe()
    assert await edit_video(ctx, row, rng=random.Random(3)) == State.editado
    got = await get_video(ctx.db, row.id)
    assert got.estado == State.editado
    assert got.estilo in ctx.cfgs["teste"].estilos["serio"]
    assert got.manchete == "Bolacha"
    final = ctx.data / "editados" / str(row.id) / f"{row.id}.mp4"  # o nome do arquivo vira o nome no Drive
    assert got.caminho_final == str(final) and final.is_file()
    info = await probe(final)
    assert (info.width, info.height) == (1080, 1920) and info.has_audio
    assert (ctx.data / "capas" / f"{row.id}-final.jpg").is_file()
    events = _drain(sub)
    assert {"type": "video.estado", "data": {"id": row.id, "estado": "editado"}} in [
        {k: e[k] for k in ("type", "data")} for e in events]
    assert any(e["type"] == "video.progresso" and e["data"]["pct"] == 100 for e in events)
    assert any(e["type"] == "log" and e["data"]["msg"].startswith(f"editado ({got.estilo}, 2 s)") for e in events)


async def test_user_style_and_text_are_respected(ctx, clip):
    row = await _editing(ctx, clip, estilo="borrado", manchete="Meu texto do André")
    assert await edit_video(ctx, row) == State.editado
    got = await get_video(ctx.db, row.id)
    assert (got.estado, got.estilo, got.manchete) == (State.editado, "borrado", "Meu texto do André")


async def test_missing_meme_clip_falls_back(ctx, clip):
    row = await _editing(ctx, clip, estilo="meme_shrek")
    sub = ctx.bus.subscribe()
    assert await edit_video(ctx, row) == State.editado
    assert (await get_video(ctx.db, row.id)).estilo == "faixa_cima"
    logs = [e["data"] for e in _drain(sub) if e["type"] == "log"]
    assert any("falta o clipe memes/shrek.mp4" in log["msg"] and log["level"] == "aviso" for log in logs)


async def test_meme_style_uses_clip_and_frame_from_source(ctx, clip):
    shutil.copy(clip / "meme.mp4", ctx.root / "memes" / "shrek.mp4")
    row = await _editing(ctx, clip, estilo="meme_shrek")  # sem caminho_capa: a capa sai de um quadro da fonte
    assert await edit_video(ctx, row) == State.editado
    got = await get_video(ctx.db, row.id)
    assert got.estilo == "meme_shrek"
    meme_len = (await probe(clip / "meme.mp4")).duration
    assert abs((await probe(Path(got.caminho_final))).duration - meme_len) < 0.35
    assert (ctx.data / "editados" / str(row.id) / "capa.jpg").is_file()


async def test_auto_style_never_repeats_three_times(ctx, clip, fast_render):
    for n, ts in enumerate(("2026-10-17T10:00:00+00:00", "2026-10-17T11:00:00+00:00")):
        await insert_video(ctx.db, "teste", f"https://youtu.be/feito{n}", "T", estado=State.editado,
                           estilo="faixa_cima", atualizado_em=ts)
    picks = set()
    for seed in range(6):
        row = await _editing(ctx, clip)
        assert await edit_video(ctx, row, rng=random.Random(seed)) == State.editado
        picks.add((await get_video(ctx.db, row.id)).estilo)
        await update_fields(ctx.db, row.id, estilo="faixa_cima", atualizado_em="2026-10-17T09:00:00+00:00")
    assert picks and picks <= {"faixa_dupla", "karaoke"}


async def test_meme_tone_uses_meme_table_and_skips_missing_clips(ctx, clip, fast_render):
    row = await _editing(ctx, clip, tom="meme")
    assert await edit_video(ctx, row, rng=random.Random(0)) == State.editado
    assert (await get_video(ctx.db, row.id)).estilo == "comentario"  # os dois memes estão sem clipe


async def test_fallback_headline_rotates_by_id(ctx, clip, fast_render):
    row = await _editing(ctx, clip, title="#shorts #fyp 😂🔥")
    assert await edit_video(ctx, row) == State.editado
    expected = ctx.cfgs["teste"].manchete_padrao[row.id % 2]
    assert (await get_video(ctx.db, row.id)).manchete == expected


async def test_title_the_font_cannot_draw_falls_back(ctx, clip, fast_render):
    row = await _editing(ctx, clip, title="予告編のトレーラー 😂")  # letras, mas a Montserrat não tem esses glifos
    assert await edit_video(ctx, row) == State.editado
    expected = ctx.cfgs["teste"].manchete_padrao[row.id % 2]
    assert (await get_video(ctx.db, row.id)).manchete == expected


async def test_revisar_when_channel_asks(ctx, clip, cfg_factory, fast_render):
    ctx.cfgs["teste"] = cfg_factory(revisar_antes_de_publicar=True)
    row = await _editing(ctx, clip)
    sub = ctx.bus.subscribe()
    assert await edit_video(ctx, row) == State.revisar
    assert (await get_video(ctx.db, row.id)).estado == State.revisar
    assert any(e["type"] == "video.estado" and e["data"]["estado"] == "revisar" for e in _drain(sub))


async def test_karaoke_without_whisper_warns_and_renders(ctx, clip, fast_render):
    row = await _editing(ctx, clip, estilo="karaoke")  # no_whisper: o transcribe diz que falta o faster-whisper
    sub = ctx.bus.subscribe()
    assert await edit_video(ctx, row) == State.editado
    logs = [e["data"] for e in _drain(sub) if e["type"] == "log"]
    assert any("sem legenda" in log["msg"] and log["level"] == "aviso" for log in logs)
    assert not (ctx.data / "editados" / str(row.id) / "legendas.ass").exists()


async def test_karaoke_transcription_failure_only_drops_captions(ctx, clip, monkeypatch, fast_render):
    async def offline(path):
        raise RuntimeError("não consegui baixar o modelo")

    monkeypatch.setattr(stage, "transcribe", offline)
    row = await _editing(ctx, clip, estilo="karaoke")
    sub = ctx.bus.subscribe()
    assert await edit_video(ctx, row) == State.editado
    logs = [e["data"] for e in _drain(sub) if e["type"] == "log"]
    assert any("baixar o modelo" in log["msg"] and "sem legenda" in log["msg"] for log in logs)


async def test_karaoke_with_words_writes_captions(ctx, clip, monkeypatch, fast_render):
    async def words(path):
        return Transcript("olá pessoal", [WordTiming("olá", 0.1, 0.5), WordTiming("pessoal", 0.5, 1.0)])

    monkeypatch.setattr(stage, "transcribe", words)
    row = await _editing(ctx, clip, estilo="karaoke")
    assert await edit_video(ctx, row) == State.editado
    assert "PESSOAL" in (ctx.data / "editados" / str(row.id) / "legendas.ass").read_text(encoding="utf-8")


async def test_render_failure_goes_to_erro_with_queue(ctx, clip, monkeypatch):
    async def boom(job, on_progress):
        raise RenderError("o FFmpeg falhou: boom")

    monkeypatch.setattr(stage, "run_ffmpeg", boom)
    row = await _editing(ctx, clip)
    sub = ctx.bus.subscribe()
    assert await edit_video(ctx, row) == State.erro
    got = await get_video(ctx.db, row.id)
    assert got.estado == State.erro and got.fila_erro == State.pronto and "boom" in got.erro
    events = _drain(sub)
    assert any(e["type"] == "video.estado" and e["data"]["estado"] == "erro" and "boom" in e["data"]["erro"]
               for e in events)
    assert any(e["type"] == "log" and e["data"]["level"] == "erro" for e in events)


async def test_unexpected_exception_never_escapes(ctx, clip, monkeypatch):
    def broken(preset, inp):
        raise ValueError("bug")

    monkeypatch.setattr(stage, "prepare_job", broken)
    row = await _editing(ctx, clip)
    assert await edit_video(ctx, row) == State.erro
    got = await get_video(ctx.db, row.id)
    assert got.fila_erro == State.pronto and "bug" in got.erro


async def test_missing_source_goes_to_erro(ctx, clip):
    row = await _editing(ctx, clip)
    Path(row.caminho_local).unlink()
    assert await edit_video(ctx, row) == State.erro
    assert "origem" in (await get_video(ctx.db, row.id)).erro


async def _run_worker(ctx, until, timeout=10.0):
    task = asyncio.create_task(edit_worker(ctx))
    try:
        for _ in range(int(timeout / 0.05)):
            if await until():
                break
            await asyncio.sleep(0.05)
    finally:
        ctx.stop.set()
        await asyncio.wait_for(task, timeout=2)  # sai logo, sem esperar o IDLE_SLEEP inteiro


async def test_worker_edits_only_active_channels(ctx, clip, monkeypatch):
    seen = []

    async def fake_edit(c, row, *, rng=None):
        seen.append((row.id, row.estado))
        await stage.transition(c.db, row.id, State.editado)
        return State.editado

    monkeypatch.setattr(stage, "edit_video", fake_edit)
    vid = await insert_video(ctx.db, "teste", "https://youtu.be/w1", "T", estado=State.pronto)
    other = await insert_video(ctx.db, "outro", "https://youtu.be/w2", "T", estado=State.pronto)
    ctx.active = lambda s: ["teste"] if s == "edicao" else []
    sub = ctx.bus.subscribe()

    async def done():
        return (await get_video(ctx.db, vid)).estado == State.editado

    await _run_worker(ctx, done)
    assert seen == [(vid, State.editando)]
    assert (await get_video(ctx.db, other)).estado == State.pronto
    assert any(e["type"] == "video.estado" and e["data"] == {"id": vid, "estado": "editando"} for e in _drain(sub))


async def test_worker_waits_when_queue_is_empty(ctx, monkeypatch):
    calls = []

    async def empty(*args, **kwargs):
        calls.append(1)
        await asyncio.sleep(0)
        return None

    monkeypatch.setattr(stage, "claim", empty)
    monkeypatch.setattr(stage, "IDLE_SLEEP", 0.2)
    task = asyncio.create_task(edit_worker(ctx))
    await asyncio.sleep(0.5)
    ctx.stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert 1 <= len(calls) <= 5  # espera IDLE_SLEEP entre as consultas, sem girar a CPU no banco


async def test_worker_survives_database_hiccup(ctx, monkeypatch):
    real_claim, calls = stage.claim, []

    async def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return await real_claim(*args, **kwargs)

    async def fake_edit(c, row, *, rng=None):
        await stage.transition(c.db, row.id, State.editado)
        return State.editado

    monkeypatch.setattr(stage, "claim", flaky)
    monkeypatch.setattr(stage, "edit_video", fake_edit)
    monkeypatch.setattr(stage, "IDLE_SLEEP", 0.05)
    vid = await insert_video(ctx.db, "teste", "https://youtu.be/h1", "T", estado=State.pronto)
    sub = ctx.bus.subscribe()

    async def done():
        return (await get_video(ctx.db, vid)).estado == State.editado

    await _run_worker(ctx, done)
    assert (await get_video(ctx.db, vid)).estado == State.editado
    assert any(e["type"] == "log" and "database is locked" in e["data"]["msg"] for e in _drain(sub))


async def test_worker_survives_crash_and_keeps_going(ctx, monkeypatch):
    async def crash(c, row, *, rng=None):
        raise RuntimeError("quebrou")

    monkeypatch.setattr(stage, "edit_video", crash)
    a = await insert_video(ctx.db, "teste", "https://youtu.be/c1", "T", estado=State.pronto)
    b = await insert_video(ctx.db, "teste", "https://youtu.be/c2", "T", estado=State.pronto)

    async def done():
        return [(await get_video(ctx.db, v)).estado for v in (a, b)] == [State.erro, State.erro]

    await _run_worker(ctx, done)
    for v in (a, b):
        row = await get_video(ctx.db, v)
        assert row.estado == State.erro and row.fila_erro == State.pronto and "quebrou" in row.erro
