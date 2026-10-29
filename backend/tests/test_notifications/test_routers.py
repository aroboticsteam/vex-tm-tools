"""Rooms admin API, the public push/subscribe API, staff broadcast, the
public pages, and the room WebSocket relay."""
from __future__ import annotations

import json
import time

import fakeredis
import pytest
from fakeredis import aioredis as fake_aioredis
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from backend.core.settings import settings
from backend.models.notification import KIND_STAFF, KIND_TEAM, PushSubscription, PushSubscriptionTeam
from backend.modules.notifications import delivery
from backend.tests.test_notifications.conftest import VALID_ENDPOINT

pytestmark = pytest.mark.asyncio

KEYS = {"p256dh": "BKey", "auth": "aSecret"}


def team_body(endpoint="a", room="r1", teams=("1234A",)):
    return {"endpoint": VALID_ENDPOINT + endpoint, "keys": KEYS, "room": room, "teams": list(teams)}


@pytest.fixture
async def ctx(session_factory, monkeypatch, vapid):
    from backend.core.db import get_db
    from backend.core.dependencies import ALL_PERMISSIONS, CurrentPrincipal, get_current_principal, get_redis
    from backend.main import create_app
    from backend.routers import notifications as notif_router

    redis = fake_aioredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    app = create_app()
    state = {"principal": CurrentPrincipal(subject="tester", permissions={ALL_PERMISSIONS})}

    async def _db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_redis] = lambda: redis
    app.dependency_overrides[get_current_principal] = lambda: state["principal"]
    monkeypatch.setattr(notif_router, "async_session_factory", session_factory)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.state = state
        client.redis = redis
        yield client
    await redis.aclose()


async def make_room(client, slug="r1", teams=("1234A", "5678B"), stream=None):
    resp = await client.post("/api/v1/rooms", json={"slug": slug, "name": slug.upper(), "stream_url": stream, "teams": list(teams)})
    assert resp.status_code == 201, resp.text
    return resp.json()


# ── rooms admin ──────────────────────────────────────────────────────


async def test_room_crud(ctx):
    room = await make_room(ctx, stream="https://youtu.be/dQw4w9WgXcQ")
    assert room["teams"] == ["1234A", "5678B"]
    assert room["stream_url"] == "https://www.youtube.com/embed/dQw4w9WgXcQ"

    assert (await ctx.post("/api/v1/rooms", json={"slug": "r1", "name": "dup"})).status_code == 409
    assert (await ctx.post("/api/v1/rooms", json={"slug": "Bad Slug", "name": "x"})).status_code == 422
    assert (await ctx.post("/api/v1/rooms", json={"slug": "ok", "name": "x", "stream_url": "javascript:1"})).status_code == 422

    resp = await ctx.put("/api/v1/rooms/r1", json={"name": "Renamed", "teams": ["9999Z"], "stream_url": None})
    assert resp.status_code == 200 and resp.json()["teams"] == ["9999Z"] and resp.json()["stream_url"] is None
    assert (await ctx.get("/api/v1/rooms/r1")).json()["name"] == "Renamed"
    assert [r["slug"] for r in (await ctx.get("/api/v1/rooms")).json()] == ["r1"]

    assert (await ctx.put("/api/v1/rooms/nope", json={"name": "x"})).status_code == 404
    assert (await ctx.delete("/api/v1/rooms/r1")).status_code == 204
    assert (await ctx.get("/api/v1/rooms/r1")).status_code == 404


async def test_room_permissions(ctx):
    from backend.core.dependencies import CurrentPrincipal

    ctx.state["principal"] = CurrentPrincipal(subject="viewer", permissions={"rooms:read"})
    assert (await ctx.get("/api/v1/rooms")).status_code == 200
    assert (await ctx.post("/api/v1/rooms", json={"slug": "x", "name": "x"})).status_code == 403
    assert (await ctx.post("/api/v1/notifications/broadcast", json={"title": "t", "message": "m"})).status_code == 403
    ctx.state["principal"] = None
    assert (await ctx.get("/api/v1/rooms")).status_code == 401


# ── public subscribe ─────────────────────────────────────────────────


async def test_public_room_and_vapid_key(ctx):
    await make_room(ctx, stream="https://example.org/embed")
    resp = await ctx.get("/api/v1/public/rooms/r1")
    assert resp.status_code == 200 and resp.json()["teams"] == ["1234A", "5678B"]
    assert (await ctx.get("/api/v1/public/rooms/missing")).status_code == 404
    assert (await ctx.get("/api/v1/public/push/vapid-key")).json() == {"public_key": "test-public"}


async def test_vapid_key_503_when_unconfigured(ctx, monkeypatch):
    monkeypatch.setattr(settings, "VAPID_PRIVATE_KEY", "")
    assert (await ctx.get("/api/v1/public/push/vapid-key")).status_code == 503
    await make_room(ctx)
    assert (await ctx.post("/api/v1/public/push/subscribe", json=team_body())).status_code == 503


