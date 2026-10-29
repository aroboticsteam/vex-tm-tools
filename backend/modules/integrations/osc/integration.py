"""Generic OSC-over-UDP sender (issue #7: OSC/lights).

Complements the `zeros` integration (which only fires ZerOS playbacks) by
exposing arbitrary `address + args` messages so any OSC-controlled lighting
or show-control software can be driven from automations or button pages.
`SimpleUDPClient.send_message` is a blocking UDP send, so it runs in a
thread (never directly on the event loop).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from pythonosc.udp_client import SimpleUDPClient

from backend.core.exceptions import IntegrationError
from backend.modules.integrations.base import Integration

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8000
MAX_MESSAGES = 100
_ARG_TYPES = (int, float, str, bool, type(None))


def _validate_message(address: Any, args: Any) -> tuple[str, Any]:
    if not isinstance(address, str) or not address.startswith("/") or len(address) < 2:
        raise ValueError("OSC address must be a string starting with '/'")
    if args is None:
        return address, []
    if not isinstance(args, (list, tuple)):
        args = [args]
    if not all(isinstance(a, _ARG_TYPES) for a in args):
        raise ValueError("OSC args must be numbers, strings, booleans or null")
    return address, list(args)


class OscIntegration(Integration):
    def __init__(self, entity_id: str, config: dict[str, Any], redis: Any, db_pool: Any) -> None:
        super().__init__(entity_id, config, redis, db_pool)
        self._client: SimpleUDPClient | None = None
        self._last_message: dict[str, Any] | None = None

    async def setup(self) -> None:
        ip = self.config.get("ip")
        if not ip:
            raise IntegrationError(f"osc[{self.entity_id}]: 'ip' is required")
        port = int(self.config.get("port") or DEFAULT_PORT)
        self._client = await asyncio.to_thread(SimpleUDPClient, ip, port)

    async def teardown(self) -> None:
        self._client = None

    async def _send(self, address: str, args: list[Any]) -> None:
        assert self._client is not None
        try:
            await asyncio.to_thread(self._client.send_message, address, args if args else None)
        except OSError as exc:
            raise IntegrationError(f"osc[{self.entity_id}]: send failed: {exc}") from exc
        self._last_message = {"address": address, "args": args}

    async def call_service(self, service: str, data: dict[str, Any]) -> dict[str, Any]:
        data = data or {}
        if service not in ("send", "send_bundle"):
            raise ValueError(f"Unknown service '{service}' for osc integration")
        if self._client is None:
            raise IntegrationError(f"osc[{self.entity_id}]: not connected")
        if service == "send":
            address, args = _validate_message(data.get("address"), data.get("args"))
            await self._send(address, args)
            return {"ok": True, "sent": 1, "address": address}
        messages = data.get("messages")
        if not isinstance(messages, list) or not messages or len(messages) > MAX_MESSAGES:
            raise ValueError(f"'messages' must be a list of 1-{MAX_MESSAGES} items")
        validated = []
        for item in messages:
            if not isinstance(item, dict):
                raise ValueError("Each message must be an object with 'address' and optional 'args'")
            validated.append(_validate_message(item.get("address"), item.get("args")))
        for address, args in validated:
            await self._send(address, args)
        return {"ok": True, "sent": len(validated)}

    async def get_state(self) -> dict[str, Any]:
        return {"entity_id": self.entity_id, "connected": self._client is not None, "last_message": self._last_message}


IntegrationClass = OscIntegration
