"""Testes da infraestrutura comum: protocolo do Drive, FakeDrive, Ctx e as fixtures do conftest."""
import pytest

from canaldark.context import Ctx
from canaldark.drive import CAPTADOS, EM_EDICAO, PUBLICADOS, USADOS, Drive, DriveError, DriveUsage, pasta
from canaldark.events import EventBus
from canaldark.models import PLATFORMS, PlatformConfig
from tests.fakes.drive import FakeDrive


def test_drive_folder_names():
    assert (CAPTADOS, EM_EDICAO, PUBLICADOS, USADOS) == ("1-captados", "2-em-edicao", "3-publicados",
                                                         "4-originais-usados")
    assert pasta("teste", CAPTADOS) == "teste/1-captados"
    assert pasta("teste", PUBLICADOS, "2026-10-17") == "teste/3-publicados/2026-10-17"


def test_drive_usage_fraction():
    assert DriveUsage(25, 100).fracao == 0.25
    assert DriveUsage(10, None).fracao is None
    assert DriveUsage(10, 0).fracao is None


async def test_fake_drive_roundtrip(tmp_path):
    d = FakeDrive()
    assert isinstance(d, Drive)
    f = tmp_path / "7.mp4"
    f.write_bytes(b"video")
    r = await d.enviar(f, pasta("teste", CAPTADOS))
    assert r == "teste/1-captados/7.mp4" and not f.exists() and d.files == {r: b"video"}
    r2 = await d.mover(r, pasta("teste", EM_EDICAO))
    assert r2 == "teste/2-em-edicao/7.mp4" and set(d.files) == {r2}
    local = await d.baixar(r2, tmp_path / "pacote" / "teste")
    assert local == tmp_path / "pacote" / "teste" / "7.mp4" and local.read_bytes() == b"video"
    assert r2 in d.files  # baixar copia, não move
    await d.apagar(r2)
    await d.apagar(r2)  # arquivo ausente não é erro
    assert d.files == {}

    d.files["teste/1-captados/8.mp4"] = b"x"
    d.fail["mover"] = DriveError("caiu")
    with pytest.raises(DriveError, match="caiu"):
        await d.mover("teste/1-captados/8.mp4", pasta("teste", EM_EDICAO))
    assert set(d.files) == {"teste/1-captados/8.mp4"} and d.fail == {}
    assert await d.mover("teste/1-captados/8.mp4", pasta("teste", EM_EDICAO)) == "teste/2-em-edicao/8.mp4"
    assert [c[0] for c in d.calls] == ["enviar", "mover", "baixar", "apagar", "apagar", "mover", "mover"]


async def test_fake_drive_enviar_overwrites_and_keeps_local_on_failure(tmp_path):
    d = FakeDrive()
    dest = pasta("teste", PUBLICADOS, "2026-10-17")
    d.files[f"{dest}/5.mp4"] = b"velho"
    f = tmp_path / "5.mp4"
    f.write_bytes(b"novo")
    d.fail["enviar"] = DriveError("rede")
    with pytest.raises(DriveError):
        await d.enviar(f, dest)
    assert f.exists() and d.files[f"{dest}/5.mp4"] == b"velho"
    assert await d.enviar(f, dest + "/") == f"{dest}/5.mp4"
    assert d.files == {f"{dest}/5.mp4": b"novo"} and not f.exists()
    with pytest.raises(DriveError):
        await d.enviar(f, dest)  # o local já não existe


async def test_fake_drive_missing_remote_and_usage(tmp_path):
    d = FakeDrive()
    with pytest.raises(DriveError):
        await d.baixar("teste/1-captados/x.mp4", tmp_path)
    with pytest.raises(DriveError):
        await d.mover("teste/1-captados/x.mp4", pasta("teste", EM_EDICAO))
    assert await d.uso() == DriveUsage(0, 15 * 1024**3)
    d.usage = DriveUsage(10, None)
    assert (await d.uso()).fracao is None
    d.usage = None
    assert await d.uso() is None
    d.fail["uso"] = RuntimeError("sem rede")
    with pytest.raises(RuntimeError):
        await d.uso()
    assert [c[0] for c in d.calls] == ["baixar", "mover", "uso", "uso", "uso", "uso"]


def test_fake_drives_do_not_share_state():
    a, b = FakeDrive(), FakeDrive()
    a.files["x"] = b"1"
    a.fail["uso"] = DriveError("x")
    a.usage.usado = 99
    assert b.files == {} and b.fail == {} and b.usage == DriveUsage(0, 15 * 1024**3) and b.calls == []


def test_cfg_factory_defaults_and_overrides(cfg_factory):
    cfg = cfg_factory()
    assert (cfg.id, [f.tipo for f in cfg.fontes], cfg.horarios) == ("teste", ["youtube_busca"], ["09:00"])
    assert set(cfg.estilos) == {"serio", "meme"}
    assert cfg.contas == {p: "teste" for p in PLATFORMS}
    over = cfg_factory(max_por_dia=1, regras="politica", hashtags=["#a"])
    assert (over.max_por_dia, over.regras, over.hashtags, over.id) == (1, "politica", ["#a"], "teste")


async def test_ctx_fixture(ctx, tmp_path, db):
    assert ctx.root == tmp_path and ctx.data == tmp_path / "dados" and ctx.db is db
    assert ctx.dry_run and isinstance(ctx.drive, FakeDrive) and ctx.plat == PlatformConfig()
    assert list(ctx.cfgs) == ["teste"] and ctx.channels_for("edicao") == ["teste"]
    assert len(ctx.styles) == 7 and set(ctx.memes) == {"shrek", "dicaprio"}
    assert sorted(p.name for p in (tmp_path / "memes").iterdir()) == ["dicaprio.yaml", "shrek.yaml"]
    assert len(list((tmp_path / "estilos").glob("*.yaml"))) == 7
    assert ctx.get_uploader is None and ctx.keys == {} and not ctx.stop.is_set()
    ctx.active = lambda stage: [] if stage == "edicao" else ["teste"]
    assert ctx.channels_for("edicao") == [] and ctx.channels_for("captacao") == ["teste"]
    sub = ctx.bus.subscribe()
    await ctx.bus.log("sistema", "oi")
    assert (await sub.__anext__())["data"]["msg"] == "oi"


async def test_ctx_defaults_are_per_instance(db, tmp_path):
    def make():
        return Ctx(db=db, bus=EventBus(db), cfgs={}, plat=PlatformConfig(), drive=FakeDrive(), root=tmp_path)

    a, b = make(), make()
    a.paused_accounts.add(("tiktok", "teste"))
    a.cooldown[("instagram", "teste")] = None
    a.keys["pexels"] = "k"
    a.styles["x"] = None
    assert (b.paused_accounts, b.cooldown, b.keys, b.styles, b.memes) == (set(), {}, {}, {}, {})
    assert a.stop is not b.stop and not a.dry_run and a.active is None and a.channels_for("captacao") == []
