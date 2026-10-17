"""Drive falso em memória para os testes: guarda os arquivos num dict e falha sob encomenda."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from canaldark.drive import DriveError, DriveUsage


def _name(remoto: str) -> str:
    return remoto.rsplit("/", 1)[-1]


def _join(pasta: str, nome: str) -> str:
    return "/".join([*(p for p in pasta.split("/") if p), nome])


@dataclass
class FakeDrive:
    files: dict[str, bytes] = field(default_factory=dict)  # "teste/1-captados/7.mp4" → conteúdo
    fail: dict[str, Exception] = field(default_factory=dict)  # nome do método → exceção lançada uma vez
    usage: DriveUsage | None = field(default_factory=lambda: DriveUsage(0, 15 * 1024**3))
    calls: list[tuple] = field(default_factory=list)  # (método, *argumentos) de cada chamada, em ordem

    def _call(self, method: str, *args) -> None:
        self.calls.append((method, *args))
        exc = self.fail.pop(method, None)
        if exc is not None:
            raise exc

    async def enviar(self, local: Path, pasta: str) -> str:
        local = Path(local)
        self._call("enviar", local, pasta)
        if not local.is_file():
            raise DriveError(f"arquivo local não encontrado: {local}")
        remoto = _join(pasta, local.name)
        self.files[remoto] = local.read_bytes()
        local.unlink()
        return remoto

    async def baixar(self, remoto: str, pasta_local: Path) -> Path:
        pasta_local = Path(pasta_local)
        self._call("baixar", remoto, pasta_local)
        if remoto not in self.files:
            raise DriveError(f"arquivo não encontrado no Drive: {remoto}")
        pasta_local.mkdir(parents=True, exist_ok=True)
        dest = pasta_local / _name(remoto)
        dest.write_bytes(self.files[remoto])
        return dest

    async def mover(self, remoto: str, pasta: str) -> str:
        self._call("mover", remoto, pasta)
        if remoto not in self.files:
            raise DriveError(f"arquivo não encontrado no Drive: {remoto}")
        dest = _join(pasta, _name(remoto))
        self.files[dest] = self.files.pop(remoto)
        return dest

    async def apagar(self, remoto: str) -> None:
        self._call("apagar", remoto)
        self.files.pop(remoto, None)

    async def uso(self) -> DriveUsage | None:
        self._call("uso")
        return self.usage
