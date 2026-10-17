"""Testes dos pacotes de 15 por canal e da descida do Drive (fixture ctx com o FakeDrive)."""
import asyncio
from pathlib import Path

import pytest

from canaldark import pack
from canaldark.db import get_video, insert_video
from canaldark.drive import DriveError
from canaldark.models import PlatformConfig, State
from canaldark.pack import (download_pack_video, pack_download_worker, pack_in_progress, pack_manager, pack_tick,
                            start_next_pack)

S = State


async def add(ctx, n, estado=S.no_drive, channel="teste", **fields) -> int:
    return await insert_video(ctx.db, channel, f"https://youtu.be/v{n}", f"Vídeo {n}", estado=estado, **fields)


def drain(sub) -> list[dict]:
    events = []
    while not sub.empty():
        events.append(sub.queue.get_nowait())
    return events


async def logs(ctx) -> list[dict]:
    return [e["data"] for e in await ctx.bus.recent_logs()]


async def wait_state(ctx, video_id, state, timeout=5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while (row := await get_video(ctx.db, video_id)).estado != state:
        assert loop.time() < end, f"vídeo {video_id} ficou em {row.estado.value}, esperava {state.value}"
        await asyncio.sleep(0.01)
    return row


# ---- separação do pacote ------------------------------------------------------


async def test_starts_pack_of_15_by_score(ctx):
    notas = [5, 1, 9, 2, 7, 0.5, 8, 3, 6, 4, 10, 1.5, 11, 12, 13, 14, 15, 2.5, 16, 17]
    ids = [await add(ctx, i, nota=n) for i, n in enumerate(notas)]
    assert ids == list(range(1, 21))
    sub = ctx.bus.subscribe()
    picked = await start_next_pack(ctx, "teste")
    assert picked == [20, 19, 17, 16, 15, 14, 13, 11, 3, 7, 5, 9, 1, 10, 8]  # nota 17, 16, 15, ... 3
    for vid in ids:
        row = await get_video(ctx.db, vid)
        assert (row.estado, row.pacote) == ((S.separado, 1) if vid in picked else (S.no_drive, None))
    events = [e["data"] for e in drain(sub) if e["type"] == "video.estado"]
    assert events == [{"id": vid, "estado": "separado", "pacote": 1} for vid in picked]
    assert "[teste] pacote 1: 15 vídeo(s)" in [log["msg"] for log in await logs(ctx)]


async def test_ties_go_to_the_oldest_and_pack_size_comes_from_platform(ctx):
    ctx.plat = PlatformConfig(pacote=2)
    a, b, _ = [await add(ctx, i, nota=3) for i in range(3)]
    assert await start_next_pack(ctx, "teste") == [a, b]


async def test_nothing_to_pack_starts_nothing(ctx):
    assert await start_next_pack(ctx, "teste") == []
    assert await logs(ctx) == []
    vid = await add(ctx, 1)
    assert await start_next_pack(ctx, "teste") == [vid]
    assert (await get_video(ctx.db, vid)).pacote == 1  # a tentativa vazia não gastou um número


@pytest.mark.parametrize("state", [S.separado, S.descendo, S.pronto, S.editando, S.revisar, S.editado,
                                   S.publicando, S.publicado, S.arquivando])
async def test_no_new_pack_while_current_runs(ctx, state):
    await add(ctx, 1, estado=state, pacote=1)
    waiting = await add(ctx, 2)
    assert await pack_in_progress(ctx.db, "teste") == 1
    assert await start_next_pack(ctx, "teste") == []
    assert (await get_video(ctx.db, waiting)).estado == S.no_drive


async def test_erro_and_descartado_do_not_block(ctx):
    for i in range(13):
        await add(ctx, i, estado=S.arquivado, pacote=1)
    await add(ctx, 13, estado=S.erro, fila_erro=S.pronto, pacote=1)
    await add(ctx, 14, estado=S.descartado, pacote=1)
    new = [await add(ctx, 20 + i, nota=i) for i in range(3)]
    assert await pack_in_progress(ctx.db, "teste") == 0
    assert sorted(await start_next_pack(ctx, "teste")) == new
    assert [(await get_video(ctx.db, v)).pacote for v in new] == [2, 2, 2]


async def test_revisar_blocks_next_pack(ctx):
    for i in range(14):
        await add(ctx, i, estado=S.arquivado, pacote=1)
    await add(ctx, 14, estado=S.revisar, pacote=1)
    waiting = await add(ctx, 20)
    assert await start_next_pack(ctx, "teste") == []
    assert (await get_video(ctx.db, waiting)).estado == S.no_drive


async def test_packs_are_per_channel(ctx, cfg_factory):
    ctx.cfgs["outro"] = cfg_factory(id="outro")
    await add(ctx, 1, estado=S.editando, channel="outro", pacote=4)  # o outro canal está no meio do pacote 4
    theirs = await add(ctx, 2, channel="outro", nota=50)
    mine = await add(ctx, 3)
    assert await start_next_pack(ctx, "teste") == [mine]
    assert (await get_video(ctx.db, mine)).pacote == 1  # a numeração é de cada canal
    assert (await get_video(ctx.db, theirs)).estado == S.no_drive


async def test_pack_tick_covers_the_active_channels(ctx, cfg_factory):
    ctx.cfgs["outro"] = cfg_factory(id="outro")
    ctx.cfgs["parado"] = cfg_factory(id="parado")
    for i, channel in enumerate(["teste", "teste", "outro", "parado"]):
        await add(ctx, i, channel=channel)
    asked = []
    ctx.active = lambda stage: asked.append(stage) or ["teste", "outro"]
    assert await pack_tick(ctx) == 3
    assert asked == ["edicao"]
    assert await pack_in_progress(ctx.db, "parado") == 0


async def test_paused_channel_gets_no_pack(ctx):
    waiting = await add(ctx, 1)
    ctx.active = lambda stage: []
    assert await pack_tick(ctx) == 0
    assert (await get_video(ctx.db, waiting)).estado == S.no_drive


async def test_pack_manager_survives_errors_and_stops(ctx):
    waiting = await add(ctx, 1)
    asked = []

    def active(stage):
        asked.append(stage)
        if len(asked) == 1:
            raise RuntimeError("falha momentânea")
        return ["teste"]

    ctx.active = active
    task = asyncio.create_task(pack_manager(ctx, every=0.01))
    await wait_state(ctx, waiting, S.separado)
    ctx.stop.set()
    await asyncio.wait_for(task, 2)
    assert any("falha momentânea" in log["msg"] and log["level"] == "erro" for log in await logs(ctx))


# ---- descida do Drive ---------------------------------------------------------


async def test_download_moves_to_em_edicao_and_downloads(ctx):
    vid = await add(ctx, 1, estado=S.descendo, pacote=1, remoto_bruto="teste/1-captados/5.mp4")
    ctx.drive.files["teste/1-captados/5.mp4"] = b"video"
    sub = ctx.bus.subscribe()
    assert await download_pack_video(ctx, await get_video(ctx.db, vid)) == S.pronto
    row = await get_video(ctx.db, vid)
    assert row.estado == S.pronto and row.remoto_bruto == "teste/2-em-edicao/5.mp4"
    assert Path(row.caminho_local) == ctx.data / "pacote/teste/5.mp4"
    assert Path(row.caminho_local).read_bytes() == b"video"
    assert set(ctx.drive.files) == {"teste/2-em-edicao/5.mp4"}
    assert {"id": vid, "estado": "pronto"} in [e["data"] for e in drain(sub) if e["type"] == "video.estado"]


async def test_download_skips_move_when_already_in_em_edicao(ctx):
    vid = await add(ctx, 1, estado=S.descendo, remoto_bruto="teste/2-em-edicao/7.mp4")
    ctx.drive.files["teste/2-em-edicao/7.mp4"] = b"v"
    assert await download_pack_video(ctx, await get_video(ctx.db, vid)) == S.pronto
    assert [c[0] for c in ctx.drive.calls] == ["baixar"]


async def test_download_finds_a_file_moved_just_before_a_crash(ctx):
    # A queda veio depois do mover e antes de gravar o novo remoto_bruto: o banco ainda aponta 1-captados.
    vid = await add(ctx, 1, estado=S.descendo, remoto_bruto="teste/1-captados/7.mp4")
    ctx.drive.files["teste/2-em-edicao/7.mp4"] = b"v"
    assert await download_pack_video(ctx, await get_video(ctx.db, vid)) == S.pronto
    row = await get_video(ctx.db, vid)
    assert row.remoto_bruto == "teste/2-em-edicao/7.mp4" and Path(row.caminho_local).read_bytes() == b"v"


async def test_download_failure_goes_to_erro_separado(ctx):
    vid = await add(ctx, 1, estado=S.descendo, remoto_bruto="teste/1-captados/7.mp4")
    ctx.drive.files["teste/1-captados/7.mp4"] = b"v"
    ctx.drive.fail["baixar"] = DriveError("a rede caiu")
    sub = ctx.bus.subscribe()
    assert await download_pack_video(ctx, await get_video(ctx.db, vid)) == S.erro
    row = await get_video(ctx.db, vid)
    assert (row.estado, row.fila_erro, row.caminho_local) == (S.erro, S.separado, None)
    assert "a rede caiu" in row.erro
    events = drain(sub)
    assert {"id": vid, "estado": "erro", "erro": row.erro} in [e["data"] for e in events if e["type"] == "video.estado"]
    assert [e["data"]["level"] for e in events if e["type"] == "log"] == ["erro"]


async def test_file_missing_from_drive_goes_to_erro(ctx):
    vid = await add(ctx, 1, estado=S.descendo, remoto_bruto="teste/1-captados/7.mp4")
    assert await download_pack_video(ctx, await get_video(ctx.db, vid)) == S.erro
    row = await get_video(ctx.db, vid)
    assert row.fila_erro == S.separado and "teste/1-captados/7.mp4" in row.erro  # cita o arquivo procurado
    assert row.remoto_bruto == "teste/1-captados/7.mp4"


async def test_video_without_drive_path_goes_to_erro(ctx):
    vid = await add(ctx, 1, estado=S.descendo)
    assert await download_pack_video(ctx, await get_video(ctx.db, vid)) == S.erro
    row = await get_video(ctx.db, vid)
    assert row.fila_erro == S.separado and "Drive" in row.erro and ctx.drive.calls == []


async def test_worker_brings_down_only_active_channels(ctx, cfg_factory):
    ctx.cfgs["parado"] = cfg_factory(id="parado")
    mine = await add(ctx, 1, estado=S.separado, remoto_bruto="teste/1-captados/1.mp4")
    paused = await add(ctx, 2, estado=S.separado, channel="parado", remoto_bruto="parado/1-captados/2.mp4")
    ctx.drive.files.update({"teste/1-captados/1.mp4": b"a", "parado/1-captados/2.mp4": b"b"})
    ctx.active = lambda stage: ["teste"] if stage == "edicao" else []
    sub = ctx.bus.subscribe()
    task = asyncio.create_task(pack_download_worker(ctx))
    await wait_state(ctx, mine, S.pronto)
    ctx.stop.set()
    await asyncio.wait_for(task, 2)
    assert (await get_video(ctx.db, paused)).estado == S.separado
    states = [e["data"]["estado"] for e in drain(sub) if e["type"] == "video.estado"]
    assert states == ["descendo", "pronto"]


async def test_worker_survives_an_unexpected_error(ctx, monkeypatch):
    bad = await add(ctx, 1, estado=S.separado, remoto_bruto="teste/1-captados/1.mp4")
    good = await add(ctx, 2, estado=S.separado, remoto_bruto="teste/1-captados/2.mp4")
    ctx.drive.files.update({"teste/1-captados/1.mp4": b"a", "teste/1-captados/2.mp4": b"b"})
    real = pack.download_pack_video

    async def buggy(c, row):
        if row.id == bad:
            raise RuntimeError("bug inesperado")
        return await real(c, row)

    monkeypatch.setattr(pack, "download_pack_video", buggy)
    task = asyncio.create_task(pack_download_worker(ctx))
    await wait_state(ctx, good, S.pronto)
    ctx.stop.set()
    await asyncio.wait_for(task, 2)
    row = await get_video(ctx.db, bad)
    assert (row.estado, row.fila_erro) == (S.erro, S.separado) and "bug inesperado" in row.erro
