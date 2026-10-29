"""Router tests for `backend/routers/integrations.py` (plan §14) against
real Postgres, with `fakeredis` standing in for `qecomp:config_change`
pub/sub publication (per plan §14's test stack)."""
from __future__ import annotations

import uuid

import fakeredis
import pytest
from fakeredis import aioredis as fake_aioredis
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import create_async_engine

from backend.core.db import engine as app_engine
from backend.core.dependencies import ALL_PERMISSIONS, CurrentPrincipal, get_current_principal
from backend.core.redis import get_redis
from backend.core.security import decrypt
from backend.core.settings import settings
from backend.main import app
from backend.models.integration import IntegrationInstance

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
        pytest.skip("Real Postgres not reachable; skipping integrations router tests")
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


def _unique_entity_id(domain: str) -> str:
    return f"{domain}.test_{uuid.uuid4().hex[:8]}"


@pytest.fixture
async def cleanup_entities(db_engine):
    created: list[str] = []
    yield created
    if created:
        from sqlalchemy.ext.asyncio import async_sessionmaker

        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as session:
            await session.execute(delete(IntegrationInstance).where(IntegrationInstance.entity_id.in_(created)))
            await session.commit()


async def test_list_integration_schemas_includes_all_five_domains(client):
    resp = await client.get("/api/v1/integrations/schemas")
    assert resp.status_code == 200
    body = resp.json()
    for domain in ("vex_tm", "spotify", "atem", "obs", "zeros"):
        assert domain in body
        assert "config_schema" in body[domain]


async def test_create_integration_rejects_unknown_domain(client, cleanup_entities):
    entity_id = _unique_entity_id("nope")
    resp = await client.post(
        "/api/v1/integrations",
        json={
            "entity_id": entity_id,
            "domain": "not_a_real_domain",
            "display_name": "Bad domain",
            "config": {},
            "tags": [],
        },
    )
    assert resp.status_code == 400


