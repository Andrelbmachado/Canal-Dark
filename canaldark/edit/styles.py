"""Os estilos de edição: disponibilidade, sorteio sem 3 repetições e o comando FFmpeg de cada um.

Os textos e o cartão são PNG do Pillow (gravados em `StyleInputs.work`) que entram com `-loop 1 -i` e
`overlay`; só o karaokê usa o filtro `ass=` (libass). Toda saída tem 1080×1920 e uma trilha estéreo.
"""
import random
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from canaldark.edit.cards import bar_image, comment_card, fit_text, text_image
from canaldark.edit.layout import FONT, build_ass
from canaldark.edit.render import FPS, H, W, MediaInfo, RenderError, RenderJob
from canaldark.models import MemeTemplate, StylePreset, WordTiming

FALLBACK_STYLE = "faixa_cima"
MAX_SECONDS = 180.0
BAR_HEIGHTS = (300, 420, 540)  # faixa de cima com 1, 2 ou 3+ linhas
DUPLA_BAR, BASE_TEXT_SIZE = 480, 44
CARD_WIDTH, CARD_TOP = 960, 160
BORRADO_BOX, BORRADO_TOP, BORRADO_STROKE = (1000, 360), 120, 6
KARAOKE_MARGIN_V = 360
OUTPUT = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(FPS),
          "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart"]
# fundo borrado: o borrão é feito em 1/4 da resolução e ampliado (mesmo visual de um boxblur=30:5 em
# 1080×1920, com uma fração do custo)
_BLUR = f"scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=8:5,scale={W}:{H},setsar=1"


def available_styles(styles: dict[str, StylePreset], memes: dict[str, MemeTemplate],
                     memes_dir: Path) -> dict[str, str | None]:
    """Nome do estilo → None (disponível) ou o motivo, como "falta o clipe memes/shrek.mp4"."""
    out: dict[str, str | None] = {}
    for name, preset in styles.items():
        reason = None
        if preset.tipo == "meme":
            template = memes.get(preset.meme)
            if template is None:
                reason = f"falta o meme {memes_dir.name}/{preset.meme}.yaml"
            elif not (memes_dir / template.clipe).is_file():
                reason = f"falta o clipe {memes_dir.name}/{template.clipe}"
        out[name] = reason
    return out


def choose_style(weights: dict[str, float], recent: list[str], available: set[str], rng: random.Random) -> str:
    """Sorteio pelos pesos entre os estilos disponíveis; nunca o mesmo estilo 3 vezes seguidas no canal."""
    candidates = [name for name, weight in weights.items() if weight > 0 and name in available]
    if len(recent) >= 2 and recent[0] == recent[1] and recent[0] in candidates and len(candidates) > 1:
        candidates.remove(recent[0])
    if not candidates:
        return FALLBACK_STYLE
    return rng.choices(candidates, weights=[weights[c] for c in candidates])[0]


@dataclass
class StyleInputs:
    src: Path
    info: MediaInfo
    text: str
    credit: str  # o autor já formatado ("@GaiaMemes"); vai no canto da faixa e como @ do comentário
    work: Path
    capa: Path | None = None
    meme_clip: Path | None = None
    meme: MemeTemplate | None = None
    texto_base: str = ""
    words: list[WordTiming] = field(default_factory=list)
    font: Path = FONT
    meme_info: MediaInfo | None = None  # duração e áudio do clipe do meme (o render dura o clipe)
    out: Path | None = None  # MP4 final; padrão work/final.mp4


def top_bar_height(text: str, preset: StylePreset, font: Path = FONT) -> int:
    """300, 420 ou 540 px conforme o texto ocupe 1, 2 ou 3+ linhas na caixa 1000×(altura − 80)."""
    sizes = (preset.fonte_min, preset.fonte_max)
    for height, most in zip(BAR_HEIGHTS, (1, 2)):
        if len(fit_text(text, font, (W - 80, height - 80), sizes)[1]) <= most:
            return height
    return BAR_HEIGHTS[-1]


def _esc(value) -> str:
    """Escapa um caminho para uma opção de filtro (nível 1) dentro do filter_complex (nível 2)."""
    s = str(value)
    for ch in ("\\", "'", ":"):
        s = s.replace(ch, "\\" + ch)
    for ch in ("\\", "'", "[", "]", ",", ";"):
        s = s.replace(ch, "\\" + ch)
    return s


