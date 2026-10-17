"""Fixtures compartilhadas: banco temporário, fábrica de canal e o Ctx com o FakeDrive."""
import shutil
from pathlib import Path

import pytest
import pytest_asyncio

from canaldark.config import load_memes, load_styles
from canaldark.context import Ctx
from canaldark.db import open_db
from canaldark.events import EventBus
from canaldark.models import PLATFORMS, ChannelConfig, PlatformConfig
from tests.fakes.drive import FakeDrive

REPO = Path(__file__).resolve().parent.parent


@pytest_asyncio.fixture
async def db(tmp_path):
    conn = await open_db(tmp_path / "t.db")
    yield conn
    await conn.close()


def make_cfg(**over) -> ChannelConfig:
    base = dict(
        id="teste", nome="Canal de teste",
        fontes=[{"tipo": "youtube_busca", "busca": "teste"}],
        estilos={"serio": {"faixa_cima": 0.5, "faixa_dupla": 0.3, "karaoke": 0.2},
                 "meme": {"comentario": 0.4, "meme_shrek": 0.3, "meme_dicaprio": 0.3}},
        manchete_padrao=["Olha isso", "Você viu?"],
        horarios=["09:00"],
        contas={p: "teste" for p in PLATFORMS},
    )
    return ChannelConfig(**{**base, **over})


@pytest.fixture
def cfg_factory():
    return make_cfg


def _copy_yaml(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for f in src.glob("*.yaml"):
        shutil.copy(f, dst / f.name)


@pytest_asyncio.fixture
async def ctx(tmp_path, db):
    _copy_yaml(REPO / "estilos", tmp_path / "estilos")
    _copy_yaml(REPO / "memes", tmp_path / "memes")  # só os .yaml: os clipes .mp4 ficam de fora
    c = Ctx(db=db, bus=EventBus(db), cfgs={"teste": make_cfg()}, plat=PlatformConfig(), drive=FakeDrive(),
            root=tmp_path, styles=load_styles(tmp_path / "estilos"), memes=load_memes(tmp_path / "memes"),
            dry_run=True)
    yield c
    c.stop.set()
