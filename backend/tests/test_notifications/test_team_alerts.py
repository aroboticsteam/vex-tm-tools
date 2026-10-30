"""Per-team alerts: come to pit admin / lost & found / custom (issue #44)."""
from __future__ import annotations

import json

import fakeredis
import pytest
from fakeredis import aioredis as fake_aioredis
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from backend.models.notification import PushSubscription, PushSubscriptionTeam
from backend.modules.notifications import delivery
from backend.schemas.notification import TeamAlertRequest
from backend.tests.test_notifications.conftest import VALID_ENDPOINT
from backend.tests.test_notifications.test_service import listen_events, room_notices

pytestmark = pytest.mark.asyncio

KEYS = {"p256dh": "BKey", "auth": "aSecret"}


# ── text and schema (no database) ────────────────────────────────────


async def test_alert_text_presets_and_custom():
    assert delivery.team_alert_text("pit_admin", ["1234A"], None) == ("Pit Admin", "Team 1234A: please come to Pit Admin.")
    title, body = delivery.team_alert_text("lost_and_found", ["1234A", "5678B"], "Blue jacket.")
    assert title == "Lost & Found"
    assert body == "Teams 1234A, 5678B: please come to Lost & Found. Blue jacket."
    assert delivery.team_alert_text("custom", ["9999Z"], "Bring your robot to the stage") == (
        "Message for Team 9999Z",
        "Bring your robot to the stage",
    )


async def test_request_normalises_teams_and_requires_custom_message():
    req = TeamAlertRequest(teams=[" 1234a ", "1234A", "5678b"], reason="pit_admin", message="  ")
    assert req.teams == ["1234A", "5678B"] and req.message is None
    with pytest.raises(ValidationError):
        TeamAlertRequest(teams=["1234A"], reason="custom")
    with pytest.raises(ValidationError):
        TeamAlertRequest(teams=["1234A"], reason="nope")
    with pytest.raises(ValidationError):
        TeamAlertRequest(teams=[], reason="pit_admin")
    with pytest.raises(ValidationError):
        TeamAlertRequest(teams=["  "], reason="pit_admin")
    with pytest.raises(ValidationError):
        TeamAlertRequest(teams=["1234A"], reason="custom", message="x" * 301)


# ── delivery against real Postgres ───────────────────────────────────


async def _add_room(session_factory, slug, teams):
    from backend.models.room import Room, RoomTeam

    async with session_factory() as session:
        session.add(Room(slug=slug, name=slug.upper()))
        await session.flush()
        session.add_all(RoomTeam(room_slug=slug, team_number=t) for t in teams)
        await session.commit()


async def _add_sub(session_factory, name, room, teams, categories=None):
    async with session_factory() as session:
        sub = PushSubscription(
            endpoint=VALID_ENDPOINT + name, p256dh="k", auth="a", kind="team", room_slug=room, categories=categories
        )
        session.add(sub)
        await session.flush()
        session.add_all(PushSubscriptionTeam(subscription_id=sub.id, team_number=t) for t in teams)
        await session.commit()


async def test_alert_reaches_room_display_and_followers_only(session_factory, fake_redis, pushes):
    await _add_room(session_factory, "r1", ["1234A", "5678B"])
    await _add_room(session_factory, "r2", ["9999Z"])
    await _add_sub(session_factory, "follows-a", "r1", ["1234A"])
    await _add_sub(session_factory, "follows-both", "r1", ["1234A", "5678B"])
    await _add_sub(session_factory, "other-team", "r2", ["9999Z"])
    listener = await listen_events(fake_redis)

    result = await delivery.alert_teams(fake_redis, session_factory, ["1234A", "5678B"], "pit_admin")

    assert result == {"rooms": 1, "pushed": 2, "unreached_teams": []}
    notices = await room_notices(fake_redis, listener)
    assert len(notices) == 1 and notices[0]["rooms"] == ["r1"] and notices[0]["modal"] is True
    assert notices[0]["message"] == "Teams 1234A, 5678B: please come to Pit Admin."
    by_endpoint = {t.endpoint.rsplit("/", 1)[1]: t.payload for t in pushes}
    assert set(by_endpoint) == {"follows-a", "follows-both"}
    assert by_endpoint["follows-a"]["body"] == "Team 1234A: please come to Pit Admin."  # only the team it follows
    assert by_endpoint["follows-both"]["body"] == "Teams 1234A, 5678B: please come to Pit Admin."
    assert by_endpoint["follows-a"]["kind"] == "team_alert" and by_endpoint["follows-a"]["url"] == "/room/r1"
    # same alert shares one tag, so a phone shows it once
    assert len({p["tag"] for p in by_endpoint.values()}) == 1


