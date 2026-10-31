"""Replay player state (issue #41).

An OBS browser source loads `/replay/<entity_id>`, which listens on
`/ws/replay/<entity_id>`. "Send to program" stores what should be playing and
publishes a `replay_command` event; the node holding the page's socket forwards
it. The stored state lets a reloaded page (OBS refreshes sources on scene
switches, depending on settings) pick up what was playing.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from backend.schemas.events import EventBusMessage

EVENTS_CHANNEL = "qecomp:events"
ENTITY_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
BUCKET_PREFIX = "replay:"
STATE_TTL_S = 6 * 3600
RATES = (0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0)
DEFAULT_RATE = 0.5
ACTIONS = ("pause", "resume", "rate", "seek", "stop", "restart")


def _state_key(entity_id: str) -> str:
    return f"qecomp:replay:{entity_id}:state"


async def get_state(redis: Any, entity_id: str) -> dict[str, Any]:
    raw = await redis.get(_state_key(entity_id))
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except ValueError:
            pass
    return {"playing": False}


async def _save(redis: Any, entity_id: str, state: dict[str, Any]) -> None:
    await redis.set(_state_key(entity_id), json.dumps(state), ex=STATE_TTL_S)


async def _publish(redis: Any, entity_id: str, command: dict[str, Any]) -> None:
    message = EventBusMessage(entity_id=entity_id, entity_tags=[], type="replay_command", timestamp=time.time(), payload=command)
    await redis.publish(EVENTS_CHANNEL, message.model_dump_json())


async def play(redis: Any, entity_id: str, url: str, label: str, rate: float = DEFAULT_RATE, loop: bool = False) -> dict[str, Any]:
    state = {"playing": True, "paused": False, "url": url, "label": label, "rate": rate, "loop": loop}
    await _save(redis, entity_id, state)
    await _publish(redis, entity_id, {"type": "play", "url": url, "label": label, "rate": rate, "loop": loop})
    return state


async def control(redis: Any, entity_id: str, action: str, value: float | None = None) -> dict[str, Any]:
    if action not in ACTIONS:
        raise ValueError(f"Unknown replay action {action!r}")
    state = await get_state(redis, entity_id)
    if action == "stop":
        state = {"playing": False}
    elif not state.get("playing"):
        raise ValueError("Nothing is playing")
    elif action == "pause":
        state["paused"] = True
    elif action in ("resume", "restart"):
        state["paused"] = False
    elif action == "rate":
        if value is None or not 0.05 <= value <= 4:
            raise ValueError("rate needs a value between 0.05 and 4")
        state["rate"] = value
    elif action == "seek":
        if value is None or value < 0:
            raise ValueError("seek needs a non-negative value in seconds")
    await _save(redis, entity_id, state)
    command: dict[str, Any] = {"type": action}
    if value is not None:
        command["value"] = value
    await _publish(redis, entity_id, command)
    return state
