import pytest
from fastapi.testclient import TestClient

from canaldark.db import get_video, insert_video
from canaldark.events import EventBus
from canaldark.models import ChannelConfig, State
from canaldark.pipeline import Pipeline
from canaldark.web.app import create_app

LOCAL = "http://localhost:8000"
CFG = ChannelConfig(id="gta6", nome="GTA 6 News", fontes=[{"tipo": "google_news", "busca": "x"}],
                    formatos={"noticia": 1.0}, estilo_manchete="a", persona_roteiro="b", voz={"id": "pm_alex"},
                    horarios=["09:00"], contas={"youtube": "gta6"})


@pytest.fixture
async def app(db, tmp_path):
    p = Pipeline(db, EventBus(db), {"gta6": CFG}, [], root=tmp_path, dry_run=True)
    return create_app(p)


async def test_columns_show_history(db, app):
    vid = await insert_video(db, "gta6", "https://x/1", "Trailer", "IGN", state=State.publicado)
    await insert_video(db, "gta6", "https://x/2", "Novo", "IGN")
    with TestClient(app, base_url=LOCAL) as c:
        cols = {s: c.get(f"/api/videos?stage={s}").json() for s in ("captacao", "edicao", "publicacao")}
    for s in cols:
        card = next(v for v in cols[s] if v["id"] == vid)
        assert card["done"] is True and card["status"] == "ok"
    assert [v["id"] for v in cols["captacao"]] == [vid + 1, vid]
    assert all(v["id"] == vid for v in cols["publicacao"])


async def test_pagination_before_id(db, app):
    for i in range(5):
        await insert_video(db, "gta6", f"https://x/{i}", str(i), "s")
    with TestClient(app, base_url=LOCAL) as c:
        page = c.get("/api/videos?stage=captacao&limit=2&before_id=4").json()
    assert [v["id"] for v in page] == [3, 2]


async def test_retry_from_erro(db, app):
    vid = await insert_video(db, "gta6", "https://x/1", "t", "s", state=State.editando)
    from canaldark.db import transition
    await transition(db, vid, State.erro, error="FFmpeg falhou")
    with TestClient(app, base_url=LOCAL) as c:
        card = next(v for v in c.get("/api/videos?stage=edicao").json() if v["id"] == vid)
        assert card["status"] == "erro" and card["erro"] == "FFmpeg falhou"
        assert c.post(f"/api/videos/{vid}/retry").status_code == 200
    assert (await get_video(db, vid)).estado == State.baixado


async def test_approve_and_discard(db, app):
    a = await insert_video(db, "gta6", "https://x/a", "t", "s", state=State.editando)
    b = await insert_video(db, "gta6", "https://x/b", "t", "s", state=State.editando)
    from canaldark.db import transition
    await transition(db, a, State.revisar, error="risco")
    await transition(db, b, State.revisar, error="risco")
    with TestClient(app, base_url=LOCAL) as c:
        assert c.post(f"/api/videos/{a}/approve").status_code == 200
        assert c.post(f"/api/videos/{b}/discard").status_code == 200
        assert c.post(f"/api/videos/{a}/discard").status_code == 409
    row = await get_video(db, a)
    assert row.estado == State.baixado and row.aprovado
    assert await get_video(db, b) is None


async def test_ws_streams_events(app):
    with TestClient(app, base_url=LOCAL) as c:
        with c.websocket_connect("/ws", headers={"host": "localhost:8000"}) as ws:
            c.post("/api/pause", json={"channel": "gta6", "stage": "edicao"})
            msgs = [ws.receive_json() for _ in range(1)]
            assert msgs[0]["type"] == "log" and "pausad" in msgs[0]["data"]["msg"]
        assert c.get("/api/state").json()["pausas"] == [{"channel": "gta6", "stage": "edicao", "until": None}]
        c.post("/api/resume", json={"channel": "gta6", "stage": "edicao"})
        assert c.get("/api/state").json()["pausas"] == []


