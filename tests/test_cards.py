"""Textos e cartões desenhados com o Pillow (entram no FFmpeg como PNG)."""
from functools import lru_cache

from PIL import ImageFont

from canaldark.edit.cards import bar_image, comment_card, fit_text, text_image
from canaldark.edit.layout import FONT

LONG = ("Rockstar confirma que GTA 6 terá ação no Brasil e mapa enorme com cidades inspiradas no Rio, "
        "em São Paulo e no Nordeste, com missões novas, carros nacionais e muito mais conteúdo para todo mundo ")


@lru_cache
def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT), size)


def _ink_bbox(img):
    return img.getchannel("A").getbbox()


def test_bar_image_is_white_with_dark_text():
    img = bar_image(1080, 420, "#FFFFFF", "Lula fala sobre economia no debate", FONT, text_color="#000000",
                    size_range=(60, 72))
    assert img.size == (1080, 420)
    assert img.convert("RGB").getpixel((5, 5)) == (255, 255, 255)
    assert img.convert("L").getextrema()[0] < 50


def test_fit_text_never_exceeds_box():
    text = (LONG * 2)[:300].upper()
    size, lines = fit_text(text, FONT, (1000, 460), (60, 72))
    assert 1 <= len(lines) <= 4 and size >= 60
    assert all(_font(size).getlength(line) <= 1000 for line in lines)
    assert lines[-1].endswith("…")  # não coube tudo: a última linha avisa que foi cortada


def test_fit_text_prefers_biggest_size_that_fits():
    size, lines = fit_text("Game Dev", FONT, (1000, 220), (60, 72))
    assert (size, lines) == (72, ["Game Dev"])


def test_fit_text_respects_box_height():
    size, lines = fit_text(LONG, FONT, (1000, 160), (56, 76))
    assert len(lines) * size <= 160  # uma linha nunca é mais baixa que o tamanho da fonte
    assert all(_font(size).getlength(line) <= 1000 for line in lines)


def test_fit_text_shrinks_until_lines_fit_box_height():
    # cabem 2 linhas de até ~59 px numa caixa de 140 px: o texto tem que encolher, não vazar por baixo
    size, lines = fit_text("Lula fala sobre economia no debate", FONT, (1000, 140), (56, 76))
    assert len(lines) == 2 and len(lines) * size <= 140


def test_long_word_is_broken_to_fit_width():
    size, lines = fit_text("W" * 120, FONT, (1000, 460), (60, 72))
    assert len(lines) > 1 and all(_font(size).getlength(line) <= 1000 for line in lines)


def test_text_with_only_emoji_does_not_crash():
    assert fit_text("😂🔥🕹️", FONT, (1000, 220), (60, 72))[1] == []
    img = text_image("😂🔥", FONT, (1000, 360), color="#FFFFFF", size_range=(56, 76), stroke=6)
    assert img.size == (1000, 360) and img.mode == "RGBA" and _ink_bbox(img) is None
    bar = bar_image(1080, 300, "#FFFFFF", "😂😂", FONT, text_color="#000000", size_range=(60, 72))
    assert bar.convert("L").getextrema() == (255, 255)


def test_glyphs_missing_from_font_are_dropped():
    # Montserrat não tem CJK: em vez de caixinhas vazias, o caractere sai do texto
    assert fit_text("GTA 予告編", FONT, (1000, 220), (60, 72))[1] == ["GTA"]
    img = text_image("予告編 ⏰", FONT, (1000, 360), color="#000000", size_range=(56, 76))
    assert _ink_bbox(img) is None


def test_text_image_exact_size_with_stroke():
    img = text_image("Olá, pessoal", FONT, (1000, 360), color="#FFFFFF", size_range=(56, 76), stroke=6,
                     stroke_color="#000000")
    assert img.size == (1000, 360) and img.mode == "RGBA"
    pixels = [p for p in img.get_flattened_data() if p[3] == 255]
    assert any(p[:3] == (255, 255, 255) for p in pixels) and any(p[:3] == (0, 0, 0) for p in pixels)


def test_alignment():
    left = _ink_bbox(text_image("Oi", FONT, (1000, 360), color="#000000", size_range=(56, 76), align="esquerda"))
    center = _ink_bbox(text_image("Oi", FONT, (1000, 360), color="#000000", size_range=(56, 76)))
    assert left[0] < 20
    assert abs((center[0] + center[2]) / 2 - 500) < 10


def test_credit_goes_to_bottom_right_corner():
    with_credit = bar_image(1080, 420, "#FFFFFF", "Texto", FONT, text_color="#000000", size_range=(60, 72),
                            credit="@GaiaMemes")
    without = bar_image(1080, 420, "#FFFFFF", "Texto", FONT, text_color="#000000", size_range=(60, 72))
    corner = (700, 380, 1080, 420)
    assert with_credit.convert("L").crop(corner).getextrema()[0] < 200
    assert without.convert("L").crop(corner).getextrema() == (255, 255)


def test_comment_card_has_transparent_hole():
    card, (x, y, w, h) = comment_card("Isso é um comentário bem legal", "@GaiaMemes", FONT, width=960,
                                      video_size=(640, 360))
    assert card.mode == "RGBA" and card.width == 960 and y + h <= card.height
    assert abs(w / h - 640 / 360) < 0.02  # o buraco tem o formato do vídeo
    alpha = card.getchannel("A")
    assert alpha.getpixel((x + w // 2, y + h // 2)) == 0
    assert alpha.getpixel((0, 0)) == 0  # canto arredondado do cartão
    assert alpha.getpixel((x, y)) == 255  # canto arredondado do buraco
    assert alpha.getpixel((card.width // 2, 8)) == 255  # corpo do cartão
    assert card.convert("RGB").getpixel((card.width // 2, 8)) == (255, 255, 255)
    assert card.convert("L").crop((0, 0, 960, y)).getextrema()[0] < 80  # @autor e texto escuros


def test_comment_card_vertical_video_stays_inside_canvas():
    card, (x, y, w, h) = comment_card(LONG, "@GaiaMemes", FONT, width=960, video_size=(360, 640))
    assert card.height <= 1600 and y + h <= card.height
    assert abs(w / h - 360 / 640) < 0.02 and x + w <= 960
    assert w % 2 == 0 and h % 2 == 0
