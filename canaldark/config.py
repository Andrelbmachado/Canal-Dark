"""Carrega plataforma.yaml, canais/, estilos/ e memes/ e reescreve as fontes de um canal."""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError

from .models import ChannelConfig, MemeTemplate, PlatformConfig, SourceSpec, StylePreset

_SOURCES_KEY = re.compile(r"fontes\s*:")
_LIST_ITEM = re.compile(r"-(\s|$)")


def _read(path: Path):
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ValueError(f"{path.name}: YAML inválido: {e}") from e


def _validate(model: type[BaseModel], data, path: Path):
    try:
        return model.model_validate(data)
    except ValidationError as e:
        raise ValueError(f"{path.name}: {e}") from e


def _load_channel(path: Path) -> ChannelConfig:
    cfg = _validate(ChannelConfig, _read(path), path)
    if cfg.id != path.stem:
        raise ValueError(f"{path.name}: o id '{cfg.id}' precisa ser igual ao nome do arquivo ('{path.stem}')")
    return cfg


def load_channels(directory: Path) -> dict[str, ChannelConfig]:
    return {path.stem: _load_channel(path) for path in sorted(directory.glob("*.yaml"))}


def load_platform(path: Path) -> PlatformConfig:
    if not path.exists():
        return PlatformConfig()
    return _validate(PlatformConfig, _read(path) or {}, path)


def _load_named(model: type[BaseModel], directory: Path) -> dict:
    out = {}
    for path in sorted(directory.glob("*.yaml")):
        data = _read(path) or {}
        out[path.stem] = _validate(model, {**data, "nome": path.stem} if isinstance(data, dict) else data, path)
    return out


def load_styles(directory: Path) -> dict[str, StylePreset]:
    return _load_named(StylePreset, directory)


def load_memes(directory: Path) -> dict[str, MemeTemplate]:
    return _load_named(MemeTemplate, directory)


def _ends_block(line: str) -> bool:
    """Linha no nível 0 que não é comentário, vazia nem item de lista (o safe_dump escreve os itens no nível 0)."""
    return bool(line.strip()) and not line[0].isspace() and not line.startswith("#") and not _LIST_ITEM.match(line)


def _sources_block(lines: list[str]) -> tuple[int, int] | None:
    start = next((i for i, line in enumerate(lines) if _SOURCES_KEY.match(line)), None)
    if start is None:
        return None
    end = start + 1
    while end < len(lines) and not _ends_block(lines[end]):
        end += 1
    # linhas vazias e comentários no nível 0 logo antes da próxima chave pertencem a ela
    while end > start + 1 and (not lines[end - 1].strip() or lines[end - 1].startswith("#")):
        end -= 1
    return start, end


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def replace_sources(path: Path, fontes: list[SourceSpec]) -> ChannelConfig:
    """Reescreve só o bloco `fontes:` do YAML do canal, mantendo o resto do texto e os comentários."""
    original = path.read_text(encoding="utf-8")
    lines = [line + "\n" for line in original.split("\n")]
    lines[-1] = lines[-1][:-1]
    block = _sources_block(lines)
    if block is None:
        raise ValueError(f"{path.name}: bloco 'fontes:' não encontrado")
    start, end = block
    dump = yaml.safe_dump({"fontes": [f.model_dump(mode="json", exclude_defaults=True) for f in fontes]},
                          allow_unicode=True, sort_keys=False)
    _write(path, "".join(lines[:start]) + dump + "".join(lines[end:]))
    try:
        return _load_channel(path)
    except Exception:
        _write(path, original)
        raise
