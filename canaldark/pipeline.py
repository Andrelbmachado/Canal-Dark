"""Runs the three stages together: capture scheduler, workers, pause/resume and crash recovery."""
import asyncio
from datetime import datetime
from pathlib import Path

from canaldark.capture import stage as capture_stage
from canaldark.db import now_iso
from canaldark.edit import stage as edit
from canaldark.events import EventBus
from canaldark.models import ChannelConfig, Sound, SourceSpec, State
from canaldark.publish import stage as publish
from canaldark.publish.base import Uploader
from canaldark.rules import SP

STAGES = ("captacao", "edicao", "publicacao")
CONCURRENCY = {"download": 2, "render": 1, "upload": 1}
RECOVERY = {State.baixando: State.descoberto, State.editando: State.baixado, State.publicando: State.editado}


async def recover(db) -> int:
    total = 0
    for stuck, back in RECOVERY.items():
        cur = await db.execute("UPDATE videos SET estado = ?, etapa_progresso = 0, atualizado_em = ? WHERE estado = ?",
                               (back.value, now_iso(), stuck.value))
        total += cur.rowcount
    await db.commit()
    return total


def real_uploaders(dry_run: bool, out_dir: Path):
    cache: dict[tuple[str, str], Uploader] = {}

    def get(platform: str, account: str) -> Uploader | None:
        key = (platform, account)
        if key not in cache:
            if dry_run:
                cache[key] = publish.DryRunUploader(platform, out_dir)
            elif platform == "youtube":
                from canaldark.publish.youtube import YouTubeUploader
                cache[key] = YouTubeUploader(account)
            elif platform == "instagram":
                from canaldark.publish.instagram import InstagramUploader
                cache[key] = InstagramUploader(account)
            else:
                return None
        return cache[key]
    return get


class Pipeline:
    def __init__(self, db, bus: EventBus, cfgs: dict[str, ChannelConfig], sounds: list[Sound], *,
                 root: Path = Path("."), dry_run: bool = False, get_uploader=None):
        self.db, self.bus, self.cfgs, self.sounds = db, bus, cfgs, sounds
        self.root, self.dry_run = root, dry_run
        self.data = root / "dados"
        self.get_uploader = get_uploader or real_uploaders(dry_run, self.data / "ensaio")
        self.paused: dict[tuple[str | None, str | None], datetime | None] = {}
        self.paused_accounts: set[tuple[str, str]] = set()
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    # ---- pause / resume ----------------------------------------------------

    def pause(self, channel: str | None = None, stage: str | None = None, until: datetime | None = None) -> None:
        self.paused[(channel, stage)] = until

    def resume(self, channel: str | None = None, stage: str | None = None) -> None:
        if channel is None and stage is None:
            self.paused.clear()
            self.paused_accounts.clear()
            return
        self.paused.pop((channel, stage), None)

    def is_paused(self, channel: str, stage: str) -> bool:
        now = datetime.now(SP)
        for (c, s), until in list(self.paused.items()):
            if until is not None and until <= now:
                del self.paused[(c, s)]
                continue
            if c in (None, channel) and s in (None, stage):
                return True
        return False

    def active(self, stage: str) -> list[str]:
        return [c for c in self.cfgs if not self.is_paused(c, stage)]

    # ---- capture -----------------------------------------------------------

    async def capture_now(self, channel: str, query: str | None = None) -> int:
        cfg = self.cfgs[channel]
        if query:
            cfg = cfg.model_copy(update={"fontes": [SourceSpec(tipo="google_news", busca=query)]})
        await self.bus.log("captacao", f"[{channel}] buscando agora{f': {query}' if query else ''}")
        return await capture_stage.capture_cycle(self.db, self.bus, cfg)

    async def _capture_loop(self, channel: str) -> None:
        cfg = self.cfgs[channel]
        while not self._stop.is_set():
            if not self.is_paused(channel, "captacao"):
                try:
                    await capture_stage.capture_cycle(self.db, self.bus, cfg)
                except Exception as e:  # keep the scheduler alive
                    await self.bus.log("captacao", f"[{channel}] erro na captação: {e}", "erro")
            try:
                await asyncio.wait_for(self._stop.wait(), cfg.intervalo_captacao_min * 60)
            except asyncio.TimeoutError:
                pass

    # ---- lifecycle ---------------------------------------------------------

    async def start(self, capture: bool = True) -> None:
        recovered = await recover(self.db)
        if recovered:
            await self.bus.log("sistema", f"{recovered} vídeo(s) retomados de onde pararam")
        self._stop.clear()
        spawn = self._tasks.append
        if capture:
            for c in self.cfgs:
                spawn(asyncio.create_task(self._capture_loop(c)))
        for _ in range(CONCURRENCY["download"]):
            spawn(asyncio.create_task(capture_stage.download_worker(
                self.db, self.bus, self.cfgs, self.data / "brutos", self._stop, lambda: self.active("captacao"))))
        for _ in range(CONCURRENCY["render"]):
            spawn(asyncio.create_task(edit.edit_worker(
                self.db, self.bus, self.cfgs, self.sounds, self.data / "editados", self._stop,
                lambda: self.active("edicao"))))
        for _ in range(CONCURRENCY["upload"]):
            spawn(asyncio.create_task(publish.publish_worker(
                self.db, self.bus, self.cfgs, self.get_uploader, self.paused_accounts, self._stop,
                lambda: self.active("publicacao"))))
        mode = " (ensaio: nada é publicado de verdade)" if self.dry_run else ""
        await self.bus.log("sistema", f"pipeline iniciado com {len(self.cfgs)} canal(is){mode}")

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

