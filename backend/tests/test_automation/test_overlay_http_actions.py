"""`spotlight` and `http` button/automation actions (issue #24 / #25)."""
from __future__ import annotations

import json
import uuid

import fakeredis
import httpx
import pytest
from fakeredis import aioredis as fake_aioredis
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.core.settings import settings
from backend.models.overlay import OverlayInstance
from backend.modules.automation.engine import AutomationEngine

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def factory():
    engine = create_async_engine(settings.POSTGRES_DSN, future=True)
    try:
        async with engine.connect() as conn:
            await conn.exec_driver_sql("SELECT 1 FROM overlay_instances LIMIT 1")
    except Exception:
        await engine.dispose()
        pytest.skip("Real Postgres (migrated) not reachable")
    f = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def wipe():
        async with f() as s:
            await s.execute(delete(OverlayInstance).where(OverlayInstance.entity_id.like("spot-%")))
            await s.commit()

    await wipe()
    yield f
    await wipe()
    await engine.dispose()


@pytest.fixture
async def redis():
    client = fake_aioredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    yield client
    await client.aclose()


async def events(redis, run):
    listener = redis.pubsub()
    await listener.subscribe("qecomp:events")
    await listener.get_message(ignore_subscribe_messages=True, timeout=0.25)
    await run()
    out = []
    while (msg := await listener.get_message(ignore_subscribe_messages=True, timeout=0.25)) is not None:
        out.append(json.loads(msg["data"]))
    await listener.aclose()
    return out


async def add_overlay(factory, entity_id, tags=()):
    async with factory() as s:
        s.add(OverlayInstance(entity_id=entity_id, display_name=entity_id, field_set_id=1, tags=list(tags)))
        await s.commit()


async def test_spotlight_action_publishes_event_for_named_overlay(factory, redis):
    await add_overlay(factory, "spot-main", ["finals"])
    await add_overlay(factory, "spot-other")
    engine = AutomationEngine(redis=redis, session_factory=factory)

    sent = await events(redis, lambda: engine.run_actions(
        [{"spotlight": {"team": "1234a", "overlay": "spot-main"}}], uuid.uuid4(), {}
    ))
    # run_actions result is checked separately; here only the bus traffic matters
    assert [(e["type"], e["entity_id"], e["payload"]["team"], e["entity_tags"]) for e in sent] == [
        ("overlay_show_team", "spot-main", "1234A", ["finals"])
    ]


async def test_spotlight_clear_template_and_errors(factory, redis):
    await add_overlay(factory, "spot-main")
    engine = AutomationEngine(redis=redis, session_factory=factory)
    holder = {}

    async def run():
        holder["clear"] = await engine.run_actions([{"spotlight": {"team": None, "overlay": "spot-main"}}], uuid.uuid4(), {})
        holder["templated"] = await engine.run_actions(
            [{"spotlight": {"team": "{{ trigger.payload.team }}", "overlay": "spot-main"}}], uuid.uuid4(), {"team": "99999z"}
        )
        holder["missing"] = await engine.run_actions([{"spotlight": {"team": "1A", "overlay": "nope"}}], uuid.uuid4(), {})
        holder["bad"] = await engine.run_actions([{"spotlight": {"team": "bad team!", "overlay": "spot-main"}}], uuid.uuid4(), {})

    sent = await events(redis, run)
    assert [e["payload"]["team"] for e in sent] == [None, "99999Z"]
    assert holder["clear"]["status"] == "success" and holder["templated"]["status"] == "success"
    assert holder["missing"]["status"] == "failed" and "No overlay" in holder["missing"]["error"]
    assert holder["bad"]["status"] == "failed" and "Invalid team" in holder["bad"]["error"]


@pytest.fixture
def http_calls(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url), request.content, dict(request.headers)))
        return httpx.Response(500 if "fail" in request.url.path else 200)

    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return calls


async def test_http_action_posts_json_and_reports_failures(http_calls):
    engine = AutomationEngine(redis=object(), session_factory=object())
    ok = await engine.run_actions(
        [{"http": {"url": "https://hooks.example.com/go", "json": {"who": "{{ trigger.payload.button }}"}, "headers": {"X-Key": "k"}}}],
        uuid.uuid4(), {"button": "Go"},
    )
    assert ok["status"] == "success"
    method, url, body, headers = http_calls[0]
    assert method == "POST" and url == "https://hooks.example.com/go" and json.loads(body) == {"who": "Go"}
    assert headers["x-key"] == "k"

    failed = await engine.run_actions([{"http": "https://hooks.example.com/fail"}], uuid.uuid4(), {})
    assert failed["status"] == "failed" and "HTTP 500" in failed["error"]
    assert http_calls[-1][0] == "POST"


@pytest.mark.parametrize(
    "spec,fragment",
    [
        ({"url": "ftp://x/y"}, "http(s)"),
        ({"url": "http://169.254.169.254/latest"}, "link-local"),
        ({"url": "https://a.example", "method": "TRACE"}, "Unsupported"),
        ({}, "requires a 'url'"),
    ],
)
async def test_http_action_rejects_unsafe_specs(http_calls, spec, fragment):
    engine = AutomationEngine(redis=object(), session_factory=object())
    result = await engine.run_actions([{"http": spec}], uuid.uuid4(), {})
    assert result["status"] == "failed" and fragment in result["error"] and http_calls == []
