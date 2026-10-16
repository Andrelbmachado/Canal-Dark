from datetime import datetime
from pathlib import Path

import pytest

from canaldark.db import get_video, insert_video, now_iso
from canaldark.events import EventBus
from canaldark.models import ChannelConfig, Package, State
from canaldark.publish import stage
from canaldark.publish.base import AuthExpired, PublishResult
from canaldark.publish.instagram import IG_DAILY_CAP
from canaldark.rules import SP

BASE = dict(fontes=[{"tipo": "google_news", "busca": "x"}], formatos={"noticia": 1.0}, estilo_manchete="a",
            persona_roteiro="b", voz={"id": "pm_alex"}, horarios=["09:00"],
            contas={"youtube": "c1", "instagram": "c1"})
CFG = ChannelConfig(id="gta6", nome="GTA", **BASE)
POL = ChannelConfig(id="pol", nome="Política", regras="politica", **BASE)
PKG = Package(formato="noticia", manchete=["x"], roteiro="r", fonte="f", titulo_post="T", descricao_post="D",
              hashtags=["#gta6"])


class FakeUploader:
    def __init__(self, fail=None):
        self.calls, self.fail = 0, fail

    async def upload(self, video, title, description, tags, on_progress, synthetic=False):
        self.calls += 1
        if self.fail:
            raise self.fail
        await on_progress(100)
        return PublishResult(id_externo=f"id{self.calls}", url=f"https://x/{self.calls}")


async def _ready(db, tmp_path, channel="gta6", n=0):
    out = tmp_path / "final.mp4"
    out.write_bytes(b"x")
    return await insert_video(db, channel, f"https://youtu.be/{channel}{n}", "t", "s", state=State.editado,
                              caminho_final=str(out), pacote_json=PKG.model_dump_json(), formato="noticia")


async def test_retry_skips_platform_already_published(db, tmp_path):
    vid = await _ready(db, tmp_path)
    yt, ig = FakeUploader(), FakeUploader(fail=RuntimeError("Graph API 500"))
    ups = {("youtube", "c1"): yt, ("instagram", "c1"): ig}
    bus = EventBus(db)
    row = await stage.claim_due(db, {"gta6": CFG}, datetime(2026, 10, 16, 9, 5, tzinfo=SP))
    assert await stage.publish_video(db, bus, CFG, row, lambda p, a: ups.get((p, a))) == State.erro
    await stage.transition(db, vid, State.editado)
    ig.fail = None
    row = await stage.claim_due(db, {"gta6": CFG}, datetime(2026, 10, 16, 9, 6, tzinfo=SP), force=True)
    assert await stage.publish_video(db, bus, CFG, row, lambda p, a: ups.get((p, a))) == State.publicado
    assert (yt.calls, ig.calls) == (1, 2)


async def test_auth_expired_returns_to_editado(db, tmp_path):
    vid = await _ready(db, tmp_path)
    ups = {("youtube", "c1"): FakeUploader(fail=AuthExpired("token revogado")), ("instagram", "c1"): FakeUploader()}
    row = await stage.claim_due(db, {"gta6": CFG}, datetime(2026, 10, 16, 9, 5, tzinfo=SP))
    paused: set = set()
    assert await stage.publish_video(db, EventBus(db), CFG, row, lambda p, a: ups.get((p, a)), paused) == State.editado
    assert (await get_video(db, vid)).estado == State.editado and ("youtube", "c1") in paused


async def test_instagram_daily_cap_25(db, tmp_path):
    for i in range(IG_DAILY_CAP):
        v = await insert_video(db, "gta6", f"https://old/{i}", "t", "s", state=State.editado)
        await db.execute("INSERT INTO publicacoes (video_id, plataforma, conta, id_externo, publicado_em) VALUES (?,?,?,?,?)",
                         (v, "instagram", "c1", "x", now_iso()))
    await db.commit()
    vid = await _ready(db, tmp_path)
    yt, ig = FakeUploader(), FakeUploader()
    row = await stage.claim_due(db, {"gta6": CFG}, datetime.now(SP), force=True, video_id=vid)
    ups = {("youtube", "c1"): yt, ("instagram", "c1"): ig}
    assert await stage.publish_video(db, EventBus(db), CFG, row, lambda p, a: ups.get((p, a))) == State.editado
    assert (yt.calls, ig.calls) == (1, 0)


