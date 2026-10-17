"""Etapa de publicação: destinos, legenda, contas conectadas, limites, horário, ensaio e arquivamento no Drive."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import NoKeyringError

from canaldark.db import claim, get_video, insert_video, now_iso, recover, transition, update_fields
from canaldark.drive import DriveError
from canaldark.models import PLATFORMS, Destination, DriveConfig, PlatformConfig, State, VideoRow
from canaldark.publish import stage
from canaldark.publish.base import AuthExpired, PublishResult, RetryLater, account_status, save_secret
from canaldark.publish.facebook import FacebookUploader
from canaldark.publish.instagram import InstagramUploader
from canaldark.publish.stage import (PublishBlocked, archive_video, caption, claim_due, destinations, publish_now,
                                     publish_video, real_uploaders)
from canaldark.publish.tiktok import TikTokUploader
from canaldark.publish.youtube import YouTubeUploader
from canaldark.rules import SP


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.data: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.data.get((service, username))

    def set_password(self, service, username, password):
        self.data[(service, username)] = password

    def delete_password(self, service, username):
        self.data.pop((service, username), None)


class BrokenKeyring(MemoryKeyring):
    def get_password(self, service, username):
        raise NoKeyringError("sem chaveiro")


@pytest.fixture
def vault():
    """Keyring em memória: nenhum teste toca o chaveiro de verdade."""
    previous = keyring.get_keyring()
    memory = MemoryKeyring()
    keyring.set_keyring(memory)
    yield memory.data
    keyring.set_keyring(previous)


def connect(*platforms: str, account: str = "teste") -> None:
    for p in platforms:
        save_secret(p, account, {"token": "x"})


class FakeUploader:
    """Uploader falso: cada item de `fails` é usado numa chamada (None = sucesso)."""

    def __init__(self, *fails, status: str = "publicado"):
        self.fails, self.status, self.calls = list(fails), status, []

    async def upload(self, video, title, description, tags, on_progress, synthetic=False):
        self.calls.append({"video": Path(video), "title": title, "description": description, "tags": list(tags),
                           "synthetic": synthetic})
        if self.fails:
            exc = self.fails.pop(0)
            if exc is not None:
                raise exc
        await on_progress(100)
        n = len(self.calls)
        return PublishResult(id_externo=f"id{n}", url=f"https://exemplo/{n}", status=self.status)


def uploaders(ctx, asked: list | None = None, **fails) -> dict[str, FakeUploader]:
    ups = {p: FakeUploader(*fails.get(p, ())) for p in PLATFORMS}

    def get(platform, account):
        if asked is not None:
            asked.append((platform, account))
        return ups[platform]

    ctx.get_uploader = get
    return ups


async def seed(ctx, n: int = 0, *, nota: float = 20.0, final: bool = True, **fields) -> int:
    """Vídeo editado do canal "teste"; nota 20 passa do URGENT_SCORE e publica fora dos horários."""
    fields.setdefault("estado", State.editado)
    fields.setdefault("manchete", f"Manchete {n}")
    vid = await insert_video(ctx.db, "teste", f"https://youtu.be/v{n}", f"Título {n}", nota=nota, autor="@autor",
                             **fields)
    if final:
        path = ctx.data / "editados" / str(vid) / "final.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"final")
        await update_fields(ctx.db, vid, caminho_final=str(path))
    return vid


async def pubs(ctx, vid: int) -> dict[str, tuple]:
    async with ctx.db.execute("SELECT plataforma, conta, status, motivo, url FROM publicacoes WHERE video_id = ?",
                              (vid,)) as cur:
        return {r["plataforma"]: (r["conta"], r["status"], r["motivo"], r["url"]) for r in await cur.fetchall()}


async def add_posts(ctx, platform: str, account: str, count: int, *, when: datetime, status="publicado") -> None:
    """Posts antigos de outro canal, para encher os limites de uma conta."""
    for i in range(count):
        vid = await insert_video(ctx.db, "outro", f"https://antigo/{platform}/{account}/{status}/{when}/{i}", "t",
                                 estado=State.arquivado)
        await ctx.db.execute(
            "INSERT INTO publicacoes (video_id, plataforma, conta, id_externo, url, status, publicado_em)"
            " VALUES (?, ?, ?, 'x', 'u', ?, ?)",
            (vid, platform, account, status, when.astimezone(timezone.utc).isoformat(timespec="seconds")))
    await ctx.db.commit()


async def log_messages(ctx) -> list[str]:
    return [e["data"]["msg"] for e in await ctx.bus.recent_logs(500)]


def at(day: int, h: int, m: int = 0) -> datetime:
    return datetime(2026, 10, day, h, m, tzinfo=SP)


def now() -> datetime:
    return datetime.now(SP)


def row(**over) -> VideoRow:
    base = dict(id=7, canal="teste", url_origem="https://youtu.be/abc", titulo_origem="Título original",
                estado=State.editado, manchete="Olha isso", autor="@GaiaMemes",
                criado_em="2026-10-17T00:00:00+00:00", atualizado_em="2026-10-17T00:00:00+00:00")
    return VideoRow(**{**base, **over})


# ---- contas ------------------------------------------------------------------


def test_account_status_reads_only_the_keyring(vault):
    connect("youtube")
    assert account_status("youtube", "teste") == "conectada"
    assert account_status("tiktok", "teste") == "falta"
    assert account_status("youtube", "outra") == "falta"
    assert json.loads(vault[("canaldark", "youtube:teste")]) == {"token": "x"}


def test_account_status_without_keyring_backend_is_falta():
    previous = keyring.get_keyring()
    keyring.set_keyring(BrokenKeyring())
    try:
        assert account_status("youtube", "teste") == "falta"
    finally:
        keyring.set_keyring(previous)


# ---- destinos e legenda ------------------------------------------------------


def test_destinations_default_and_override(cfg_factory):
    cfg = cfg_factory(contas={"youtube": "politica", "tiktok": "politica2"})
    assert destinations(row(), cfg) == [Destination(plataforma="youtube", conta="politica"),
                                        Destination(plataforma="tiktok", conta="politica2")]
    override = json.dumps([{"plataforma": "instagram", "conta": "jogos"}])
    assert destinations(row(destinos=override), cfg) == [Destination(plataforma="instagram", conta="jogos")]
    assert destinations(row(destinos="[]"), cfg) == []  # "não publicar" em todas as redes
    with pytest.raises(ValueError):
        destinations(row(destinos='[{"plataforma": "orkut", "conta": "x"}]'), cfg)
    with pytest.raises(ValueError):
        destinations(row(destinos="isso não é json"), cfg)


def test_destinations_keep_one_account_per_network(cfg_factory):
    # publicacoes guarda uma linha por rede: um segundo YouTube postaria de novo e apagaria o link do primeiro
    dup = json.dumps([{"plataforma": "youtube", "conta": "jogos"}, {"plataforma": "tiktok", "conta": "jogos"},
                      {"plataforma": "youtube", "conta": "politica"}])
    assert destinations(row(destinos=dup), cfg_factory()) == [Destination(plataforma="youtube", conta="jogos"),
                                                               Destination(plataforma="tiktok", conta="jogos")]


def test_caption_has_credit_and_hashtags(cfg_factory):
    r = row(manchete="Olha isso", autor="@GaiaMemes", url_origem="https://youtu.be/abc")
    assert caption(r, cfg_factory(hashtags=["#gta6", "games"])) == (
        "Olha isso\n\nCréditos: @GaiaMemes (https://youtu.be/abc)\n\n#gta6 #games")
    assert caption(r, cfg_factory()) == "Olha isso\n\nCréditos: @GaiaMemes (https://youtu.be/abc)"
    assert caption(row(manchete=None, autor=""), cfg_factory()) == (
        "Título original\n\nCréditos: https://youtu.be/abc")


# ---- publicação ----------------------------------------------------------------


async def test_publishes_to_connected_and_skips_missing(ctx, vault):
    ctx.dry_run = False
    connect("youtube")
    ups = uploaders(ctx)
    vid = await seed(ctx, manchete="M" * 150)

    r = await claim_due(ctx, now())
    assert (r.id, r.estado) == (vid, State.publicando)
    assert await publish_video(ctx, r) == State.publicado

    assert (await get_video(ctx.db, vid)).estado == State.publicado
    assert await pubs(ctx, vid) == {
        "youtube": ("teste", "publicado", None, "https://exemplo/1"),
        "instagram": ("teste", "pulado", "conta não conectada", None),
        "facebook": ("teste", "pulado", "conta não conectada", None),
        "tiktok": ("teste", "pulado", "conta não conectada", None)}
    (call,) = ups["youtube"].calls
    assert call["title"] == "M" * 100 and call["synthetic"] is False
    assert call["description"] == "M" * 150 + "\n\nCréditos: @autor (https://youtu.be/v0)"
    assert call["video"] == ctx.data / "editados" / str(vid) / "final.mp4"
    assert not any(ups[p].calls for p in ("instagram", "facebook", "tiktok"))


async def test_no_connected_account_pauses_channel(ctx, vault):
    ctx.dry_run = False
    ups = uploaders(ctx)
    vid = await seed(ctx)

    assert await claim_due(ctx, now()) is None
    with pytest.raises(PublishBlocked, match="nenhuma conta conectada"):
        await publish_now(ctx, vid)
    assert (await get_video(ctx.db, vid)).estado == State.editado
    assert await pubs(ctx, vid) == {} and not any(u.calls for u in ups.values())


async def test_override_to_unconnected_only_is_never_claimed(ctx, vault):
    ctx.dry_run = False
    connect("youtube")
    uploaders(ctx)
    vid = await seed(ctx, nota=30, destinos=json.dumps([{"plataforma": "tiktok", "conta": "teste"}]))

    assert await claim_due(ctx, now()) is None
    assert (await get_video(ctx.db, vid)).estado == State.editado
    other = await seed(ctx, 1, nota=20)  # um vídeo com destino conectado passa na frente do que não pode sair
    assert (await claim_due(ctx, now())).id == other


async def test_publish_video_never_marks_every_destination_skipped(ctx, vault):
    # mesmo pego por fora do claim_due, um vídeo sem nenhuma conta conectada não vira "publicado"
    ctx.dry_run = False
    uploaders(ctx)
    vid = await seed(ctx)
    r = await claim(ctx.db, State.editado, State.publicando, video_id=vid)
    assert await publish_video(ctx, r) == State.editado
    assert await pubs(ctx, vid) == {}


async def test_skipped_destination_is_retried_while_the_video_has_not_gone_out(ctx, vault):
    # o YouTube esperou e nada saiu ainda: o Instagram conectado nesse meio-tempo também recebe o vídeo
    ctx.dry_run = False
    connect("youtube")
    ups = uploaders(ctx, youtube=[RetryLater("cota do YouTube esgotada")])
    vid = await seed(ctx, destinos=json.dumps([{"plataforma": "youtube", "conta": "teste"},
                                               {"plataforma": "instagram", "conta": "teste"}]))
    assert await publish_video(ctx, await claim_due(ctx, now())) == State.editado
    assert (await pubs(ctx, vid))["instagram"][1] == "pulado"

    connect("instagram")
    ctx.cooldown.clear()
    assert await publish_video(ctx, await claim_due(ctx, now())) == State.publicado
    assert {k: v[1] for k, v in (await pubs(ctx, vid)).items()} == {"youtube": "publicado", "instagram": "publicado"}
    assert len(ups["instagram"].calls) == 1


async def test_video_left_only_with_skipped_destinations_waits_for_a_connection(ctx, vault):
    ctx.dry_run = False
    connect("youtube")
    uploaders(ctx, youtube=[RetryLater("cota do YouTube esgotada")])
    vid = await seed(ctx, destinos=json.dumps([{"plataforma": "youtube", "conta": "teste"},
                                               {"plataforma": "instagram", "conta": "teste"}]))
    assert await publish_video(ctx, await claim_due(ctx, now())) == State.editado
    await update_fields(ctx.db, vid, destinos=json.dumps([{"plataforma": "instagram", "conta": "teste"}]))  # na aba

    with pytest.raises(PublishBlocked, match="nenhuma conta conectada"):
        await publish_now(ctx, vid)
    connect("instagram")
    assert await publish_now(ctx, vid) == State.publicado
    assert (await pubs(ctx, vid))["instagram"][1] == "publicado"


async def test_video_with_no_network_chosen_is_never_published(ctx):
    ups = uploaders(ctx)
    vid = await seed(ctx, destinos="[]")
    assert await claim_due(ctx, now()) is None
    with pytest.raises(PublishBlocked, match="nenhuma rede escolhida"):
        await publish_now(ctx, vid)
    assert (await get_video(ctx.db, vid)).estado == State.editado and not any(u.calls for u in ups.values())


async def test_override_destinations_are_used(ctx):
    asked = []
    uploaders(ctx, asked)
    vid = await seed(ctx, destinos=json.dumps([{"plataforma": "youtube", "conta": "jogos"},
                                               {"plataforma": "tiktok", "conta": "politica"}]))
    assert await publish_video(ctx, await claim_due(ctx, now())) == State.publicado
    assert asked == [("youtube", "jogos"), ("tiktok", "politica")]
    assert {k: v[0] for k, v in (await pubs(ctx, vid)).items()} == {"youtube": "jogos", "tiktok": "politica"}


async def test_tiktok_draft_counts_as_done(ctx):
    ups = uploaders(ctx)
    ups["tiktok"].status = "rascunho"
    vid = await seed(ctx)
    assert await publish_video(ctx, await claim_due(ctx, now())) == State.publicado
    assert (await pubs(ctx, vid))["tiktok"][1] == "rascunho"


async def test_daily_limit_keeps_video_pending(ctx):
    ups = uploaders(ctx)
    await add_posts(ctx, "instagram", "teste", 25, when=now() - timedelta(hours=23))
    vid = await seed(ctx)

    assert await publish_video(ctx, await claim_due(ctx, now())) == State.editado
    assert (await get_video(ctx.db, vid)).estado == State.editado
    assert ups["instagram"].calls == [] and all(len(ups[p].calls) == 1 for p in ("youtube", "facebook", "tiktok"))
    assert set(await pubs(ctx, vid)) == {"youtube", "facebook", "tiktok"}
    assert await claim_due(ctx, now()) is None  # só falta o Instagram, que está no limite


@pytest.mark.parametrize("platform,cap,status", [
    ("instagram", 25, "publicado"), ("facebook", 30, "publicado"), ("tiktok", 15, "rascunho")])
async def test_each_platform_cap_in_24h(ctx, platform, cap, status):
    ups = uploaders(ctx)
    await add_posts(ctx, platform, "teste", cap - 1, when=now() - timedelta(hours=1), status=status)
    await add_posts(ctx, platform, "teste", 5, when=now() - timedelta(hours=25))   # fora da janela de 24 h
    await add_posts(ctx, platform, "teste", 5, when=now() - timedelta(hours=1), status="pulado")  # não conta
    await add_posts(ctx, platform, "outra", 5, when=now() - timedelta(hours=1))     # outra conta
    first = await seed(ctx)
    assert await publish_video(ctx, await claim_due(ctx, now())) == State.publicado
    second = await seed(ctx, 1)
    assert await publish_video(ctx, await claim_due(ctx, now())) == State.editado   # o post do first fechou a conta
    assert len(ups[platform].calls) == 1
    assert platform in await pubs(ctx, first) and platform not in await pubs(ctx, second)


@pytest.mark.parametrize("minutes,limited", [(1, True), (-1, False)])
async def test_youtube_daily_cap_sums_all_accounts_in_the_sp_day(ctx, minutes, limited):
    ups = uploaders(ctx)
    midnight = now().replace(hour=0, minute=0, second=0, microsecond=0)
    await add_posts(ctx, "youtube", "outra_conta", 90, when=midnight + timedelta(minutes=minutes))
    await seed(ctx)
    expected = State.editado if limited else State.publicado
    assert await publish_video(ctx, await claim_due(ctx, now())) == expected
    assert len(ups["youtube"].calls) == (0 if limited else 1)


async def test_retry_later_cooldown_and_auth_expired_pause(ctx):
    ups = uploaders(ctx, youtube=[RetryLater("cota do YouTube esgotada")], instagram=[AuthExpired("token vencido")])
    vid = await seed(ctx)

    assert await publish_video(ctx, await claim_due(ctx, now())) == State.editado
    wait = ctx.cooldown[("youtube", "teste")] - datetime.now(timezone.utc)
    assert timedelta(minutes=59) < wait <= timedelta(hours=1)
    assert ("instagram", "teste") in ctx.paused_accounts
    assert set(await pubs(ctx, vid)) == {"facebook", "tiktok"}
    assert any("canaldark auth instagram teste" in m for m in await log_messages(ctx))
    assert await claim_due(ctx, now()) is None  # YouTube esperando e Instagram pausado

    ctx.cooldown[("youtube", "teste")] = datetime.now(timezone.utc) - timedelta(seconds=1)  # a espera acabou
    assert await publish_video(ctx, await claim_due(ctx, now())) == State.editado  # o Instagram segue pausado
    assert (len(ups["youtube"].calls), len(ups["instagram"].calls)) == (2, 1)

    ctx.paused_accounts.clear()  # o André reconectou e retomou
    assert await publish_video(ctx, await claim_due(ctx, now())) == State.publicado
    assert len(ups["instagram"].calls) == 2
    assert {k: v[1] for k, v in (await pubs(ctx, vid)).items()} == {p: "publicado" for p in PLATFORMS}


async def test_other_failure_goes_to_erro_and_retry_skips_done(ctx):
    ups = uploaders(ctx, tiktok=[RuntimeError("TikTok respondeu 500")])
    vid = await seed(ctx)

    assert await publish_video(ctx, await claim_due(ctx, now())) == State.erro
    v = await get_video(ctx.db, vid)
    assert (v.estado, v.fila_erro) == (State.erro, State.editado) and "tiktok" in v.erro.lower()

    await transition(ctx.db, vid, State.editado)  # "Tentar de novo"
    assert await publish_video(ctx, await claim_due(ctx, now())) == State.publicado
    assert [len(ups[p].calls) for p in PLATFORMS] == [1, 1, 1, 2]


async def test_video_resolved_everywhere_before_a_crash_is_finished(ctx):
    # caiu depois de gravar todos os destinos e antes de virar publicado: o recover devolve o vídeo para editado
    ups = uploaders(ctx)
    vid = await seed(ctx, estado=State.publicando)
    for platform, status in {"youtube": "publicado", "instagram": "pulado", "facebook": "pulado",
                             "tiktok": "rascunho"}.items():
        await ctx.db.execute("INSERT INTO publicacoes (video_id, plataforma, conta, status, publicado_em)"
                             " VALUES (?, ?, 'teste', ?, ?)", (vid, platform, status, now_iso()))
    await ctx.db.commit()
    assert await recover(ctx.db) == 1

    assert await publish_video(ctx, await claim_due(ctx, now())) == State.publicado
    assert not any(u.calls for u in ups.values())


async def test_malformed_destinations_go_to_erro(ctx):
    ups = uploaders(ctx)
    vid = await seed(ctx, destinos="{quebrado")
    r = await claim_due(ctx, now())
    assert r.id == vid
    assert await publish_video(ctx, r) == State.erro
    v = await get_video(ctx.db, vid)
    assert v.fila_erro == State.editado and "destinos" in v.erro
    assert not any(u.calls for u in ups.values())


async def test_missing_final_goes_to_erro_before_posting(ctx):
    ups = uploaders(ctx)
    vid = await seed(ctx, final=False)
    assert await publish_video(ctx, await claim_due(ctx, now())) == State.erro
    assert await pubs(ctx, vid) == {} and not any(u.calls for u in ups.values())


async def test_waits_for_the_channel_slot(ctx):
    uploaders(ctx)
    vid = await seed(ctx, nota=3)
    assert await claim_due(ctx, at(16, 8)) is None  # o horário do canal é 09:00
    assert (await claim_due(ctx, at(16, 9, 5))).id == vid


async def test_paused_channel_is_not_claimed(ctx):
    uploaders(ctx)
    await seed(ctx)
    ctx.active = lambda tab: [] if tab == "publicacao" else ["teste"]
    assert await claim_due(ctx, now()) is None


async def test_blackout_blocks_politics(ctx, cfg_factory):
    ctx.cfgs["teste"] = cfg_factory(regras="politica")
    uploaders(ctx)
    vid = await seed(ctx)
    assert await claim_due(ctx, at(24, 12)) is None
    with pytest.raises(PublishBlocked):
        await publish_now(ctx, vid, now=at(24, 12))
    assert (await get_video(ctx.db, vid)).estado == State.editado
    assert (await claim_due(ctx, at(28, 12))).id == vid


async def test_publish_now_requires_editado(ctx):
    ups = uploaders(ctx)
    vid = await seed(ctx, estado=State.pronto)
    with pytest.raises(PublishBlocked):
        await publish_now(ctx, vid)
    assert (await get_video(ctx.db, vid)).estado == State.pronto
    assert not any(u.calls for u in ups.values())


async def test_publish_now_ignores_the_slot(ctx):
    ups = uploaders(ctx)
    vid = await seed(ctx, nota=1)
    assert await claim_due(ctx, at(16, 8)) is None
    assert await publish_now(ctx, vid, now=at(16, 8)) == State.publicado
    assert all(len(ups[p].calls) == 1 for p in PLATFORMS)


async def test_dry_run_copies_to_ensaio(ctx):
    assert ctx.dry_run and ctx.get_uploader is None  # sem uploader definido, o ensaio usa o DryRunUploader
    vid = await seed(ctx)
    assert await publish_video(ctx, await claim_due(ctx, now())) == State.publicado

    copies = sorted((ctx.data / "ensaio").glob("*.mp4"))
    assert sorted(c.name.split("-")[0] for c in copies) == sorted(PLATFORMS)
    assert all(c.read_bytes() == b"final" for c in copies)
    assert "Créditos: @autor (https://youtu.be/v0)" in copies[0].with_suffix(".txt").read_text(encoding="utf-8")
    assert all(status == "publicado" and url.startswith("file://")
               for _, status, _, url in (await pubs(ctx, vid)).values())


async def test_real_uploaders_pick_platform_classes_and_tiktok_mode(ctx, cfg_factory):
    ctx.dry_run = False
    ctx.cfgs["jogos"] = cfg_factory(id="jogos", contas={"tiktok": "jogos"}, tiktok_modo="publicar")
    get = real_uploaders(ctx)
    assert isinstance(get("youtube", "teste"), YouTubeUploader)
    assert isinstance(get("instagram", "teste"), InstagramUploader)
    assert isinstance(get("facebook", "teste"), FacebookUploader)
    tiktok = get("tiktok", "jogos")
    assert isinstance(tiktok, TikTokUploader) and (tiktok.account, tiktok.modo) == ("jogos", "publicar")
    assert get("tiktok", "teste").modo == "rascunho" and get("tiktok", "sem_canal").modo == "rascunho"
    assert get("facebook", "teste") is get("facebook", "teste")


# ---- trabalhadores -------------------------------------------------------------


async def _run(ctx, *workers, until, timeout: float = 5.0) -> None:
    tasks = [asyncio.create_task(w(ctx)) for w in workers]
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not await until() and loop.time() < deadline:
            await asyncio.sleep(0.01)
    finally:
        ctx.stop.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 5)


async def test_workers_publish_then_archive(ctx, monkeypatch):
    monkeypatch.setattr(stage, "IDLE_SLEEP", 0.01)
    monkeypatch.setattr(stage, "ARCHIVE_IDLE_SLEEP", 0.01)
    uploaders(ctx)
    vid = await seed(ctx)

    async def archived():
        return (await get_video(ctx.db, vid)).estado == State.arquivado

    await _run(ctx, stage.publish_worker, stage.archive_worker, until=archived)
    assert await archived()


async def test_worker_warns_once_when_no_account_is_connected(ctx, vault, monkeypatch):
    monkeypatch.setattr(stage, "IDLE_SLEEP", 0.01)
    ctx.dry_run = False
    uploaders(ctx)
    await seed(ctx)

    async def never():
        return False

    await _run(ctx, stage.publish_worker, until=never, timeout=0.3)
    warnings = [m for m in await log_messages(ctx) if "nenhuma conta conectada" in m]
    assert len(warnings) == 1 and "teste" in warnings[0]


async def test_archive_worker_respects_paused_channel(ctx, monkeypatch):
    monkeypatch.setattr(stage, "ARCHIVE_IDLE_SLEEP", 0.01)
    vid = await seed(ctx, estado=State.publicado)
    ctx.active = lambda tab: []

    async def never():
        return False

    await _run(ctx, stage.archive_worker, until=never, timeout=0.2)
    assert (await get_video(ctx.db, vid)).estado == State.publicado


# ---- arquivamento --------------------------------------------------------------


async def seed_published(ctx) -> VideoRow:
    """Vídeo publicado com tudo que o arquivamento mexe: final, fonte local, capas, original e info.json no Drive."""
    vid = await insert_video(ctx.db, "teste", "https://youtu.be/pub", "Título", estado=State.publicado)
    work = ctx.data / "editados" / str(vid)
    work.mkdir(parents=True)
    (work / "final.mp4").write_bytes(b"final")
    (work / "faixa.png").write_bytes(b"png")
    local = ctx.data / "pacote" / "teste" / f"{vid}.mp4"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"orig")
    capas = ctx.data / "capas"
    capas.mkdir(parents=True)
    (capas / f"{vid}-origem.jpg").write_bytes(b"jpg")
    (capas / f"{vid}-final.jpg").write_bytes(b"jpg")
    ctx.drive.files[f"teste/2-em-edicao/{vid}.mp4"] = b"orig"
    ctx.drive.files[f"teste/1-captados/{vid}.info.json"] = b"{}"
    await update_fields(ctx.db, vid, caminho_final=str(work / "final.mp4"), caminho_local=str(local),
                        caminho_capa=str(capas / f"{vid}-origem.jpg"), remoto_bruto=f"teste/2-em-edicao/{vid}.mp4")
    return await claim(ctx.db, State.publicado, State.arquivando, video_id=vid)


def sp_day() -> str:
    return datetime.now(SP).strftime("%Y-%m-%d")


async def test_archive_moves_final_and_deletes_original(ctx):
    r = await seed_published(ctx)
    before = sp_day()
    assert await archive_video(ctx, r) == State.arquivado
    after = sp_day()

    v = await get_video(ctx.db, r.id)
    assert v.estado == State.arquivado
    day = v.remoto_final.split("/")[2]
    assert day in {before, after} and v.remoto_final == f"teste/3-publicados/{day}/{r.id}.mp4"
    assert ctx.drive.files == {v.remoto_final: b"final"}  # o original e o info.json saíram do Drive
    assert not (ctx.data / "editados" / str(r.id)).exists()
    assert not (ctx.data / "pacote" / "teste" / f"{r.id}.mp4").exists()
    assert (ctx.data / "capas" / f"{r.id}-origem.jpg").exists() and (ctx.data / "capas" / f"{r.id}-final.jpg").exists()
    assert (v.caminho_local, v.caminho_final, v.remoto_bruto) == (None, None, None)


async def test_archive_keeps_original_when_configured(ctx):
    ctx.plat = PlatformConfig(drive=DriveConfig(manter_originais=True))
    r = await seed_published(ctx)
    assert await archive_video(ctx, r) == State.arquivado

    v = await get_video(ctx.db, r.id)
    assert v.remoto_bruto == f"teste/4-originais-usados/{r.id}.mp4"
    assert ctx.drive.files == {v.remoto_final: b"final", f"teste/4-originais-usados/{r.id}.mp4": b"orig",
                               f"teste/4-originais-usados/{r.id}.info.json": b"{}"}


async def test_archive_resumes_after_crash(ctx):
    # caiu depois que o final subiu: o local já sumiu e o remoto_final está gravado
    r = await seed_published(ctx)
    remoto = f"teste/3-publicados/2026-10-16/{r.id}.mp4"  # a data da tentativa que caiu
    ctx.drive.files[remoto] = b"final"
    Path(r.caminho_final).unlink()
    await update_fields(ctx.db, r.id, remoto_final=remoto)
    assert await recover(ctx.db) == 1
    r = await claim(ctx.db, State.publicado, State.arquivando, video_id=r.id)

    assert await archive_video(ctx, r) == State.arquivado
    assert "enviar" not in [c[0] for c in ctx.drive.calls]
    v = await get_video(ctx.db, r.id)
    assert v.remoto_final == remoto and ctx.drive.files == {remoto: b"final"}


async def test_archive_resends_when_final_is_still_local(ctx):
    # caiu no meio do envio: o remoto_final está gravado, mas o final ainda está no disco
    r = await seed_published(ctx)
    remoto = f"teste/3-publicados/2026-10-16/{r.id}.mp4"
    ctx.drive.files[remoto] = b"pedaco"
    await update_fields(ctx.db, r.id, remoto_final=remoto)
    assert await recover(ctx.db) == 1
    r = await claim(ctx.db, State.publicado, State.arquivando, video_id=r.id)

    assert await archive_video(ctx, r) == State.arquivado
    assert [c[2] for c in ctx.drive.calls if c[0] == "enviar"] == ["teste/3-publicados/2026-10-16"]
    assert ctx.drive.files == {remoto: b"final"}


async def test_archive_failure_goes_to_erro_publicado_and_retry_works(ctx):
    r = await seed_published(ctx)
    ctx.drive.fail["enviar"] = DriveError("sem rede")
    assert await archive_video(ctx, r) == State.erro

    v = await get_video(ctx.db, r.id)
    assert (v.estado, v.fila_erro) == (State.erro, State.publicado) and "sem rede" in v.erro
    assert Path(v.caminho_final).read_bytes() == b"final"  # nada se perdeu
    assert f"teste/2-em-edicao/{r.id}.mp4" in ctx.drive.files

    await transition(ctx.db, r.id, State.publicado)  # "Tentar de novo"
    r = await claim(ctx.db, State.publicado, State.arquivando, video_id=r.id)
    assert await archive_video(ctx, r) == State.arquivado
    v = await get_video(ctx.db, r.id)
    assert ctx.drive.files == {v.remoto_final: b"final"}


async def test_archive_without_final_file_keeps_the_original(ctx):
    r = await seed_published(ctx)
    Path(r.caminho_final).unlink()
    assert await archive_video(ctx, r) == State.erro
    v = await get_video(ctx.db, r.id)
    assert v.remoto_final is None and ctx.drive.files[f"teste/2-em-edicao/{r.id}.mp4"] == b"orig"
