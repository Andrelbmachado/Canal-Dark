"""Publish stage: post each edited video to its destinations at the channel's slots, then archive it in the Drive."""
import asyncio
import shutil
from collections.abc import Callable
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from canaldark.context import Ctx
from canaldark.db import claim, get_video, now_iso, transition, update_fields
from canaldark.drive import CAPTADOS, PUBLICADOS, USADOS, DriveError, pasta
from canaldark.models import ChannelConfig, Destination, State, VideoRow
from canaldark.publish.base import AuthExpired, PublishResult, RetryLater, account_status
from canaldark.publish.facebook import FB_DAILY_CAP, FacebookUploader
from canaldark.publish.instagram import IG_DAILY_CAP, InstagramUploader
from canaldark.publish.schedule import next_slot
from canaldark.publish.tiktok import TIKTOK_DAILY_CAP, TikTokUploader
from canaldark.publish.youtube import YouTubeUploader
from canaldark.rules import SP, publish_allowed

LIMITS_24H = {"instagram": IG_DAILY_CAP, "facebook": FB_DAILY_CAP, "tiktok": TIKTOK_DAILY_CAP}  # per account
YOUTUBE_DAILY = 90  # the upload quota belongs to the Google project, so it sums every YouTube account
COOLDOWN_AFTER_LIMIT = timedelta(hours=1)
URGENT_SCORE = 12.0
IDLE_SLEEP = 15  # publishing follows the channel slots: no need to poll faster
ARCHIVE_IDLE_SLEEP = 3
POSTED = ("publicado", "rascunho")
RESOLVED = (*POSTED, "pulado")
NAMES = {"youtube": "YouTube", "instagram": "Instagram", "facebook": "Facebook", "tiktok": "TikTok"}
_DESTINATIONS = TypeAdapter(list[Destination])


class PublishBlocked(Exception):
    """`publish_now` refused; the message (in Portuguese) says why."""


class DryRunUploader:
    """`--ensaio`: copies the MP4 (and a .txt with the caption) to dados/ensaio instead of publishing."""

    def __init__(self, platform: str, out_dir: Path):
        self.platform, self.out_dir = platform, out_dir

    async def upload(self, video, title, description, tags, on_progress, synthetic=False) -> PublishResult:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        dest = self.out_dir / f"{self.platform}-{video.parent.name}-{video.name}"
        await asyncio.to_thread(shutil.copyfile, video, dest)
        dest.with_suffix(".txt").write_text(f"{title}\n\n{description}\n\n{' '.join(tags)}", encoding="utf-8")
        await on_progress(100)
        return PublishResult(id_externo="ensaio", url=f"file://{dest.resolve()}")


def _tiktok_mode(ctx: Ctx, account: str) -> str:
    for cfg in ctx.cfgs.values():
        if cfg.contas.get("tiktok") == account:
            return cfg.tiktok_modo
    return "rascunho"  # an account no channel lists: the safe mode


def real_uploaders(ctx: Ctx) -> Callable[[str, str], Any]:
    """(platform, account) → uploader, one per account; every platform copies to dados/ensaio in a dry run."""
    cache: dict[tuple[str, str, str | None], Any] = {}

    def get(platform: str, account: str):
        mode = _tiktok_mode(ctx, account) if platform == "tiktok" else None
        key = (platform, account, mode)
        if key not in cache:
            if ctx.dry_run:
                cache[key] = DryRunUploader(platform, ctx.data / "ensaio")
            elif platform == "youtube":
                cache[key] = YouTubeUploader(account)
            elif platform == "instagram":
                cache[key] = InstagramUploader(account)
            elif platform == "facebook":
                cache[key] = FacebookUploader(account)
            elif platform == "tiktok":
                cache[key] = TikTokUploader(account, modo=mode)
            else:
                return None
        return cache[key]
    return get


# ---- destinations, caption, accounts ------------------------------------------


def _channel_destinations(cfg: ChannelConfig) -> list[Destination]:
    return [Destination(plataforma=p, conta=c) for p, c in cfg.contas.items()]