async def test_subscribe_update_status_unsubscribe(ctx, session_factory):
    await make_room(ctx)
    resp = await ctx.post("/api/v1/public/push/subscribe", json=team_body(teams=("1234a",)))
    assert resp.status_code == 200 and resp.json() == {"subscribed": True, "room": "r1", "teams": ["1234A"], "categories": None}

    # Same endpoint again replaces the team list (no duplicate subscription).
    resp = await ctx.post("/api/v1/public/push/subscribe", json=team_body(teams=("1234A", "5678B")))
    assert resp.json()["teams"] == ["1234A", "5678B"]
    async with session_factory() as session:
        subs = (await session.execute(select(PushSubscription))).scalars().all()
        assert len(subs) == 1 and subs[0].kind == KIND_TEAM and subs[0].room_slug == "r1"

    status = await ctx.post("/api/v1/public/push/status", json={"endpoint": VALID_ENDPOINT + "a"})
    assert status.json() == {"subscribed": True, "room": "r1", "teams": ["1234A", "5678B"], "categories": None}
    unknown = await ctx.post("/api/v1/public/push/status", json={"endpoint": VALID_ENDPOINT + "other"})
    assert unknown.json()["subscribed"] is False

    assert (await ctx.post("/api/v1/public/push/unsubscribe", json={"endpoint": VALID_ENDPOINT + "a"})).status_code == 204
    async with session_factory() as session:
        assert (await session.execute(select(PushSubscription))).first() is None
        assert (await session.execute(select(PushSubscriptionTeam))).first() is None  # cascade


async def test_subscribe_validation(ctx):
    await make_room(ctx)
    # team not in that room
    resp = await ctx.post("/api/v1/public/push/subscribe", json=team_body(teams=("9999Z",)))
    assert resp.status_code == 422 and "9999Z" in resp.json()["detail"]
    # unknown room
    assert (await ctx.post("/api/v1/public/push/subscribe", json=team_body(room="nope"))).status_code == 404
    # non-push-service endpoint
    bad = team_body()
    bad["endpoint"] = "https://evil.example.com/x"
    assert (await ctx.post("/api/v1/public/push/subscribe", json=bad)).status_code == 422
    # no teams
    assert (await ctx.post("/api/v1/public/push/subscribe", json=team_body(teams=()))).status_code == 422


async def test_public_writes_are_rate_limited(ctx, monkeypatch):
    from backend.routers import notifications as notif_router

    monkeypatch.setattr(notif_router, "_RATE_LIMIT", 3)
    codes = [
        (await ctx.post("/api/v1/public/push/status", json={"endpoint": VALID_ENDPOINT + "x"})).status_code
        for _ in range(5)
    ]
    assert codes == [200, 200, 200, 429, 429]


# ── staff ────────────────────────────────────────────────────────────


async def test_staff_subscribe_and_stats(ctx, session_factory):
    await make_room(ctx)
    await ctx.post("/api/v1/public/push/subscribe", json=team_body())
    body = {"endpoint": VALID_ENDPOINT + "staff1", "keys": KEYS, "categories": ["system"]}
    assert (await ctx.post("/api/v1/notifications/staff/subscribe", json=body)).status_code == 204
    assert (await ctx.post("/api/v1/notifications/staff/subscribe", json={**body, "categories": ["bogus"]})).status_code == 422

    async with session_factory() as session:
        staff = (await session.execute(select(PushSubscription).where(PushSubscription.kind == KIND_STAFF))).scalar_one()
        assert staff.user_subject == "tester" and staff.categories == ["system"] and staff.room_slug is None

    stats = (await ctx.get("/api/v1/notifications/stats")).json()
    assert stats == {"push_configured": True, "team_subscriptions": 1, "staff_subscriptions": 1, "subscriptions_by_room": {"r1": 1}}

    assert (await ctx.post("/api/v1/notifications/staff/unsubscribe", json={"endpoint": VALID_ENDPOINT + "staff1"})).status_code == 204
    assert (await ctx.get("/api/v1/notifications/stats")).json()["staff_subscriptions"] == 0


async def test_broadcast_endpoint(ctx, pushes):
    await make_room(ctx, "r1", ("1234A",))
    await make_room(ctx, "r2", ("5678B",))
    await ctx.post("/api/v1/public/push/subscribe", json=team_body("t1", "r1", ("1234A",)))
    await ctx.post("/api/v1/public/push/subscribe", json=team_body("t2", "r2", ("5678B",)))

    resp = await ctx.post("/api/v1/notifications/broadcast", json={"title": "Hi", "message": "Everyone", "rooms": ["r1"]})
    assert resp.status_code == 200 and resp.json() == {"rooms": 1, "pushed": 1}
    assert [t.endpoint.rsplit("/", 1)[1] for t in pushes] == ["t1"]

    assert (await ctx.post("/api/v1/notifications/broadcast", json={"title": "Hi", "message": "x", "rooms": ["nope"]})).status_code == 422
    resp = await ctx.post("/api/v1/notifications/broadcast", json={"title": "Hi", "message": "all"})
    assert resp.json()["rooms"] == 2


