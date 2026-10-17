from pathlib import Path
from typing import get_args

import pytest
import yaml
from pydantic import ValidationError

from canaldark.config import load_channels, load_memes, load_platform, load_styles, replace_sources
from canaldark.models import PLATFORMS, ChannelConfig, Platform, PlatformConfig, SourceSpec, StylePreset

ROOT = Path(__file__).resolve().parent.parent
SERIO = {"faixa_cima": 0.5, "faixa_dupla": 0.3, "karaoke": 0.2}
MEME = {"comentario": 0.4, "meme_shrek": 0.3, "meme_dicaprio": 0.3}
VALID = {
    "id": "x", "nome": "X",
    "fontes": [{"tipo": "youtube_busca", "busca": "gatos"}],
    "estilos": {"serio": {"faixa_cima": 1.0}, "meme": {"comentario": 0.5, "faixa_cima": 0.5}},
    "manchete_padrao": ["Olha isso"],
    "horarios": ["09:00"],
}


def write_channel(tmp_path, name="x.yaml", **over):
    d = tmp_path / "canais"
    d.mkdir(exist_ok=True)
    data = {**VALID, "id": Path(name).stem, **over}
    (d / name).write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return d


def sources(cfg):
    return [(f.tipo, f.url or f.busca, f.tom, f.autorizado, f.so_creative_commons) for f in cfg.fontes]


def test_repo_channels_load():
    cfgs = load_channels(ROOT / "canais")
    assert set(cfgs) == {"politica", "tecnologia", "jogos", "bebes_gatos"}
    assert all(set(c.contas) == set(PLATFORMS) for c in cfgs.values())
    assert all(c.contas == {p: c.id for p in PLATFORMS} for c in cfgs.values())
    assert all((c.captar_por_dia, c.estoque_max) == (20, 60) for c in cfgs.values())
    assert all(not c.revisar_antes_de_publicar and c.tiktok_modo == "rascunho" for c in cfgs.values())