def destinations(row: VideoRow, cfg: ChannelConfig) -> list[Destination]:
    """The destinations chosen for this video in the panel or, without them, the channel's `contas`.

    Raises ValueError when the stored JSON is not a list of destinations."""
    if not (row.destinos or "").strip():
        return _channel_destinations(cfg)
    chosen: dict[str, Destination] = {}
    for dest in _DESTINATIONS.validate_json(row.destinos):
        chosen.setdefault(dest.plataforma, dest)  # publicacoes keeps one row per network: the first account wins
    return list(chosen.values())


def caption(row: VideoRow, cfg: ChannelConfig) -> str:
    credit = f"{row.autor} ({row.url_origem})" if row.autor else row.url_origem
    text = f"{(row.manchete or row.titulo_origem).strip()}\n\nCréditos: {credit}"
    tags = [t if t.startswith("#") else f"#{t}" for t in (h.strip() for h in cfg.hashtags) if t]
    return f"{text}\n\n{' '.join(tags)}" if tags else text


def connected(ctx: Ctx, dest: Destination) -> bool:
    return ctx.dry_run or account_status(dest.plataforma, dest.conta) == "conectada"


def _connections(ctx: Ctx) -> Callable[[Destination], bool]:
    """`connected` remembered for one pass: every video asks about the same few accounts."""
    seen: dict[tuple[str, str], bool] = {}

    def check(dest: Destination) -> bool:
        key = (dest.plataforma, dest.conta)
        if key not in seen:
            seen[key] = connected(ctx, dest)
        return seen[key]
    return check


def _publishable(cfg: ChannelConfig, now: datetime, is_connected: Callable[[Destination], bool]) -> bool:
    return publish_allowed(cfg, now) and any(is_connected(d) for d in _channel_destinations(cfg))


def channel_publishable(ctx: Ctx, cfg: ChannelConfig, now: datetime) -> bool:
    return _publishable(cfg, now, lambda d: connected(ctx, d))


# ---- limits and history -------------------------------------------------------


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _day_start(now: datetime) -> datetime:
    return datetime.combine(now.astimezone(SP).date(), dtime(0), tzinfo=SP)


def _marks(values: tuple) -> str:
    return ", ".join("?" * len(values))


async def _limit_reached(db, dest: Destination, now: datetime) -> bool:
    """YouTube: 90 in the São Paulo day, all accounts together; the others: their cap in the last 24 h per account."""
    if dest.plataforma == "youtube":
        sql = f"SELECT COUNT(*) FROM publicacoes WHERE plataforma = 'youtube' AND status IN ({_marks(POSTED)})" \
              f" AND publicado_em >= ?"
        params, cap = (*POSTED, _utc(_day_start(now))), YOUTUBE_DAILY
    else:
        cap = LIMITS_24H.get(dest.plataforma)
        if cap is None:
            return False
        sql = f"SELECT COUNT(*) FROM publicacoes WHERE plataforma = ? AND conta = ? AND status IN ({_marks(POSTED)})" \
              f" AND publicado_em >= ?"
        params = (dest.plataforma, dest.conta, *POSTED, _utc(now - timedelta(hours=24)))
    async with db.execute(sql, params) as cur:
        (count,) = await cur.fetchone()
    return count >= cap


def _blocked(ctx: Ctx, dest: Destination, now: datetime) -> bool:
    """The account's token expired (paused until reconnected) or it hit a limit less than an hour ago."""
    key = (dest.plataforma, dest.conta)
    if key in ctx.paused_accounts:
        return True
    until = ctx.cooldown.get(key)
    if until is None:
        return False
    if until <= now:
        ctx.cooldown.pop(key, None)
        return False
    return True


