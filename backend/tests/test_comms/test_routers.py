"""Desk <-> iPad comms API against real Postgres + fakeredis."""
from __future__ import annotations

import json
import time

import fakeredis
import pytest
from fakeredis import aioredis as fake_aioredis
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.core.settings import settings
from backend.models.comms import AgendaItem, CommsPreset, Station, StationMessage
from backend.models.team import TeamProfile

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def session_factory():
    engine = create_async_engine(settings.POSTGRES_DSN, future=True)
    try:
        async with engine.connect() as conn:
            await conn.exec_driver_sql("SELECT 1 FROM stations LIMIT 1")
    except Exception:
        await engine.dispose()
        pytest.skip("Real Postgres (migrated) not reachable; skipping comms tests")
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def wipe():
        async with factory() as session:
            for model in (StationMessage, Station, CommsPreset, AgendaItem):
                await session.execute(delete(model))
            await session.execute(delete(TeamProfile).where(TeamProfile.team_number.like("CM%")))
            await session.commit()

    await wipe()
    yield factory
    await wipe()
    await engine.dispose()


@pytest.fixture
async def ctx(session_factory):
    from backend.core.db import get_db
    from backend.core.dependencies import ALL_PERMISSIONS, CurrentPrincipal, get_current_principal, get_redis
    from backend.main import create_app

    redis = fake_aioredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    app = create_app()
    state = {"principal": CurrentPrincipal(subject="tester", permissions={ALL_PERMISSIONS})}

    async def _db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_redis] = lambda: redis
    app.dependency_overrides[get_current_principal] = lambda: state["principal"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.state, client.redis, client.app_ = state, redis, app
        yield client
    await redis.aclose()


async def make_station(c, slug="emcee-1", role="emcee", name=None):
    resp = await c.post("/api/v1/comms/stations", json={"slug": slug, "name": name or slug, "role": role})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def events(c, fn):
    """Runs `fn` while capturing everything published on the event bus."""
    pubsub = c.redis.pubsub()
    await pubsub.subscribe("qecomp:events")
    await pubsub.get_message(timeout=1)
    await fn()
    out = []
    while (m := await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.2)) is not None:
        out.append(json.loads(m["data"]))
    return out


async def test_station_crud_and_conflicts(ctx):
    await make_station(ctx, "emcee-1")
    assert (await ctx.post("/api/v1/comms/stations", json={"slug": "emcee-1", "name": "x", "role": "emcee"})).status_code == 409
    assert (await ctx.post("/api/v1/comms/stations", json={"slug": "BAD SLUG", "name": "x", "role": "emcee"})).status_code == 422
    assert (await ctx.put("/api/v1/comms/stations/emcee-1", json={"name": "Main emcee", "role": "floor_manager"})).json()["role"] == "floor_manager"
    assert [s["slug"] for s in (await ctx.get("/api/v1/comms/stations")).json()] == ["emcee-1"]
    assert (await ctx.delete("/api/v1/comms/stations/emcee-1")).status_code == 204
    assert (await ctx.delete("/api/v1/comms/stations/emcee-1")).status_code == 404
    assert (await ctx.put("/api/v1/comms/stations/nope", json={"name": "x", "role": "emcee"})).status_code == 404


async def test_preset_and_agenda_crud(ctx):
    preset = (await ctx.post("/api/v1/comms/presets", json={"label": "Wrap up", "message": "Please wrap up", "flash": True})).json()
    assert preset["flash"] is True and preset["role"] is None
    assert (await ctx.put(f"/api/v1/comms/presets/{preset['id']}", json={"label": "Wrap", "message": "Wrap", "role": "emcee"})).json()["role"] == "emcee"
    assert len((await ctx.get("/api/v1/comms/presets")).json()) == 1
    assert (await ctx.delete(f"/api/v1/comms/presets/{preset['id']}")).status_code == 204
    assert (await ctx.delete(f"/api/v1/comms/presets/{preset['id']}")).status_code == 404

    a = (await ctx.post("/api/v1/comms/agenda", json={"scope": "day", "start_time": "10:30", "end_time": "13:00", "title": "Quals"})).json()
    await ctx.post("/api/v1/comms/agenda", json={"scope": "day", "start_time": "08:30", "title": "Open"})
    assert [i["title"] for i in (await ctx.get("/api/v1/comms/agenda")).json()] == ["Open", "Quals"]
    assert (await ctx.put(f"/api/v1/comms/agenda/{a['id']}", json={"scope": "finals", "start_time": "16:00", "title": "Finals"})).json()["scope"] == "finals"
    assert (await ctx.post("/api/v1/comms/agenda", json={"scope": "day", "start_time": "9am", "title": "x"})).status_code == 422
    assert (await ctx.delete(f"/api/v1/comms/agenda/{a['id']}")).status_code == 204
    assert (await ctx.delete(f"/api/v1/comms/agenda/{a['id']}")).status_code == 404


