"""Google Drive: protocolo comum e nomes das pastas; as implementações (pasta local e rclone) vêm abaixo."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

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