async def _resolved(db, video_id: int) -> dict[str, str]:
    """platform → status of the destinations already settled for the video.

    A "pulado" only settles once the video went out somewhere: until then an account connected meanwhile still gets
    the video, and a video whose destinations were all skipped waits for a connection instead of getting stuck."""
    async with db.execute(f"SELECT plataforma, status FROM publicacoes WHERE video_id = ? AND status IN "
                          f"({_marks(RESOLVED)})", (video_id, *RESOLVED)) as cur:
        done = {r["plataforma"]: r["status"] for r in await cur.fetchall()}
    return done if any(s in POSTED for s in done.values()) else {}


async def _channel_stats(db, channel: str, now: datetime) -> tuple[int, datetime | None]:
    """(videos of the channel posted today in São Paulo, time of the channel's last post)."""
    join = "FROM publicacoes p JOIN videos v ON v.id = p.video_id WHERE v.canal = ?"
    async with db.execute(f"SELECT COUNT(DISTINCT p.video_id) {join} AND p.status IN ({_marks(POSTED)})"
                          f" AND p.publicado_em >= ?", (channel, *POSTED, _utc(_day_start(now)))) as cur:
        (count,) = await cur.fetchone()
    async with db.execute(f"SELECT MAX(p.publicado_em) {join} AND p.status IN ({_marks(POSTED)})",
                          (channel, *POSTED)) as cur:
        (last,) = await cur.fetchone()
    return count, datetime.fromisoformat(last) if last else None


async def _blocker(ctx: Ctx, row: VideoRow, cfg: ChannelConfig, now: datetime,
                   is_connected: Callable[[Destination], bool]) -> str | None:
    """None when the video has a connected, pending destination it can go to now; otherwise the reason."""
    try:
        dests = destinations(row, cfg)
    except ValueError:
        return None  # publish_video sends it to erro with the reason, where the panel shows it
    done = await _resolved(ctx.db, row.id)
    pending = [d for d in dests if d.plataforma not in done]
    if not pending:
        # everything resolved but the move to publicado was lost in a crash: let publish_video finish it
        return None if any(s in POSTED for s in done.values()) else "o vídeo não tem nenhuma rede escolhida"
    live = [d for d in pending if is_connected(d)]
    if not live:
        return "nenhuma conta conectada para os destinos deste vídeo"
    for d in live:
        if not _blocked(ctx, d, now) and not await _limit_reached(ctx.db, d, now):
            return None
    return "as contas deste vídeo estão no limite de posts, esperando ou com o token vencido; tente mais tarde"


# ---- publishing -----------------------------------------------------------------


async def claim_due(ctx: Ctx, now: datetime) -> VideoRow | None:
    """The best edited video of a channel whose slot is due and that can go to at least one account now."""
    is_connected = _connections(ctx)
    for channel in ctx.channels_for("publicacao"):
        cfg = ctx.cfgs.get(channel)
        if cfg is None or not _publishable(cfg, now, is_connected):
            continue
        async with ctx.db.execute("SELECT * FROM videos WHERE canal = ? AND estado = ? ORDER BY nota DESC, id",
                                  (channel, State.editado.value)) as cur:
            rows = [VideoRow(**dict(r)) for r in await cur.fetchall()]
        chosen = None
        for row in rows:
            if await _blocker(ctx, row, cfg, now, is_connected) is None:
                chosen = row
                break
        if chosen is None:
            continue
        count, last = await _channel_stats(ctx.db, channel, now)
        slot = next_slot(cfg, now, count, chosen.nota >= URGENT_SCORE, last)
        if slot is None or slot > now:
            continue
        claimed = await claim(ctx.db, State.editado, State.publicando, video_id=chosen.id)
        if claimed is not None:
            return claimed
    return None


async def _fail(ctx: Ctx, video_id: int, msg: str) -> None:
    """Moves the video to `erro` without ever raising (it may have been discarded meanwhile)."""
    try:
        await transition(ctx.db, video_id, State.erro, error=msg)
        await ctx.bus.publish("video.estado", {"id": video_id, "estado": State.erro.value, "erro": msg})
        await ctx.bus.log("publicacao", msg, "erro", video_id)
    except Exception as e:
        try:
            await ctx.bus.log("publicacao", f"{msg} (e não consegui marcar o erro: {e})", "erro", video_id)
        except Exception:
            pass


