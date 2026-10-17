"""Font, emoji stripping and the word-by-word ASS captions of the karaoke style."""
import re
from pathlib import Path

from canaldark.models import WordTiming

FONT = Path(__file__).resolve().parents[2] / "fontes" / "Montserrat-ExtraBold.ttf"
FONT_NAME = "Montserrat ExtraBold"

_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\U0000FE0F\U0000200D\U00002B00-\U00002BFF]"
)


def strip_emoji(text: str) -> str:
    return re.sub(r"\s+", " ", _EMOJI.sub("", text)).strip()


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


def _ass_text(word: str) -> str:
    """Upper-case word with the characters ASS reads as override blocks or escapes neutralized."""
    return strip_emoji(word).upper().replace("{", "(").replace("}", ")").replace("\\", "/")


def build_ass(words: list[WordTiming], *, margin_v: int, destaque: str = "&H0000E0FF&") -> str:
    """Groups of up to 3 words; one event per word, with the current word colored `destaque` (yellow)."""
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: palavra,{FONT_NAME},78,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,6,2,2,60,60,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    for group in _groups([w for w in words if strip_emoji(w.word)]):
        texts = [_ass_text(w.word) for w in group]
        for i, w in enumerate(group):
            end = max(w.start, group[i + 1].start if i + 1 < len(group) else w.end)
            line = " ".join(f"{{\\c{destaque}}}{t}{{\\r}}" if j == i else t for j, t in enumerate(texts))
            events.append(f"Dialogue: 0,{_ts(w.start)},{_ts(end)},palavra,,0,0,0,,{line}")
    return header + "\n".join(events) + "\n"