async def test_test_push_requires_config(ctx, pushes, monkeypatch):
    body = {"endpoint": VALID_ENDPOINT + "s", "keys": KEYS}
    await ctx.post("/api/v1/notifications/staff/subscribe", json=body)
    resp = await ctx.post("/api/v1/notifications/test")
    assert resp.json()["pushed"] == 1 and pushes[0].payload["title"] == "Test notification"
    monkeypatch.setattr(settings, "VAPID_PRIVATE_KEY", "")
    assert (await ctx.post("/api/v1/notifications/test")).status_code == 503


# ── public pages ─────────────────────────────────────────────────────


async def test_room_page_qr_and_assets(ctx):
    await make_room(ctx, "pit-1")
    page = await ctx.get("/room/pit-1")
    assert page.status_code == 200 and '"pit-1"' in page.text and "__ROOM_SLUG__" not in page.text
    assert (await ctx.get("/room/missing")).status_code == 404
    assert (await ctx.get("/room/Bad%20Slug")).status_code == 404

    qr = await ctx.get("/room/pit-1/qr.svg", headers={"x-forwarded-proto": "https", "x-forwarded-host": "tm.example.org"})
    assert qr.status_code == 200 and qr.headers["content-type"].startswith("image/svg+xml") and "<svg" in qr.text
    assert (await ctx.get("/room/missing/qr.svg")).status_code == 404

    notify = await ctx.get("/notify")
    assert notify.status_code == 200 and "pushManager" in notify.text
    sw = await ctx.get("/sw.js")
    assert sw.status_code == 200 and "showNotification" in sw.text and sw.headers["service-worker-allowed"] == "/"
    manifest = (await ctx.get("/notify.webmanifest", params={"room": "pit-1"})).json()
    assert manifest["start_url"] == "/notify?room=pit-1" and manifest["display"] == "standalone"
    assert (await ctx.get("/notify.webmanifest", params={"room": "../x"})).json()["start_url"] == "/notify"


async def test_qr_encodes_public_base_url(ctx, monkeypatch):
    import io

    import segno

    await make_room(ctx, "pit-1")
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://tm.example.org/")
    qr = await ctx.get("/room/pit-1/qr.svg")
    buf = io.BytesIO()
    segno.make("https://tm.example.org/notify?room=pit-1", error="m").save(
        buf, kind="svg", scale=10, border=2, dark="#000000", light="#ffffff", xmldecl=False
    )
    assert qr.content == buf.getvalue()  # encodes exactly the configured public URL
    from backend.routers.public_pages import notify_url
    from starlette.requests import Request

    scope = {"type": "http", "headers": [], "server": ("x", 80), "scheme": "http", "path": "/", "query_string": b""}
    assert notify_url(Request(scope), "pit-1") == "https://tm.example.org/notify?room=pit-1"


# ── ws relay ─────────────────────────────────────────────────────────


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, message):
        self.sent.append(message)


async def test_room_notice_is_relayed_only_to_named_rooms():
    from backend.routers.ws import ROOM_BUCKET_PREFIX, ConnectionManager

    mgr = ConnectionManager(redis=object())
    a, b, other = FakeWS(), FakeWS(), FakeWS()
    mgr._by_entity[f"{ROOM_BUCKET_PREFIX}r1"].add(a)
    mgr._by_entity[f"{ROOM_BUCKET_PREFIX}r1"].add(b)
    mgr._by_entity[f"{ROOM_BUCKET_PREFIX}r2"].add(other)

    await mgr._dispatch(
        {
            "type": "room_notice",
            "entity_id": "notifications",
            "timestamp": time.time(),
            "payload": {"id": "n1", "rooms": ["r1"], "title": "T", "message": "M", "modal": True, "duration": 45, "secret": "no"},
        }
    )
    assert len(a.sent) == len(b.sent) == 1 and other.sent == []
    msg = a.sent[0]
    assert msg["type"] == "notice" and msg["title"] == "T" and msg["modal"] is True and msg["duration"] == 45
    assert "secret" not in msg and "rooms" not in msg


async def test_ws_room_rejects_unknown_room():
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from backend.main import create_app

    app = create_app()
    client = TestClient(app)
    for slug in ("Bad_Slug", "x" * 60):
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(f"/ws/room/{slug}"):
                pass
        assert exc.value.code == 4003


async def test_subscribe_with_categories_round_trips_and_validates(ctx, session_factory):
    await make_room(ctx)
    resp = await ctx.post("/api/v1/public/push/subscribe", json={**team_body(), "categories": ["score", "late", "late"]})
    assert resp.status_code == 200 and resp.json()["categories"] == ["late", "score"]
    status = await ctx.post("/api/v1/public/push/status", json={"endpoint": team_body()["endpoint"]})
    assert status.json()["categories"] == ["late", "score"]

    bad = await ctx.post("/api/v1/public/push/subscribe", json={**team_body(), "categories": ["spam"]})
    assert bad.status_code == 422