async def test_politics_not_claimed_in_blackout(db, tmp_path):
    await _ready(db, tmp_path, "pol")
    assert await stage.claim_due(db, {"pol": POL}, datetime(2026, 10, 24, 9, 5, tzinfo=SP)) is None
    assert await stage.claim_due(db, {"pol": POL}, datetime(2026, 10, 28, 9, 5, tzinfo=SP)) is not None


async def test_not_claimed_before_slot(db, tmp_path):
    await _ready(db, tmp_path)
    assert await stage.claim_due(db, {"gta6": CFG}, datetime(2026, 10, 16, 8, 0, tzinfo=SP)) is None


async def test_dry_run_copies_file(tmp_path):
    src = tmp_path / "final.mp4"
    src.write_bytes(b"video")

    async def noop(p): pass
    res = await stage.DryRunUploader("youtube", tmp_path / "ensaio").upload(src, "T", "D", [], noop)
    assert Path(res.url.removeprefix("file://")).read_bytes() == b"video"


async def test_instagram_resumable_flow(tmp_path, monkeypatch):
    import httpx
    import respx

    from canaldark.publish import instagram as igm
    monkeypatch.setattr(igm, "load_secret", lambda p, a: {"token": "TK", "ig_user_id": "17841"})
    monkeypatch.setattr(igm, "POLL_EVERY", 0)
    video = tmp_path / "final.mp4"
    video.write_bytes(b"abc")
    statuses = iter([{"status_code": "IN_PROGRESS"}, {"status_code": "FINISHED"}])
    with respx.mock() as m:
        create = m.post(f"{igm.GRAPH}/17841/media").mock(return_value=httpx.Response(200, json={"id": "C1"}))
        up = m.post(f"{igm.RUPLOAD}/C1").mock(return_value=httpx.Response(200, json={"success": True}))
        m.get(f"{igm.GRAPH}/C1").mock(side_effect=lambda req: httpx.Response(200, json=next(statuses)))
        m.post(f"{igm.GRAPH}/17841/media_publish").mock(return_value=httpx.Response(200, json={"id": "M9"}))
        m.get(f"{igm.GRAPH}/M9").mock(return_value=httpx.Response(200, json={"permalink": "https://instagram.com/reel/x"}))
        seen = []

        async def rec(p):
            seen.append(p)
        res = await igm.InstagramUploader("c1").upload(video, "T", "D", [], rec)
    assert res.id_externo == "M9" and seen[-1] == 100
    assert b"upload_type=resumable" in create.calls[0].request.content
    assert up.calls[0].request.headers["file_size"] == "3"


async def test_instagram_token_expired(tmp_path, monkeypatch):
    import httpx
    import respx

    from canaldark.publish import instagram as igm
    monkeypatch.setattr(igm, "load_secret", lambda p, a: {"token": "TK", "ig_user_id": "17841"})
    video = tmp_path / "final.mp4"
    video.write_bytes(b"abc")
    with respx.mock() as m:
        m.post(f"{igm.GRAPH}/17841/media").mock(return_value=httpx.Response(
            400, json={"error": {"code": 190, "message": "Error validating access token"}}))

        async def noop(p): pass
        with pytest.raises(AuthExpired):
            await igm.InstagramUploader("c1").upload(video, "T", "D", [], noop)


async def test_pending_video_does_not_block_channel_queue(db, tmp_path):
    ids = [await _ready(db, tmp_path, n=i) for i in range(3)]
    yt, ig = FakeUploader(), FakeUploader(fail=AuthExpired("token vencido"))
    ups = {("youtube", "c1"): yt, ("instagram", "c1"): ig}
    paused: set = set()
    seen = []
    for _ in range(4):
        row = await stage.claim_due(db, {"gta6": CFG}, datetime.now(SP), force=True, paused=paused)
        if row is None:
            break
        seen.append(row.id)
        await stage.publish_video(db, EventBus(db), CFG, row, lambda p, a: ups.get((p, a)), paused)
    assert seen == ids and yt.calls == 3 and ig.calls == 1


async def test_retry_later_cools_down_account(db, tmp_path):
    from canaldark.publish.base import RetryLater
    stage.COOLDOWN.clear()
    await _ready(db, tmp_path)
    yt = FakeUploader(fail=RetryLater("cota"))
    ups = {("youtube", "c1"): yt, ("instagram", "c1"): FakeUploader()}
    row = await stage.claim_due(db, {"gta6": CFG}, datetime.now(SP), force=True)
    assert await stage.publish_video(db, EventBus(db), CFG, row, lambda p, a: ups.get((p, a))) == State.editado
    assert await stage.claim_due(db, {"gta6": CFG}, datetime.now(SP), force=True) is None
    stage.COOLDOWN.clear()