async def test_send_targets_roles_and_stations_and_publishes(ctx):
    await make_station(ctx, "emcee-1", "emcee")
    await make_station(ctx, "emcee-2", "emcee")
    await make_station(ctx, "floor-1", "floor_manager")

    out = []

    async def go():
        out.append((await ctx.post("/api/v1/comms/send", json={"roles": ["emcee"], "text": "Hello", "flash": True})).json())
        out.append((await ctx.post("/api/v1/comms/send", json={"stations": ["floor-1"], "text": "Hi floor"})).json())
        out.append((await ctx.post("/api/v1/comms/send", json={"text": "Everyone"})).json())

    published = await events(ctx, go)
    assert out[0]["stations"] == ["emcee-1", "emcee-2"]
    assert out[1]["stations"] == ["floor-1"]
    assert out[2]["stations"] == ["emcee-1", "emcee-2", "floor-1"]
    msgs = [e for e in published if e["type"] == "station_message"]
    assert len(msgs) == 3
    assert msgs[0]["payload"]["flash"] is True and msgs[0]["payload"]["text"] == "Hello"
    last = json.loads(await ctx.redis.get("qecomp:station:floor-1:last"))
    assert last["text"] == "Everyone"


async def test_send_errors(ctx):
    assert (await ctx.post("/api/v1/comms/send", json={"text": "x"})).status_code == 422  # no stations exist
    await make_station(ctx, "emcee-1")
    assert (await ctx.post("/api/v1/comms/send", json={"stations": ["ghost"], "text": "x"})).status_code == 422
    assert (await ctx.post("/api/v1/comms/send", json={"stations": ["emcee-1"], "text": ""})).status_code == 422


async def test_screen_push_respects_role(ctx):
    await make_station(ctx, "emcee-1", "emcee")
    await make_station(ctx, "floor-1", "floor_manager")
    resp = await ctx.post("/api/v1/comms/screen", json={"screen": "countdown"})
    assert resp.status_code == 200 and resp.json()["stations"] == ["emcee-1"]  # floor manager skipped
    assert (await ctx.post("/api/v1/comms/screen", json={"stations": ["floor-1"], "screen": "countdown"})).status_code == 422
    assert (await ctx.post("/api/v1/comms/screen", json={"screen": "bogus"})).status_code == 422
    assert (await ctx.post("/api/v1/comms/screen", json={"screen": "agenda_finals"})).json()["stations"] == ["emcee-1", "floor-1"]
    assert await ctx.redis.get("qecomp:station:emcee-1:screen") == "agenda_finals"


async def test_public_station_state_restores_screen_and_last_message(ctx):
    await make_station(ctx, "emcee-1", "emcee", "Main Stage")
    await make_station(ctx, "cam-1", "camera")
    await ctx.post("/api/v1/comms/send", json={"stations": ["emcee-1"], "text": "Welcome", "flash": True})
    await ctx.post("/api/v1/comms/screen", json={"stations": ["emcee-1"], "screen": "agenda_day"})
    info = (await ctx.get("/api/v1/public/stations/emcee-1")).json()
    assert info["name"] == "Main Stage" and info["screen"] == "agenda_day"
    assert info["last_message"]["text"] == "Welcome"
    assert "countdown" in info["screens"]
    assert "countdown" not in (await ctx.get("/api/v1/public/stations/cam-1")).json()["screens"]
    assert (await ctx.get("/api/v1/public/stations/ghost")).status_code == 404


async def test_stored_screen_no_longer_allowed_falls_back(ctx):
    await make_station(ctx, "floor-1", "floor_manager")
    await ctx.redis.set("qecomp:station:floor-1:screen", "countdown")
    assert (await ctx.get("/api/v1/public/stations/floor-1")).json()["screen"] == "messages"


async def test_public_agenda_by_scope(ctx):
    await make_station(ctx, "emcee-1")
    await ctx.post("/api/v1/comms/agenda", json={"scope": "day", "start_time": "09:00", "title": "Opening"})
    await ctx.post("/api/v1/comms/agenda", json={"scope": "finals", "start_time": "16:00", "title": "Finals"})
    assert [i["title"] for i in (await ctx.get("/api/v1/public/stations/emcee-1/agenda?scope=finals")).json()] == ["Finals"]
    assert len((await ctx.get("/api/v1/public/stations/emcee-1/agenda")).json()) == 2
    assert (await ctx.get("/api/v1/public/stations/emcee-1/agenda?scope=week")).status_code == 422
    assert (await ctx.get("/api/v1/public/stations/ghost/agenda")).status_code == 404