def test_repo_channel_contents():
    c = load_channels(ROOT / "canais")
    pol, tec, jog, beb = c["politica"], c["tecnologia"], c["jogos"], c["bebes_gatos"]

    assert (pol.nome, pol.regras, pol.pausa_eleitoral) == ("Eleições 2026", "politica", True)
    assert sources(pol) == [("youtube_canal", "https://www.youtube.com/@investigapinhel", "serio", False, False),
                            ("youtube_busca", "Lula Flávio Bolsonaro", "serio", False, False),
                            ("youtube_busca", "meme Lula Bolsonaro", "meme", False, False)]
    assert pol.palavras_chave == ["Lula", "Flávio Bolsonaro", "Bolsonaro", "eleição", "eleições", "debate",
                                  "segundo turno", "pesquisa", "TSE"]
    assert pol.estilos == {"serio": SERIO, "meme": MEME}
    assert pol.manchete_padrao == ["Olha o que aconteceu na eleição", "Você viu isso?",
                                   "Isso está dando o que falar"]
    assert pol.texto_base == "Siga para acompanhar as eleições"
    assert (pol.horarios, pol.max_por_dia) == (["07:30", "12:30", "19:00", "22:00"], 4)
    assert pol.hashtags == ["#eleicoes2026", "#politica", "#lula", "#bolsonaro"]

    assert tec.nome == "Tecnologia Hoje"
    assert sources(tec) == [("youtube_canal", "https://www.youtube.com/@AlmanaqueSOS", "serio", False, False),
                            ("youtube_busca", "tecnologia IA celular", "serio", False, False),
                            ("youtube_busca", "meme tecnologia", "meme", False, False)]
    assert tec.palavras_chave == ["IA", "inteligência artificial", "ChatGPT", "iPhone", "Samsung", "Android",
                                  "Apple", "Google", "celular"]
    assert tec.estilos == {"serio": SERIO, "meme": MEME}
    assert tec.manchete_padrao == ["Olha essa tecnologia", "Você sabia disso?", "Isso vai mudar tudo"]
    assert tec.texto_base == "Siga para mais tecnologia"
    assert (tec.horarios, tec.max_por_dia) == (["08:00", "11:30", "18:30", "21:30"], 4)
    assert tec.hashtags == ["#tecnologia", "#ia", "#tech"]

    assert jog.nome == "Jogos e GTA 6"
    assert sources(jog) == [("youtube_canal", "https://www.youtube.com/@GaiaMemes", "meme", False, False),
                            ("youtube_busca", "GTA 6", "serio", False, False),
                            ("youtube_busca", "meme games", "meme", False, False)]
    assert jog.palavras_chave == ["GTA 6", "GTA VI", "Rockstar", "PlayStation", "Xbox", "Nintendo", "Minecraft",
                                  "Fortnite"]
    assert jog.palavras_proibidas == ["leak", "vazado", "vazamento"]
    assert jog.estilos == {"serio": SERIO, "meme": MEME}
    assert jog.manchete_padrao == ["Quem joga vai entender", "Olha isso no jogo", "Gamers, vocês viram?"]
    assert jog.texto_base == "Siga para mais games"
    assert (jog.horarios, jog.max_por_dia) == (["09:00", "12:00", "18:00", "21:00"], 4)
    assert jog.hashtags == ["#gta6", "#games", "#gamer"]

    assert beb.nome == "Bebês e Gatos"
    assert sources(beb) == [("pexels", "cute cat", "serio", False, False),
                            ("pexels", "baby", "serio", False, False),
                            ("pixabay", "kitten", "serio", False, False),
                            ("youtube_busca", "gatos fofos", "meme", False, True)]
    assert beb.palavras_chave == ["gato", "gatinho", "bebê", "filhote", "fofo"]
    assert beb.estilos == {"serio": {"faixa_cima": 0.6, "borrado": 0.4}, "meme": {"comentario": 0.5, "faixa_cima": 0.5}}
    assert beb.manchete_padrao == ["Olha que fofura", "Impossível não sorrir", "Quem aí tem um assim?"]
    assert beb.texto_base == "Siga para mais fofura"
    assert (beb.horarios, beb.max_por_dia) == (["10:00", "15:00", "20:00"], 3)
    assert beb.hashtags == ["#gatos", "#bebes", "#fofura"]

    assert all(x.regras == "padrao" and not x.palavras_proibidas for x in (tec, beb))
    assert jog.regras == "padrao"


def test_platforms_constant_matches_literal():
    assert PLATFORMS == get_args(Platform) == ("youtube", "instagram", "facebook", "tiktok")


def test_style_weights_must_sum_to_one(tmp_path):
    d = write_channel(tmp_path, "ruim.yaml", estilos={"serio": {"faixa_cima": 0.5}, "meme": {"comentario": 1.0}})
    with pytest.raises(ValueError) as e:
        load_channels(d)
    assert "ruim.yaml" in str(e.value) and "serio" in str(e.value)


def test_style_weights_tolerate_rounding():
    cfg = ChannelConfig(**{**VALID, "estilos": {"serio": {"a": 0.33, "b": 0.33, "c": 0.33}, "meme": {"a": 1.0}}})
    assert set(cfg.estilos) == {"serio", "meme"}


def test_both_style_tables_required(tmp_path):
    d = write_channel(tmp_path, "um.yaml", estilos={"serio": {"faixa_cima": 1.0}})
    with pytest.raises(ValueError, match="meme"):
        load_channels(d)
    with pytest.raises(ValidationError):
        ChannelConfig(**{**VALID, "estilos": {**VALID["estilos"], "humor": {"faixa_cima": 1.0}}})


def test_fallback_headlines_times_and_platforms_are_validated():
    with pytest.raises(ValidationError):
        ChannelConfig(**{**VALID, "manchete_padrao": []})
    with pytest.raises(ValidationError):
        ChannelConfig(**{**VALID, "manchete_padrao": ["Olha", "  "]})
    with pytest.raises(ValidationError):
        ChannelConfig(**{**VALID, "horarios": ["25:00"]})
    with pytest.raises(ValidationError):
        ChannelConfig(**{**VALID, "contas": {"kwai": "x"}})


