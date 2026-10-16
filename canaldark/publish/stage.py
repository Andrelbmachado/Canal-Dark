"""Publish stage: wait for the channel's slot, then upload to every account in `contas`."""
import asyncio
import shutil
from collections.abc import Callable
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

from canaldark.capture.stage import fail_safely
from canaldark.db import claim, now_iso, transition
from canaldark.models import ChannelConfig, Package, State, VideoRow
from canaldark.publish.base import AuthExpired, PublishResult, RetryLater, Uploader
from canaldark.publish.instagram import IG_DAILY_CAP
from canaldark.publish.schedule import next_slot
from canaldark.rules import SP, publish_allowed

IDLE_SLEEP = 15
COOLDOWN_AFTER_LIMIT = timedelta(hours=1)
COOLDOWN: dict[tuple[str, str], datetime] = {}  # (platform, account) → try again after
URGENT_SCORE = 9
AI_LABEL = "Narração com voz sintética (IA)."

GetUploader = Callable[[str, str], Uploader | None]


class DryRunUploader:
    """`--ensaio`: copies the MP4 to a local folder instead of publishing."""

    def __init__(self, platform: str, out_dir: Path = Path("dados/ensaio")):
        self.platform, self.out_dir = platform, out_dir

    async def upload(self, video, title, description, tags, on_progress, synthetic=False) -> PublishResult:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        dest = self.out_dir / f"{self.platform}-{video.parent.name}-{video.name}"
        await asyncio.to_thread(shutil.copyfile, video, dest)
        dest.with_suffix(".txt").write_text(f"{title}\n\n{description}\n\n{' '.join(tags)}", encoding="utf-8")
        await on_progress(100)
        return PublishResult(id_externo="ensaio", url=f"file://{dest.resolve()}")


def _day_start_utc(now: datetime) -> str:
    start = datetime.combine(now.astimezone(SP).date(), dtime(0), tzinfo=SP)
    return start.astimezone(timezone.utc).isoformat(timespec="seconds")


async def _channel_stats(db, channel: str, now: datetime) -> tuple[int, datetime | None]:
    async with db.execute(
        "SELECT COUNT(DISTINCT p.video_id) FROM publicacoes p JOIN videos v ON v.id = p.video_id"
        " WHERE v.canal = ? AND p.publicado_em IS NOT NULL AND p.publicado_em >= ?",
        (channel, _day_start_utc(now))) as cur:
        (count,) = await cur.fetchone()
    async with db.execute(
        "SELECT MAX(p.publicado_em) FROM publicacoes p JOIN videos v ON v.id = p.video_id WHERE v.canal = ?",
        (channel,)) as cur:
        (last,) = await cur.fetchone()
    return count, datetime.fromisoformat(last) if last else None


def _blocked(platform: str, account: str, paused: set) -> bool:
    until = COOLDOWN.get((platform, account))
    if until is not None and until <= datetime.now(timezone.utc):
        COOLDOWN.pop((platform, account), None)
        until = None
    return (platform, account) in paused or until is not None


async def _workable(db, cfg: ChannelConfig, video_id: int, paused: set) -> bool:
    """True when at least one platform still pending for this video can be tried now."""
    async with db.execute("SELECT plataforma FROM publicacoes WHERE video_id = ? AND id_externo IS NOT NULL",
                          (video_id,)) as cur:
        done = {r[0] for r in await cur.fetchall()}
    for platform, account in cfg.contas.items():
        if platform in done or _blocked(platform, account, paused):
            continue
        if platform == "instagram" and await _ig_count_24h(db, account) >= IG_DAILY_CAP:
            continue
        return True
    return False


async def claim_due(db, cfgs: dict[str, ChannelConfig], now: datetime, *, force: bool = False,
                    video_id: int | None = None, paused: set | None = None) -> VideoRow | None:
    paused = paused or set()
    for cfg in cfgs.values():
        if not publish_allowed(cfg, now):
            continue
        sql = "SELECT id, nota FROM videos WHERE canal = ? AND estado = 'editado'"
        params: list = [cfg.id]
        if video_id is not None:
            sql += " AND id = ?"
            params.append(video_id)
        async with db.execute(sql + " ORDER BY COALESCE(nota, 0) DESC, id", params) as cur:
            candidates = await cur.fetchall()
        cand = None
        for c in candidates:
            if await _workable(db, cfg, c["id"], paused):
                cand = c
                break
        if cand is None:
            continue
        if not force:
            count, last = await _channel_stats(db, cfg.id, now)
            slot = next_slot(cfg, now, count, (cand["nota"] or 0) >= URGENT_SCORE, last)
            if slot is None or slot > now:
                continue
        row = await claim(db, State.editado, State.publicando, video_id=cand["id"])
        if row is not None:
            return row
    return None


