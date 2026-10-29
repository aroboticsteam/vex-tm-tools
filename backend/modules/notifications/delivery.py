"""Stateless delivery helpers shared by the leader-only `NotificationService`
and the REST routers (staff broadcasts run on whichever pod got the request).

Two channels:

- **Room displays** — a `room_notice` event is published on the Redis event
  bus; every pod's `ConnectionManager` forwards it to the `/ws/room/<slug>`
  clients of the named rooms.
- **Web Push** — VAPID-signed pushes to stored subscriptions (team-facing,
  scoped to teams, or staff). Dead subscriptions (404/410, or repeated
  failures) are pruned.

Nothing here raises for a delivery failure: a notification problem must not
break whatever triggered it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select, update

from backend.core.settings import settings
from backend.models.notification import KIND_STAFF, KIND_TEAM, PushSubscription, PushSubscriptionTeam
from backend.models.room import Room
from backend.schemas.events import EventBusMessage

logger = logging.getLogger(__name__)

EVENTS_CHANNEL = "qecomp:events"
#: Operator alerts (the ones that also go to ntfy) fan out to staff push over this.
STAFF_ALERTS_CHANNEL = "qecomp:staff_alerts"

ROOM_NOTICE_EVENT = "room_notice"
NOTIFICATIONS_ENTITY_ID = "notifications"

CATEGORY_SYSTEM = "system"
CATEGORY_BROADCAST = "broadcast"
STAFF_CATEGORIES = (CATEGORY_SYSTEM, CATEGORY_BROADCAST)

#: Subscriptions are dropped after this many consecutive non-gone failures.
MAX_FAILURES = 5
_PUSH_TTL_SECONDS = 600  # a "match in 3 min" push is useless after 10 min
_PUSH_CONCURRENCY = 10
_PUSH_TIMEOUT_SECONDS = 10


def push_configured() -> bool:
    return bool(settings.VAPID_PRIVATE_KEY and settings.VAPID_PUBLIC_KEY)


# ── Room displays ────────────────────────────────────────────────────


async def publish_room_notice(
    redis: Any,
    rooms: list[str],
    title: str,
    message: str,
    *,
    modal: bool = False,
    duration: int = 30,
    source: str = "system",
) -> None:
    """Publishes a notice for the given room slugs. Best-effort."""
    if not rooms:
        return
    event = EventBusMessage(
        entity_id=NOTIFICATIONS_ENTITY_ID,
        entity_tags=[],
        type=ROOM_NOTICE_EVENT,
        timestamp=time.time(),
        payload={
            "id": str(uuid.uuid4()),
            "rooms": sorted(set(rooms)),
            "title": title,
            "message": message,
            "modal": modal,
            "duration": duration,
            "source": source,
        },
    )
    try:
        await redis.publish(EVENTS_CHANNEL, event.model_dump_json())
    except Exception:
        logger.exception("Failed to publish room notice (title=%r)", title)


async def all_room_slugs(session_factory: Any) -> list[str]:
    async with session_factory() as session:
        result = await session.execute(select(Room.slug))
        return [row[0] for row in result.all()]


# ── Web Push ─────────────────────────────────────────────────────────


@dataclass
class PushTarget:
    subscription_id: uuid.UUID
    endpoint: str
    p256dh: str
    auth: str
    payload: dict[str, Any] = field(default_factory=dict)


def _send_blocking(target: PushTarget) -> str:
    """Runs in a worker thread (pywebpush uses blocking `requests`). Returns
    "ok", "gone" (subscription is dead — delete it) or "error"."""
    from pywebpush import WebPushException, webpush

    try:
        webpush(
            subscription_info={"endpoint": target.endpoint, "keys": {"p256dh": target.p256dh, "auth": target.auth}},
            data=json.dumps(target.payload),
            vapid_private_key=settings.VAPID_PRIVATE_KEY,
            vapid_claims={"sub": settings.VAPID_SUBJECT},
            ttl=_PUSH_TTL_SECONDS,
            timeout=_PUSH_TIMEOUT_SECONDS,
        )
        return "ok"
    except WebPushException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (404, 410):
            return "gone"
        logger.warning("Web Push failed (status=%s): %s", status, exc)
        return "error"
    except Exception:
        logger.exception("Unexpected Web Push error")
        return "error"


async def send_push(session_factory: Any, targets: list[PushTarget]) -> int:
    """Sends each target its own payload; updates success/failure counters and
    prunes dead subscriptions. Returns the number delivered. Never raises."""
    if not targets or not push_configured():
        return 0
    semaphore = asyncio.Semaphore(_PUSH_CONCURRENCY)

    async def _one(target: PushTarget) -> tuple[PushTarget, str]:
        async with semaphore:
            return target, await asyncio.to_thread(_send_blocking, target)

    try:
        results = await asyncio.gather(*(_one(t) for t in targets))
    except Exception:
        logger.exception("Web Push batch failed")
        return 0

    ok_ids = [t.subscription_id for t, r in results if r == "ok"]
    gone_ids = [t.subscription_id for t, r in results if r == "gone"]
    err_ids = [t.subscription_id for t, r in results if r == "error"]
    try:
        async with session_factory() as session:
            if ok_ids:
                await session.execute(
                    update(PushSubscription)
                    .where(PushSubscription.id.in_(ok_ids))
                    .values(failure_count=0, last_success_at=datetime.now(timezone.utc).replace(tzinfo=None))
                )
            if err_ids:
                await session.execute(
                    update(PushSubscription)
                    .where(PushSubscription.id.in_(err_ids))
                    .values(failure_count=PushSubscription.failure_count + 1)
                )
                await session.execute(
                    delete(PushSubscription).where(
                        PushSubscription.id.in_(err_ids), PushSubscription.failure_count >= MAX_FAILURES
                    )
                )
            if gone_ids:
                await session.execute(delete(PushSubscription).where(PushSubscription.id.in_(gone_ids)))
            await session.commit()
    except Exception:
        logger.exception("Failed to record Web Push results")
    return len(ok_ids)


async def team_subscriptions(
    session_factory: Any, team_numbers: list[str] | tuple[str, ...], rooms: list[str] | None = None
) -> list[tuple[PushSubscription, list[str]]]:
    """Team-kind subscriptions subscribed to any of `team_numbers` (optionally
    restricted to subscriptions made from the given rooms), each paired with
    the subset of `team_numbers` it follows."""
    if not team_numbers:
        return []
    async with session_factory() as session:
        stmt = (
            select(PushSubscription, PushSubscriptionTeam.team_number)
            .join(PushSubscriptionTeam, PushSubscriptionTeam.subscription_id == PushSubscription.id)
            .where(PushSubscription.kind == KIND_TEAM, PushSubscriptionTeam.team_number.in_(list(team_numbers)))
        )
        if rooms is not None:
            stmt = stmt.where(PushSubscription.room_slug.in_(rooms))
        rows = (await session.execute(stmt)).all()
    grouped: dict[uuid.UUID, tuple[PushSubscription, list[str]]] = {}
    for sub, team in rows:
        grouped.setdefault(sub.id, (sub, []))[1].append(team)
    return [(sub, sorted(teams)) for sub, teams in grouped.values()]


async def all_team_subscriptions(session_factory: Any, rooms: list[str] | None = None) -> list[PushSubscription]:
    async with session_factory() as session:
        stmt = select(PushSubscription).where(PushSubscription.kind == KIND_TEAM)
        if rooms is not None:
            stmt = stmt.where(PushSubscription.room_slug.in_(rooms))
        return list((await session.execute(stmt)).scalars().all())


async def staff_subscriptions(session_factory: Any, category: str) -> list[PushSubscription]:
    async with session_factory() as session:
        result = await session.execute(select(PushSubscription).where(PushSubscription.kind == KIND_STAFF))
        subs = list(result.scalars().all())
    # `categories` NULL = opted into everything.
    return [s for s in subs if s.categories is None or category in s.categories]


def _target(sub: PushSubscription, payload: dict[str, Any]) -> PushTarget:
    return PushTarget(sub.id, sub.endpoint, sub.p256dh, sub.auth, payload)


def team_payload(sub: PushSubscription, title: str, body: str, *, tag: str, kind: str) -> dict[str, Any]:
    """Push payload for a team-facing subscription; the click target is the
    room display the subscriber signed up from (if any)."""
    url = f"/room/{sub.room_slug}" if sub.room_slug else "/"
    return {"title": title, "body": body, "tag": tag, "url": url, "kind": kind}


async def push_to_staff(session_factory: Any, title: str, message: str, category: str = CATEGORY_SYSTEM) -> int:
    subs = await staff_subscriptions(session_factory, category)
    payload = {"title": title, "body": message, "tag": f"staff-{category}", "url": "/", "kind": "staff"}
    return await send_push(session_factory, [_target(s, payload) for s in subs])


async def broadcast(
    redis: Any,
    session_factory: Any,
    title: str,
    message: str,
    *,
    rooms: list[str] | None = None,
    push: bool = True,
    modal: bool = True,
    duration: int = 60,
) -> dict[str, int]:
    """Staff broadcast. `rooms=None` means every room (and every team
    subscriber); otherwise only the named rooms and the subscribers who
    signed up from them. Staff subscribed to the `broadcast` category get it
    either way. Returns delivery counts."""
    target_rooms = rooms if rooms is not None else await all_room_slugs(session_factory)
    await publish_room_notice(
        redis, target_rooms, title, message, modal=modal, duration=duration, source="broadcast"
    )
    pushed = 0
    if push and push_configured():
        team_subs = await all_team_subscriptions(session_factory, rooms)
        pushed += await send_push(
            session_factory,
            [_target(s, team_payload(s, title, message, tag="broadcast", kind="broadcast")) for s in team_subs],
        )
        pushed += await push_to_staff(session_factory, title, message, CATEGORY_BROADCAST)
    return {"rooms": len(target_rooms), "pushed": pushed}