def test_channel_id_must_match_file_name(tmp_path):
    d = write_channel(tmp_path, "y.yaml", id="outro")
    with pytest.raises(ValueError, match="y.yaml"):
        load_channels(d)


def test_source_requires_field_and_label():
    with pytest.raises(ValidationError):
        SourceSpec(tipo="youtube_canal")
    with pytest.raises(ValidationError):
        SourceSpec(tipo="pexels")
    with pytest.raises(ValidationError):
        SourceSpec(tipo="youtube_busca", busca="  ")
    with pytest.raises(ValidationError):
        SourceSpec(tipo="instagram_perfil", busca="gatos")
    assert SourceSpec(tipo="youtube_canal", url="https://www.youtube.com/@GaiaMemes").rotulo() == "@GaiaMemes"
    assert SourceSpec(tipo="youtube_busca", busca="meme gta 6").rotulo() == "busca: meme gta 6"
    assert SourceSpec(tipo="pexels", busca="cute cat").rotulo() == "Pexels: cute cat"
    assert SourceSpec(tipo="pixabay", busca="baby").rotulo() == "Pixabay: baby"
    assert SourceSpec(tipo="youtube_canal", url="https://www.youtube.com/@GaiaMemes/shorts").rotulo() == "@GaiaMemes"
    assert SourceSpec(tipo="tiktok_perfil", url="https://www.tiktok.com/@perfil?lang=pt-BR").rotulo() == "@perfil"
    assert SourceSpec(tipo="instagram_perfil", url="https://www.instagram.com/perfil/reels/").rotulo() == "@perfil"


def test_platform_defaults_when_missing(tmp_path):
    p = load_platform(tmp_path / "nao.yaml")
    assert (p.porta, p.pacote, p.drive.tipo) == (8001, 15, "pasta")
    assert (p.drive.remote, p.drive.pasta, p.drive.pasta_local, p.drive.manter_originais, p.drive.limite_uso,
            p.tendencias_geo) == ("gdrive:", "CanalDark", "dados/drive", False, 0.9, "BR")
    assert PlatformConfig().drive is not PlatformConfig().drive