def _fill(w: int, h: int) -> str:
    """Escala e recorte central para ocupar exatamente w×h."""
    return f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1"


def _save(img: Image.Image, path: Path) -> Path:
    img.save(path)
    return path


def _duration(info: MediaInfo) -> float:
    if info.duration <= 0:
        raise RenderError("não consegui ler a duração do vídeo")
    return min(info.duration, MAX_SECONDS)


class _Graph:
    """Monta as entradas e o filter_complex; `job()` fecha o áudio e as opções de saída."""

    def __init__(self, duration: float):
        self.duration = duration
        self.d = f"{duration:.3f}"
        self.args: list[str] = []
        self.filters: list[str] = []
        self.count = 0

    def _input(self, *opts) -> int:
        self.args += [str(o) for o in opts]
        self.count += 1
        return self.count - 1

    def video(self, path: Path) -> int:
        return self._input("-t", self.d, "-i", path)

    def still(self, path: Path) -> int:
        return self._input("-loop", "1", "-t", self.d, "-i", path)

    def source(self, idx: int) -> str:
        """O vídeo da entrada em 30 fps, com o último quadro congelado se ele acabar antes da duração."""
        return f"[{idx}:v]fps={FPS},tpad=stop_mode=clone:stop_duration={self.d}"

    def job(self, audio_from: int | None, out: Path) -> RenderJob:
        if audio_from is not None:
            self.filters.append(f"[{audio_from}:a]aresample=48000,"
                                f"aformat=sample_rates=48000:channel_layouts=stereo,apad[a]")
            audio = "[a]"
        else:  # fonte sem trilha: silêncio estéreo com a duração do vídeo
            audio = f"{self._input('-f', 'lavfi', '-t', self.d, '-i', 'anullsrc=r=48000:cl=stereo')}:a"
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-nostats", "-progress", "pipe:1",
               *self.args, "-filter_complex", ";".join(self.filters), "-map", "[v]", "-map", audio,
               "-t", self.d, *OUTPUT, str(out)]
        return RenderJob(cmd, self.duration, out)


def _bar(preset: StylePreset, inp: StyleInputs, height: int, text: str, *, credit: str = "",
         size_range: tuple[int, int] | None = None) -> Image.Image:
    return bar_image(W, height, preset.cor_faixa, text, inp.font, text_color=preset.cor_texto,
                     size_range=size_range or (preset.fonte_min, preset.fonte_max), align=preset.alinhamento,
                     credit=credit)


def _faixa_cima(preset: StylePreset, inp: StyleInputs, g: _Graph) -> int:
    src = g.video(inp.src)
    h = top_bar_height(inp.text, preset, inp.font)
    bar = g.still(_save(_bar(preset, inp, h, inp.text, credit=inp.credit), inp.work / "faixa.png"))
    g.filters += [f"{g.source(src)},{_fill(W, H - h)},pad={W}:{H}:0:{h}[base]", f"[base][{bar}:v]overlay=0:0[v]"]
    return src


def _faixa_dupla(preset: StylePreset, inp: StyleInputs, g: _Graph) -> int:
    src = g.video(inp.src)
    top = g.still(_save(_bar(preset, inp, DUPLA_BAR, inp.text), inp.work / "faixa.png"))
    bottom = g.still(_save(_bar(preset, inp, DUPLA_BAR, inp.texto_base, credit=inp.credit,
                                size_range=(BASE_TEXT_SIZE, BASE_TEXT_SIZE)), inp.work / "rodape.png"))
    g.filters += [f"{g.source(src)},{_fill(W, H - 2 * DUPLA_BAR)},pad={W}:{H}:0:{DUPLA_BAR}[base]",
                  f"[base][{top}:v]overlay=0:0[t]", f"[t][{bottom}:v]overlay=0:{H - DUPLA_BAR}[v]"]
    return src