async def _ig_count_24h(db, account: str) -> int:
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    async with db.execute("SELECT COUNT(*) FROM publicacoes WHERE plataforma = 'instagram' AND conta = ?"
                          " AND publicado_em >= ?", (account, since)) as cur:
        return (await cur.fetchone())[0]


def _description(pkg: Package, cfg: ChannelConfig) -> str:
    parts = [pkg.descricao_post.strip(), f"Fonte: {pkg.fonte}"]
    if pkg.formato in ("noticia", "dialogo") or cfg.regras == "politica":
        parts.append(AI_LABEL)
    if pkg.hashtags:
        parts.append(" ".join(h if h.startswith("#") else f"#{h}" for h in pkg.hashtags))
    return "\n\n".join(p for p in parts if p)


async def publish_video(db, bus, cfg: ChannelConfig, row: VideoRow, get_uploader: GetUploader,
                        paused: set | None = None) -> State:
    paused = paused if paused is not None else set()
    await bus.publish("video.estado", {"id": row.id, "estado": "publicando"})
    pkg = Package.model_validate_json(row.pacote_json)
    video = Path(row.caminho_final)
    async with db.execute("SELECT plataforma FROM publicacoes WHERE video_id = ? AND id_externo IS NOT NULL",
                          (row.id,)) as cur:
        done = {r[0] for r in await cur.fetchall()}
    pending = False
    platforms = list(cfg.contas.items())
    for n, (platform, account) in enumerate(platforms):
        if platform in done:
            continue
        if _blocked(platform, account, paused):
            pending = True
            continue
        if platform == "instagram" and await _ig_count_24h(db, account) >= IG_DAILY_CAP:
            await bus.log("publicacao", f"Instagram {account}: limite de {IG_DAILY_CAP} posts em 24 h; fica para depois",
                          "aviso", row.id)
            pending = True
            continue
        uploader = get_uploader(platform, account)
        if uploader is None:
            await bus.log("publicacao", f"sem uploader para {platform}:{account}", "erro", row.id)
            pending = True
            continue

        async def on_progress(p: float, n=n) -> None:
            await bus.progress(row.id, (n + p / 100) / len(platforms) * 100)

        try:
            res = await uploader.upload(video, pkg.titulo_post, _description(pkg, cfg), pkg.hashtags, on_progress,
                                        synthetic=pkg.sintetico_realista)
        except AuthExpired as e:
            paused.add((platform, account))
            await bus.log("publicacao", f"{e} — reconectar com `canaldark auth {platform} {account}`", "erro", row.id)
            pending = True
            continue
        except RetryLater as e:
            COOLDOWN[(platform, account)] = datetime.now(timezone.utc) + COOLDOWN_AFTER_LIMIT
            await bus.log("publicacao", f"{e}; tento de novo em 1 h", "aviso", row.id)
            pending = True
            continue
        except Exception as e:
            await fail_safely(db, bus, row.id, "publicacao", f"falha ao publicar no {platform}: {e}")
            return State.erro
        await db.execute(
            "INSERT INTO publicacoes (video_id, plataforma, conta, id_externo, url, publicado_em) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(video_id, plataforma) DO UPDATE SET id_externo = excluded.id_externo, url = excluded.url,"
            " publicado_em = excluded.publicado_em",
            (row.id, platform, account, res.id_externo, res.url, now_iso()))
        await db.commit()
        await bus.log("publicacao", f"publicado no {platform}: {res.url}", video_id=row.id)

    final = State.editado if pending else State.publicado
    await transition(db, row.id, final)
    await bus.publish("video.estado", {"id": row.id, "estado": final.value})
    return final


async def publish_guarded(db, bus, cfg: ChannelConfig, row: VideoRow, get_uploader: GetUploader,
                          paused: set | None = None) -> None:
    try:
        await publish_video(db, bus, cfg, row, get_uploader, paused)
    except Exception as e:  # never let one video stop the queue
        await fail_safely(db, bus, row.id, "publicacao", f"erro inesperado na publicação: {e}")


async def publish_worker(db, bus, cfgs: dict[str, ChannelConfig], get_uploader: GetUploader,
                         paused: set | None = None, stop: asyncio.Event | None = None,
                         active: Callable[[], list[str]] | None = None) -> None:
    paused = paused if paused is not None else set()
    while stop is None or not stop.is_set():
        chosen = {c: cfgs[c] for c in active()} if active else cfgs
        row = await claim_due(db, chosen, datetime.now(SP), paused=paused)
        if row is None:
            await asyncio.sleep(IDLE_SLEEP)
            continue
        await publish_guarded(db, bus, cfgs[row.canal], row, get_uploader, paused)
