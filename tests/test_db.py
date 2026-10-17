import sqlite3

import pytest

from canaldark import db as dbmod
from canaldark.db import (ALLOWED, PACK_STATES, TAB_OF_QUEUE, TAB_STATES, WORKING, InvalidTransition, claim,
                          count_states, get_video, insert_video, normalize_url, recent_styles, recover, seen,
                          set_progress, transition, update_fields)
from canaldark.models import State, VideoRow

S = State
HAPPY = [S.baixando, S.baixado, S.enviando, S.no_drive, S.separado, S.descendo, S.pronto, S.editando,
         S.editado, S.publicando, S.publicado, S.arquivando, S.arquivado]


async def add(db, n=1, channel="teste", **fields):
    return await insert_video(db, channel, f"https://youtu.be/v{n}", f"Título {n}", **fields)


def test_normalize_url():
    assert normalize_url("https://X.com/a/?utm_source=t&b=1#frag") == "https://x.com/a?b=1"


def test_states_in_plan_order():
    assert [s.value for s in State] == [
        "descoberto", "baixando", "baixado", "enviando", "no_drive", "separado", "descendo", "pronto", "editando",
        "revisar", "editado", "publicando", "publicado", "arquivando", "arquivado", "erro", "descartado"]


def test_state_tables_match_plan():
    assert WORKING == {S.baixando: S.descoberto, S.enviando: S.baixado, S.descendo: S.separado,
                       S.editando: S.pronto, S.publicando: S.editado, S.arquivando: S.publicado}
    assert ALLOWED == {
        S.descoberto: {S.baixando, S.erro, S.descartado},
        S.baixando: {S.baixado, S.descoberto, S.erro},
        S.baixado: {S.enviando, S.erro, S.descartado},
        S.enviando: {S.no_drive, S.baixado, S.erro},
        S.no_drive: {S.separado, S.descartado},
        S.separado: {S.descendo, S.erro, S.descartado},
        S.descendo: {S.pronto, S.separado, S.erro},
        S.pronto: {S.editando, S.descartado},
        S.editando: {S.editado, S.revisar, S.pronto, S.erro},
        S.revisar: {S.editado, S.pronto, S.descartado},
        S.editado: {S.publicando, S.pronto, S.descartado},
        S.publicando: {S.publicado, S.editado, S.erro},
        S.publicado: {S.arquivando},
        S.arquivando: {S.arquivado, S.publicado, S.erro},
        S.erro: {S.descoberto, S.baixado, S.separado, S.pronto, S.editado, S.publicado, S.descartado},
        S.arquivado: set(),
        S.descartado: set(),
    }
    assert TAB_STATES == {"captacao": (S.descoberto, S.baixando, S.baixado, S.enviando, S.no_drive),
                          "edicao": (S.separado, S.descendo, S.pronto, S.editando, S.revisar, S.editado),
                          "publicacao": (S.editado, S.publicando, S.publicado, S.arquivando, S.arquivado)}
    assert TAB_OF_QUEUE == {S.descoberto: "captacao", S.baixado: "captacao", S.separado: "edicao",
                            S.pronto: "edicao", S.editado: "publicacao", S.publicado: "publicacao"}
    assert PACK_STATES == (S.separado, S.descendo, S.pronto, S.editando, S.revisar, S.editado, S.publicando,
                           S.publicado, S.arquivando)


async def test_schema_matches_plan(db):
    async with db.execute("PRAGMA table_info(videos)") as cur:
        assert [r["name"] for r in await cur.fetchall()] == list(VideoRow.model_fields)
    async with db.execute("PRAGMA table_info(publicacoes)") as cur:
        assert [r["name"] for r in await cur.fetchall()] == [
            "id", "video_id", "plataforma", "conta", "id_externo", "url", "status", "motivo", "publicado_em"]
    for name, cols in {"idx_videos_estado": ["estado", "id"], "idx_videos_canal": ["canal", "estado"]}.items():
        async with db.execute(f"PRAGMA index_info({name})") as cur:
            assert [r["name"] for r in await cur.fetchall()] == cols
    vid = await add(db)
    await db.execute("INSERT INTO publicacoes (video_id, plataforma, conta) VALUES (?, 'youtube', 'teste')", (vid,))
    async with db.execute("SELECT status FROM publicacoes") as cur:
        assert (await cur.fetchone())["status"] == "publicado"
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute("INSERT INTO publicacoes (video_id, plataforma, conta) VALUES (?, 'youtube', 'b')", (vid,))


