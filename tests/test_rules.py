from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from canaldark.models import ChannelConfig, Package
from canaldark.rules import check, in_tse_blackout, prompt_rules, publish_allowed

SP = ZoneInfo("America/Sao_Paulo")
BASE = dict(id="x", nome="X", fontes=[{"tipo": "reddit", "sub": "a"}],
            formatos={"noticia": 0.6, "meme": 0.25, "dialogo": 0.15}, estilo_manchete="a",
            persona_roteiro="b", voz={"id": "pm_alex"}, horarios=["09:00"], contas={"youtube": "x"})


@pytest.fixture
def gta_cfg():
    return ChannelConfig(**BASE)


@pytest.fixture
def pol_cfg():
    return ChannelConfig(**{**BASE, "regras": "politica", "formatos": {"noticia": 0.85, "dialogo": 0.15}})


def pkg(**kw):
    d = dict(formato="noticia", manchete=["Rockstar confirma", "data do GTA 6"], roteiro="texto",
             fonte="Rockstar", titulo_post="t", descricao_post="d")
    d.update(kw)
    return Package(**d)


def test_blackout_edges():
    assert not in_tse_blackout(datetime(2026, 10, 21, 23, 59, tzinfo=SP))
    assert in_tse_blackout(datetime(2026, 10, 22, 0, 0, tzinfo=SP))
    assert in_tse_blackout(datetime(2026, 10, 26, 23, 59, tzinfo=SP))
    assert not in_tse_blackout(datetime(2026, 10, 27, 0, 0, tzinfo=SP))


def test_blackout_converts_utc():
    # 22/10 02:00 UTC ainda é 21/10 23:00 em São Paulo
    assert not in_tse_blackout(datetime(2026, 10, 22, 2, 0, tzinfo=ZoneInfo("UTC")))


def test_meme_ratio(gta_cfg):
    meme = pkg(formato="meme")
    assert check(meme, gta_cfg, ["noticia", "meme"])
    assert not check(meme, gta_cfg, ["noticia", "noticia"])


def test_headline_too_long(gta_cfg):
    assert check(pkg(manchete=["x" * 40, "y" * 30]), gta_cfg, [])


def test_missing_source(gta_cfg):
    assert check(pkg(fonte=" "), gta_cfg, [])


def test_risk_revisar_is_violation(gta_cfg):
    assert check(pkg(risco="revisar", motivo_risco="duvida"), gta_cfg, [])


def test_politics_rules(pol_cfg):
    assert check(pkg(formato="meme"), pol_cfg, [])
    assert check(pkg(formato="dialogo", dialogo=[{"personagem": "a", "fala": "b"}]), pol_cfg, [])
    assert check(pkg(sintetico_realista=True), pol_cfg, [])
    assert not check(pkg(), pol_cfg, [])


def test_politics_blocks_publish_in_window(pol_cfg, gta_cfg):
    t = datetime(2026, 10, 24, 12, tzinfo=SP)
    assert not publish_allowed(pol_cfg, t)
    assert publish_allowed(gta_cfg, t)


def test_prompt_rules_mentions_extras(gta_cfg):
    cfg = gta_cfg.model_copy(update={"regras_extras": ["nunca usar material vazado"]})
    assert "nunca usar material vazado" in prompt_rules(cfg)
