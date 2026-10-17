"""Textos e cartões desenhados com o Pillow: cada um vira um PNG RGBA que entra no FFmpeg com overlay.

Todo texto passa por `strip_emoji` e perde os caracteres que a fonte não desenha (nada de caixinhas vazias).
A quebra de linha e o tamanho são medidos de verdade com a fonte, então o texto nunca sai da caixa.
"""
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from canaldark.edit.layout import strip_emoji

LINE_SPACING = 1.15  # distância entre linhas, em múltiplos do tamanho da fonte
BAR_MARGIN = 40  # caixa do texto numa faixa: (largura − 80) × (altura − 80)
CREDIT_SIZE, CREDIT_MARGIN = 28, 16
ELLIPSIS = "…"

CARD_PAD, CARD_RADIUS, HOLE_RADIUS, CARD_GAP = 40, 36, 28, 24
AVATAR, HANDLE_SIZE = 96, 40
COMMENT_SIZES, COMMENT_MAX_H = (44, 60), 300
CARD_MAX_H = 1600  # o cartão começa em y=160 numa tela de 1920


@lru_cache(maxsize=256)
def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def _mask(path: str, ch: str) -> bytes:
    im = Image.new("L", (96, 48))
    ImageDraw.Draw(im).text((24, 4), ch, font=_font(path, 24), fill=255)
    return im.tobytes()


@lru_cache(maxsize=8192)
def _has_glyph(path: str, ch: str) -> bool:
    # um caractere que a fonte não tem sai igual ao .notdef (U+FFFF nunca existe numa fonte)
    return _mask(path, ch) != _mask(path, chr(0xFFFF))


def drawable(text: str, font: Path) -> str:
    """O texto como ele sai na imagem: sem emoji e sem os caracteres que a fonte não desenha."""
    path = str(font)
    words = ("".join(ch for ch in word if _has_glyph(path, ch)) for word in strip_emoji(text).split())
    return " ".join(w for w in words if w)


def _line_height(size: int) -> int:
    return round(size * LINE_SPACING)


def _block_height(font: ImageFont.FreeTypeFont, lines: int) -> int:
    ascent, descent = font.getmetrics()
    return (lines - 1) * _line_height(int(font.size)) + ascent + descent if lines else 0


def _wrap(words: list[str], font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    lines, cur = [], ""
    for word in words:
        trial = f"{cur} {word}" if cur else word
        if font.getlength(trial) <= width:
            cur = trial
            continue
        if cur:
            lines.append(cur)
        while len(word) > 1 and font.getlength(word) > width:  # palavra maior que a caixa: quebra por letra
            n = 1
            while n < len(word) - 1 and font.getlength(word[:n + 1]) <= width:
                n += 1
            lines.append(word[:n])
            word = word[n:]
        cur = word
    if cur:
        lines.append(cur)
    return lines


def _ellipsize(line: str, font: ImageFont.FreeTypeFont, width: int) -> str:
    line = line.rstrip(" .,;:!?…")
    while line and font.getlength(line + ELLIPSIS) > width:
        cut = line.rfind(" ")
        line = (line[:cut] if cut > 0 else line[:-1]).rstrip(" .,;:!?…")
    return line + ELLIPSIS


def fit_text(text: str, font: Path, box: tuple[int, int], size_range: tuple[int, int],
             max_lines: int = 4) -> tuple[int, list[str]]:
    """O maior tamanho em `size_range` em que o texto cabe em `box` com até `max_lines` linhas.

    Se nem no menor tamanho couber, fica o que cabe e a última linha termina em "…"."""
    lo, hi = sorted(size_range)
    words = drawable(text, font).split()
    if not words:
        return hi, []
    width, height = box
    for size in range(hi, lo - 1, -1):
        f = _font(str(font), size)
        lines = _wrap(words, f, width)
        if len(lines) <= max_lines and _block_height(f, len(lines)) <= height:
            return size, lines
    f = _font(str(font), lo)
    lines = _wrap(words, f, width)
    room = 1
    while room < max_lines and _block_height(f, room + 1) <= height:
        room += 1
    return lo, [*lines[:room - 1], _ellipsize(lines[room - 1], f, width)]


def _draw_lines(img: Image.Image, lines: list[str], font: Path, size: int, area: tuple[int, int, int, int],
                color: str, align: str, stroke: int = 0, stroke_color: str = "#000000") -> None:
    """Desenha as linhas centralizadas na vertical dentro de `area` (x, y, largura, altura)."""
    if not lines:
        return
    x, y, w, h = area
    f = _font(str(font), size)
    draw = ImageDraw.Draw(img)
    top = y + (h - _block_height(f, len(lines))) // 2
    for i, line in enumerate(lines):
        lx = x if align == "esquerda" else x + (w - f.getlength(line)) / 2
        draw.text((round(lx), top + i * _line_height(size)), line, font=f, fill=color, anchor="la",
                  stroke_width=stroke, stroke_fill=stroke_color)


def text_image(text: str, font: Path, box: tuple[int, int], *, color: str, size_range: tuple[int, int],
               align: str = "centro", stroke: int = 0, stroke_color: str = "#000000") -> Image.Image:
    """Texto transparente em volta, exatamente do tamanho de `box` (o contorno também cabe nela)."""
    img = Image.new("RGBA", box, (0, 0, 0, 0))
    inner = (box[0] - 2 * stroke, box[1] - 2 * stroke)
    size, lines = fit_text(text, font, inner, size_range)
    _draw_lines(img, lines, font, size, (stroke, stroke, *inner), color, align, stroke, stroke_color)
    return img


def bar_image(width: int, height: int, color: str, text: str, font: Path, *, text_color: str,
              size_range: tuple[int, int], align: str = "centro", credit: str = "",
              credit_color: str = "#666666") -> Image.Image:
    """Faixa de cor sólida com o texto numa caixa de (largura − 80) × (altura − 80) e o crédito no canto."""
    img = Image.new("RGBA", (width, height), color)
    box = (width - 2 * BAR_MARGIN, height - 2 * BAR_MARGIN)
    size, lines = fit_text(text, font, box, size_range)
    _draw_lines(img, lines, font, size, (BAR_MARGIN, BAR_MARGIN, *box), text_color, align)
    if credit:
        _, credit_lines = fit_text(credit, font, (width - 2 * CREDIT_MARGIN, 2 * CREDIT_SIZE),
                                   (CREDIT_SIZE, CREDIT_SIZE), max_lines=1)
        if credit_lines:  # canto inferior direito, dentro da margem de baixo da faixa
            ImageDraw.Draw(img).text((width - CREDIT_MARGIN, height - 6), credit_lines[0],
                                     font=_font(str(font), CREDIT_SIZE), fill=credit_color, anchor="rd")
    return img


def _avatar(img: Image.Image, x: int, y: int, d: int = AVATAR) -> None:
    """Avatar genérico: círculo cinza com a silhueta de uma pessoa."""
    layer = Image.new("RGBA", (d, d), "#C3C9D1")
    draw = ImageDraw.Draw(layer)
    draw.ellipse((d * 0.31, d * 0.17, d * 0.69, d * 0.55), fill="#F2F4F7")
    draw.ellipse((d * 0.13, d * 0.62, d * 0.87, d * 1.3), fill="#F2F4F7")
    mask = Image.new("L", (d, d), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, d - 1, d - 1), fill=255)
    img.paste(layer, (x, y), mask)