async def test_insert_dedup_and_defaults(db):
    vid = await insert_video(db, "teste", "https://youtu.be/x?utm_source=a", "T", autor="@a", nota=3.5)
    assert vid and await insert_video(db, "teste", "https://youtu.be/x", "T") is None
    row = await get_video(db, vid)
    assert row.estado == State.descoberto and row.nota == 3.5 and row.autor == "@a"
    assert (row.tom, row.fonte_autorizada, row.exige_cc, row.fila_erro, row.pacote) == (
        "serio", False, False, None, None)
    assert await seen(db, "https://youtu.be/x/") and not await seen(db, "https://youtu.be/y")
    assert await get_video(db, 999) is None


async def test_insert_stores_capture_fields_and_can_seed_a_state(db, tmp_path):
    vid = await insert_video(db, "teste", "https://www.pexels.com/video/1", "Gato", autor="Ana · Pexels",
                             fonte="Pexels: cute cat", fonte_autorizada=True, tom="meme", exige_cc=True,
                             url_midia="https://videos.pexels.com/1.mp4", duracao=12.5, estado=State.no_drive,
                             caminho_capa=tmp_path / "capa.jpg")
    row = await get_video(db, vid)
    assert (row.fonte_autorizada, row.tom, row.exige_cc, row.estado) == (True, "meme", True, State.no_drive)
    assert (row.url_midia, row.duracao, row.caminho_capa) == (
        "https://videos.pexels.com/1.mp4", 12.5, str(tmp_path / "capa.jpg"))


async def test_insert_rejects_unknown_field_without_marking_seen(db):
    with pytest.raises(ValueError):
        await add(db, hack=1)
    assert not await seen(db, "https://youtu.be/v1")
    with pytest.raises(sqlite3.IntegrityError):
        await add(db, autor=None)  # falha no INSERT do vídeo: o link não pode ficar marcado como visto
    assert not await seen(db, "https://youtu.be/v1")
    assert await add(db) is not None


async def test_happy_path_transitions(db):
    vid = await add(db)
    for to in HAPPY:
        await transition(db, vid, to)
        assert (await get_video(db, vid)).estado == to


async def test_invalid_transition_raises(db):
    vid = await add(db)
    with pytest.raises(InvalidTransition):
        await transition(db, vid, State.editado)
    assert (await get_video(db, vid)).estado == State.descoberto
    with pytest.raises(InvalidTransition):
        await transition(db, 999, State.baixando)


async def test_error_records_queue_and_retry_returns(db):
    vid = await add(db, estado=S.pronto)
    await transition(db, vid, S.editando)
    await set_progress(db, vid, 40)
    await transition(db, vid, S.erro, error="falhou")
    row = await get_video(db, vid)
    assert row.estado == S.erro and row.fila_erro == S.pronto and row.erro == "falhou"
    await transition(db, vid, S.pronto)
    row = await get_video(db, vid)
    assert row.estado == S.pronto
    assert row.erro is None and row.fila_erro is None and row.etapa_progresso == 0


async def test_error_from_queue_state_keeps_it(db):
    vid = await add(db, estado=S.separado)
    await transition(db, vid, S.erro, error="drive fora do ar")
    assert (await get_video(db, vid)).fila_erro == S.separado


@pytest.mark.parametrize("working", list(WORKING))
async def test_error_from_working_state_points_to_its_queue(db, working):
    vid = await add(db, estado=working)
    await transition(db, vid, S.erro, error="x")
    row = await get_video(db, vid)
    assert row.fila_erro == WORKING[working]
    await transition(db, vid, row.fila_erro)
    assert (await get_video(db, vid)).estado == WORKING[working]


async def test_transition_refuses_when_state_changed_underneath(db, monkeypatch):
    vid = await add(db, estado=S.editando)
    stale = (await get_video(db, vid)).model_copy(update={"estado": S.pronto})

    async def stale_get(conn, video_id):
        return stale

    monkeypatch.setattr(dbmod, "get_video", stale_get)
    with pytest.raises(InvalidTransition):
        await transition(db, vid, S.editando)  # permitido a partir do pronto lido, mas o vídeo já está em editando
    monkeypatch.undo()
    assert (await get_video(db, vid)).estado == S.editando


async def test_recover_resets_every_working_state(db):
    expected = {}
    for n, working in enumerate(WORKING):
        vid = await add(db, n, estado=working)
        await set_progress(db, vid, 55)
        expected[vid] = WORKING[working]
    other = await add(db, 99, estado=S.no_drive)
    assert await recover(db) == 6
    for vid, queue in expected.items():
        row = await get_video(db, vid)
        assert row.estado == queue and row.etapa_progresso == 0
    assert (await get_video(db, other)).estado == S.no_drive
    assert await recover(db) == 0


