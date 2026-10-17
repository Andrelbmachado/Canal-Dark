"""Local transcription with faster-whisper (free, runs on CPU)."""
import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from canaldark.models import WordTiming


@dataclass
class Transcript:
    text: str
    words: list[WordTiming] = field(default_factory=list)


class TranscribeUnavailable(Exception):
    pass


_model = None


def _run(path: Path) -> Transcript:
    global _model
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise TranscribeUnavailable("faster-whisper não instalado: rode `pip install -e .[ml]`") from e
    if _model is None:
        _model = WhisperModel("small", device="cpu", compute_type="int8")
    segments, _ = _model.transcribe(str(path), language="pt", word_timestamps=True, vad_filter=True)
    words, texts = [], []
    for seg in segments:
        texts.append(seg.text.strip())
        words.extend(WordTiming(w.word.strip(), w.start, w.end) for w in (seg.words or []) if w.word.strip())
    return Transcript(" ".join(texts), words)


async def transcribe(path: Path) -> Transcript:
    from canaldark.edit.render import probe

    if not (await probe(path)).has_audio:
        return Transcript("", [])
    return await asyncio.to_thread(_run, path)
