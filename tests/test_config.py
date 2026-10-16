from pathlib import Path

import pytest
from pydantic import ValidationError

from canaldark.config import load_channels, load_sounds

ROOT = Path(__file__).resolve().parent.parent


def test_load_gta6_example():
    cfg = load_channels(ROOT / "canais")["gta6"]
    assert cfg.formatos["noticia"] == 0.6
    assert cfg.regras == "padrao"
    assert cfg.fontes[0].tipo == "google_news"


def _write(tmp_path, name, body):
    d = tmp_path / "canais"
    d.mkdir(exist_ok=True)
    (d / name).write_text(body, encoding="utf-8")
    return d


BASE = """id: x
nome: X
fontes: [{tipo: reddit, sub: a}]
formatos: {noticia: %s, meme: 0.2, dialogo: 0.2}
estilo_manchete: a
persona_roteiro: b
voz: {motor: kokoro, id: pm_alex}
horarios: ["09:00"]
contas: {youtube: x}
"""


def test_weights_must_sum_to_one(tmp_path):
    d = _write(tmp_path, "ruim.yaml", BASE % "0.5")
    with pytest.raises(ValueError) as e:
        load_channels(d)
    assert "ruim.yaml" in str(e.value)


def test_google_news_requires_busca(tmp_path):
    d = _write(tmp_path, "g.yaml", (BASE % "0.6").replace("{tipo: reddit, sub: a}", "{tipo: google_news}"))
    with pytest.raises(ValueError):
        load_channels(d)


def test_invalid_time(tmp_path):
    d = _write(tmp_path, "h.yaml", (BASE % "0.6").replace('"09:00"', '"25:00"'))
    with pytest.raises(ValueError):
        load_channels(d)


def test_sounds_empty_catalog():
    assert load_sounds(ROOT / "sons" / "catalogo.yaml") == []


def test_sounds_parse(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("- {id: t, arquivo: t.mp3, quando_usar: final triste, momento: final}\n")
    s = load_sounds(p)
    assert s[0].momento == "final"


def test_all_bundled_channels_load():
    cfgs = load_channels(Path(__file__).parents[1] / "canais")
    assert set(cfgs) == {"gta6", "ia", "politica"}
    pol = cfgs["politica"]
    assert pol.regras == "politica" and pol.formatos["meme"] == 0.0 and pol.formatos["noticia"] == 0.85
    assert {f.tipo for f in cfgs["ia"].fontes} >= {"rss", "google_news"}