def _comentario(preset: StylePreset, inp: StyleInputs, g: _Graph) -> int:
    src = g.video(inp.src)
    card, (hx, hy, hw, hh) = comment_card(inp.text, inp.credit, inp.font, width=CARD_WIDTH,
                                          video_size=(inp.info.width, inp.info.height))
    cx = (W - CARD_WIDTH) // 2
    c = g.still(_save(card, inp.work / "cartao.png"))
    g.filters += [f"{g.source(src)},split[s1][s2]", f"[s1]{_BLUR}[bg]", f"[s2]{_fill(hw, hh)}[fg]",
                  f"[bg][fg]overlay={cx + hx}:{CARD_TOP + hy}[t]", f"[t][{c}:v]overlay={cx}:{CARD_TOP}[v]"]
    return src


def _borrado(preset: StylePreset, inp: StyleInputs, g: _Graph) -> int:
    src = g.video(inp.src)
    txt = text_image(inp.text, inp.font, BORRADO_BOX, color=preset.cor_texto,
                     size_range=(preset.fonte_min, preset.fonte_max), align=preset.alinhamento,
                     stroke=BORRADO_STROKE, stroke_color="#000000")
    t = g.still(_save(txt, inp.work / "texto.png"))
    g.filters += [f"{g.source(src)},split[s1][s2]", f"[s1]{_BLUR}[bg]",
                  f"[s2]scale={W}:{H}:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1[fg]",
                  "[bg][fg]overlay=(W-w)/2:(H-h)/2[t]",
                  f"[t][{t}:v]overlay={(W - BORRADO_BOX[0]) // 2}:{BORRADO_TOP}[v]"]
    return src


def _karaoke(preset: StylePreset, inp: StyleInputs, g: _Graph) -> int:
    src = g.video(inp.src)
    h = top_bar_height(inp.text, preset, inp.font)
    bar = g.still(_save(_bar(preset, inp, h, inp.text, credit=inp.credit), inp.work / "faixa.png"))
    g.filters += [f"{g.source(src)},{_fill(W, H)}[base]"]
    if inp.words:
        ass = inp.work / "legendas.ass"
        ass.write_text(build_ass(inp.words, margin_v=KARAOKE_MARGIN_V), encoding="utf-8")
        g.filters += [f"[base][{bar}:v]overlay=0:0[t]",
                      f"[t]ass=filename={_esc(ass)}:fontsdir={_esc(inp.font.parent)}[v]"]
    else:  # sem palavras (sem o faster-whisper ou sem fala), a legenda fica de fora
        g.filters += [f"[base][{bar}:v]overlay=0:0[v]"]
    return src


def _meme(preset: StylePreset, inp: StyleInputs, g: _Graph) -> int:
    clip = g.video(inp.meme_clip)
    capa = g.still(inp.capa)
    h = top_bar_height(inp.text, preset, inp.font)
    bar = g.still(_save(_bar(preset, inp, h, inp.text, credit=inp.credit), inp.work / "faixa.png"))
    half = (H - h) // 2
    capa_y, clip_y = (h, h + half) if inp.meme.imagem_em == "cima" else (h + half, h)
    g.filters += [f"{g.source(clip)},{_fill(W, half)},pad={W}:{H}:0:{clip_y}[base]",
                  f"[{capa}:v]{_fill(W, half)}[capa]", f"[base][capa]overlay=0:{capa_y}[t]",
                  f"[t][{bar}:v]overlay=0:0[v]"]
    return clip


_BUILDERS = {"faixa_cima": _faixa_cima, "faixa_dupla": _faixa_dupla, "comentario": _comentario,
             "borrado": _borrado, "karaoke": _karaoke, "meme": _meme}


def prepare_job(preset: StylePreset, inp: StyleInputs) -> RenderJob:
    """Grava os PNG (e o ASS do karaokê) em `inp.work` e devolve o comando FFmpeg do estilo."""
    if preset.tipo == "meme":
        if not (inp.meme and inp.meme_clip and inp.meme_info and inp.capa):
            raise RenderError("o estilo de meme precisa do clipe do meme (com a duração) e da capa do vídeo")
        media = inp.meme_info  # o meme dura o clipe e usa o som dele
    else:
        media = inp.info
    inp.work.mkdir(parents=True, exist_ok=True)
    g = _Graph(_duration(media))
    audio_from = _BUILDERS[preset.tipo](preset, inp, g)
    return g.job(audio_from if media.has_audio else None, inp.out or inp.work / "final.mp4")
