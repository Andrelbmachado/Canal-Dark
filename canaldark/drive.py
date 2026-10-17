"""Google Drive: protocolo comum e nomes das pastas; as implementações (pasta local e rclone) vêm abaixo."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from .models import DriveConfig

# ---- Protocolo (Task 1, congelado) -----------------------------------------

CAPTADOS, EM_EDICAO, PUBLICADOS, USADOS = "1-captados", "2-em-edicao", "3-publicados", "4-originais-usados"


class DriveError(Exception):
    """Falha ao falar com o Drive; a mensagem já vem em português para o log e o painel."""


@dataclass
class DriveUsage:
    usado: int
    total: int | None

    @property
    def fracao(self) -> float | None:
        return self.usado / self.total if self.total else None


@runtime_checkable
class Drive(Protocol):
    async def enviar(self, local: Path, pasta: str) -> str:
        """Move o arquivo para `pasta`, sobrescrevendo no destino; apaga o local só depois do sucesso.

        Devolve "pasta/<nome>"."""
        ...

    async def baixar(self, remoto: str, pasta_local: Path) -> Path:
        """Copia `remoto` para `pasta_local/<nome>` e devolve esse caminho; o remoto continua no Drive."""
        ...

    async def mover(self, remoto: str, pasta: str) -> str:
        """Move `remoto` para `pasta` dentro do Drive e devolve "pasta/<nome>"."""
        ...

    async def apagar(self, remoto: str) -> None:
        """Apaga `remoto`; arquivo ausente não é erro."""
        ...

    async def uso(self) -> DriveUsage | None:
        ...


def pasta(canal: str, sub: str, *extra: str) -> str:
    return "/".join((canal, sub, *extra))


# ---- Implementações (Task 3) ------------------------------------------------

Runner = Callable[[list[str]], Awaitable[tuple[int, str, str]]]  # argv → (código de saída, stdout, stderr)
RCLONE_MISSING = "rclone não encontrado: instale com `brew install rclone` e rode `rclone config`"
RCLONE_NOT_FOUND = (3, 4)  # códigos de saída do rclone para pasta ou arquivo inexistente


def _parts(path: str) -> list[str]:
    return [p for p in path.split("/") if p]


def _join(pasta: str, nome: str) -> str:
    return "/".join([*_parts(pasta), nome])


def _name(remoto: str) -> str:
    parts = _parts(remoto)
    if not parts:
        raise DriveError(f"caminho vazio no Drive: {remoto!r}")
    return parts[-1]


def _move(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(src, dest)  # mesma partição: rename que sobrescreve; outra: copia e só então apaga a origem


def _copy(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)


def _tree_size(root: Path) -> int:
    total = 0
    for folder, _, names in os.walk(root):
        for name in names:
            with contextlib.suppress(OSError):  # apagado ou movido durante a contagem
                total += os.stat(os.path.join(folder, name)).st_size
    return total


async def _in_thread(what: str, fn: Callable, *args) -> None:
    try:
        await asyncio.to_thread(fn, *args)
    except OSError as e:
        raise DriveError(f"{what}: {e}") from e


class PastaDrive:
    """Drive numa pasta do computador (o padrão, e o dos testes): o remoto "a/b/c.mp4" é root/a/b/c.mp4."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, remoto: str) -> Path:
        return self.root.joinpath(*_parts(remoto))

    def _existing(self, remoto: str) -> Path:
        path = self._path(remoto)
        if not path.is_file():
            raise DriveError(f"arquivo não encontrado no Drive: {remoto}")
        return path

    async def enviar(self, local: Path, pasta: str) -> str:
        local = Path(local)
        if not local.is_file():
            raise DriveError(f"arquivo local não encontrado: {local}")
        remoto = _join(pasta, local.name)
        await _in_thread(f"não consegui enviar {local.name} para {pasta}", _move, local, self._path(remoto))
        return remoto

    async def baixar(self, remoto: str, pasta_local: Path) -> Path:
        src = self._existing(remoto)
        dest = Path(pasta_local) / src.name
        await _in_thread(f"não consegui baixar {remoto}", _copy, src, dest)
        return dest

    async def mover(self, remoto: str, pasta: str) -> str:
        src = self._existing(remoto)
        dest = _join(pasta, src.name)
        await _in_thread(f"não consegui mover {remoto} para {pasta}", _move, src, self._path(dest))
        return dest

    async def apagar(self, remoto: str) -> None:
        try:
            self._path(remoto).unlink(missing_ok=True)
        except OSError as e:
            raise DriveError(f"não consegui apagar {remoto}: {e}") from e

    async def uso(self) -> DriveUsage:
        return DriveUsage(await asyncio.to_thread(_tree_size, self.root), None)  # uma pasta não tem cota