async def test_claim_respects_channels_and_order(db):
    a1 = await add(db, 1, "a", nota=1)
    b = await add(db, 2, "b", nota=9)
    await add(db, 3, "a", nota=5)
    a4 = await add(db, 4, "a", nota=7)
    row = await claim(db, S.descoberto, S.baixando, channels=["a"])
    assert (row.id, row.estado, row.canal) == (a1, S.baixando, "a")
    row = await claim(db, S.descoberto, S.baixando, channels=["a"], order="nota DESC, id")
    assert row.id == a4
    assert await claim(db, S.descoberto, S.baixando, channels=[]) is None
    assert (await claim(db, S.descoberto, S.baixando, video_id=b)).id == b
    assert await claim(db, S.descoberto, S.baixando, video_id=b) is None
    assert (await get_video(db, b)).estado == S.baixando


async def test_claim_resets_progress_and_guards_its_inputs(db):
    vid = await add(db, estado=S.pronto)
    await set_progress(db, vid, 30)
    assert (await claim(db, S.pronto, S.editando)).etapa_progresso == 0
    await add(db, 2)
    with pytest.raises(InvalidTransition):
        await claim(db, S.descoberto, S.editado)
    with pytest.raises(ValueError):
        await claim(db, S.descoberto, S.baixando, order="id; DROP TABLE videos")
    with pytest.raises(ValueError):
        await claim(db, S.descoberto, S.baixando, order="senha DESC")
    assert await count_states(db, "teste", [S.descoberto]) == 1


async def test_count_states_and_recent_styles(db):
    await add(db, 1, estado=S.editado, estilo="faixa_cima", atualizado_em="2026-10-17T10:00:00+00:00")
    await add(db, 2, estado=S.arquivado, estilo="borrado", atualizado_em="2026-10-17T11:00:00+00:00")
    await add(db, 3, estado=S.publicado, estilo="comentario", atualizado_em="2026-10-17T09:00:00+00:00")
    await add(db, 4, estado=S.pronto, estilo="karaoke", atualizado_em="2026-10-17T12:00:00+00:00")
    await add(db, 5, "outro", estado=S.editado, estilo="meme_shrek", atualizado_em="2026-10-17T13:00:00+00:00")
    await add(db, 6, estado=S.editado, atualizado_em="2026-10-17T14:00:00+00:00")
    assert await recent_styles(db, "teste") == ["borrado", "faixa_cima"]
    assert await recent_styles(db, "teste", limit=3) == ["borrado", "faixa_cima", "comentario"]
    assert await count_states(db, "teste", [S.editado, S.arquivado]) == 3
    assert await count_states(db, "teste", []) == 0
    assert await count_states(db, "outro", PACK_STATES) == 1


async def test_recent_styles_breaks_ties_by_id_and_counts_review(db):
    ts = "2026-10-17T10:00:00+00:00"
    await add(db, 1, estado=S.publicando, estilo="faixa_cima", atualizado_em=ts)
    await add(db, 2, estado=S.editado, estilo="borrado", atualizado_em=ts)
    assert await recent_styles(db, "teste") == ["borrado", "faixa_cima"]
    await add(db, 3, estado=S.revisar, estilo="karaoke", atualizado_em="2026-10-17T11:00:00+00:00")
    assert await recent_styles(db, "teste") == ["karaoke", "borrado"]


async def test_progress_and_fields(db, tmp_path):
    vid = await add(db)
    await set_progress(db, vid, 142)
    assert (await get_video(db, vid)).etapa_progresso == 100
    await set_progress(db, vid, 42.5)
    await update_fields(db, vid, estilo="borrado", fila_erro=S.pronto, fonte_autorizada=True,
                        caminho_local=tmp_path / "1.mp4", pacote=2)
    row = await get_video(db, vid)
    assert (row.etapa_progresso, row.estilo, row.fila_erro, row.fonte_autorizada) == (42.5, "borrado", S.pronto, True)
    assert (row.caminho_local, row.pacote) == (str(tmp_path / "1.mp4"), 2)
    assert row.atualizado_em != "2026-01-01T00:00:00+00:00"
    await update_fields(db, vid, estilo="karaoke", atualizado_em="2026-01-01T00:00:00+00:00")
    assert (await get_video(db, vid)).atualizado_em == "2026-01-01T00:00:00+00:00"
    with pytest.raises(ValueError):
        await update_fields(db, vid, hack="1")