def _even(value: float) -> int:
    return max(2, int(value) // 2 * 2)


def comment_card(text: str, handle: str, font: Path, *, width: int = 960,
                 video_size: tuple[int, int]) -> tuple[Image.Image, tuple[int, int, int, int]]:
    """Cartão branco de comentário: avatar, "@autor", o texto e, embaixo, o buraco do vídeo.

    O buraco é transparente, com cantos arredondados e o formato do vídeo; devolve (x, y, w, h) dele."""
    inner = width - 2 * CARD_PAD
    size, lines = fit_text(text, font, (inner, COMMENT_MAX_H), COMMENT_SIZES)
    text_top = CARD_PAD + AVATAR + CARD_GAP
    text_h = _block_height(_font(str(font), size), len(lines))
    hole_y = text_top + text_h + (CARD_GAP if lines else 0)
    vw, vh = video_size if min(video_size) > 0 else (9, 16)
    hole_w, hole_h = inner, inner * vh / vw
    max_h = CARD_MAX_H - hole_y - CARD_PAD
    if hole_h > max_h:  # vídeo em pé: o buraco fica mais estreito, no mesmo formato
        hole_w, hole_h = max_h * vw / vh, max_h
    hole_w, hole_h = _even(hole_w), _even(hole_h)
    hole_x = (width - hole_w) // 2
    height = hole_y + hole_h + CARD_PAD

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=CARD_RADIUS, fill="#FFFFFF")
    _avatar(img, CARD_PAD, CARD_PAD)
    handle_x = CARD_PAD + AVATAR + 20
    _, handle_lines = fit_text(handle, font, (width - handle_x - CARD_PAD, 2 * HANDLE_SIZE),
                               (HANDLE_SIZE, HANDLE_SIZE), max_lines=1)
    if handle_lines:
        draw.text((handle_x, CARD_PAD + AVATAR // 2), handle_lines[0], font=_font(str(font), HANDLE_SIZE),
                  fill="#0F0F0F", anchor="lm")
    _draw_lines(img, lines, font, size, (CARD_PAD, text_top, inner, text_h), "#262626", "esquerda")
    # o buraco: pixels totalmente transparentes (o ImageDraw troca o pixel, não mistura)
    draw.rounded_rectangle((hole_x, hole_y, hole_x + hole_w - 1, hole_y + hole_h - 1), radius=HOLE_RADIUS,
                           fill=(0, 0, 0, 0))
    return img, (hole_x, hole_y, hole_w, hole_h)
