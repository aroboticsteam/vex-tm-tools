"""`/ws/agent` end to end: real app lifespan, real Redis fan-out (issue #45)."""
from __future__ import annotations

import asyncio
import json
import time
import uuid

import pytest
import redis as redis_sync
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.testclient import TestClient

from backend.core.security import generate_api_key, hash_api_key
from backend.core.settings import settings
from backend.main import app
from backend.models.settings import ApiKey
from backend.modules.capture import hub


def _infra() -> bool:
    async def _check() -> bool:
        engine = create_async_engine(settings.POSTGRES_DSN)
        try:
            async with engine.connect() as conn:
                await conn.exec_driver_sql("SELECT 1")
            redis_sync.Redis.from_url(settings.REDIS_URL).ping()
            return True
        except Exception:
            return False
        finally:
            await engine.dispose()

    return asyncio.run(_check())


pytestmark = pytest.mark.skipif(not _infra(), reason="Real Postgres/Redis not reachable")


def _make_key(permissions: list[str]) -> tuple[str, uuid.UUID]:
    raw = generate_api_key()

    async def _go() -> uuid.UUID:
        engine = create_async_engine(settings.POSTGRES_DSN)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                row = ApiKey(name=f"capture-test-{uuid.uuid4().hex[:6]}", key_hash=hash_api_key(raw), permissions=permissions)
                session.add(row)
                await session.commit()
                return row.id
        finally:
            await engine.dispose()

    return raw, asyncio.run(_go())


def _drop_key(key_id: uuid.UUID) -> None:
    async def _go() -> None:
        engine = create_async_engine(settings.POSTGRES_DSN)
        try:
            async with engine.begin() as conn:
                await conn.execute(delete(ApiKey).where(ApiKey.id == key_id))
        finally:
            await engine.dispose()

    asyncio.run(_go())


def _publish_command(command: dict) -> None:
    r = redis_sync.Redis.from_url(settings.REDIS_URL, decode_responses=True)
    r.publish(
        "qecomp:events",
        json.dumps(
            {"entity_id": hub.ENTITY_ID, "entity_tags": [], "type": "capture_command", "timestamp": time.time(), "payload": command}
        ),
    )
    r.close()


def test_agent_rejects_missing_or_underprivileged_key():
    raw, key_id = _make_key(["analysis:view"])
    try:
        with TestClient(app) as client:
            with pytest.raises(Exception):
                with client.websocket_connect("/ws/agent"):
                    pass
            with pytest.raises(Exception):
                with client.websocket_connect("/ws/agent", headers={"Authorization": f"Bearer {raw}"}):
                    pass
    finally:
        _drop_key(key_id)


def test_agent_connects_receives_commands_and_reports_back():
    raw, key_id = _make_key(["capture:upload"])
    r = redis_sync.Redis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        with TestClient(app) as client:
            with client.websocket_connect("/ws/agent", headers={"Authorization": f"Bearer {raw}"}) as ws:
                ws.send_json({"type": "hello", "agent_id": "ws-test", "version": "1", "state": "idle"})
                deadline = time.time() + 5
                presence = None
                while time.time() < deadline:
                    presence = r.get(hub.PRESENCE_KEY)
                    if presence and "ws-test" in presence:
                        break
                    time.sleep(0.05)
                assert presence and json.loads(presence)["agent_id"] == "ws-test"

                command = {"type": "start_recording", "recording_id": str(uuid.uuid4()), "match_type": "QUAL", "match_number": "7"}
                _publish_command(command)
                received = ws.receive_json()
                assert received == command  # exactly once, only the payload

                # Unknown agent messages are ignored rather than closing the socket.
                ws.send_json({"type": "bogus"})
                ws.send_json({"type": "status", "state": "recording"})
                time.sleep(0.3)
                assert json.loads(r.get(hub.PRESENCE_KEY))["state"] == "recording"
            time.sleep(0.3)
            assert r.get(hub.PRESENCE_KEY) is None  # cleared when the agent disconnects
    finally:
        r.delete(hub.PRESENCE_KEY)
        r.close()
        _drop_key(key_id)
