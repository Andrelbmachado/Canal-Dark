"""Barramento de eventos em memória + log persistente na tabela eventos."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Literal

import aiosqlite

from .db import now_iso

Stage = Literal["captacao", "edicao", "publicacao", "sistema", "chat"]
Level = Literal["info", "aviso", "erro"]
PROGRESS_INTERVAL = 0.25
PERSISTED = {"log", "video.estado", "chat.mensagem"}


class Subscription:
    def __init__(self, bus: "EventBus", size: int = 1000):
        self._bus = bus
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=size)

    def put(self, event: dict) -> None:
        if self.queue.full():
            self.queue.get_nowait()
        self.queue.put_nowait(event)

    def empty(self) -> bool:
        return self.queue.empty()

    def close(self) -> None:
        self._bus._subs.discard(self)

    def __aiter__(self):
        return self

    async def __anext__(self) -> dict:
        return await self.queue.get()


class EventBus:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db
        self._subs: set[Subscription] = set()
        self._last_progress: dict[int, float] = {}

    def subscribe(self) -> Subscription:
        sub = Subscription(self)
        self._subs.add(sub)
        return sub

    async def publish(self, type: str, data: dict) -> None:
        event = {"type": type, "data": data, "ts": now_iso()}
        if type in PERSISTED:
            await self.db.execute(
                "INSERT INTO eventos (tipo, dados, criado_em) VALUES (?, ?, ?)",
                (type, json.dumps(data, ensure_ascii=False), event["ts"]),
            )
            await self.db.commit()
        for sub in list(self._subs):
            sub.put(event)

    async def log(self, stage: Stage, msg: str, level: Level = "info", video_id: int | None = None) -> None:
        await self.publish("log", {"stage": stage, "msg": msg, "level": level, "video_id": video_id})

    async def progress(self, video_id: int, pct: float) -> None:
        now = time.monotonic()
        last = self._last_progress.get(video_id)
        if pct < 100 and last is not None and now - last < PROGRESS_INTERVAL:
            return
        self._last_progress[video_id] = now
        if pct >= 100:
            self._last_progress.pop(video_id, None)
        await self.publish("video.progresso", {"id": video_id, "pct": round(pct, 1)})

    async def recent_logs(self, limit: int = 200) -> list[dict]:
        async with self.db.execute(
            "SELECT dados, criado_em FROM eventos WHERE tipo = 'log' ORDER BY id DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
        return [{"type": "log", "data": json.loads(r["dados"]), "ts": r["criado_em"]} for r in reversed(rows)]
