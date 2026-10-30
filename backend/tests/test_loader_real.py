"""Tests for backend.loader.Loader against REAL Postgres + Redis (the
compose services, not fakeredis/a fake session factory) — mirrors the
pattern in tests/test_core/test_leader.py: skip gracefully if the real
infra isn't reachable, since these exercise genuine DB round-trips and
Redis pub/sub timing that a mock can't fully stand in for.

Requires `docker compose up -d postgres redis` (see compose.yml) and the
Alembic migrations applied (`alembic upgrade head`).

Uses the real `vex_tm` integration pointed at `backend.tests.mock_tm_server`
so `load_all()`/`reconcile()` exercise an actual `setup()` that succeeds
(CONNECTED), not just a domain that's guaranteed to fail.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
import redis.asyncio as redis
import uvicorn
from sqlalchemy import delete, insert, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.loader as loader_module
from backend.core.settings import settings
from backend.models.integration import IntegrationInstance
from backend.tests.mock_tm_server import create_app

pytestmark = pytest.mark.asyncio

MOCK_TM_HOST = "127.0.0.1"
MOCK_TM_PORT = 18185


@pytest.fixture
async def real_redis():
    client = redis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        await client.ping()
    except Exception:
        pytest.skip("Real Redis not reachable at REDIS_URL; skipping loader real-infra tests")
    yield client
    await client.aclose()


@pytest.fixture
async def real_session_factory():
    engine = create_async_engine(settings.POSTGRES_DSN, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            await conn.exec_driver_sql("SELECT 1")
    except Exception:
        await engine.dispose()
        pytest.skip("Real Postgres not reachable at POSTGRES_DSN; skipping loader real-infra tests")
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def mock_tm_server():
    app = create_app()
    config = uvicorn.Config(app, host=MOCK_TM_HOST, port=MOCK_TM_PORT, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.02)
    else:
        pytest.fail("mock TM server did not start in time")
    yield app
    server.should_exit = True
    await asyncio.wait_for(task, timeout=5)


def _entity_id() -> str:
    return f"vex_tm.real_loader_test_{uuid.uuid4().hex[:8]}"


async def test_discover_registers_all_domains_against_real_infra(real_redis, real_session_factory):
    ldr = loader_module.Loader(redis=real_redis, session_factory=real_session_factory)
    ldr.discover()
    assert set(loader_module.INTEGRATION_REGISTRY.keys()) == {
        "vex_tm",
        "spotify",
        "atem",
        "zeros",
        "obs",
        "wled",
        "osc",
        "ptz",
        "artnet",
    }


async def test_load_all_and_hot_reload_against_real_postgres_and_redis(
    real_redis, real_session_factory, mock_tm_server
):
    entity_id = _entity_id()
    config = {
        "base_url": f"http://{MOCK_TM_HOST}:{MOCK_TM_PORT}",
        "client_id": "test-client-id",
        "client_secret": "test-client-secret",
        "api_key": "test-api-key-for-hmac-signing",
        "field_set_id": 1,
        "poll_interval_seconds": 3600,
        "auth_url": f"http://{MOCK_TM_HOST}:{MOCK_TM_PORT}/oauth2/token",
    }

    async with real_session_factory() as session:
        await session.execute(
            insert(IntegrationInstance).values(
                entity_id=entity_id,
                domain="vex_tm",
                display_name="Real Loader Test",
                config=config,
                enabled=True,
                tags=["fs1"],
            )
        )
        await session.commit()

    ldr = loader_module.Loader(redis=real_redis, session_factory=real_session_factory)
    try:
        await ldr.load_all()

        # 1) Instantiated + connected against the real mock TM server.
        instance = ldr.get_instance(entity_id)
        assert instance is not None
        assert ldr.get_instance_status(entity_id) == "CONNECTED"
        assert [i.entity_id for i in ldr.get_instances_by_tag("fs1")] == [entity_id]
        assert instance in ldr.get_all_instances()

        # 2) Update the row's tags in Postgres, publish config_change, and
        #    confirm the loader hot-reloads (tears down + respins) live,
        #    with no process restart.
        async with real_session_factory() as session:
            await session.execute(
                update(IntegrationInstance)
                .where(IntegrationInstance.entity_id == entity_id)
                .values(tags=["fs1", "fs2"])
            )
            await session.commit()

        await asyncio.sleep(0.2)  # let the pubsub listener finish subscribing
        await real_redis.publish(loader_module.CONFIG_CHANGE_CHANNEL, "reload")

        for _ in range(200):
            new_instance = ldr.get_instance(entity_id)
            if new_instance is not None and new_instance is not instance and "fs2" in new_instance.tags:
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("hot-reload after Postgres UPDATE did not respin the instance in time")

        assert ldr.get_instance_status(entity_id) == "CONNECTED"

        # 3) Delete the row, publish config_change, confirm the loader tears
        #    the instance down and reports DISCONNECTED — still live.
        async with real_session_factory() as session:
            await session.execute(delete(IntegrationInstance).where(IntegrationInstance.entity_id == entity_id))
            await session.commit()

        await real_redis.publish(loader_module.CONFIG_CHANGE_CHANNEL, "reload")

        for _ in range(200):
            if ldr.get_instance(entity_id) is None:
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("hot-reload after Postgres DELETE did not tear down the instance in time")

        assert ldr.get_instance_status(entity_id) == "DISCONNECTED"
        redis_status = await real_redis.get(f"qecomp:integration:{entity_id}:status")
        assert redis_status == "DISCONNECTED"
    finally:
        await ldr.teardown_all()
        async with real_session_factory() as session:
            await session.execute(delete(IntegrationInstance).where(IntegrationInstance.entity_id == entity_id))
            await session.commit()
