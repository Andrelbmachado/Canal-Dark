"""Capture stage: read sources, triage with the brain, insert the best items, download videos."""
import asyncio
from collections.abc import Callable
from datetime import datetime, time as dtime, timezone
from pathlib import Path

import httpx

from canaldark.brain import BrainError, triage
from canaldark.capture.download import DownloadFailed, download
from canaldark.capture.sources import read_source
from canaldark.db import claim, insert_video, transition, update_fields
from canaldark.models import ChannelConfig, SourceItem, State
from canaldark.rules import SP

DOWNLOAD_DIR = Path("dados/brutos")
IDLE_SLEEP = 5


async def _today_count(db, channel: str) -> int:
    start = datetime.combine(datetime.now(SP).date(), dtime(0), tzinfo=SP).astimezone(timezone.utc).isoformat(timespec="seconds")
    async with db.execute("SELECT COUNT(*) FROM videos WHERE canal = ? AND criado_em >= ?",
                          (channel, start)) as cur:
        return (await cur.fetchone())[0]


async def capture_cycle(db, bus, cfg: ChannelConfig) -> int:
    items: list[SourceItem] = []
    async with httpx.AsyncClient() as client:
        for spec in cfg.fontes:
            try:
                items.extend(await read_source(spec, client))
            except Exception as e:  # one broken feed must not stop the others
                await bus.log("captacao", f"[{cfg.id}] fonte {spec.tipo} falhou: {e}", "aviso")
    if not items:
        await bus.log("captacao", f"[{cfg.id}] nenhuma novidade nas fontes")
        return 0
    try:
        results = await triage(cfg, items)
    except BrainError as e:
        await bus.log("captacao", f"[{cfg.id}] triagem indisponível: {e}", "erro")
        return 0

    by_url = {i.url: i for i in items}
    best: dict[str, tuple[int, SourceItem]] = {}
    for r in results:
        item = by_url.get(r.url)
        if item is None or r.nota < cfg.nota_minima:
            continue
        if r.grupo not in best or r.nota > best[r.grupo][0]:
            best[r.grupo] = (r.nota, item)

    room = cfg.max_por_dia - await _today_count(db, cfg.id)
    inserted = 0
    for nota, item in sorted(best.values(), key=lambda t: -t[0]):
        if inserted >= room:
            break
        if item.video_url:
            vid = await insert_video(db, cfg.id, item.video_url, item.title, item.source_name, nota=nota)
        else:
            vid = await insert_video(db, cfg.id, item.url, item.title, item.source_name,
                                     state=State.baixado, formato="dialogo", nota=nota)
        if vid is None:
            continue
        inserted += 1
        await bus.publish("video.estado", {"id": vid, "estado": "descoberto" if item.video_url else "baixado"})
        await bus.log("captacao", f"[{cfg.id}] nova pauta (nota {nota}): {item.title}", video_id=vid)
    return inserted


async def download_worker(db, bus, cfgs: dict[str, ChannelConfig], out_dir: Path = DOWNLOAD_DIR,
                          stop: asyncio.Event | None = None,
                          active: Callable[[], list[str]] | None = None) -> None:
    while stop is None or not stop.is_set():
        row = await claim(db, State.descoberto, State.baixando, channels=active() if active else list(cfgs))
        if row is None:
            await asyncio.sleep(IDLE_SLEEP)
            continue
        await bus.publish("video.estado", {"id": row.id, "estado": "baixando"})
        cfg = cfgs[row.canal]

        async def on_progress(pct: float, vid=row.id) -> None:
            await bus.progress(vid, pct)

        try:
            path = await download(row.url_origem, out_dir / row.canal, on_progress, cfg.cookies_navegador)
        except DownloadFailed as e:
            await transition(db, row.id, State.erro, error=str(e))
            await bus.publish("video.estado", {"id": row.id, "estado": "erro", "erro": str(e)})
            await bus.log("captacao", f"download falhou: {e}", "erro", row.id)
            continue
        await update_fields(db, row.id, caminho_bruto=str(path))
        await transition(db, row.id, State.baixado)
        await bus.publish("video.estado", {"id": row.id, "estado": "baixado"})
        await bus.log("captacao", f"baixado: {path.name}", video_id=row.id)
