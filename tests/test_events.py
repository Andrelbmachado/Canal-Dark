import asyncio

from canaldark.events import EventBus


async def test_subscriber_receives_event(db):
    bus = EventBus(db)
    sub = bus.subscribe()
    await bus.publish("log", {"msg": "oi"})
    ev = await asyncio.wait_for(sub.__anext__(), 1)
    assert ev["type"] == "log" and ev["data"]["msg"] == "oi"


async def test_progress_throttled(db):
    bus = EventBus(db)
    sub = bus.subscribe()
    for i in range(50):
        await bus.progress(7, i * 2)
    await bus.progress(7, 100)
    got = []
    while not sub.empty():
        got.append(await sub.__anext__())
    assert len(got) <= 2
    assert got[-1]["data"]["pct"] == 100


async def test_events_persisted(db):
    bus = EventBus(db)
    await bus.log("captacao", "baixando X", video_id=3)
    async with db.execute("SELECT tipo, dados FROM eventos") as cur:
        rows = await cur.fetchall()
    assert rows[0]["tipo"] == "log" and "baixando X" in rows[0]["dados"]


async def test_recent_logs(db):
    bus = EventBus(db)
    for i in range(5):
        await bus.log("sistema", f"m{i}")
    logs = await bus.recent_logs(3)
    assert [l["data"]["msg"] for l in logs] == ["m2", "m3", "m4"]
