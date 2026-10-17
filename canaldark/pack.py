"""Pacotes por canal: separa os próximos vídeos do Drive (15 por vez) e os baixa para a edição."""
from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path, PurePosixPath

import aiosqlite

from .context import Ctx
from .db import PACK_STATES, claim, count_states, transition, update_fields
from .drive import EM_EDICAO, DriveError, pasta
from .models import State, VideoRow

IDLE_SLEEP = 3


async def pack_in_progress(db: aiosqlite.Connection, channel: str) -> int:
    """Vídeos do canal que ainda ocupam o pacote atual (erro e descartado contam como terminados)."""
    return await count_states(db, channel, PACK_STATES)


async def start_next_pack(ctx: Ctx, channel: str) -> list[int]:
    """Quando o pacote do canal acabou, separa os próximos: maior nota primeiro, depois os mais antigos."""
    if await pack_in_progress(ctx.db, channel) > 0:
        return []
    async with ctx.db.execute("SELECT MAX(pacote) FROM videos WHERE canal = ?", (channel,)) as cur:
        number = ((await cur.fetchone())[0] or 0) + 1
    picked: list[int] = []
    while len(picked) < ctx.plat.pacote:
        row = await claim(ctx.db, State.no_drive, State.separado, channels=[channel], order="nota DESC, id")
        if row is None:
            break
        await update_fields(ctx.db, row.id, pacote=number)
        picked.append(row.id)
        await ctx.bus.publish("video.estado", {"id": row.id, "estado": State.separado.value, "pacote": number})
    if picked:
        await ctx.bus.log("edicao", f"[{channel}] pacote {number}: {len(picked)} vídeo(s)")
    return picked


async def pack_tick(ctx: Ctx) -> int:
    """Tenta começar o próximo pacote em cada canal ativo na edição; devolve quantos vídeos foram separados."""
    total = 0
    for channel in ctx.channels_for("edicao"):
        total += len(await start_next_pack(ctx, channel))
    return total


async def pack_manager(ctx: Ctx, every: float = 15) -> None:
    """Chama pack_tick a cada `every` segundos até ctx.stop; nunca morre por exceção."""
    while not ctx.stop.is_set():
        try:
            await pack_tick(ctx)
        except Exception as e:  # tenta de novo no próximo ciclo
            await _log_error(ctx, f"erro ao separar o pacote: {e}")
        await _sleep(ctx, every)


async def download_pack_video(ctx: Ctx, row: VideoRow) -> State:
    """Traz do Drive um vídeo separado (`row` já está em descendo); qualquer falha vira erro, nunca relança."""
    try:
        local = await _bring_down(ctx, row)
        await update_fields(ctx.db, row.id, caminho_local=str(local))
        await transition(ctx.db, row.id, State.pronto)
    except Exception as e:
        await _fail(ctx, row.id, f"falha ao baixar do Drive: {e}")
        return State.erro
    await ctx.bus.publish("video.estado", {"id": row.id, "estado": State.pronto.value})
    await ctx.bus.log("edicao", f"desceu do Drive: {local.name}", video_id=row.id)
    return State.pronto


async def pack_download_worker(ctx: Ctx) -> None:
    """Desce do Drive, um por vez, os vídeos separados dos canais ativos na edição."""
    while not ctx.stop.is_set():
        row = await claim(ctx.db, State.separado, State.descendo, channels=ctx.channels_for("edicao"))
        if row is None:
            await _sleep(ctx, IDLE_SLEEP)
            continue
        await ctx.bus.publish("video.estado", {"id": row.id, "estado": State.descendo.value})
        try:
            await download_pack_video(ctx, row)
        except Exception as e:  # um vídeo nunca para a fila
            await _fail(ctx, row.id, f"erro inesperado ao baixar do Drive: {e}")


async def _bring_down(ctx: Ctx, row: VideoRow) -> Path:
    """Move o bruto para 2-em-edicao (gravando o novo caminho logo depois) e o baixa para dados/pacote/<canal>/."""
    if not row.remoto_bruto:
        raise DriveError("o vídeo não tem arquivo no Drive (remoto_bruto vazio)")
    folder = pasta(row.canal, EM_EDICAO)
    remoto, move_error = row.remoto_bruto, None
    if str(PurePosixPath(remoto).parent) != folder:
        try:
            remoto = await ctx.drive.mover(remoto, folder)
            await update_fields(ctx.db, row.id, remoto_bruto=remoto)
        except DriveError as e:  # uma queda entre o mover e o banco deixa o arquivo já em 2-em-edicao
            remoto, move_error = f"{folder}/{PurePosixPath(remoto).name}", e
    try:
        local = await ctx.drive.baixar(remoto, ctx.data / "pacote" / row.canal)
    except Exception:
        if move_error is not None:
            raise move_error from None
        raise
    if move_error is not None:
        await update_fields(ctx.db, row.id, remoto_bruto=remoto)
    return local


async def _fail(ctx: Ctx, video_id: int, msg: str) -> None:
    """Leva o vídeo para erro (a fila de volta é separado) sem nunca relançar."""
    try:
        await transition(ctx.db, video_id, State.erro, error=msg)
        await ctx.bus.publish("video.estado", {"id": video_id, "estado": State.erro.value, "erro": msg})
        await ctx.bus.log("edicao", msg, "erro", video_id)
    except Exception as e:  # o vídeo pode ter mudado de estado no meio
        await _log_error(ctx, f"{msg} (e não consegui marcar o erro: {e})", video_id)


async def _log_error(ctx: Ctx, msg: str, video_id: int | None = None) -> None:
    with contextlib.suppress(Exception):
        await ctx.bus.log("edicao", msg, "erro", video_id)


async def _sleep(ctx: Ctx, seconds: float) -> None:
    """Espera `seconds`, mas volta na hora se a plataforma for parada."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(ctx.stop.wait(), seconds)
