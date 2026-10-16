import pytest

from canaldark.db import InvalidTransition, claim, insert_video, normalize_url, set_progress, transition, update_fields, get_video
from canaldark.models import State


def test_normalize_url():
    assert normalize_url("https://X.com/a/?utm_source=t&b=1#frag") == "https://x.com/a?b=1"


async def test_insert_dedup(db):
    a = await insert_video(db, "gta6", "https://x.com/a?utm_source=t", "T", "rss")
    b = await insert_video(db, "gta6", "https://X.com/a/", "T", "rss")
    assert a is not None and b is None


async def test_invalid_transition(db):
    vid = await insert_video(db, "gta6", "https://x.com/b", "T", "rss")
    with pytest.raises(InvalidTransition):
        await transition(db, vid, State.publicado)


async def test_claim_moves_oldest(db):
    v1 = await insert_video(db, "gta6", "https://x.com/1", "1", "rss")
    await insert_video(db, "gta6", "https://x.com/2", "2", "rss")
    row = await claim(db, State.descoberto, State.baixando)
    assert row.id == v1 and row.estado == State.baixando


async def test_claim_filters_channel(db):
    await insert_video(db, "gta6", "https://x.com/1", "1", "rss")
    assert await claim(db, State.descoberto, State.baixando, channel="ia") is None


async def test_progress_and_fields(db):
    vid = await insert_video(db, "gta6", "https://x.com/c", "T", "rss")
    await set_progress(db, vid, 42.5)
    await update_fields(db, vid, formato="meme", caminho_bruto="/tmp/x.mp4")
    row = await get_video(db, vid)
    assert row.etapa_progresso == 42.5 and row.formato == "meme"


async def test_update_fields_rejects_unknown_column(db):
    vid = await insert_video(db, "gta6", "https://x.com/d", "T", "rss")
    with pytest.raises(ValueError):
        await update_fields(db, vid, hack="1")


async def test_error_transition_records_message(db):
    vid = await insert_video(db, "gta6", "https://x.com/e", "T", "rss")
    await transition(db, vid, State.erro, error="boom")
    row = await get_video(db, vid)
    assert row.estado == State.erro and row.erro == "boom"