async def test_alert_ignores_category_opt_outs(session_factory, fake_redis, pushes):
    await _add_room(session_factory, "r1", ["1234A"])
    await _add_sub(session_factory, "quiet", "r1", ["1234A"], categories=["score"])
    result = await delivery.alert_teams(fake_redis, session_factory, ["1234A"], "lost_and_found")
    assert result["pushed"] == 1


async def test_alert_groups_notice_per_room(session_factory, fake_redis, pushes):
    await _add_room(session_factory, "r1", ["1234A"])
    await _add_room(session_factory, "r2", ["5678B"])
    listener = await listen_events(fake_redis)
    result = await delivery.alert_teams(fake_redis, session_factory, ["1234A", "5678B"], "pit_admin")
    assert result["rooms"] == 2
    notices = {n["rooms"][0]: n["message"] for n in await room_notices(fake_redis, listener)}
    assert notices == {"r1": "Team 1234A: please come to Pit Admin.", "r2": "Team 5678B: please come to Pit Admin."}


async def test_unreached_teams_are_reported(session_factory, fake_redis, pushes):
    await _add_room(session_factory, "r1", ["1234A"])
    result = await delivery.alert_teams(fake_redis, session_factory, ["1234A", "7777X"], "pit_admin")
    assert result["unreached_teams"] == ["7777X"]
    # a team reachable only by a phone (no room) still counts as reached
    await _add_sub(session_factory, "phone", "r1", ["7777X"])
    result = await delivery.alert_teams(fake_redis, session_factory, ["1234A", "7777X"], "pit_admin")
    assert result["unreached_teams"] == []


async def test_push_can_be_disabled_and_missing_vapid_skips_push(session_factory, fake_redis, pushes, monkeypatch):
    from backend.core.settings import settings

    await _add_room(session_factory, "r1", ["1234A"])
    await _add_sub(session_factory, "p", "r1", ["1234A"])
    off = await delivery.alert_teams(fake_redis, session_factory, ["1234A"], "pit_admin", push=False)
    assert off["pushed"] == 0 and off["rooms"] == 1 and pushes == []
    monkeypatch.setattr(settings, "VAPID_PRIVATE_KEY", "")
    nokey = await delivery.alert_teams(fake_redis, session_factory, ["1234A"], "pit_admin")
    assert nokey["pushed"] == 0 and pushes == []


# ── REST ─────────────────────────────────────────────────────────────


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


async def test_endpoint_sends_and_audits(ctx, session_factory, pushes):
    from sqlalchemy import delete, select

    from backend.models.audit import AuditLog

    async with session_factory() as session:
        await session.execute(delete(AuditLog).where(AuditLog.action == "team_alert"))
        await session.commit()
    await _add_room(session_factory, "r1", ["1234A"])
    await _add_sub(session_factory, "p", "r1", ["1234A"])
    resp = await ctx.post("/api/v1/notifications/team-alert", json={"teams": ["1234a"], "reason": "pit_admin", "message": "Bring the robot"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"rooms": 1, "pushed": 1, "unreached_teams": []}
    assert pushes[0].payload["body"] == "Team 1234A: please come to Pit Admin. Bring the robot"
    async with session_factory() as session:
        rows = (await session.execute(select(AuditLog).where(AuditLog.action == "team_alert"))).scalars().all()
    assert len(rows) == 1 and rows[0].resource_id == "1234A" and rows[0].resource_type == "notification" and rows[0].changes["reason"] == "pit_admin"


async def test_endpoint_validation(ctx, session_factory, pushes):
    await _add_room(session_factory, "r1", ["1234A"])
    bad = ctx.post
    assert (await bad("/api/v1/notifications/team-alert", json={"teams": ["1234A"], "reason": "custom"})).status_code == 422
    assert (await bad("/api/v1/notifications/team-alert", json={"teams": ["1234A"], "reason": "x"})).status_code == 422
    assert (await bad("/api/v1/notifications/team-alert", json={"teams": [], "reason": "pit_admin"})).status_code == 422
    resp = await bad("/api/v1/notifications/team-alert", json={"teams": ["1234A", "0000Q"], "reason": "pit_admin"})
    assert resp.status_code == 422 and "0000Q" in resp.json()["detail"]
    assert pushes == []


async def test_endpoint_requires_broadcast_permission(ctx, session_factory):
    from backend.core.dependencies import CurrentPrincipal

    await _add_room(session_factory, "r1", ["1234A"])
    ctx.state["principal"] = CurrentPrincipal(subject="viewer", permissions={"rooms:read", "notifications:receive"})
    body = {"teams": ["1234A"], "reason": "pit_admin"}
    assert (await ctx.post("/api/v1/notifications/team-alert", json=body)).status_code == 403
    ctx.state["principal"] = CurrentPrincipal(subject="b", permissions={"notifications:broadcast"})
    assert (await ctx.post("/api/v1/notifications/team-alert", json=body)).status_code == 200
    ctx.state["principal"] = None
    assert (await ctx.post("/api/v1/notifications/team-alert", json=body)).status_code == 401