async def _record(db, video_id: int, dest: Destination, *, status: str, id_externo: str | None = None,
                  url: str | None = None, motivo: str | None = None, publicado_em: str | None = None) -> None:
    await db.execute(
        "INSERT INTO publicacoes (video_id, plataforma, conta, id_externo, url, status, motivo, publicado_em)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(video_id, plataforma) DO UPDATE SET conta = excluded.conta,"
        " id_externo = excluded.id_externo, url = excluded.url, status = excluded.status, motivo = excluded.motivo,"
        " publicado_em = excluded.publicado_em",
        (video_id, dest.plataforma, dest.conta, id_externo, url, status, motivo, publicado_em))
    await db.commit()


async def publish_video(ctx: Ctx, row: VideoRow) -> State:
    """Posts to every destination still pending; publicado when all are resolved, editado while some wait."""
    try:
        return await _publish(ctx, row)
    except Exception as e:  # never let one video stop the queue
        await _fail(ctx, row.id, f"falha na publicação: {e}")
        return State.erro


async def _publish(ctx: Ctx, row: VideoRow) -> State:
    await ctx.bus.publish("video.estado", {"id": row.id, "estado": State.publicando.value})
    cfg = ctx.cfgs[row.canal]
    try:
        dests = destinations(row, cfg)
    except ValueError as e:
        raise RuntimeError("os destinos gravados neste vídeo são inválidos; escolha as contas de novo na aba "
                           "Publicação") from e
    done = await _resolved(ctx.db, row.id)
    pending = [d for d in dests if d.plataforma not in done]
    is_connected = _connections(ctx)
    if not any(s in POSTED for s in done.values()) and not any(is_connected(d) for d in pending):
        # never mark every destination "pulado": the video would be archived without going out anywhere
        await transition(ctx.db, row.id, State.editado)
        await ctx.bus.publish("video.estado", {"id": row.id, "estado": State.editado.value})
        await ctx.bus.log("publicacao", f"[{row.canal}] nenhuma conta conectada para este vídeo; ele espera em editado",
                          "aviso", row.id)
        return State.editado
    video = Path(row.caminho_final) if row.caminho_final else None
    if any(is_connected(d) for d in pending) and (video is None or not video.is_file()):
        raise RuntimeError(f"o arquivo final não está no disco: {row.caminho_final}")
    if ctx.get_uploader is None:
        ctx.get_uploader = real_uploaders(ctx)
    title = (row.manchete or row.titulo_origem)[:100]
    text = caption(row, cfg)
    waiting = False
    for n, dest in enumerate(pending):
        name = f"{NAMES.get(dest.plataforma, dest.plataforma)} ({dest.conta})"
        if not is_connected(dest):
            await _record(ctx.db, row.id, dest, status="pulado", motivo="conta não conectada")
            await ctx.bus.log("publicacao", f"{name}: pulado, a conta não está conectada", "aviso", row.id)
            continue
        current = datetime.now(timezone.utc)
        if _blocked(ctx, dest, current):
            waiting = True
            continue
        if await _limit_reached(ctx.db, dest, current):
            await ctx.bus.log("publicacao", f"{name}: limite de posts atingido; fica para depois", "aviso", row.id)
            waiting = True
            continue
        uploader = ctx.get_uploader(dest.plataforma, dest.conta)
        if uploader is None:
            raise RuntimeError(f"não há uploader para {name}")

        async def on_progress(pct: float, n=n) -> None:
            await ctx.bus.progress(row.id, (n + pct / 100) / len(pending) * 100)

        try:
            res = await uploader.upload(video, title, text, cfg.hashtags, on_progress, synthetic=False)
        except AuthExpired as e:
            ctx.paused_accounts.add((dest.plataforma, dest.conta))
            await ctx.bus.log("publicacao", f"{name}: {e} — reconecte com `canaldark auth {dest.plataforma} "
                                            f"{dest.conta}`", "aviso", row.id)
            waiting = True
            continue
        except RetryLater as e:
            ctx.cooldown[(dest.plataforma, dest.conta)] = datetime.now(timezone.utc) + COOLDOWN_AFTER_LIMIT
            await ctx.bus.log("publicacao", f"{name}: {e}; tento de novo em 1 h", "aviso", row.id)
            waiting = True
            continue
        except Exception as e:
            await _fail(ctx, row.id, f"falha ao publicar no {name}: {e}")
            return State.erro
        await _record(ctx.db, row.id, dest, status=res.status, id_externo=res.id_externo, url=res.url,
                      publicado_em=now_iso())
        if res.status == "rascunho":
            msg = f"{name}: rascunho na caixa de entrada do app; poste pelo celular com a legenda e os créditos"
        else:
            msg = f"publicado no {name}" + (f": {res.url}" if res.url else "")
        await ctx.bus.log("publicacao", msg, video_id=row.id)

    final = State.editado if waiting else State.publicado
    await transition(ctx.db, row.id, final)
    await ctx.bus.publish("video.estado", {"id": row.id, "estado": final.value})
    return final


