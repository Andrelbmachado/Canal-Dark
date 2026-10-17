"""Etapa de captação: busca nas fontes com nota, download e envio ao Drive (pasta 1-captados)."""
from __future__ import annotations

import asyncio
import shutil
from collections.abc import Awaitable, Callable
from datetime import datetime, time as dtime, timezone
from pathlib import Path

import httpx

from canaldark.capture.download import DownloadFailed, download
from canaldark.capture.scoring import passes, score
from canaldark.capture.sources import list_candidates, ytdlp_extractor
from canaldark.capture.trends import trending_terms
from canaldark.context import Ctx
from canaldark.db import claim, count_states, get_video, insert_video, seen, transition, update_fields
from canaldark.drive import CAPTADOS, pasta
from canaldark.models import Candidate, SourceSpec, State, VideoRow
from canaldark.rules import SP

S = State
STOCK_STATES = (S.descoberto, S.baixando, S.baixado, S.enviando, S.no_drive)  # vídeos esperando na aba Captação
IDLE_SLEEP = 3


async def _created_today(db, channel: str, now: datetime) -> int:
    start = datetime.combine(now.astimezone(SP).date(), dtime(0), tzinfo=SP)
    since = start.astimezone(timezone.utc).isoformat(timespec="seconds")
    async with db.execute("SELECT COUNT(*) FROM videos WHERE canal = ? AND criado_em >= ?", (channel, since)) as cur:
        return (await cur.fetchone())[0]


async def _drive_full(ctx: Ctx, channel: str) -> bool:
    """True quando o Drive passou do limite de uso; sem total ou sem resposta, a captação segue."""
    try:
        usage = await ctx.drive.uso()
    except Exception as e:
        await ctx.bus.log("captacao", f"[{channel}] não consegui ler o uso do Drive ({e}); a captação segue",
                          "aviso")
        return False
    used = usage.fracao if usage is not None else None
    limit = ctx.plat.drive.limite_uso
    if used is not None and used >= limit:
        await ctx.bus.log("captacao", f"[{channel}] o Drive está com {used:.0%} de uso (limite {limit:.0%}); "
                                      "captação pausada até liberar espaço", "aviso")
        return True
    return False


async def capture_cycle(ctx: Ctx, channel: str, *, list_fn: Callable[..., Awaitable[list[Candidate]]] | None = None,
                        trends_fn: Callable[..., Awaitable[list[str]]] | None = None,
                        now: datetime | None = None) -> int:
    """Lê as fontes do canal e grava em `descoberto` os candidatos de maior nota; devolve quantos entraram."""
    list_fn = list_fn or list_candidates  # resolvidos na chamada: dá para trocar as funções do módulo
    trends_fn = trends_fn or trending_terms
    cfg = ctx.cfgs[channel]
    now = now or datetime.now(timezone.utc)
    stock = await count_states(ctx.db, channel, STOCK_STATES)
    if stock >= cfg.estoque_max:
        await ctx.bus.log("captacao", f"[{channel}] estoque cheio ({stock} de {cfg.estoque_max} vídeos); "
                                      "captação em espera")
        return 0
    if await _drive_full(ctx, channel):
        return 0
    room = min(cfg.captar_por_dia - await _created_today(ctx.db, channel, now), cfg.estoque_max - stock)
    if room <= 0:
        await ctx.bus.log("captacao", f"[{channel}] limite de {cfg.captar_por_dia} vídeos captados hoje atingido")
        return 0

    found: list[tuple[SourceSpec, Candidate]] = []
    trending: list[str] = []
    async with httpx.AsyncClient() as client:
        extract = ytdlp_extractor(cfg.cookies_navegador)
        for spec in cfg.fontes:
            try:
                listed = await list_fn(spec, client=client, extract=extract, keys=ctx.keys)
            except Exception as e:  # uma fonte quebrada não para as outras
                await ctx.bus.log("captacao", f"[{channel}] não consegui ler a fonte {spec.rotulo()}: {e}", "aviso")
                continue
            for c in listed:
                if passes(c, banned=cfg.palavras_proibidas) and not await seen(ctx.db, c.url):
                    found.append((spec, c))
        if found:
            try:
                trending = await trends_fn(client, ctx.plat.tendencias_geo)
            except Exception:
                trending = []

    ranked = sorted(((score(c, keywords=cfg.palavras_chave, trending=trending, authorized=spec.autorizado, now=now),
                      spec, c) for spec, c in found), key=lambda t: t[0], reverse=True)
    inserted = 0
    for nota, spec, c in ranked:
        if inserted >= room:
            break
        vid = await insert_video(ctx.db, channel, c.url, c.titulo, autor=c.autor, fonte=spec.rotulo(),
                                 fonte_autorizada=spec.autorizado, tom=spec.tom, exige_cc=spec.so_creative_commons,
                                 nota=nota, duracao=c.duracao, url_midia=c.direto)
        if vid is None:  # o mesmo vídeo veio de duas fontes
            continue
        inserted += 1
        await ctx.bus.publish("video.estado", {"id": vid, "estado": S.descoberto.value})
        await ctx.bus.log("captacao", f"[{channel}] novo vídeo (nota {nota}): {c.titulo or c.url}", video_id=vid)
    if not inserted:
        await ctx.bus.log("captacao", f"[{channel}] nenhum vídeo novo nas fontes")
    return inserted


