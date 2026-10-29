"""ntfy.sh-compatible push notification client (Appendix A.8).

Reads `server_url`/`topic`/`enabled` from the `notifications` row of the
`system_settings` table (Settings page -> Notifications section) and, when
enabled and configured, POSTs to the ntfy server's simple HTTP API
(`POST {server_url}/{topic}`, body = message, `Title`/`Priority` headers).

Per Appendix C.2, `backend/core/` may only depend on stdlib + third-party
packages — it must not import from `backend/models/`. Rather than import
`backend.models.settings.SystemSetting` (the narrow exception
`backend/core/dependencies.py` documents for RBAC), this reads the
`system_settings` row via a raw SQL `SELECT` against the shared session
factory, keeping this module inside the stdlib+third-party-only boundary.

`send_ntfy_notification()` never raises — a notification failure (ntfy
unreachable, DB unreachable, bad config, etc.) must never crash whatever
event triggered it (leader failover, integration DEGRADED, automation
retry-exhaustion, video processing failure).
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from sqlalchemy import text

from backend.core.db import async_session_factory
from backend.core.redis import redis_client

logger = logging.getLogger(__name__)

_SETTINGS_KEY = "notifications"
#: Redis channel the leader's `NotificationService` forwards to staff Web Push
#: (kept as a literal here: `backend/core/` may not import `backend/modules/`).
STAFF_ALERTS_CHANNEL = "qecomp:staff_alerts"
_REQUEST_TIMEOUT_SECONDS = 5.0


async def _read_notification_settings() -> dict[str, Any]:
    try:
        async with async_session_factory() as session:
            result = await session.execute(
                text("SELECT value FROM system_settings WHERE key = :key"),
                {"key": _SETTINGS_KEY},
            )
            row = result.first()
    except Exception:
        logger.exception("Failed to read '%s' system_settings row", _SETTINGS_KEY)
        return {}
    if row is None or not isinstance(row[0], dict):
        return {}
    return row[0]


async def send_ntfy_notification(title: str, message: str, priority: str = "default") -> None:
    """Best-effort push notification via a self-hosted ntfy server.

    Also fans the alert out to staff Web Push subscribers via Redis, whether
    or not ntfy itself is configured. The ntfy POST no-ops silently if
    notifications are disabled, or `server_url`/`topic` are unset. Never
    raises.
    """
    try:
        await redis_client.publish(
            STAFF_ALERTS_CHANNEL, json.dumps({"title": title, "message": message, "priority": priority})
        )
    except Exception as exc:
        logger.warning("Could not publish staff alert (title=%r): %s", title, exc)

    try:
        config = await _read_notification_settings()
        if not config.get("enabled"):
            return
        server_url = str(config.get("server_url") or "").rstrip("/")
        topic = str(config.get("topic") or "")
        if not server_url or not topic:
            return

        url = f"{server_url}/{topic}"
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
            await client.post(
                url,
                content=message.encode("utf-8"),
                headers={"Title": title, "Priority": priority},
            )
    except Exception:
        logger.exception("Failed to send ntfy notification (title=%r)", title)