async def publish_now(ctx: Ctx, video_id: int, *, now: datetime | None = None) -> State:
    """The "Publicar agora" button: ignores the slot, but not the election pause, the accounts or the limits."""
    now = now or datetime.now(SP)
    row = await get_video(ctx.db, video_id)
    if row is None:
        raise PublishBlocked("vídeo não encontrado")
    if row.estado != State.editado:
        raise PublishBlocked(f"o vídeo está em {row.estado.value}; só dá para publicar agora um vídeo editado")
    cfg = ctx.cfgs.get(row.canal)
    if cfg is None:
        raise PublishBlocked(f"o canal {row.canal} não está configurado")
    if not publish_allowed(cfg, now):
        raise PublishBlocked("pausa eleitoral: este canal não publica na janela do TSE "
                             "(desligue com pausa_eleitoral: false)")
    reason = await _blocker(ctx, row, cfg, now, _connections(ctx))
    if reason:
        raise PublishBlocked(reason)
    claimed = await claim(ctx.db, State.editado, State.publicando, video_id=video_id)
    if claimed is None:
        raise PublishBlocked("o vídeo saiu de editado antes de publicar")
    return await publish_video(ctx, claimed)


async def _idle(ctx: Ctx, seconds: float) -> None:
    try:
        await asyncio.wait_for(ctx.stop.wait(), seconds)
    except asyncio.TimeoutError:
        pass


async def _log_safely(ctx: Ctx, msg: str) -> None:
    try:
        await ctx.bus.log("publicacao", msg, "erro")
    except Exception:
        pass


async def _warn_unconnected(ctx: Ctx, warned: set[str]) -> None:
    """Logs once per channel when none of its accounts is connected (its publishing stays paused)."""
    if ctx.dry_run:
        return
    for channel in ctx.channels_for("publicacao"):
        cfg = ctx.cfgs.get(channel)
        if cfg is None:
            continue
        if any(connected(ctx, d) for d in _channel_destinations(cfg)):
            warned.discard(channel)
        elif channel not in warned:
            warned.add(channel)
            accounts = ", ".join(f"{p}:{c}" for p, c in cfg.contas.items()) or "nenhuma em contas:"
            await ctx.bus.log("publicacao", f"[{channel}] nenhuma conta conectada ({accounts}): a publicação do canal "
                                            f"fica em pausa até você rodar `canaldark auth`", "aviso")


async def publish_worker(ctx: Ctx) -> None:
    warned: set[str] = set()
    while not ctx.stop.is_set():
        row = None
        try:
            await _warn_unconnected(ctx, warned)
            row = await claim_due(ctx, datetime.now(SP))
        except Exception as e:  # keep the worker alive
            await _log_safely(ctx, f"erro ao procurar vídeo para publicar: {e}")
        if row is None:
            await _idle(ctx, IDLE_SLEEP)
            continue
        await publish_video(ctx, row)


