"""Notifications API (issue #5).

Public (no login — used from phones that scanned a room's QR code):
  - `GET  /api/v1/public/rooms/{slug}`          room name, stream, teams
  - `GET  /api/v1/public/push/vapid-key`        key for `pushManager.subscribe`
  - `POST /api/v1/public/push/subscribe`        follow teams (from a room)
  - `POST /api/v1/public/push/status|unsubscribe`

The push-service endpoint URL is the only credential a subscriber holds, so
it is never echoed back and only the holder can look up / remove it. Public
writes are rate-limited per client IP.

Staff (OIDC permissions `notifications:*`):
  - staff push subscribe/unsubscribe, broadcasts, test push, stats.
"""
from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.audit import log_action
from backend.core.db import async_session_factory
from backend.core.dependencies import CurrentPrincipal, get_db, get_redis, require_permission
from backend.core.settings import settings
from backend.models.notification import KIND_STAFF, KIND_TEAM, PushSubscription, PushSubscriptionTeam
from backend.models.room import Room, RoomTeam
from backend.modules.notifications import delivery
from backend.schemas.notification import (
    BroadcastRequest,
    BroadcastResult,
    EndpointRef,
    NotificationStats,
    StaffSubscribe,
    SubscriptionStatus,
    TeamSubscribe,
    VapidKey,
)
from backend.schemas.room import PublicRoom

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["notifications"])

#: Public write endpoints: this many calls per window per client IP.
_RATE_LIMIT = 30
_RATE_WINDOW_SECONDS = 600


async def _rate_limit(request: Request, redis: Any, scope: str) -> None:
    """Fixed-window per-IP limiter on Redis. Fails open if Redis is down
    (Appendix A.10 graceful degradation)."""
    # Behind the ingress proxy the *last* X-Forwarded-For entry is the one the
    # proxy itself appended (earlier ones are client-supplied and spoofable).
    forwarded = request.headers.get("x-forwarded-for", "")
    ip = forwarded.split(",")[-1].strip() or (request.client.host if request.client else "unknown")
    key = f"qecomp:ratelimit:{scope}:{ip}"
    try:
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, _RATE_WINDOW_SECONDS)
    except Exception:
        return
    if count > _RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Too many requests; try again later")


# ── Public ───────────────────────────────────────────────────────────


async def _load_public_room(db: AsyncSession, slug: str) -> PublicRoom:
    room = await db.get(Room, slug)
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found")
    teams = (
        await db.execute(select(RoomTeam.team_number).where(RoomTeam.room_slug == slug).order_by(RoomTeam.team_number))
    ).scalars()
    return PublicRoom(slug=room.slug, name=room.name, stream_url=room.stream_url, teams=list(teams))


@router.get("/public/rooms/{slug}", response_model=PublicRoom)
async def public_room(slug: str, db: Annotated[AsyncSession, Depends(get_db)]) -> PublicRoom:
    return await _load_public_room(db, slug)


@router.get("/public/push/vapid-key", response_model=VapidKey)
async def vapid_key() -> VapidKey:
    if not delivery.push_configured():
        raise HTTPException(status_code=503, detail="Push notifications are not configured on this server")
    return VapidKey(public_key=settings.VAPID_PUBLIC_KEY)


async def _upsert_subscription(
    db: AsyncSession, body: Any, *, kind: str, user_agent: str | None
) -> PushSubscription:
    result = await db.execute(select(PushSubscription).where(PushSubscription.endpoint == body.endpoint))
    sub = result.scalar_one_or_none()
    if sub is None:
        sub = PushSubscription(endpoint=body.endpoint, p256dh=body.keys.p256dh, auth=body.keys.auth, kind=kind)
        db.add(sub)
    sub.p256dh, sub.auth, sub.kind = body.keys.p256dh, body.keys.auth, kind
    sub.user_agent = (user_agent or "")[:255] or None
    sub.failure_count = 0
    await db.flush()
    await db.execute(delete(PushSubscriptionTeam).where(PushSubscriptionTeam.subscription_id == sub.id))
    return sub


@router.post("/public/push/subscribe", response_model=SubscriptionStatus)
async def subscribe_team(
    body: TeamSubscribe,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Any, Depends(get_redis)],
) -> SubscriptionStatus:
    await _rate_limit(request, redis, "push-subscribe")
    if not delivery.push_configured():
        raise HTTPException(status_code=503, detail="Push notifications are not configured on this server")
    room = await _load_public_room(db, body.room)
    allowed = set(room.teams)
    unknown = [t for t in body.teams if t not in allowed]
    if unknown:
        raise HTTPException(status_code=422, detail=f"Teams not in this room: {', '.join(unknown)}")

    sub = await _upsert_subscription(db, body, kind=KIND_TEAM, user_agent=request.headers.get("user-agent"))
    sub.room_slug, sub.user_subject, sub.categories = body.room, None, None
    db.add_all(PushSubscriptionTeam(subscription_id=sub.id, team_number=t) for t in body.teams)
    await db.commit()
    return SubscriptionStatus(subscribed=True, room=body.room, teams=body.teams)


