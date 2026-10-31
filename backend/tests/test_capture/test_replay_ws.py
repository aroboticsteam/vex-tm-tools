"""Replay player page route and `/ws/replay` end to end (issue #41).

Sync tests on purpose: Starlette's TestClient runs the app in its own loop and
must not be nested inside an async test."""
from __future__ import annotations

import asyncio
import json
import time

import pytest
import redis as redis_sync
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.testclient import TestClient

from backend.core.settings import settings
from backend.main import app
from backend.modules.capture import replay

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


needs_infra = pytest.mark.skipif(not _infra(), reason="Real Postgres/Redis not reachable")


@needs_infra
def test_page_route_substitutes_id_and_rejects_bad_ids():
    with TestClient(app) as client:
        ok = client.get("/replay/program-1")
        assert ok.status_code == 200 and '"program-1"' in ok.text and "__ENTITY_ID__\"" not in ok.text.split("CONFIG")[1][:60]
        assert client.get("/replay/bad id!").status_code in (404, 422)
        assert client.get("/replay/" + "a" * 101).status_code in (404, 422)


@needs_infra
def test_ws_replay_state_on_connect_and_command_forwarding():
    r = redis_sync.Redis.from_url(settings.REDIS_URL, decode_responses=True)
    eid = f"t{int(time.time() * 1000)}"
    r.set(f"qecomp:replay:{eid}:state", json.dumps({"playing": True, "paused": True, "url": "http://x/a.mp4", "label": "L", "rate": 0.5, "loop": False}))
    try:
        with TestClient(app) as client:
            with client.websocket_connect(f"/ws/replay/{eid}") as ws:
                first = ws.receive_json()
                assert first["type"] == "play" and first["url"] == "http://x/a.mp4" and first["paused"] is True
                r.publish(
                    replay.EVENTS_CHANNEL,
                    json.dumps({"entity_id": eid, "entity_tags": [], "type": "replay_command", "timestamp": time.time(), "payload": {"type": "rate", "value": 0.25}}),
                )
                assert ws.receive_json() == {"type": "rate", "value": 0.25}
            with pytest.raises(Exception):
                with client.websocket_connect("/ws/replay/bad id!"):
                    pass
    finally:
        r.delete(f"qecomp:replay:{eid}:state")
        r.close()
