"""Free local text-to-speech (Kokoro, or Piper as an alternative) with per-word timings."""
import asyncio
import re
import shutil
import wave
from dataclasses import dataclass
from pathlib import Path


@dataclass
class WordTiming:
    word: str
    start: float
    end: float


class TTSUnavailable(Exception):
    pass


_VOWELS = re.compile(r"[aeiouáéíóúâêôãõàüy]+", re.I)


def _weight(word: str) -> float:
    syllables = max(1, len(_VOWELS.findall(word)))
    pause = 1.5 if word[-1:] in ".!?" else 0.8 if word[-1:] in ",;:" else 0
    return syllables + pause


def estimate_timings(text: str, duration: float, offset: float = 0.0) -> list[WordTiming]:
    words = text.split()
    if not words:
        return []
    weights = [_weight(w) for w in words]
    total = sum(weights)
    out, t = [], offset
    for w, wt in zip(words, weights):
        d = duration * wt / total
        out.append(WordTiming(w, t, t + d))
        t += d
    out[-1].end = offset + duration
    return out


def wav_duration(path: Path) -> float:
    with wave.open(str(path)) as w:
        return w.getnframes() / w.getframerate()


_kokoro = None


def _kokoro_say(text: str, voice: str, out: Path) -> None:
    global _kokoro
    try:
        import numpy as np
        import soundfile as sf
        from kokoro import KPipeline
    except ImportError as e:
        raise TTSUnavailable("voz Kokoro não instalada: rode `pip install -e .[ml]`") from e
    if _kokoro is None:
        _kokoro = KPipeline(lang_code="p")  # p = português do Brasil
    chunks = [r[2] if isinstance(r, tuple) else r.audio for r in _kokoro(text, voice=voice, speed=1.1)]
    audio = np.concatenate([np.asarray(c, dtype="float32") for c in chunks if c is not None])
    sf.write(str(out), audio, 24000, subtype="PCM_16")


async def _piper_say(text: str, model: str, out: Path) -> None:
    exe = shutil.which("piper")
    if not exe:
        raise TTSUnavailable("Piper não encontrado no PATH")
    proc = await asyncio.create_subprocess_exec(exe, "--model", model, "--output_file", str(out),
                                                stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate(text.encode())
    if proc.returncode != 0:
        raise TTSUnavailable(f"Piper falhou: {err.decode(errors='replace')[-300:]}")


async def synthesize(text: str, voice: str, out: Path, engine: str = "kokoro",
                     offset: float = 0.0) -> list[WordTiming]:
    out.parent.mkdir(parents=True, exist_ok=True)
    if engine == "piper":
        await _piper_say(text, voice, out)
    else:
        await asyncio.to_thread(_kokoro_say, text, voice, out)
    return estimate_timings(text, wav_duration(out), offset)