@router.post("/public/push/status", response_model=SubscriptionStatus)
async def subscription_status(
    body: EndpointRef,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Any, Depends(get_redis)],
) -> SubscriptionStatus:
    await _rate_limit(request, redis, "push-status")
    sub = (
        await db.execute(
            select(PushSubscription).where(PushSubscription.endpoint == body.endpoint, PushSubscription.kind == KIND_TEAM)
        )
    ).scalar_one_or_none()
    if sub is None:
        return SubscriptionStatus(subscribed=False)
    teams = (
        await db.execute(
            select(PushSubscriptionTeam.team_number)
            .where(PushSubscriptionTeam.subscription_id == sub.id)
            .order_by(PushSubscriptionTeam.team_number)
        )
    ).scalars()
    return SubscriptionStatus(subscribed=True, room=sub.room_slug, teams=list(teams))


@router.post("/public/push/unsubscribe", status_code=204, response_model=None)
async def unsubscribe(
    body: EndpointRef,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Any, Depends(get_redis)],
) -> None:
    await _rate_limit(request, redis, "push-unsubscribe")
    await db.execute(delete(PushSubscription).where(PushSubscription.endpoint == body.endpoint))
    await db.commit()


# ── Staff ────────────────────────────────────────────────────────────


@router.post("/notifications/staff/subscribe", status_code=204, response_model=None)
async def staff_subscribe(
    body: StaffSubscribe,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("notifications:receive"))],
) -> None:
    if not delivery.push_configured():
        raise HTTPException(status_code=503, detail="Push notifications are not configured on this server")
    categories = body.categories
    if categories is not None:
        bad = [c for c in categories if c not in delivery.STAFF_CATEGORIES]
        if bad:
            raise HTTPException(status_code=422, detail=f"Unknown categories: {', '.join(bad)}")
    sub = await _upsert_subscription(db, body, kind=KIND_STAFF, user_agent=request.headers.get("user-agent"))
    sub.room_slug, sub.user_subject, sub.categories = None, principal.subject[:100], categories
    await db.commit()


@router.post("/notifications/staff/unsubscribe", status_code=204, response_model=None)
async def staff_unsubscribe(
    body: EndpointRef,
    db: Annotated[AsyncSession, Depends(get_db)],
    _principal: Annotated[CurrentPrincipal, Depends(require_permission("notifications:receive"))],
) -> None:
    await db.execute(
        delete(PushSubscription).where(PushSubscription.endpoint == body.endpoint, PushSubscription.kind == KIND_STAFF)
    )
    await db.commit()


@router.post("/notifications/broadcast", response_model=BroadcastResult)
async def send_broadcast(
    body: BroadcastRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Any, Depends(get_redis)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("notifications:broadcast"))],
) -> BroadcastResult:
    if body.rooms is not None:
        existing = set((await db.execute(select(Room.slug).where(Room.slug.in_(body.rooms)))).scalars())
        missing = [r for r in body.rooms if r not in existing]
        if missing:
            raise HTTPException(status_code=422, detail=f"Unknown rooms: {', '.join(missing)}")
    result = await delivery.broadcast(
        redis, async_session_factory, body.title, body.message, rooms=body.rooms, push=body.push, modal=body.modal
    )
    await log_action(
        db,
        principal.subject,
        "broadcast",
        "notification",
        ",".join(body.rooms) if body.rooms is not None else "*",
        changes={"title": body.title, "message": body.message, "push": body.push},
    )
    return BroadcastResult(**result)


@router.post("/notifications/test", response_model=BroadcastResult)
async def send_test(
    _principal: Annotated[CurrentPrincipal, Depends(require_permission("notifications:manage"))],
) -> BroadcastResult:
    """Pushes a test message to every staff subscription (all categories)."""
    if not delivery.push_configured():
        raise HTTPException(status_code=503, detail="Push notifications are not configured on this server")
    pushed = await delivery.push_to_staff(
        async_session_factory, "Test notification", "TMTools push notifications are working.", delivery.CATEGORY_SYSTEM
    )
    return BroadcastResult(rooms=0, pushed=pushed)


@router.get(
    "/notifications/stats",
    response_model=NotificationStats,
    dependencies=[Depends(require_permission("notifications:manage"))],
)
async def notification_stats(db: Annotated[AsyncSession, Depends(get_db)]) -> NotificationStats:
    by_kind = dict(
        (await db.execute(select(PushSubscription.kind, func.count()).group_by(PushSubscription.kind))).all()
    )
    by_room = {
        slug: count
        for slug, count in (
            await db.execute(
                select(PushSubscription.room_slug, func.count())
                .where(PushSubscription.kind == KIND_TEAM, PushSubscription.room_slug.is_not(None))
                .group_by(PushSubscription.room_slug)
            )
        ).all()
    }
    return NotificationStats(
        push_configured=delivery.push_configured(),
        team_subscriptions=by_kind.get(KIND_TEAM, 0),
        staff_subscriptions=by_kind.get(KIND_STAFF, 0),
        subscriptions_by_room=by_room,
    )