async def test_create_list_update_delete_integration(client, cleanup_entities, db_engine):
    entity_id = _unique_entity_id("obs")
    cleanup_entities.append(entity_id)

    create_resp = await client.post(
        "/api/v1/integrations",
        json={
            "entity_id": entity_id,
            "domain": "obs",
            "display_name": "OBS Test",
            "config": {"host": "127.0.0.1", "port": 4455, "password": "supersecret"},
            "tags": ["fieldset.1"],
        },
    )
    assert create_resp.status_code == 201
    body = create_resp.json()
    assert body["entity_id"] == entity_id
    # Secret field must never come back in plaintext (redacted).
    assert body["config"]["password"] != "supersecret"

    list_resp = await client.get("/api/v1/integrations")
    assert any(row["entity_id"] == entity_id for row in list_resp.json())

    update_resp = await client.put(
        f"/api/v1/integrations/{entity_id}",
        json={"display_name": "OBS Renamed", "tags": ["fieldset.1", "extra"]},
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["display_name"] == "OBS Renamed"
    assert update_resp.json()["config"]["password"] != "supersecret"

    delete_resp = await client.delete(f"/api/v1/integrations/{entity_id}")
    assert delete_resp.status_code == 204

    get_after_delete = await client.get("/api/v1/integrations")
    assert not any(row["entity_id"] == entity_id for row in get_after_delete.json())


async def test_secret_field_encrypted_at_rest(client, cleanup_entities, db_engine):
    """Regression/security guard: `secret: true` config fields (per the
    domain's manifest.yaml) must be Fernet-encrypted in Postgres, never
    stored in plaintext — and must round-trip via `backend.core.security.decrypt`."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    entity_id = _unique_entity_id("obs")
    cleanup_entities.append(entity_id)

    resp = await client.post(
        "/api/v1/integrations",
        json={
            "entity_id": entity_id,
            "domain": "obs",
            "display_name": "OBS Secret Test",
            "config": {"host": "127.0.0.1", "port": 4455, "password": "supersecret-value"},
            "tags": [],
        },
    )
    assert resp.status_code == 201

    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as session:
        result = await session.execute(
            IntegrationInstance.__table__.select().where(IntegrationInstance.entity_id == entity_id)
        )
        row = result.mappings().one()

    stored_password = row["config"]["password"]
    assert stored_password != "supersecret-value"
    assert decrypt(stored_password) == "supersecret-value"


async def test_update_does_not_reencrypt_untouched_secrets(client, cleanup_entities, db_engine):
    """Regression: PUT merged the stored (already-encrypted) config with the
    request and then re-encrypted every secret field, so each save wrapped
    unchanged secrets in another Fernet layer and the loader handed ciphertext
    to the remote service. Secrets must stay single-encrypted across updates,
    and echoing back the redaction mask must not overwrite the stored value."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    entity_id = _unique_entity_id("obs")
    cleanup_entities.append(entity_id)
    await client.post(
        "/api/v1/integrations",
        json={
            "entity_id": entity_id,
            "domain": "obs",
            "display_name": "OBS Reencrypt Test",
            "config": {"host": "127.0.0.1", "port": 4455, "password": "supersecret-value"},
            "tags": [],
        },
    )

    for config in ({"port": 4456}, {"port": 4457, "password": "\u2022" * 8}):
        resp = await client.put(f"/api/v1/integrations/{entity_id}", json={"config": config})
        assert resp.status_code == 200

    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as session:
        result = await session.execute(
            IntegrationInstance.__table__.select().where(IntegrationInstance.entity_id == entity_id)
        )
        row = result.mappings().one()

    assert row["config"]["port"] == 4457
    assert decrypt(row["config"]["password"]) == "supersecret-value"


async def test_list_and_state_report_shared_redis_status_on_passive_replica(client, cleanup_entities, fake_redis):
    """Regression: a replica that isn't running the integration (only the
    leader's `Loader` holds instances) reported DISCONNECTED from its own
    empty in-memory map, disagreeing with the leader-published status the
    dashboard's cluster card reads from Redis."""
    from backend.modules.integrations.base import STATUS_KEY_TMPL

    entity_id = _unique_entity_id("obs")
    cleanup_entities.append(entity_id)
    await client.post(
        "/api/v1/integrations",
        json={
            "entity_id": entity_id,
            "domain": "obs",
            "display_name": "OBS Shared Status",
            "config": {"host": "127.0.0.1", "port": 4455, "password": "x"},
            "tags": [],
        },
    )
    await fake_redis.set(STATUS_KEY_TMPL.format(entity_id=entity_id), "CONNECTED")

    listed = (await client.get("/api/v1/integrations")).json()
    assert next(r for r in listed if r["entity_id"] == entity_id)["status"] == "CONNECTED"

    state = (await client.get(f"/api/v1/integrations/{entity_id}/state")).json()
    assert state["status"] == "CONNECTED"


async def test_create_duplicate_entity_id_returns_409(client, cleanup_entities):
    entity_id = _unique_entity_id("zeros")
    cleanup_entities.append(entity_id)
    body = {
        "entity_id": entity_id,
        "domain": "zeros",
        "display_name": "Zeros Test",
        "config": {"ip": "10.0.0.5", "port": 8000},
        "tags": [],
    }
    first = await client.post("/api/v1/integrations", json=body)
    assert first.status_code == 201
    second = await client.post("/api/v1/integrations", json=body)
    assert second.status_code == 409


async def test_delete_missing_integration_returns_404(client):
    resp = await client.delete(f"/api/v1/integrations/does-not-exist-{uuid.uuid4().hex}")
    assert resp.status_code == 404


async def test_service_call_on_not_running_instance_returns_503(client, cleanup_entities):
    """No Loader is running in this test process (no leader-election
    lifespan), so any existing instance's `call_service` must surface a 503
    rather than crash."""
    entity_id = _unique_entity_id("atem")
    cleanup_entities.append(entity_id)
    create_resp = await client.post(
        "/api/v1/integrations",
        json={
            "entity_id": entity_id,
            "domain": "atem",
            "display_name": "ATEM Test",
            "config": {"ip": "10.0.0.6", "field_to_input": {"1": 1}},
            "tags": [],
        },
    )
    assert create_resp.status_code == 201

    resp = await client.post(f"/api/v1/integrations/{entity_id}/service/cut", json={})
    assert resp.status_code == 503


async def test_test_connection_on_unknown_entity_returns_404(client):
    resp = await client.post(f"/api/v1/integrations/does-not-exist-{uuid.uuid4().hex}/test")
    assert resp.status_code == 404


async def test_test_connection_on_not_running_instance_reports_not_ok(client, cleanup_entities):
    """No Loader is running in this test process, so `POST /test` (plan
    §12 Integrations page "Test Connection" button, finding 1.11) must
    report `ok: false` rather than crash or 503 — it's a read-only status
    check, not a mutation, so a not-running instance is a normal (if
    negative) result."""
    entity_id = _unique_entity_id("atem")
    cleanup_entities.append(entity_id)
    create_resp = await client.post(
        "/api/v1/integrations",
        json={
            "entity_id": entity_id,
            "domain": "atem",
            "display_name": "ATEM Test",
            "config": {"ip": "10.0.0.6", "field_to_input": {"1": 1}},
            "tags": [],
        },
    )
    assert create_resp.status_code == 201

    resp = await client.post(f"/api/v1/integrations/{entity_id}/test")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "not currently running" in body["detail"]


async def test_config_change_published_on_create(client, cleanup_entities, fake_redis):
    """Confirms `config_change` is published to the pub/sub channel the
    Loader hot-reload listener subscribes to (plan §5.2)."""
    pubsub = fake_redis.pubsub()
    await pubsub.subscribe("qecomp:config_change")
    # Drain the subscribe confirmation message.
    await pubsub.get_message(ignore_subscribe_messages=False, timeout=1)

    entity_id = _unique_entity_id("zeros")
    cleanup_entities.append(entity_id)
    resp = await client.post(
        "/api/v1/integrations",
        json={
            "entity_id": entity_id,
            "domain": "zeros",
            "display_name": "Zeros Config Change Test",
            "config": {"ip": "10.0.0.7", "port": 8000},
            "tags": [],
        },
    )
    assert resp.status_code == 201

    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=2)
    assert message is not None
    assert entity_id in message["data"]
    await pubsub.aclose()


async def test_schemas_expose_service_catalog(client):
    data = (await client.get("/api/v1/integrations/schemas")).json()
    wled = data["wled"]["services"]
    assert wled["set_color"]["fields"]["color"]["required"] is True
    assert "flash" in wled and "description" in wled["flash"]
    assert all(isinstance(d["services"], dict) for d in data.values())
    assert "switch_input" in data["atem"]["services"]