async def test_inbound_message_inbox_ack_and_clear(ctx):
    await make_station(ctx, "emcee-1", "emcee", "Main Stage")

    result = {}

    async def go():
        result["r"] = await ctx.post("/api/v1/public/stations/emcee-1/messages", json={"text": "  Need water  "})

    published = await events(ctx, go)
    assert result["r"].status_code == 204
    inbound = [e for e in published if e["type"] == "station_inbound"]
    assert inbound and inbound[0]["payload"] == {"station": "emcee-1", "name": "Main Stage", "text": "Need water"}

    inbox = (await ctx.get("/api/v1/comms/inbox?unacknowledged=true")).json()
    assert [m["text"] for m in inbox] == ["Need water"]
    assert (await ctx.post(f"/api/v1/comms/inbox/{inbox[0]['id']}/ack")).status_code == 204
    assert (await ctx.get("/api/v1/comms/inbox?unacknowledged=true")).json() == []
    assert len((await ctx.get("/api/v1/comms/inbox")).json()) == 1
    assert (await ctx.delete("/api/v1/comms/inbox")).status_code == 204
    assert (await ctx.get("/api/v1/comms/inbox")).json() == []
    assert (await ctx.post("/api/v1/comms/inbox/00000000-0000-0000-0000-000000000000/ack")).status_code == 404


async def test_inbound_validation_unknown_station_and_limits(ctx, monkeypatch):
    await make_station(ctx, "emcee-1")
    assert (await ctx.post("/api/v1/public/stations/emcee-1/messages", json={"text": "   "})).status_code == 422
    assert (await ctx.post("/api/v1/public/stations/ghost/messages", json={"text": "hi"})).status_code == 404
    from backend.routers import comms

    monkeypatch.setattr(comms, "MAX_UNACKED_PER_STATION", 2)
    for _ in range(2):
        assert (await ctx.post("/api/v1/public/stations/emcee-1/messages", json={"text": "x"})).status_code == 204
    assert (await ctx.post("/api/v1/public/stations/emcee-1/messages", json={"text": "x"})).status_code == 429


async def test_next_match_for_emcee_only(ctx, session_factory):
    await make_station(ctx, "emcee-1", "emcee")
    await make_station(ctx, "floor-1", "floor_manager")
    assert (await ctx.get("/api/v1/public/stations/emcee-1/next-match")).json() == {
        "available": False, "label": None, "division": None, "scheduled_at": None, "red": [], "blue": []
    }
    assert (await ctx.get("/api/v1/public/stations/floor-1/next-match")).status_code == 403

    async with session_factory() as session:
        session.add(TeamProfile(team_number="CM1A", cached_stats={"team_name": "Gears", "organisation": "QE", "ranking": {"rank": 2}}))
        await session.commit()
    now = time.time()
    schedule = {
        "divisions": [
            {
                "id": 1,
                "matches": [
                    {"matchInfo": {"timeScheduled": now - 3600, "state": "SCORED", "alliances": [{"teams": [{"number": "CM9Z"}]}, {"teams": [{"number": "CM8Z"}]}], "matchTuple": {"session": 0, "division": 1, "round": "QUAL", "instance": 1, "match": 1}}, "finalScore": [1, 2]},
                    {"matchInfo": {"timeScheduled": now + 300, "state": "UNPLAYED", "alliances": [{"teams": [{"number": "CM1A"}]}, {"teams": [{"number": "CM2B"}]}], "matchTuple": {"session": 0, "division": 1, "round": "QUAL", "instance": 1, "match": 2}}},
                ],
            }
        ]
    }
    await ctx.redis.set("qecomp:tm:vex_tm.test:schedule", json.dumps(schedule))
    data = (await ctx.get("/api/v1/public/stations/emcee-1/next-match")).json()
    assert data["available"] and data["label"] == "Q2" and abs(data["scheduled_at"] - (now + 300)) < 1
    assert data["red"][0]["team_number"] == "CM1A" and data["red"][0]["team_name"] == "Gears" and data["red"][0]["qual_rank"] == 2
    assert data["blue"][0]["team_number"] == "CM2B" and data["blue"][0]["team_name"] is None


async def test_permissions(ctx):
    from backend.core.dependencies import CurrentPrincipal

    await make_station(ctx, "emcee-1")
    ctx.state["principal"] = CurrentPrincipal(subject="viewer", permissions={"comms:read"})
    assert (await ctx.get("/api/v1/comms/stations")).status_code == 200
    assert (await ctx.post("/api/v1/comms/stations", json={"slug": "x", "name": "x", "role": "emcee"})).status_code == 403
    assert (await ctx.post("/api/v1/comms/send", json={"text": "x"})).status_code == 403
    assert (await ctx.get("/api/v1/comms/inbox")).status_code == 200
    ctx.state["principal"] = CurrentPrincipal(subject="sender", permissions={"comms:send"})
    assert (await ctx.post("/api/v1/comms/send", json={"text": "x"})).status_code == 200
    assert (await ctx.post("/api/v1/comms/presets", json={"label": "a", "message": "b"})).status_code == 403
    assert (await ctx.get("/api/v1/comms/stations")).status_code == 403


async def test_station_page_served_and_unknown_404(ctx):
    await make_station(ctx, "emcee-1")
    page = await ctx.get("/station/emcee-1")
    assert page.status_code == 200 and '"emcee-1"' in page.text and "__STATION_SLUG__" not in page.text
    assert (await ctx.get("/station/ghost")).status_code == 404
    assert (await ctx.get("/station/Bad%20Slug")).status_code == 404