# ---- archive --------------------------------------------------------------------


def _final_file(row: VideoRow) -> Path | None:
    """The final MP4 on disk: caminho_final, or the <id>.mp4 it is renamed to right before the upload."""
    if not row.caminho_final:
        return None
    path = Path(row.caminho_final)
    for candidate in (path, path.with_name(f"{row.id}.mp4")):
        if candidate.is_file():
            return candidate
    return None


async def _drop_original(ctx: Ctx, row: VideoRow) -> None:
    """The source leaves the Drive with its info.json, or goes to 4-originais-usados with manter_originais."""
    info = pasta(row.canal, CAPTADOS, f"{row.id}.info.json")
    if ctx.plat.drive.manter_originais:
        usados = pasta(row.canal, USADOS)
        if row.remoto_bruto and not row.remoto_bruto.startswith(usados + "/"):
            moved = await ctx.drive.mover(row.remoto_bruto, usados)
            await update_fields(ctx.db, row.id, remoto_bruto=moved)
        try:
            await ctx.drive.mover(info, usados)
        except DriveError:
            pass  # a source without info.json, or a retry after it already moved
        return
    if row.remoto_bruto:
        await ctx.drive.apagar(row.remoto_bruto)
        await update_fields(ctx.db, row.id, remoto_bruto=None)
    await ctx.drive.apagar(info)


async def archive_video(ctx: Ctx, row: VideoRow) -> State:
    """`row` is in arquivando: final to 3-publicados/<day>/<id>.mp4, original out of the Drive, local files gone."""
    try:
        await _archive(ctx, row)
    except Exception as e:
        await _fail(ctx, row.id, f"falha ao arquivar no Drive: {e}")
        return State.erro
    return State.arquivado


async def _archive(ctx: Ctx, row: VideoRow) -> None:
    await ctx.bus.publish("video.estado", {"id": row.id, "estado": State.arquivando.value})
    name = f"{row.id}.mp4"
    local = _final_file(row)
    remoto = row.remoto_final
    if local is None and remoto is None:
        raise RuntimeError(f"o arquivo final não está no disco: {row.caminho_final}")
    if remoto is None:
        # recorded before the upload: after a crash, the retry knows the file may already be in the Drive
        remoto = pasta(row.canal, PUBLICADOS, datetime.now(SP).strftime("%Y-%m-%d"), name)
        await update_fields(ctx.db, row.id, remoto_final=remoto)
    if local is not None:  # no local file and remoto_final recorded: it went up before the crash
        if local.name != name:  # the Drive names finals <id>.mp4, whatever the edit stage called the file
            local = local.replace(local.with_name(name))
            await update_fields(ctx.db, row.id, caminho_final=str(local))
        await ctx.drive.enviar(local, remoto.rsplit("/", 1)[0])
    await _drop_original(ctx, row)
    shutil.rmtree(ctx.data / "editados" / str(row.id), ignore_errors=True)
    if row.caminho_local:
        Path(row.caminho_local).unlink(missing_ok=True)
    await update_fields(ctx.db, row.id, caminho_local=None, caminho_final=None)
    await transition(ctx.db, row.id, State.arquivado)
    await ctx.bus.publish("video.estado", {"id": row.id, "estado": State.arquivado.value})
    await ctx.bus.log("publicacao", f"[{row.canal}] arquivado no Drive: {remoto}", video_id=row.id)


async def archive_worker(ctx: Ctx) -> None:
    while not ctx.stop.is_set():
        row = None
        try:
            row = await claim(ctx.db, State.publicado, State.arquivando, channels=ctx.channels_for("publicacao"))
        except Exception as e:  # keep the worker alive
            await _log_safely(ctx, f"erro ao procurar vídeo para arquivar: {e}")
        if row is None:
            await _idle(ctx, ARCHIVE_IDLE_SLEEP)
            continue
        await archive_video(ctx, row)
