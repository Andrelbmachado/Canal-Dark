"""Contexto que todo trabalhador recebe: banco, eventos, configs, Drive e uploaders."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

from .drive import Drive
from .events import EventBus
from .models import ChannelConfig, MemeTemplate, PlatformConfig, StylePreset


@dataclass
class Ctx:
    db: aiosqlite.Connection
    bus: EventBus
    cfgs: dict[str, ChannelConfig]
    plat: PlatformConfig
    drive: Drive
    root: Path
    styles: dict[str, StylePreset] = field(default_factory=dict)
    memes: dict[str, MemeTemplate] = field(default_factory=dict)
    dry_run: bool = False
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    active: Callable[[str], list[str]] | None = None  # etapa → canais não pausados
    get_uploader: Callable[[str, str], Any] | None = None  # (plataforma, conta) → uploader
    keys: dict[str, str] = field(default_factory=dict)  # {"pexels": ..., "pixabay": ...}
    paused_accounts: set[tuple[str, str]] = field(default_factory=set)  # (plataforma, conta) com token vencido
    cooldown: dict[tuple[str, str], datetime] = field(default_factory=dict)  # (plataforma, conta) → tenta após isso

    @property
    def data(self) -> Path:
        return self.root / "dados"

    def channels_for(self, stage: str) -> list[str]:
        return self.active(stage) if self.active else list(self.cfgs)
