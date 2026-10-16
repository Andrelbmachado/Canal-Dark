"""Edit stage: transcribe → package from the brain → rules check → voice → 9:16 render."""
import asyncio
from collections.abc import Callable
import json
from pathlib import Path

from canaldark.brain import BrainError, make_package
from canaldark.db import claim, transition, update_fields
from canaldark.edit.layout import FONT, bar_height, build_ass, font_size_for, wrap_headline
from canaldark.edit.render import ImageLayer, RenderError, RenderSpec, make_thumb, probe, render
from canaldark.edit.transcribe import Transcript, TranscribeUnavailable, transcribe
from canaldark.edit.tts import TTSUnavailable, WordTiming, synthesize, wav_duration
from canaldark.models import ChannelConfig, Package, Sound, State, VideoRow
from canaldark.rules import check

WORK_DIR = Path("dados/editados")
SOUNDS_DIR = Path("sons")
CHARACTERS_DIR = Path("personagens")
EXTRA_VOICES = ["pf_dora", "pm_santa"]
FORMAT_SECONDS = {"noticia": (15, 40), "meme": (7, 15), "dialogo": (30, 60)}
IDLE_SLEEP = 3


class EditFailed(Exception):
    pass


async def _recent(db, channel: str, exclude: int) -> tuple[list[str], list[str]]:
    async with db.execute(
        "SELECT pacote_json, formato FROM videos WHERE canal = ? AND id != ? AND pacote_json IS NOT NULL"
        " ORDER BY id DESC LIMIT 20", (channel, exclude)) as cur:
        rows = await cur.fetchall()
    headlines = [" ".join(json.loads(r["pacote_json"]).get("manchete", [])) for r in reversed(rows)]
    return headlines, [r["formato"] for r in rows[:2] if r["formato"]]


def _clamp(fmt: str, seconds: float) -> float:
    lo, hi = FORMAT_SECONDS[fmt]
    return max(lo, min(hi, seconds))


