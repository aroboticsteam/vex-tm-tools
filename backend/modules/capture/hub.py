"""Server side of the link to the capture agent.

The agent's WebSocket lives in whichever API process accepted it, while
commands originate on the leader (automations) or in any request handler, so
commands travel over the event bus (`capture_command`) and the
`ConnectionManager` of the process holding the socket forwards them. Presence
is a short-TTL Redis key the agent's heartbeat keeps alive.
"""
from __future__ import annotations

import json
import time
from typing import Any

from backend.core.exceptions import IntegrationError
from backend.schemas.events import EventBusMessage

EVENTS_CHANNEL = "qecomp:events"
PRESENCE_KEY = "qecomp:capture:agent"
PRESENCE_TTL_S = 45
#: Bucket in the WebSocket `ConnectionManager` holding the agent's socket.
AGENT_BUCKET = "capture:agent"
ENTITY_ID = "capture.agent"


async def publish(redis: Any, event_type: str, payload: dict[str, Any]) -> None:
    message = EventBusMessage(entity_id=ENTITY_ID, entity_tags=[], type=event_type, timestamp=time.time(), payload=payload)
    await redis.publish(EVENTS_CHANNEL, message.model_dump_json())


async def set_presence(redis: Any, info: dict[str, Any]) -> None:
    await redis.set(PRESENCE_KEY, json.dumps(info), ex=PRESENCE_TTL_S)


async def clear_presence(redis: Any) -> None:
    await redis.delete(PRESENCE_KEY)


async def get_presence(redis: Any) -> dict[str, Any] | None:
    raw = await redis.get(PRESENCE_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


async def send_command(redis: Any, command: dict[str, Any]) -> None:
    """Relay `command` to the connected agent, or fail clearly if there is none."""
    if await get_presence(redis) is None:
        raise IntegrationError("The capture agent is not connected")
    await publish(redis, "capture_command", command)