async def test_static_index_served(app):
    with TestClient(app, base_url=LOCAL) as c:
        r = c.get("/")
    assert r.status_code == 200 and "Canal Dark" in r.text and "app.js" in r.text


async def test_redo_records_instruction(db, app):
    vid = await insert_video(db, "gta6", "https://x/r", "t", "s", state=State.editando)
    from canaldark.db import transition, update_fields
    await update_fields(db, vid, pacote_json='{"manchete": ["velha"]}')
    await transition(db, vid, State.editado)
    with TestClient(app, base_url=LOCAL) as c:
        r = c.post(f"/api/videos/{vid}/redo", json={"campo": "manchete", "instrucao": "mais curta"})
        assert r.status_code == 200
        status = c.get("/api/status").json()
    row = await get_video(db, vid)
    assert row.estado == State.baixado and "mais curta" in row.instrucao and row.pacote_json is None
    assert status["por_estado"]["gta6"]["baixado"] == 1


async def test_add_source_appends_to_yaml(db, tmp_path):
    (tmp_path / "canais").mkdir()
    yml = tmp_path / "canais" / "gta6.yaml"
    yml.write_text("id: gta6\nnome: GTA\nfontes:\n  - tipo: google_news\n    busca: x  # comentário\n"
                   "formatos: {noticia: 1.0}\nestilo_manchete: a\npersona_roteiro: b\nvoz: {id: pm_alex}\n"
                   "horarios: ['09:00']\ncontas: {youtube: gta6}\n")
    from canaldark.config import load_channels
    p = Pipeline(db, EventBus(db), load_channels(tmp_path / "canais"), [], root=tmp_path, dry_run=True)
    with TestClient(create_app(p), base_url=LOCAL) as c:
        r = c.post("/api/sources", json={"channel": "gta6", "url": "https://www.reddit.com/r/GTA6/"})
    assert r.status_code == 200, r.text
    assert "# comentário" in yml.read_text()
    assert [f.tipo for f in load_channels(tmp_path / "canais")["gta6"].fontes] == ["google_news", "reddit"]
    assert [f.tipo for f in p.cfgs["gta6"].fontes] == ["google_news", "reddit"]


async def test_foreign_origin_and_host_rejected(app):
    with TestClient(app, base_url=LOCAL) as c:
        assert c.post("/api/pause", json={}, headers={"Origin": "https://evil.example"}).status_code == 403
        assert c.post("/api/pause", json={}, headers={"Origin": LOCAL}).status_code == 200
    with TestClient(app, base_url="http://evil.example") as c:
        assert c.get("/api/state").status_code == 403
    with TestClient(app, base_url=LOCAL) as c:
        import pytest as _p
        from starlette.websockets import WebSocketDisconnect
        with _p.raises(WebSocketDisconnect):
            with c.websocket_connect("/ws", headers={"host": "localhost:8000", "Origin": "https://evil.example"}) as ws:
                ws.receive_json()


async def test_publish_now_blocked_in_tse_window(db, tmp_path, monkeypatch):
    from datetime import datetime

    from canaldark.rules import SP
    from canaldark.web import app as webapp
    pol = CFG.model_copy(update={"id": "pol", "regras": "politica"})
    vid = await insert_video(db, "pol", "https://x/p", "t", "s", state=State.editado)
    monkeypatch.setattr(webapp, "_now", lambda: datetime(2026, 10, 24, 10, tzinfo=SP))
    p = Pipeline(db, EventBus(db), {"pol": pol}, [], root=tmp_path, dry_run=True)
    with TestClient(create_app(p), base_url=LOCAL) as c:
        r = c.post(f"/api/videos/{vid}/publish-now")
    assert r.status_code == 409 and "TSE" in r.json()["detail"]
    assert (await get_video(db, vid)).estado == State.editado