async def run_process(argv: list[str]) -> tuple[int, str, str]:
    """Runner padrão do rclone: roda o comando e devolve (código de saída, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await proc.communicate()
    except asyncio.CancelledError:  # a plataforma está parando: não deixa um rclone órfão mexendo no Drive
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


class RcloneDrive:
    """Google Drive pelo rclone (client OAuth do próprio usuário): o remoto "a/b.mp4" é <remote><base>/a/b.mp4."""

    def __init__(self, remote: str, base: str, runner: Runner | None = None,
                 delays: tuple[float, ...] = (5, 20, 60)):
        if not remote.endswith(":"):  # sem os dois-pontos o rclone gravaria numa pasta local com esse nome
            raise ValueError(f"remote do rclone inválido: {remote!r}; "
                             'use o nome com os dois-pontos no fim, ex.: "gdrive:"')
        self.remote, self.base = remote, base
        self.runner = runner or run_process
        self.delays = delays

    def _target(self, remoto: str) -> str:
        return self.remote + "/".join([*_parts(self.base), *_parts(remoto)])

    async def _rclone(self, *args: str, ok: tuple[int, ...] = (0,)) -> str:
        """Roda o rclone até len(delays) vezes, esperando delays[i] depois da falha i; devolve o stdout."""
        attempts = max(1, len(self.delays))
        for attempt in range(attempts):
            try:
                code, out, err = await self.runner(["rclone", *args])
            except FileNotFoundError:
                raise DriveError(RCLONE_MISSING) from None
            if code in ok:
                return out
            if attempt < attempts - 1:
                await asyncio.sleep(self.delays[attempt])
        detail = err.strip()[-300:] or "sem detalhes"
        raise DriveError(f"rclone {args[0]} falhou (código {code}) depois de {attempts} tentativa(s): {detail}")

    async def enviar(self, local: Path, pasta: str) -> str:
        local = Path(local)
        if not local.is_file():
            raise DriveError(f"arquivo local não encontrado: {local}")
        remoto = _join(pasta, local.name)
        await self._rclone("moveto", str(local), self._target(remoto))  # o rclone só apaga o local depois do envio
        return remoto

    async def baixar(self, remoto: str, pasta_local: Path) -> Path:
        dest = Path(pasta_local) / _name(remoto)
        await self._rclone("copyto", self._target(remoto), str(dest))
        return dest

    async def mover(self, remoto: str, pasta: str) -> str:
        dest = _join(pasta, _name(remoto))
        await self._rclone("moveto", self._target(remoto), self._target(dest))
        return dest

    async def apagar(self, remoto: str) -> None:
        await self._rclone("deletefile", self._target(remoto), ok=(0, *RCLONE_NOT_FOUND))

    async def uso(self) -> DriveUsage:
        out = await self._rclone("about", self.remote, "--json")
        try:
            data = json.loads(out)
            total, free = data.get("total"), data.get("free")
            # A cota grátis do Google é dividida com o Gmail e o Fotos: o usado é tudo o que não está livre.
            used = total - free if total is not None and free is not None else data.get("used", 0)
            return DriveUsage(int(used), int(total) if total is not None else None)
        except (ValueError, TypeError, AttributeError) as e:
            raise DriveError(f"resposta inesperada do rclone about: {out.strip()[:200]}") from e


def make_drive(cfg: DriveConfig, root: Path) -> Drive:
    """O Drive da plataforma.yaml: "pasta" usa <raiz>/<pasta_local>/<pasta>; "rclone" usa <remote><pasta>."""
    if cfg.tipo == "rclone":
        return RcloneDrive(cfg.remote, cfg.pasta)
    return PastaDrive(Path(root) / Path(cfg.pasta_local).expanduser() / cfg.pasta)
