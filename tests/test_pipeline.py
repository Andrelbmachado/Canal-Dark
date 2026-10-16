import asyncio
import shutil
import subprocess
from datetime import datetime, timedelta

import pytest

from canaldark import pipeline as pl
from canaldark.capture import stage as cap
from canaldark.db import get_video, insert_video
from canaldark.edit import stage as ed
from canaldark.edit.transcribe import Transcript
from canaldark.edit.tts import estimate_timings
from canaldark.events import EventBus
from canaldark.models import ChannelConfig, Package, SourceItem, State, TriageResult
from canaldark.publish import stage as pub
from canaldark.rules import SP

CFG = ChannelConfig(id="gta6", nome="GTA", fontes=[{"tipo": "google_news", "busca": "GTA 6"}],
                    formatos={"noticia": 1.0}, estilo_manchete="a", persona_roteiro="b", voz={"id": "pm_alex"},
                    horarios=["09:00"], contas={"youtube": "gta6", "instagram": "gta6"})


async def test_recover_resets_in_progress_states(db):
    ids = []
    for n, st in enumerate([State.baixando, State.editando, State.publicando]):
        vid = await insert_video(db, "gta6", f"https://x/{n}", "t", "s", state=st, etapa_progresso=55)
        ids.append(vid)
    assert await pl.recover(db) == 3
    got = [await get_video(db, i) for i in ids]
    assert [g.estado for g in got] == [State.descoberto, State.baixado, State.editado]
    assert all(g.etapa_progresso == 0 for g in got)


async def test_pause_matching_and_expiry(db, tmp_path):
    p = pl.Pipeline(db, EventBus(db), {"gta6": CFG}, [], root=tmp_path, dry_run=True)
    p.pause("gta6", "edicao")
    assert p.is_paused("gta6", "edicao") and not p.is_paused("gta6", "captacao")
    assert p.active("edicao") == [] and p.active("captacao") == ["gta6"]
    p.resume("gta6", "edicao")
    p.pause(None, None, until=datetime.now(SP) - timedelta(seconds=1))
    assert not p.is_paused("gta6", "publicacao")


async def test_paused_stage_does_not_claim(db, tmp_path, monkeypatch):
    monkeypatch.setattr(ed, "IDLE_SLEEP", 0.01)
    vid = await insert_video(db, "gta6", "https://x/1", "t", "s", state=State.baixado)
    p = pl.Pipeline(db, EventBus(db), {"gta6": CFG}, [], root=tmp_path, dry_run=True)
    p.pause("gta6", "edicao")
    await p.start(capture=False)
    await asyncio.sleep(0.1)
    await p.stop()
    assert (await get_video(db, vid)).estado == State.baixado


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg ausente")
async def test_end_to_end_dry_run(db, tmp_path, monkeypatch):
    clip = tmp_path / "clip10s.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=1280x720:duration=10",
                    "-f", "lavfi", "-i", "sine=duration=10", "-pix_fmt", "yuv420p", "-shortest", str(clip)],
                   check=True)

    async def fake_read(spec, client):
        return [SourceItem(url="https://news/1", title="GTA 6 trailer 3", source_name="IGN",
                           video_url="https://youtu.be/abc")]

    async def fake_triage(cfg, items):
        return [TriageResult(url="https://news/1", nota=9, grupo="trailer")]

    async def fake_download(url, out_dir, on_progress, cookies_browser=None):
        out_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(clip, out_dir / "abc.mp4")
        await on_progress(100)
        return out_dir / "abc.mp4"

    async def fake_pkg(*a, **k):
        return Package(formato="noticia", manchete=["Trailer 3 de GTA 6", "saiu agora"], roteiro="Saiu o trailer",
                       fonte="IGN", titulo_post="Trailer 3!", descricao_post="d", hashtags=["gta6"])

    async def fake_tts(text, voice, out, engine="kokoro", offset=0.0):
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=duration=2", str(out)],
                       check=True)
        return estimate_timings(text, 2.0, offset)

    async def fake_transcribe(path):
        return Transcript("", [])

    monkeypatch.setattr(cap, "read_source", fake_read)
    monkeypatch.setattr(cap, "triage", fake_triage)
    monkeypatch.setattr(cap, "download", fake_download)
    monkeypatch.setattr(ed, "make_package", fake_pkg)
    monkeypatch.setattr(ed, "synthesize", fake_tts)
    monkeypatch.setattr(ed, "transcribe", fake_transcribe)
    for mod in (cap, ed, pub):
        monkeypatch.setattr(mod, "IDLE_SLEEP", 0.05)

    bus = EventBus(db)
    sub = bus.subscribe()
    p = pl.Pipeline(db, bus, {"gta6": CFG}, [], root=tmp_path, dry_run=True)
    await p.start(capture=False)
    assert await p.capture_now("gta6") == 1
    for _ in range(400):
        row = await get_video(db, 1)
        if row.estado in (State.publicado, State.erro, State.revisar):
            break
        await asyncio.sleep(0.05)
    await p.stop()
    assert row.estado == State.publicado, row.erro
    assert len(list((tmp_path / "dados" / "ensaio").glob("*.mp4"))) == 2
    states = []
    while not sub.empty():
        ev = await sub.__anext__()
        if ev["type"] == "video.estado":
            states.append(ev["data"]["estado"])
    for st in ["descoberto", "baixando", "baixado", "editando", "editado", "publicando", "publicado"]:
        assert st in states
