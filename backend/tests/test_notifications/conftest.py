"""Shared fixtures for notification tests that need real Postgres."""
from __future__ import annotations

import fakeredis
import pytest
from fakeredis import aioredis as fake_aioredis
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.core.settings import settings
from backend.models.notification import NotificationLog, PushSubscription
from backend.models.room import Room
from backend.models.settings import SystemSetting
from backend.modules.notifications import delivery

VALID_ENDPOINT = "https://fcm.googleapis.com/fcm/send/"


@pytest.fixture
async def session_factory():
    engine = create_async_engine(settings.POSTGRES_DSN, future=True)
    try:
        async with engine.connect() as conn:
            await conn.exec_driver_sql("SELECT 1")
            await conn.exec_driver_sql("SELECT 1 FROM rooms LIMIT 1")
    except Exception:
        await engine.dispose()
        pytest.skip("Real Postgres (migrated) not reachable; skipping notification DB tests")
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def _wipe() -> None:
        async with factory() as session:
            await session.execute(delete(PushSubscription))
            await session.execute(delete(Room))
            await session.execute(delete(NotificationLog))
            await session.commit()

    await _wipe()  # start from a clean slate even if an earlier run left rows behind
    yield factory
    await _wipe()
    await engine.dispose()


@pytest.fixture
async def fake_redis():
    client = fake_aioredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
def vapid(monkeypatch):
    monkeypatch.setattr(settings, "VAPID_PRIVATE_KEY", "test-private")
    monkeypatch.setattr(settings, "VAPID_PUBLIC_KEY", "test-public")


@pytest.fixture
def pushes(monkeypatch, vapid):
    """Replaces the blocking pywebpush call; records `PushTarget`s. Set
    `pushes.result[endpoint] = "gone"` to simulate a dead subscription."""

    class Recorder(list):
        result: dict[str, str] = {}

    rec = Recorder()
    rec.result = {}

    def fake_send(target):
        rec.append(target)
        return rec.result.get(target.endpoint, "ok")

    monkeypatch.setattr(delivery, "_send_blocking", fake_send)
    return rec


@pytest.fixture
async def notif_settings(session_factory):
    """Sets the `notifications` system_settings value for a test, restoring it after."""
    async with session_factory() as session:
        row = await session.get(SystemSetting, "notifications")
        original = dict(row.value) if row else None

    async def _set(value: dict):
        async with session_factory() as session:
            row = await session.get(SystemSetting, "notifications")
            if row is None:
                session.add(SystemSetting(key="notifications", value=value))
            else:
                row.value = value
            await session.commit()

    yield _set

    async with session_factory() as session:
        row = await session.get(SystemSetting, "notifications")
        if original is None:
            if row is not None:
                await session.delete(row)
        elif row is not None:
            row.value = original
        await session.commit()
