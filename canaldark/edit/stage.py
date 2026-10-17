"""Etapa de edição: pronto → editando → editado (ou revisar); texto e estilo por regras fixas, sem IA."""
import asyncio
import random
from pathlib import Path

from canaldark.context import Ctx
from canaldark.db import claim, recent_styles, transition, update_fields
from canaldark.edit.cards import drawable
from canaldark.edit.layout import FONT
from canaldark.edit.render import RenderError, extract_frame, make_thumb, probe, run_ffmpeg
from canaldark.edit.styles import FALLBACK_STYLE, StyleInputs, available_styles, choose_style, prepare_job
from canaldark.edit.transcribe import TranscribeUnavailable, transcribe
from canaldark.manchete import headline_for
from canaldark.models import ChannelConfig, State, StylePreset, VideoRow, WordTiming

IDLE_SLEEP = 3
# usado só se estilos/faixa_cima.yaml sumir: o estilo de reserva precisa existir sempre
_FALLBACK_PRESET = StylePreset(nome=FALLBACK_STYLE, rotulo="Faixa em cima", tipo="faixa_cima", fonte_min=60,
                               fonte_max=72)


class EditFailed(Exception):
    """Falha prevista da edição; a mensagem (em português) vai como está para o erro do vídeo."""


async def _fail(ctx: Ctx, video_id: int, msg: str) -> State:
    """Leva o vídeo para `erro` sem nunca lançar (ele pode ter mudado de estado no meio do caminho)."""
    try:
        await transition(ctx.db, video_id, State.erro, error=msg)
        await ctx.bus.publish("video.estado", {"id": video_id, "estado": "erro", "erro": msg})
        await ctx.bus.log("edicao", msg, "erro", video_id)
    except Exception as e:
        try:
            await ctx.bus.log("edicao", f"{msg} (e não consegui marcar o erro: {e})", "erro", video_id)
        except Exception:
            pass
    return State.erro


def _auto_headline(row: VideoRow, cfg: ChannelConfig) -> str:
    text = headline_for(row.titulo_origem, cfg.manchete_padrao, row.id)
    if sum(ch.isalpha() for ch in drawable(text, FONT)) < 3:  # letras que a fonte não desenha (ex.: japonês)
        return headline_for("", cfg.manchete_padrao, row.id)
    return text


async def _pick_style(ctx: Ctx, row: VideoRow, cfg: ChannelConfig, rng: random.Random | None) -> str:
    reasons = available_styles(ctx.styles, ctx.memes, ctx.root / "memes")
    free = {name for name, why in reasons.items() if why is None}
    if row.estilo:
        if row.estilo in free:
            return row.estilo
        why = reasons.get(row.estilo) or "não existe em estilos/"
        await ctx.bus.log("edicao", f"estilo {row.estilo} indisponível ({why}); usando {FALLBACK_STYLE}", "aviso",
                          row.id)
        return FALLBACK_STYLE
    return choose_style(cfg.estilos[row.tom], await recent_styles(ctx.db, row.canal), free, rng or random.Random())


async def _words(ctx: Ctx, row: VideoRow, src: Path) -> list[WordTiming]:
    try:
        return (await transcribe(src)).words
    except TranscribeUnavailable as e:
        await ctx.bus.log("edicao", f"{e}; o karaokê sai sem legenda", "aviso", row.id)
    except Exception as e:
        await ctx.bus.log("edicao", f"não consegui transcrever ({e}); o karaokê sai sem legenda", "aviso", row.id)
    return []


async def edit_video(ctx: Ctx, row: VideoRow, *, rng: random.Random | None = None) -> State:
    """Edita um vídeo que já está em `editando`; qualquer falha vira `erro` (fila `pronto`) e nunca relança."""
    try:
        cfg = ctx.cfgs.get(row.canal)
        if cfg is None:
            raise EditFailed(f"o canal {row.canal} não está mais em canais/")
        work = ctx.data / "editados" / str(row.id)
        src = Path(row.caminho_local) if row.caminho_local else None
        if src is None or not src.is_file():
            raise EditFailed(f"arquivo de origem não encontrado: {row.caminho_local or '(nenhum)'}")
        text = row.manchete or _auto_headline(row, cfg)
        name = await _pick_style(ctx, row, cfg, rng)
        preset = ctx.styles.get(name) or _FALLBACK_PRESET
        work.mkdir(parents=True, exist_ok=True)
        inp = StyleInputs(src=src, info=await probe(src), text=text, credit=row.autor, work=work,
                          texto_base=cfg.texto_base, out=work / f"{row.id}.mp4")  # o nome vira o nome no Drive
        if preset.tipo == "karaoke":
            inp.words = await _words(ctx, row, src)
        elif preset.tipo == "meme":
            inp.meme = ctx.memes[preset.meme]
            inp.meme_clip = ctx.root / "memes" / inp.meme.clipe
            inp.meme_info = await probe(inp.meme_clip)
            capa = Path(row.caminho_capa) if row.caminho_capa else None
            inp.capa = capa if capa and capa.is_file() else await extract_frame(src, work / "capa.jpg")
        job = prepare_job(preset, inp)

        async def progress(pct: float) -> None:
            await ctx.bus.progress(row.id, pct)

        out = await run_ffmpeg(job, progress)
        try:
            await make_thumb(out, ctx.data / "capas" / f"{row.id}-final.jpg")
        except RenderError as e:  # sem a miniatura o painel mostra a capa da origem; o vídeo segue
            await ctx.bus.log("edicao", str(e), "aviso", row.id)
        await update_fields(ctx.db, row.id, estilo=name, manchete=text, caminho_final=str(out))
        to = State.revisar if cfg.revisar_antes_de_publicar else State.editado
        await transition(ctx.db, row.id, to)
        await ctx.bus.publish("video.estado", {"id": row.id, "estado": to.value})
        await ctx.bus.log("edicao", f"editado ({name}, {job.duration:.0f} s)", video_id=row.id)
        return to
    except (RenderError, EditFailed) as e:
        return await _fail(ctx, row.id, str(e))
    except Exception as e:
        return await _fail(ctx, row.id, f"erro inesperado na edição: {e}")


async def _idle(ctx: Ctx, seconds: float) -> None:
    try:
        await asyncio.wait_for(ctx.stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def edit_worker(ctx: Ctx) -> None:
    """Tira um vídeo de `pronto` dos canais ativos na aba Edição por vez, até `ctx.stop`."""
    while not ctx.stop.is_set():
        row = None
        try:
            row = await claim(ctx.db, State.pronto, State.editando, channels=ctx.channels_for("edicao"))
            if row is None:
                await _idle(ctx, IDLE_SLEEP)
                continue
            await ctx.bus.publish("video.estado", {"id": row.id, "estado": "editando"})
            await edit_video(ctx, row)
        except Exception as e:  # um vídeo nunca para a fila, e uma falha do banco não mata o trabalhador
            if row is not None:
                await _fail(ctx, row.id, f"erro inesperado na edição: {e}")
                continue
            try:
                await ctx.bus.log("edicao", f"não consegui pegar o próximo vídeo: {e}", "erro")
            except Exception:
                pass
            await _idle(ctx, IDLE_SLEEP)