def test_platform_errors_cite_the_file(tmp_path):
    f = tmp_path / "plataforma.yaml"
    f.write_text("drive: {tipo: ftp}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="plataforma.yaml"):
        load_platform(f)
    f.write_text("", encoding="utf-8")
    assert load_platform(f) == PlatformConfig()


def test_repo_platform_styles_memes():
    assert load_platform(ROOT / "plataforma.yaml") == PlatformConfig()
    styles = load_styles(ROOT / "estilos")
    assert len(styles) == 7 and styles["meme_shrek"].meme == "shrek"
    assert all(s.nome == n for n, s in styles.items())
    assert {n: (s.tipo, s.rotulo) for n, s in styles.items()} == {
        "faixa_cima": ("faixa_cima", "Faixa em cima"),
        "faixa_dupla": ("faixa_dupla", "Faixa dupla"),
        "comentario": ("comentario", "Comentário"),
        "borrado": ("borrado", "Fundo borrado"),
        "karaoke": ("karaoke", "Legenda karaokê"),
        "meme_shrek": ("meme", "Meme do Shrek"),
        "meme_dicaprio": ("meme", "Meme do DiCaprio"),
    }
    top = styles["faixa_cima"]
    assert (top.alinhamento, top.fonte_min, top.fonte_max) == ("centro", 60, 72)
    assert styles["borrado"].cor_texto == "#FFFFFF" and styles["meme_dicaprio"].meme == "dicaprio"
    memes = load_memes(ROOT / "memes")
    assert memes["dicaprio"].clipe == "dicaprio.mp4"
    assert {n: (m.nome, m.clipe, m.imagem_em) for n, m in memes.items()} == {
        "shrek": ("shrek", "shrek.mp4", "cima"), "dicaprio": ("dicaprio", "dicaprio.mp4", "cima")}


def test_meme_style_requires_meme_name(tmp_path):
    with pytest.raises(ValidationError):
        StylePreset(nome="m", rotulo="M", tipo="meme")
    d = tmp_path / "estilos"
    d.mkdir()
    (d / "meme_x.yaml").write_text("rotulo: X\ntipo: meme\n", encoding="utf-8")
    with pytest.raises(ValueError, match="meme_x.yaml"):
        load_styles(d)


CHANNEL_WITH_COMMENTS = """\
# Canal de teste
id: x
nome: X
# Fontes do canal (antes)
fontes:
  - tipo: youtube_canal
    url: https://www.youtube.com/@antigo   # canal provisório
  - tipo: youtube_busca
    busca: gatos

# Pesos dos estilos (depois)
estilos:
  serio: {faixa_cima: 1.0}
  meme: {comentario: 1.0}
manchete_padrao: ["Olha isso"]
horarios: ["09:00"]   # um horário
"""


def test_replace_sources_keeps_comments(tmp_path):
    path = tmp_path / "x.yaml"
    path.write_text(CHANNEL_WITH_COMMENTS, encoding="utf-8")
    cfg = replace_sources(path, [SourceSpec(tipo="pexels", busca="cute cat", autorizado=True)])
    text = path.read_text(encoding="utf-8")
    assert text.startswith(CHANNEL_WITH_COMMENTS[:CHANNEL_WITH_COMMENTS.index("fontes:")])
    assert text.endswith(CHANNEL_WITH_COMMENTS[CHANNEL_WITH_COMMENTS.index("\n# Pesos"):])
    assert "@antigo" not in text and "# um horário" in text
    assert [f.rotulo() for f in cfg.fontes] == ["Pexels: cute cat"]
    assert load_channels(tmp_path)["x"] == cfg
    assert yaml.safe_load(text)["fontes"] == [{"tipo": "pexels", "busca": "cute cat", "autorizado": True}]


def test_replace_sources_twice_in_a_row(tmp_path):
    path = tmp_path / "x.yaml"
    path.write_text(CHANNEL_WITH_COMMENTS, encoding="utf-8")
    first = [SourceSpec(tipo="youtube_busca", busca="Lula Flávio Bolsonaro", tom="meme"),
             SourceSpec(tipo="youtube_canal", url="https://www.youtube.com/@GaiaMemes")]
    replace_sources(path, first)  # o yaml.safe_dump escreve os itens da lista no nível 0
    cfg = replace_sources(path, first + [SourceSpec(tipo="pixabay", busca="kitten")])
    text = path.read_text(encoding="utf-8")
    assert "Flávio" in text and cfg.fontes == first + [SourceSpec(tipo="pixabay", busca="kitten")]
    assert "# Pesos dos estilos (depois)" in text and "# Fontes do canal (antes)" in text


SOURCES_LAST = """\
id: x
nome: X
estilos: {serio: {faixa_cima: 1.0}, meme: {comentario: 1.0}}
manchete_padrao: [Olha isso]
horarios: ["09:00"]
fontes: [{tipo: youtube_busca, busca: a}]
# fim do arquivo
"""


def test_replace_sources_at_end_of_file(tmp_path):
    path = tmp_path / "x.yaml"
    path.write_text(SOURCES_LAST, encoding="utf-8")
    cfg = replace_sources(path, [])
    assert cfg.fontes == []
    assert path.read_text(encoding="utf-8") == SOURCES_LAST.replace("[{tipo: youtube_busca, busca: a}]", "[]")


def test_replace_sources_restores_on_failure(tmp_path):
    path = tmp_path / "x.yaml"
    bad = CHANNEL_WITH_COMMENTS.replace('"09:00"', '"25:00"')
    path.write_text(bad, encoding="utf-8")
    with pytest.raises(ValueError, match="x.yaml"):
        replace_sources(path, [SourceSpec(tipo="pexels", busca="cat")])
    assert path.read_text(encoding="utf-8") == bad
    assert sorted(p.name for p in tmp_path.iterdir()) == ["x.yaml"]


def test_replace_sources_needs_a_sources_block(tmp_path):
    path = tmp_path / "x.yaml"
    text = "id: x\nnome: X\n"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="fontes"):
        replace_sources(path, [])
    assert path.read_text(encoding="utf-8") == text
