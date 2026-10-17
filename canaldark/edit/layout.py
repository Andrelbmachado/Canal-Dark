"""Layout of the 9:16 template: white bars, headline sizing and word-by-word ASS captions."""
import re
import textwrap
from pathlib import Path
from typing import Literal

from canaldark.models import WordTiming

FONT = Path(__file__).resolve().parents[2] / "fontes" / "Montserrat-ExtraBold.ttf"
FONT_NAME = "Montserrat ExtraBold"
W, H, BOX = 1080, 1920, 1080
MIN_BAR = 420
MAX_TEXT_W = 1000
CHAR_W = 0.62  # average glyph width of Montserrat ExtraBold, as a fraction of font size

_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\U0000FE0F\U0000200D\U00002B00-\U00002BFF]"
)


def fitted_height(src_w: int, src_h: int) -> int:
    scale = min(BOX / src_w, BOX / src_h)
    h = round(src_h * scale)
    return h + (h % 2)  # libx264 needs even dimensions; ffmpeg rounds the same way


def bar_height(src_w: int, src_h: int) -> int:
    return max(MIN_BAR, (H - fitted_height(src_w, src_h)) // 2)


def strip_emoji(text: str) -> str:
    return re.sub(r"\s+", " ", _EMOJI.sub("", text)).strip()


def wrap_headline(text: str, max_chars: int = 22) -> list[str]:
    lines = textwrap.wrap(strip_emoji(text), max_chars, break_long_words=False, break_on_hyphens=False)
    if not lines:
        raise ValueError("manchete vazia depois de remover emoji")
    if len(lines) > 3:
        raise ValueError(f"manchete precisa de {len(lines)} linhas; o máximo é 3")
    return lines


def font_size_for(lines: list[str], bar: int) -> int:
    longest = max((len(line) for line in lines), default=1)
    size = 76
    while size > 44 and (len(lines) * 1.25 * size > bar - 60 or longest * CHAR_W * size > MAX_TEXT_W):
        size -= 4
    return size


def _ts(t: float) -> str:
    cs = max(0, round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _groups(words: list[WordTiming]) -> list[list[WordTiming]]:
    groups: list[list[WordTiming]] = []
    for w in words:
        cur = groups[-1] if groups else None
        if (cur is None or len(cur) == 3 or w.start - cur[-1].end > 0.4
                or cur[-1].word.rstrip()[-1:] in ".,!?;:"):
            groups.append([w])
        else:
            cur.append(w)
    return groups


def build_ass(words: list[WordTiming], style: Literal["palavra"] = "palavra", *, margin_v: int = 560) -> str:
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: palavra,{FONT_NAME},78,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,6,2,2,60,60,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    for g in _groups([w for w in words if strip_emoji(w.word)]):
        text = " ".join(strip_emoji(w.word) for w in g).upper().replace("{", "(").replace("}", ")")
        events.append(f"Dialogue: 0,{_ts(g[0].start)},{_ts(g[-1].end)},{style},,0,0,0,,{text}")
    return header + "\n".join(events) + "\n"
