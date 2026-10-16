import pytest

from canaldark.db import get_video, insert_video
from canaldark.edit import stage
from canaldark.edit.transcribe import Transcript
from canaldark.events import EventBus
from canaldark.models import ChannelConfig, Package, State

CFG = ChannelConfig(id="gta6", nome="GTA", fontes=[{"tipo": "google_news", "busca": "GTA 6"}],
                    formatos={"noticia": 0.6, "meme": 0.25, "dialogo": 0.15}, estilo_manchete="a",
                    persona_roteiro="b", voz={"id": "pm_alex"}, horarios=["09:00"], contas={"youtube": "gta6"})


def _pkg(**kw):
    base = dict(formato="noticia", manchete=["GTA 6 adiado?"], roteiro="texto", fonte="IGN",
                titulo_post="t", descricao_post="d")
    base.update(kw)
    return Package(**base)


@pytest.fixture
def no_render(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("não deveria renderizar")
    monkeypatch.setattr(stage, "render", boom)
    monkeypatch.setattr(stage, "synthesize", boom)

    async def fake_transcribe(path):
        return Transcript("", [])
    monkeypatch.setattr(stage, "transcribe", fake_transcribe)


async def _row(db, tmp_path):
    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"x")
    vid = await insert_video(db, "gta6", "https://youtu.be/a", "t", "s", state=State.editando,
                             caminho_bruto=str(clip))
    return await get_video(db, vid)


async def test_violation_goes_to_revisar(db, tmp_path, monkeypatch, no_render):
    async def fake_pkg(*a, **k):
        return _pkg(risco="revisar", motivo_risco="fato não confirmado")
    monkeypatch.setattr(stage, "make_package", fake_pkg)
    row = await _row(db, tmp_path)
    assert await stage.edit_video(db, EventBus(db), {"gta6": CFG}, [], row, work_dir=tmp_path) == State.revisar
    after = await get_video(db, row.id)
    assert after.estado == State.revisar and "fato não confirmado" in after.erro and after.pacote_json


async def test_brain_failure_goes_to_erro(db, tmp_path, monkeypatch, no_render):
    async def fail(*a, **k):
        raise stage.BrainError("claude: não encontrado; ollama: recusou conexão")
    monkeypatch.setattr(stage, "make_package", fail)
    row = await _row(db, tmp_path)
    assert await stage.edit_video(db, EventBus(db), {"gta6": CFG}, [], row, work_dir=tmp_path) == State.erro
    assert "ollama" in (await get_video(db, row.id)).erro


async def test_noticia_renders_to_editado(db, tmp_path, monkeypatch):
    import shutil
    import subprocess
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg ausente")
    from canaldark.edit.tts import estimate_timings
    clip = tmp_path / "c.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=640x360:duration=4",
                    "-pix_fmt", "yuv420p", "-an", str(clip)], check=True)

    async def fake_pkg(*a, **k):
        return _pkg(manchete=["POV: GTA 6 🔥 chegou"], roteiro="Olá pessoal, novidade de GTA")

    async def fake_tts(text, voice, out, engine="kokoro", offset=0.0):
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=duration=2",
                        str(out)], check=True)
        return estimate_timings(text, 2.0, offset)

    async def fake_transcribe(path):
        return Transcript("", [])
    monkeypatch.setattr(stage, "make_package", fake_pkg)
    monkeypatch.setattr(stage, "synthesize", fake_tts)
    monkeypatch.setattr(stage, "transcribe", fake_transcribe)
    vid = await insert_video(db, "gta6", "https://youtu.be/b", "t", "s", state=State.editando,
                             caminho_bruto=str(clip))
    row = await get_video(db, vid)
    assert await stage.edit_video(db, EventBus(db), {"gta6": CFG}, [], row, work_dir=tmp_path) == State.editado
    after = await get_video(db, vid)
    assert after.caminho_final.endswith("final.mp4") and after.formato == "noticia"
    assert (tmp_path / str(vid) / "thumb.jpg").exists()
