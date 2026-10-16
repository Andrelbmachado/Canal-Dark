"""Carrega canais/*.yaml e o catálogo de sons."""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from .models import ChannelConfig, Sound


def load_channels(directory: Path) -> dict[str, ChannelConfig]:
    channels: dict[str, ChannelConfig] = {}
    for path in sorted(directory.glob("*.yaml")):
        try:
            cfg = ChannelConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except ValidationError as e:
            raise ValueError(f"{path.name}: {e}") from e
        channels[cfg.id] = cfg
    return channels


def load_sounds(path: Path) -> list[Sound]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return [Sound.model_validate(item) for item in data]
