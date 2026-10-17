"""Testes das implementações do Drive: pasta local, rclone (com runner falso, sem rede) e make_drive."""
import asyncio
import os
import sys
import time

import pytest

from canaldark.drive import Drive, DriveError, DriveUsage, PastaDrive, RcloneDrive, make_drive, run_process
from canaldark.models import DriveConfig


class FakeRunner:
    """Runner falso do rclone: grava cada argv e devolve os resultados em ordem (o último se repete)."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls: list[list[str]] = []

    async def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(argv)
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, BaseException):
            raise result
        return result


def rclone(run, delays=(0, 0, 0)) -> RcloneDrive:
    return RcloneDrive("gdrive:", "CanalDark", runner=run, delays=delays)


# ---- PastaDrive ---------------------------------------------------------------


async def test_pasta_drive_roundtrip(tmp_path):
    d = PastaDrive(tmp_path / "drive")
    assert isinstance(d, Drive)
    f = tmp_path / "1.mp4"
    f.write_bytes(b"x")
    r = await d.enviar(f, "politica/1-captados")
    assert r == "politica/1-captados/1.mp4" and not f.exists()
    assert (tmp_path / "drive/politica/1-captados/1.mp4").read_bytes() == b"x"
    r2 = await d.mover(r, "politica/2-em-edicao")
    assert r2 == "politica/2-em-edicao/1.mp4" and not (tmp_path / "drive/politica/1-captados/1.mp4").exists()
    local = await d.baixar(r2, tmp_path / "pack")
    assert local == tmp_path / "pack/1.mp4" and local.read_bytes() == b"x"
    assert (tmp_path / "drive/politica/2-em-edicao/1.mp4").exists()  # baixar copia, não move
    assert await d.uso() == DriveUsage(1, None)  # a pasta local não tem total
    await d.apagar(r2)
    await d.apagar(r2)  # segunda vez não falha
    assert not (tmp_path / "drive/politica/2-em-edicao/1.mp4").exists()
    assert (await d.uso()).usado == 0


async def test_pasta_drive_enviar_overwrites_existing_remote(tmp_path):
    d = PastaDrive(tmp_path / "drive")
    old = tmp_path / "drive/teste/3-publicados/2026-10-17/5.mp4"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"velho")
    f = tmp_path / "5.mp4"
    f.write_bytes(b"novo")
    assert await d.enviar(f, "teste/3-publicados/2026-10-17/") == "teste/3-publicados/2026-10-17/5.mp4"
    assert old.read_bytes() == b"novo" and not f.exists()
    assert [p.name for p in old.parent.iterdir()] == ["5.mp4"]  # nada duplicado nem arquivo temporário


async def test_pasta_drive_baixar_overwrites_partial_local_copy(tmp_path):
    d = PastaDrive(tmp_path / "drive")
    remote = tmp_path / "drive/teste/2-em-edicao/5.mp4"
    remote.parent.mkdir(parents=True)
    remote.write_bytes(b"inteiro")
    partial = tmp_path / "pacote/5.mp4"  # sobra de uma descida interrompida por uma queda
    partial.parent.mkdir()
    partial.write_bytes(b"pe")
    assert (await d.baixar("teste/2-em-edicao/5.mp4", tmp_path / "pacote")).read_bytes() == b"inteiro"


async def test_pasta_drive_missing_files_raise_drive_error(tmp_path):
    d = PastaDrive(tmp_path / "drive")
    assert await d.uso() == DriveUsage(0, None)  # a pasta ainda nem existe
    with pytest.raises(DriveError, match="não encontrado"):
        await d.enviar(tmp_path / "nao.mp4", "teste/1-captados")
    with pytest.raises(DriveError, match="teste/1-captados/nao.mp4"):
        await d.baixar("teste/1-captados/nao.mp4", tmp_path / "pacote")
    with pytest.raises(DriveError, match="teste/1-captados/nao.mp4"):
        await d.mover("teste/1-captados/nao.mp4", "teste/2-em-edicao")
    assert not (tmp_path / "pacote").exists()


# ---- RcloneDrive --------------------------------------------------------------


async def test_rclone_commands(tmp_path):
    run = FakeRunner((0, "", ""))
    d = rclone(run)
    assert isinstance(d, Drive)
    f = tmp_path / "1.mp4"
    f.write_bytes(b"x")
    assert await d.enviar(f, "politica/1-captados") == "politica/1-captados/1.mp4"
    assert await d.baixar("politica/2-em-edicao/1.mp4", tmp_path / "pack") == tmp_path / "pack/1.mp4"
    assert await d.mover("politica/1-captados/1.mp4", "politica/2-em-edicao/") == "politica/2-em-edicao/1.mp4"
    await d.apagar("politica/2-em-edicao/1.mp4")
    assert run.calls == [
        ["rclone", "moveto", str(f), "gdrive:CanalDark/politica/1-captados/1.mp4"],
        ["rclone", "copyto", "gdrive:CanalDark/politica/2-em-edicao/1.mp4", str(tmp_path / "pack/1.mp4")],
        ["rclone", "moveto", "gdrive:CanalDark/politica/1-captados/1.mp4",
         "gdrive:CanalDark/politica/2-em-edicao/1.mp4"],
        ["rclone", "deletefile", "gdrive:CanalDark/politica/2-em-edicao/1.mp4"],
    ]


async def test_rclone_retries_then_fails():
    run = FakeRunner((1, "", "x" * 500 + "boom"))
    d = rclone(run, delays=(0, 0, 30))  # depois da última tentativa não espera: falha na hora
    with pytest.raises(DriveError, match="boom") as exc:
        await asyncio.wait_for(d.mover("a/1-captados/1.mp4", "a/2-em-edicao"), 5)
    assert len(run.calls) == 3
    assert "x" * 296 + "boom" in str(exc.value) and "x" * 297 not in str(exc.value)  # só as últimas 300 letras


async def test_rclone_waits_between_attempts_and_recovers():
    run = FakeRunner((1, "", "rede"), (1, "", "rede"), (0, "", ""))
    d = rclone(run, delays=(0.05, 0.1, 30))
    start = time.monotonic()
    await asyncio.wait_for(d.apagar("a/1.mp4"), 5)
    assert len(run.calls) == 3 and time.monotonic() - start >= 0.14


@pytest.mark.parametrize("code", [3, 4])
async def test_rclone_apagar_ignores_missing(code):
    run = FakeRunner((code, "", "not found"))
    await rclone(run).apagar("a/1.mp4")
    assert len(run.calls) == 1
    with pytest.raises(DriveError):  # nos outros comandos, "não encontrado" continua sendo erro
        await rclone(FakeRunner((code, "", "not found"))).mover("a/1.mp4", "b")


async def test_rclone_uso_parses_json():
    run = FakeRunner((0, '{"total": 100, "used": 25, "free": 75}', ""))
    assert (await rclone(run).uso()).fracao == 0.25
    assert run.calls == [["rclone", "about", "gdrive:", "--json"]]


async def test_rclone_uso_counts_gmail_and_photos_in_the_quota():
    # Os 15 GB do Google são divididos com o Gmail e o Fotos ("other"): conta o que não está livre.
    run = FakeRunner((0, '{"total": 100, "used": 25, "trashed": 0, "other": 60, "free": 15}', ""))
    assert await rclone(run).uso() == DriveUsage(85, 100)
    assert await rclone(FakeRunner((0, '{"used": 25}', ""))).uso() == DriveUsage(25, None)
    with pytest.raises(DriveError, match="rclone about"):
        await rclone(FakeRunner((0, "isto não é json", ""))).uso()


async def test_rclone_missing_binary():
    run = FakeRunner(FileNotFoundError("rclone"))
    with pytest.raises(DriveError, match="brew install rclone"):
        await rclone(run).uso()
    assert len(run.calls) == 1


async def test_rclone_enviar_without_local_file_fails_before_running(tmp_path):
    run = FakeRunner((0, "", ""))
    with pytest.raises(DriveError, match="não encontrado"):
        await rclone(run).enviar(tmp_path / "nao.mp4", "a/1-captados")
    assert run.calls == []


def test_rclone_remote_needs_the_colon():
    with pytest.raises(ValueError, match="gdrive:"):
        RcloneDrive("gdrive", "CanalDark")  # sem os dois-pontos o rclone gravaria numa pasta local


async def test_run_process_returns_code_and_output():
    code = "import sys; print('saida'); print('falha', file=sys.stderr); sys.exit(3)"
    assert await run_process([sys.executable, "-c", code]) == (3, "saida\n", "falha\n")


async def test_run_process_kills_the_child_when_cancelled(tmp_path):
    pid_file = tmp_path / "pid"
    code = "import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(30)"
    task = asyncio.create_task(run_process([sys.executable, "-c", code, str(pid_file)]))
    deadline = time.monotonic() + 5
    while not (pid_file.exists() and pid_file.read_text()):
        assert time.monotonic() < deadline and not task.done(), "o processo filho não começou"
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    with pytest.raises(ProcessLookupError):  # parar a plataforma não deixa um rclone órfão mexendo no Drive
        os.kill(int(pid_file.read_text()), 0)


# ---- make_drive -----------------------------------------------------------------


def test_make_drive_by_type(tmp_path, monkeypatch):
    d = make_drive(DriveConfig(), tmp_path)
    assert isinstance(d, PastaDrive) and d.root == tmp_path / "dados/drive/CanalDark"
    r = make_drive(DriveConfig(tipo="rclone", remote="meu:", pasta="Canal"), tmp_path)
    assert isinstance(r, RcloneDrive) and (r.remote, r.base) == ("meu:", "Canal")
    monkeypatch.setenv("HOME", str(tmp_path / "casa"))
    assert make_drive(DriveConfig(pasta_local="~/Drive"), tmp_path).root == tmp_path / "casa/Drive/CanalDark"
