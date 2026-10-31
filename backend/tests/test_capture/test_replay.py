"""Replay player: state logic, the page route and `/ws/replay` (issue #41)."""
from __future__ import annotations

import json

import fakeredis.aioredis
import pytest

from backend.modules.capture import replay

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


async def _published(r, fn):
    pubsub = r.pubsub()
    await pubsub.subscribe(replay.EVENTS_CHANNEL)
    await pubsub.get_message(timeout=0.1)
    await fn()
    msgs = []
    for _ in range(5):
        m = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.2)
        if m:
            msgs.append(json.loads(m["data"]))
    await pubsub.aclose()
    return msgs


async def test_play_stores_state_and_publishes(redis):
    msgs = await _published(redis, lambda: replay.play(redis, "main", "http://x/v.mp4", "Q 7 · moment 1", 0.25, True))
    assert msgs[0]["type"] == "replay_command" and msgs[0]["entity_id"] == "main"
    assert msgs[0]["payload"] == {"type": "play", "url": "http://x/v.mp4", "label": "Q 7 · moment 1", "rate": 0.25, "loop": True}
    state = await replay.get_state(redis, "main")
    assert state["playing"] and state["rate"] == 0.25 and state["paused"] is False


async def test_get_state_default_and_corrupt(redis):
    assert await replay.get_state(redis, "nope") == {"playing": False}
    await redis.set("qecomp:replay:bad:state", "{not json")
    assert await replay.get_state(redis, "bad") == {"playing": False}


async def test_control_actions_update_state(redis):
    with pytest.raises(ValueError, match="Nothing is playing"):
        await replay.control(redis, "main", "pause")
    await replay.play(redis, "main", "u", "l")
    assert (await replay.control(redis, "main", "pause"))["paused"] is True
    assert (await replay.control(redis, "main", "resume"))["paused"] is False
    await replay.control(redis, "main", "pause")
    assert (await replay.control(redis, "main", "restart"))["paused"] is False
    assert (await replay.control(redis, "main", "rate", 0.75))["rate"] == 0.75
    assert (await replay.control(redis, "main", "seek", 3))["playing"] is True
    assert (await replay.control(redis, "main", "stop")) == {"playing": False}
    assert await replay.get_state(redis, "main") == {"playing": False}


async def test_control_validation(redis):
    await replay.play(redis, "main", "u", "l")
    for action, value in (("bogus", None), ("rate", None), ("rate", 9), ("rate", 0), ("seek", None), ("seek", -1)):
        with pytest.raises(ValueError):
            await replay.control(redis, "main", action, value)


async def test_stop_when_idle_is_ok(redis):
    assert await replay.control(redis, "main", "stop") == {"playing": False}
