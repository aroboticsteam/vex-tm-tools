"""Capture agent control integration (issues #42, #38, #45).

The heavy lifting (recording rows, agent commands) lives in
`backend.modules.capture.service`, shared with the REST routes and the agent
WebSocket; this class is the automation-facing front and the status watcher.
It is the one integration that imports another module: it needs the recording
database, which the integration contract otherwise keeps out of reach.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from backend.core.db import async_session_factory
from backend.core.exceptions import IntegrationError
from backend.modules.capture import hub
from backend.modules.capture import service as capture_service
from backend.modules.integrations.base import Integration

logger = logging.getLogger(__name__)

WATCH_INTERVAL_S = 5.0


class CaptureIntegration(Integration):
    def __init__(self, entity_id: str, config: dict[str, Any], redis: Any, db_pool: Any) -> None:
        super().__init__(entity_id, config, redis, db_pool)
        self._watch_task: asyncio.Task | None = None
        self._connected: bool | None = None

    async def setup(self) -> None:
        # The watcher keeps status in step with the agent's presence; it only
        # starts once, and never keeps `setup` from returning.
        await self._refresh_status()
        self._watch_task = asyncio.create_task(self._watch())

    async def teardown(self) -> None:
        task, self._watch_task = self._watch_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task

    async def _refresh_status(self) -> None:
        connected = await hub.get_presence(self._redis) is not None
        if connected != self._connected:
            self._connected = connected
            await self.report_status("CONNECTED" if connected else "DEGRADED")

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(WATCH_INTERVAL_S)
            try:
                await self._refresh_status()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("capture[%s]: status check failed", self.entity_id)

    async def call_service(self, service: str, data: dict[str, Any]) -> dict[str, Any]:
        if service == "start_recording":
            return await capture_service.start_recording(self._redis, async_session_factory, data)
        if service == "stop_recording":
            return await capture_service.stop_recording(self._redis, async_session_factory)
        if service == "mark_moment":
            try:
                seconds = float(data.get("seconds"))
            except (TypeError, ValueError):
                raise IntegrationError(f"capture[{self.entity_id}]: 'seconds' must be a number") from None
            return await capture_service.mark_moment(self._redis, async_session_factory, seconds)
        raise ValueError(f"Unknown capture service '{service}'")

    async def get_state(self) -> dict[str, Any]:
        try:
            presence = await hub.get_presence(self._redis)
        except Exception:
            presence = None
        return {"entity_id": self.entity_id, "connected": presence is not None, "agent": presence}


IntegrationClass = CaptureIntegration