async def _fail(ctx: Ctx, video_id: int, msg: str) -> State:
    """Leva o vídeo para `erro` sem nunca lançar (ele pode ter mudado de estado no meio)."""
    try:
        await transition(ctx.db, video_id, S.erro, error=msg)
        await ctx.bus.publish("video.estado", {"id": video_id, "estado": S.erro.value, "erro": msg})
        await ctx.bus.log("captacao", msg, "erro", video_id)
        return S.erro
    except Exception as e:
        try:
            await ctx.bus.log("captacao", f"{msg} (e não consegui marcar o erro: {e})", "erro", video_id)
            row = await get_video(ctx.db, video_id)
            return row.estado if row else S.erro
        except Exception:
            return S.erro


async def download_one(ctx: Ctx, row: VideoRow) -> State:
    """Baixa o vídeo (que está em `baixando`) para dados/captados/<canal>/ e o deixa em `baixado`."""
    async def on_progress(pct: float) -> None:
        await ctx.bus.progress(row.id, pct)

    try:
        cfg = ctx.cfgs[row.canal]
        res = await download(row.url_origem, out_dir=ctx.data / "captados" / row.canal, on_progress=on_progress,
                             cookies_browser=cfg.cookies_navegador, name=str(row.id), require_cc=row.exige_cc,
                             direct_url=row.url_midia)
        fields: dict = {"caminho_local": res.video}
        if res.capa is not None and Path(res.capa).is_file():
            capa = ctx.data / "capas" / f"{row.id}-origem.jpg"
            capa.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(res.capa, capa)
            fields["caminho_capa"] = capa
        if isinstance(res.meta.get("duration"), (int, float)):
            fields["duracao"] = float(res.meta["duration"])
        await update_fields(ctx.db, row.id, **fields)
        await transition(ctx.db, row.id, S.baixado)
    except Exception as e:  # um vídeo nunca para a fila de downloads
        msg = str(e) if isinstance(e, DownloadFailed) else f"erro inesperado no download: {e}"
        return await _fail(ctx, row.id, msg)
    await ctx.bus.publish("video.estado", {"id": row.id, "estado": S.baixado.value})
    await ctx.bus.log("captacao", f"[{row.canal}] baixado: {Path(res.video).name}", video_id=row.id)
    return S.baixado


async def upload_one(ctx: Ctx, row: VideoRow) -> State:
    """Sobe o vídeo (em `enviando`) e o info.json para <canal>/1-captados e apaga a cópia local."""
    try:
        local = Path(row.caminho_local) if row.caminho_local else None
        if local is not None and local.is_file():
            folder = pasta(row.canal, CAPTADOS)
            remote = pasta(row.canal, CAPTADOS, local.name)
            # grava o destino antes: se a plataforma cair no meio, a nova tentativa sabe que o arquivo já subiu
            await update_fields(ctx.db, row.id, remoto_bruto=remote)
            info = local.with_name(f"{local.stem}.info.json")
            if info.is_file():
                await ctx.drive.enviar(info, folder)
            remote = await ctx.drive.enviar(local, folder)
        elif row.remoto_bruto:  # retomada: o arquivo já tinha subido antes da queda
            remote = row.remoto_bruto
        else:
            raise FileNotFoundError(f"o arquivo baixado sumiu do disco ({row.caminho_local}); descarte o vídeo")
        await update_fields(ctx.db, row.id, caminho_local=None, remoto_bruto=remote)
        await transition(ctx.db, row.id, S.no_drive)
    except Exception as e:
        return await _fail(ctx, row.id, f"falha ao enviar para o Drive: {e}")
    await ctx.bus.publish("video.estado", {"id": row.id, "estado": S.no_drive.value})
    await ctx.bus.log("captacao", f"[{row.canal}] no Drive: {remote}", video_id=row.id)
    return S.no_drive


async def _idle(ctx: Ctx) -> None:
    try:
        await asyncio.wait_for(ctx.stop.wait(), IDLE_SLEEP)
    except asyncio.TimeoutError:
        pass


async def _worker(ctx: Ctx, src: State, dst: State, job: Callable[[Ctx, VideoRow], Awaitable[State]],
                  label: str) -> None:
    while not ctx.stop.is_set():
        try:
            row = await claim(ctx.db, src, dst, channels=ctx.channels_for("captacao"))
            if row is None:
                await _idle(ctx)
                continue
            await ctx.bus.publish("video.estado", {"id": row.id, "estado": dst.value})
            await job(ctx, row)
        except Exception as e:  # o trabalhador nunca morre: registra e tenta de novo
            try:
                await ctx.bus.log("captacao", f"erro inesperado no trabalhador de {label}: {e}", "erro")
            except Exception:
                pass
            await _idle(ctx)


async def download_worker(ctx: Ctx) -> None:
    await _worker(ctx, S.descoberto, S.baixando, download_one, "download")


async def drive_upload_worker(ctx: Ctx) -> None:
    await _worker(ctx, S.baixado, S.enviando, upload_one, "envio ao Drive")