async def _concat_wavs(parts: list[Path], out: Path, gap: float = 0.25) -> None:
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for p in parts:
        args += ["-i", str(p)]
    chain = "".join(f"[{i}:a]aresample=24000,apad=pad_dur={gap}[p{i}];" for i in range(len(parts)))
    chain += "".join(f"[p{i}]" for i in range(len(parts))) + f"concat=n={len(parts)}:v=0:a=1[a]"
    proc = await asyncio.create_subprocess_exec(*args, "-filter_complex", chain, "-map", "[a]", str(out),
                                                stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise EditFailed(f"não consegui juntar as falas: {err.decode(errors='replace')[-300:]}")


async def _voice(pkg: Package, cfg: ChannelConfig, work: Path) -> tuple[Path | None, list[WordTiming], list[ImageLayer]]:
    if pkg.formato == "noticia":
        narr = work / "narracao.wav"
        words = await synthesize(pkg.roteiro, cfg.voz.id, narr, cfg.voz.motor)
        return narr, words, []
    if pkg.formato == "dialogo":
        names = list(dict.fromkeys([*cfg.personagens, *(line.personagem for line in pkg.dialogo)]))
        voices = {n: ([cfg.voz.id, *EXTRA_VOICES])[i % (1 + len(EXTRA_VOICES))] for i, n in enumerate(names)}
        parts, words, layers, t = [], [], [], 0.0
        for i, line in enumerate(pkg.dialogo):
            part = work / f"fala{i:02d}.wav"
            words += await synthesize(line.fala, voices[line.personagem], part, cfg.voz.motor, offset=t)
            dur = wav_duration(part)
            img = CHARACTERS_DIR / f"{line.personagem}.png"
            if img.exists():
                layers.append(ImageLayer(img, t, t + dur + 0.25))
            parts.append(part)
            t += dur + 0.25
        narr = work / "narracao.wav"
        await _concat_wavs(parts, narr)
        return narr, words, layers
    return None, [], []


async def _sound(pkg: Package, sounds: list[Sound], duration: float) -> tuple[Path | None, float | None]:
    snd = next((s for s in sounds if s.id == pkg.som), None) if pkg.som else None
    if snd is None or not (SOUNDS_DIR / snd.arquivo).exists():
        return None, None
    path = SOUNDS_DIR / snd.arquivo
    if snd.momento == "inicio":
        return path, 0.0
    if snd.momento == "antes_revelacao":
        return path, round(duration * 0.6, 2)
    length = (await probe(path)).duration
    return path, max(0.0, duration - length)


async def edit_video(db, bus, cfgs: dict[str, ChannelConfig], sounds: list[Sound], row: VideoRow,
                     work_dir: Path = WORK_DIR) -> State:
    cfg = cfgs[row.canal]
    work = work_dir / str(row.id)
    work.mkdir(parents=True, exist_ok=True)
    src = Path(row.caminho_bruto) if row.caminho_bruto else None

    async def fail(msg: str) -> State:
        await transition(db, row.id, State.erro, error=msg)
        await bus.publish("video.estado", {"id": row.id, "estado": "erro", "erro": msg})
        await bus.log("edicao", msg, "erro", row.id)
        return State.erro

    async def step(pct: float) -> None:
        await bus.progress(row.id, pct)

    try:
        transcript = Transcript("", [])
        if src is not None:
            try:
                transcript = await transcribe(src)
            except TranscribeUnavailable as e:
                await bus.log("edicao", f"{e}; seguindo sem transcrição", "aviso", row.id)
        await step(10)

        if row.aprovado and row.pacote_json:
            pkg = Package.model_validate_json(row.pacote_json)
            violations: list[str] = []
        else:
            headlines, recent_formats = await _recent(db, row.canal, row.id)
            try:
                pkg = await make_package(cfg, row.titulo_origem, row.fonte, transcript.text, headlines, sounds,
                                         has_video=src is not None, instruction=row.instrucao)
            except BrainError as e:
                return await fail(f"o cérebro não respondeu: {e}")
            violations = check(pkg, cfg, recent_formats)
            if src is None and pkg.formato != "dialogo":
                violations.append("sem vídeo de origem: só o formato dialogo é possível")
        try:
            lines = wrap_headline(" ".join(pkg.manchete))
        except ValueError as e:
            lines, violations = pkg.manchete, [*violations, str(e)]
        await update_fields(db, row.id, pacote_json=pkg.model_dump_json(), formato=pkg.formato)
        await step(25)

        if violations or (pkg.risco == "revisar" and not row.aprovado):
            reason = "; ".join(violations) or pkg.motivo_risco or "o cérebro marcou risco"
            await transition(db, row.id, State.revisar, error=reason)
            await bus.publish("video.estado", {"id": row.id, "estado": "revisar", "erro": reason})
            await bus.log("edicao", f"foi para revisão: {reason}", "aviso", row.id)
            return State.revisar

        narration, words, layers = await _voice(pkg, cfg, work)
        await step(40)

        info = await probe(src) if src else None
        if pkg.formato == "meme":
            clip_len = info.duration - (pkg.corte.inicio if pkg.corte else 0)
            if pkg.corte and pkg.corte.fim:
                clip_len = pkg.corte.fim - pkg.corte.inicio
            duration = _clamp("meme", clip_len)
            offset = pkg.corte.inicio if pkg.corte else 0
            words = [WordTiming(w.word, w.start - offset, w.end - offset) for w in transcript.words
                     if offset <= w.start < offset + duration]
        else:
            duration = _clamp(pkg.formato, wav_duration(narration) + 0.6)
        bar = bar_height(info.width, info.height) if info else 420
        ass = work / "legendas.ass"
        ass.write_text(build_ass(words, "palavra", margin_v=bar + 40), encoding="utf-8")
        sound, sound_at = await _sound(pkg, sounds, duration)

        spec = RenderSpec(src=src, cut=pkg.corte if pkg.formato == "meme" else None, headline_lines=lines,
                          font=FONT, font_size=font_size_for(lines, bar), bar=bar,
                          bottom_text=f"Fonte: {pkg.fonte}", ass_path=ass, narration=narration, sound=sound,
                          sound_at=sound_at, out=work / "final.mp4", image_layers=layers, duration=duration,
                          src_has_audio=bool(info and info.has_audio))

        async def on_render(p: float) -> None:
            await step(40 + p * 0.6)

        out = await render(spec, on_render)
        try:
            await make_thumb(out, work / "thumb.jpg")
        except RenderError as e:
            await bus.log("edicao", str(e), "aviso", row.id)
    except (TTSUnavailable, RenderError, EditFailed) as e:
        return await fail(str(e))

    await update_fields(db, row.id, caminho_final=str(out))
    await transition(db, row.id, State.editado)
    await bus.publish("video.estado", {"id": row.id, "estado": "editado"})
    await bus.log("edicao", f"editado ({pkg.formato}, {duration:.0f} s)", video_id=row.id)
    return State.editado


async def edit_worker(db, bus, cfgs: dict[str, ChannelConfig], sounds: list[Sound],
                      work_dir: Path = WORK_DIR, stop: asyncio.Event | None = None,
                      active: Callable[[], list[str]] | None = None) -> None:
    while stop is None or not stop.is_set():
        row = await claim(db, State.baixado, State.editando, channels=active() if active else list(cfgs))
        if row is None:
            await asyncio.sleep(IDLE_SLEEP)
            continue
        await bus.publish("video.estado", {"id": row.id, "estado": "editando"})
        try:
            await edit_video(db, bus, cfgs, sounds, row, work_dir)
        except Exception as e:  # never let one video stop the queue
            await transition(db, row.id, State.erro, error=f"erro inesperado na edição: {e}")
            await bus.publish("video.estado", {"id": row.id, "estado": "erro", "erro": str(e)})
            await bus.log("edicao", f"erro inesperado: {e}", "erro", row.id)
