"""Router tests for `backend/routers/overlays.py` (plan §14) against real
Postgres, with `fakeredis` standing in for `qecomp:config_change` pub/sub."""
from __future__ import annotations

import uuid

import fakeredis
import pytest
from fakeredis import aioredis as fake_aioredis
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.core.db import engine as app_engine
from backend.core.dependencies import ALL_PERMISSIONS, CurrentPrincipal, get_current_principal
from backend.core.redis import get_redis
from backend.core.settings import settings
from backend.main import app
from backend.models.overlay import OverlayInstance

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _reset_app_engine_pool():
    await app_engine.dispose()
    yield
    await app_engine.dispose()


@pytest.fixture
async def db_engine():
    engine = create_async_engine(settings.POSTGRES_DSN)
    try:
        async with engine.connect() as conn:
            await conn.exec_driver_sql("SELECT 1")
    except Exception:
        pytest.skip("Real Postgres not reachable; skipping overlays router tests")
    yield engine
    await engine.dispose()


@pytest.fixture
async def fake_redis():
    server = fakeredis.FakeServer()
    client = fake_aioredis.FakeRedis(server=server, decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
async def client(fake_redis, db_engine):
    async def _override_get_redis():
        yield fake_redis

    async def _override_get_current_principal():
        return CurrentPrincipal(subject="test-admin", permissions={ALL_PERMISSIONS})

    app.dependency_overrides[get_redis] = _override_get_redis
    app.dependency_overrides[get_current_principal] = _override_get_current_principal
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_redis, None)
        app.dependency_overrides.pop(get_current_principal, None)


def _unique_entity_id() -> str:
    return f"overlay.test_{uuid.uuid4().hex[:8]}"


@pytest.fixture
async def cleanup_entities(db_engine):
    created: list[str] = []
    yield created
    if created:
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as session:
            await session.execute(delete(OverlayInstance).where(OverlayInstance.entity_id.in_(created)))
            await session.commit()


async def test_create_list_update_delete_overlay(client, cleanup_entities):
    entity_id = _unique_entity_id()
    cleanup_entities.append(entity_id)

    create_resp = await client.post(
        "/api/v1/overlays",
        json={"entity_id": entity_id, "display_name": "Field 1 Overlay", "field_set_id": 1, "tags": ["fieldset.1"]},
    )
    assert create_resp.status_code == 201
    assert create_resp.json()["entity_id"] == entity_id

    list_resp = await client.get("/api/v1/overlays")
    assert any(row["entity_id"] == entity_id for row in list_resp.json())

    update_resp = await client.put(
        f"/api/v1/overlays/{entity_id}", json={"display_name": "Field 1 Overlay Renamed"}
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["display_name"] == "Field 1 Overlay Renamed"

    delete_resp = await client.delete(f"/api/v1/overlays/{entity_id}")
    assert delete_resp.status_code == 204


async def test_create_duplicate_overlay_returns_409(client, cleanup_entities):
    entity_id = _unique_entity_id()
    cleanup_entities.append(entity_id)
    body = {"entity_id": entity_id, "display_name": "Dup", "field_set_id": 1, "tags": []}
    assert (await client.post("/api/v1/overlays", json=body)).status_code == 201
    assert (await client.post("/api/v1/overlays", json=body)).status_code == 409


async def test_update_missing_overlay_returns_404(client):
    resp = await client.put(
        f"/api/v1/overlays/does-not-exist-{uuid.uuid4().hex}", json={"display_name": "X"}
    )
    assert resp.status_code == 404


async def test_preview_returns_no_match_queued_shape(client, cleanup_entities):
    """Gap flagged in `routers/overlays.py`'s docstring: the full team-video
    pipeline isn't wired up by this wave, so `/preview` returns the
    well-formed "no match queued" shape rather than 404/501."""
    entity_id = _unique_entity_id()
    cleanup_entities.append(entity_id)
    await client.post(
        "/api/v1/overlays", json={"entity_id": entity_id, "display_name": "Preview Test", "field_set_id": 1, "tags": []}
    )

    resp = await client.get(f"/api/v1/overlays/{entity_id}/preview")
    assert resp.status_code == 200
    body = resp.json()
    assert body["entity_id"] == entity_id
    assert body["match"] is None


async def test_preview_missing_overlay_returns_404(client):
    resp = await client.get(f"/api/v1/overlays/does-not-exist-{uuid.uuid4().hex}/preview")
    assert resp.status_code == 404


async def test_show_team_publishes_event(client, fake_redis, cleanup_entities):
    entity_id = _unique_entity_id()
    cleanup_entities.append(entity_id)
    await client.post("/api/v1/overlays", json={"entity_id": entity_id, "display_name": "O", "field_set_id": 1})

    pubsub = fake_redis.pubsub()
    await pubsub.subscribe("qecomp:events")
    await pubsub.get_message(timeout=1)  # subscribe ack

    resp = await client.post(f"/api/v1/overlays/{entity_id}/show-team", json={"team": "1234a"})
    assert resp.status_code == 204
    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)
    import json as _json

    body = _json.loads(message["data"])
    assert body["type"] == "overlay_show_team" and body["entity_id"] == entity_id
    assert body["payload"] == {"team": "1234A"}

    resp = await client.post(f"/api/v1/overlays/{entity_id}/show-team", json={"team": None})
    assert resp.status_code == 204
    cleared = _json.loads((await pubsub.get_message(ignore_subscribe_messages=True, timeout=1))["data"])
    assert cleared["payload"] == {"team": None}


async def test_show_team_validation_and_404(client, cleanup_entities):
    entity_id = _unique_entity_id()
    cleanup_entities.append(entity_id)
    await client.post("/api/v1/overlays", json={"entity_id": entity_id, "display_name": "O", "field_set_id": 1})
    assert (await client.post(f"/api/v1/overlays/{entity_id}/show-team", json={"team": "bad team!"})).status_code == 422
    assert (await client.post("/api/v1/overlays/overlay.nope/show-team", json={"team": "1234A"})).status_code == 404
