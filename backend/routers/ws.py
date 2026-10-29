"""WebSocket routes (plan §11 "WebSocket Channels" / Appendix C.2 `ws.py`).

Owns `ConnectionManager` (frozen public API, plan §C.2) and the three
WebSocket endpoints: `/ws/events`, `/ws/prompter/{entity_id}`,
`/ws/overlay/{entity_id}`.

Per §C.2: may import `backend/core/` (Redis pub/sub), `backend/loader`
(entity_id / field_set_id resolution), `backend/models/` (validating the
prompter token, resolving bound timer/overlay instances). Must NOT import
`modules/automation/`, `modules/timer/`, `modules/scraper/`, or integration
modules — the running `TimerManager` instance is reached only via
`request.app.state.timer_manager` (set by `main.py`, same pattern
`routers/timers.py` already uses), treated here as a duck-typed `Any` so
this module never imports `backend.modules.timer.manager`.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import defaultdict
from typing import Any, Callable

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from backend import loader
from backend.core.db import async_session_factory
from backend.core.dependencies import ALL_PERMISSIONS
from backend.core.redis import redis_client
from backend.core.security import validate_prompter_token
from backend.core.sessions import SESSION_COOKIE_NAME, get_session, unsign_session_id
from backend.models.overlay import OverlayInstance
from backend.models.room import Room
from backend.models.timer import PrompterCue, TimerInstance

logger = logging.getLogger(__name__)

router = APIRouter(tags=["websocket"])

EVENTS_CHANNEL = "qecomp:events"
TIMER_EVENT_TYPES = {"timer_started", "timer_milestone", "timer_finished", "timer_stopped"}

#: Bookkeeping bucket key for `/ws/events` connections (not entity-scoped,
#: but `ConnectionManager.connect`/`disconnect`'s frozen signature always
#: takes an entity_id — this sentinel keeps every connection symmetric).
EVENTS_BUCKET = "__events__"
#: Bookkeeping bucket prefix for the public `/ws/room/{slug}` connections.
ROOM_BUCKET_PREFIX = "room:"

# WebSocket close codes (RFC 6455 range 4000-4999 is free for application use).
WS_CLOSE_POLICY_VIOLATION = 4003  # invalid/missing prompter token
WS_CLOSE_SERVICE_UNAVAILABLE = 4013  # TimerManager not running on this node (not leader)


class ConnectionManager:
    """Frozen public API (plan §C.2):

        async def connect(self, entity_id: str, ws: WebSocket) -> None
        async def disconnect(self, entity_id: str, ws: WebSocket) -> None
        async def broadcast(self, entity_id: str, message: dict) -> None
        async def broadcast_all(self, message: dict) -> None

    Also owns the single background task that subscribes to
    `qecomp:events` and fans messages out to the right connected clients —
    every WebSocket route registered below shares one Redis subscription
    rather than each opening its own.
    """

    def __init__(self, redis: Any = None) -> None:
        self._redis = redis if redis is not None else redis_client
        self._by_entity: dict[str, set[WebSocket]] = defaultdict(set)
        self._all: set[WebSocket] = set()
        self._pubsub_task: asyncio.Task | None = None
        self._shutdown = asyncio.Event()

        # Set by `main.py` (mirroring how `routers/timers.py` reaches the
        # leader-only `TimerManager` via `app.state.timer_manager`) so this
        # module never imports `backend.modules.timer.manager` directly.
        self.get_timer_manager: Callable[[], Any] | None = None
        # Same pattern for the (optional) Predictor singleton — used only to
        # read the sanitized boolean flag via `get_flag()`, never the raw
        # `match_prediction` event body (see predictor.py's module docstring
        # on predicted-score leakage).
        self.get_predictor: Callable[[], Any] | None = None

    # ── Frozen public API ────────────────────────────────────────────

    async def connect(self, entity_id: str, ws: WebSocket) -> None:
        await ws.accept()
        self._by_entity[entity_id].add(ws)
        self._all.add(ws)

    async def disconnect(self, entity_id: str, ws: WebSocket) -> None:
        bucket = self._by_entity.get(entity_id)
        if bucket is not None:
            bucket.discard(ws)
            if not bucket:
                self._by_entity.pop(entity_id, None)
        self._all.discard(ws)

    async def broadcast(self, entity_id: str, message: dict) -> None:
        dead: list[WebSocket] = []
        for ws in list(self._by_entity.get(entity_id, ())):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.disconnect(entity_id, ws)

    async def broadcast_all(self, message: dict) -> None:
        dead: list[WebSocket] = []
        for ws in list(self._all):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._all.discard(ws)
            for bucket in self._by_entity.values():
                bucket.discard(ws)

    # ── Lifecycle (started/stopped from main.py's lifespan) ──────────

    async def start(self) -> None:
        self._shutdown.clear()
        self._pubsub_task = asyncio.create_task(self._listen())

    async def stop(self) -> None:
        self._shutdown.set()
        if self._pubsub_task is not None:
            self._pubsub_task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await self._pubsub_task
            self._pubsub_task = None

    # ── Internal: Redis pub/sub fan-out ───────────────────────────────

    async def _listen(self) -> None:
        pubsub = self._redis.pubsub()
        try:
            await pubsub.subscribe(EVENTS_CHANNEL)
            while not self._shutdown.is_set():
                try:
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Error reading from %s pub/sub", EVENTS_CHANNEL)
                    await asyncio.sleep(1)
                    continue
                if message is None:
                    continue
                try:
                    data = json.loads(message["data"])
                except (KeyError, TypeError, ValueError):
                    continue
                try:
                    await self._dispatch(data)
                except Exception:
                    logger.exception("Error dispatching event to WebSocket clients: %r", data)
        finally:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe(EVENTS_CHANNEL)
                await pubsub.aclose()

    async def _dispatch(self, data: dict[str, Any]) -> None:
        # /ws/events: every event, broadcast-all to any browser-facing
        # client. Per plan §5.12, `match_prediction` events' raw
        # `predicted_red`/`predicted_blue` payload fields must NEVER reach a
        # browser-facing channel (only the sanitized `high_potential` bool
        # may) — this is the single place `/ws/events` fans events out, so
        # sanitizing here (rather than at the publisher) is the one spot
        # that can't be bypassed by a future publisher forgetting to redact.
        await self.broadcast_all(self._sanitize_for_events(data))

        event_type = data.get("type")
        entity_id = data.get("entity_id")

        if event_type in TIMER_EVENT_TYPES and entity_id:
            await self._dispatch_timer_state(entity_id, event_type, data)
        elif event_type == "fieldMatchAssigned":
            await self._dispatch_upcoming_match(data)
        elif event_type == "room_notice":
            await self._dispatch_room_notice(data)

    async def _dispatch_room_notice(self, data: dict[str, Any]) -> None:
        """Forwards a `room_notice` (published by `modules/notifications`) to
        the public `/ws/room/<slug>` clients of each room it names. Only the
        display-safe fields go out, never the whole bus message."""
        payload = data.get("payload") or {}
        notice = {
            "type": "notice",
            "id": payload.get("id"),
            "title": payload.get("title", ""),
            "message": payload.get("message", ""),
            "modal": bool(payload.get("modal")),
            "duration": payload.get("duration", 30),
            "timestamp": data.get("timestamp", time.time()),
        }
        for slug in payload.get("rooms") or []:
            await self.broadcast(f"{ROOM_BUCKET_PREFIX}{slug}", notice)

    @staticmethod
    def _sanitize_for_events(data: dict[str, Any]) -> dict[str, Any]:
        """Strips `predicted_red`/`predicted_blue` from a `match_prediction`
        event's payload before it goes out over `/ws/events` (plan §5.12).
        `high_potential`/`matchNum`/`divisionId` (and anything else in the
        payload) pass through untouched; every other event type is returned
        as-is. Returns a shallow copy — never mutates the caller's `data`,
        which other `_dispatch` branches (e.g. `_dispatch_upcoming_match`)
        still need in its original form."""
        if data.get("type") != "match_prediction":
            return data
        payload = data.get("payload")
        if not isinstance(payload, dict):
            return data
        sanitized_payload = {k: v for k, v in payload.items() if k not in ("predicted_red", "predicted_blue")}
        return {**data, "payload": sanitized_payload}

    async def _dispatch_timer_state(self, entity_id: str, event_type: str, data: dict[str, Any]) -> None:
        """Translates a raw `timer_*` event (whose payload varies by type,
        see `TimerManager._publish`) into the full state snapshot
        `prompter.html` expects (`{phase, running, elapsed, remaining, ...}`)
        by re-reading `TimerManager.get_state()` on the leader node."""
        if self.get_timer_manager is None:
            return
        manager = self.get_timer_manager()
        if manager is None:
            return
        state = manager.get_state(entity_id)
        if not state:
            return
        await self.broadcast(
            entity_id,
            {
                "entity_id": entity_id,
                "entity_tags": data.get("entity_tags", []),
                "type": event_type,
                "timestamp": time.time(),
                "payload": state,
            },
        )

    async def _dispatch_upcoming_match(self, data: dict[str, Any]) -> None:
        """On `fieldMatchAssigned`, push a sanitized `upcoming_match` message
        (plan §C.4's `redTeams`/`blueTeams`/`matchNum`/`round`, plus a
        `high_potential` bool — NEVER `predicted_red`/`predicted_blue`, per
        `predictor.py`'s module docstring) to every Timer/Overlay instance
        bound to the firing field.
        """
        payload = data.get("payload", {}) or {}
        field_id = payload.get("fieldID")
        if field_id is None:
            return

        field_set_id = self._resolve_field_set_id(data.get("entity_id"))
        high_potential = self._resolve_high_potential(payload)

        upcoming_match = {
            "matchNum": payload.get("matchNum"),
            "round": payload.get("round"),
            "redTeams": payload.get("redTeams", []),
            "blueTeams": payload.get("blueTeams", []),
            "high_potential": high_potential,
        }

        async with async_session_factory() as session:
            timer_result = await session.execute(select(TimerInstance).where(TimerInstance.field_id == field_id))
            timer_rows = list(timer_result.scalars().all())
            overlay_result = await session.execute(select(OverlayInstance))
            overlay_rows = list(overlay_result.scalars().all())

        for row in timer_rows:
            if field_set_id is not None and row.field_set_id != field_set_id:
                continue
            await self.broadcast(
                row.entity_id,
                {
                    "entity_id": row.entity_id,
                    "entity_tags": list(row.tags or []),
                    "type": "upcoming_match",
                    "timestamp": time.time(),
                    "payload": upcoming_match,
                },
            )

        if field_set_id is not None:
            for row in overlay_rows:
                if row.field_set_id != field_set_id:
                    continue
                await self.broadcast(
                    row.entity_id,
                    {
                        "entity_id": row.entity_id,
                        "entity_tags": list(row.tags or []),
                        "type": "upcoming_match",
                        "timestamp": time.time(),
                        "payload": upcoming_match,
                    },
                )

    def _resolve_field_set_id(self, tm_entity_id: str | None) -> int | None:
        """Best-effort lookup of the firing `vex_tm` instance's
        `field_set_id` via the Loader's public API (plan §B.5) — same
        degradation pattern as `TimerManager._resolve_field_set_id`: falls
        back to matching on `field_id` alone if unavailable."""
        if not tm_entity_id:
            return None
        try:
            instance = loader.get_instance(tm_entity_id)
        except Exception:
            return None
        if instance is None:
            return None
        config = getattr(instance, "config", {}) or {}
        return config.get("field_set_id")

    def _resolve_high_potential(self, payload: dict[str, Any]) -> bool:
        """Reads only the sanitized boolean flag from the (optional)
        Predictor singleton — never the raw `match_prediction` event body."""
        if self.get_predictor is None:
            return False
        predictor = self.get_predictor()
        if predictor is None:
            return False
        match_num = payload.get("matchNum")
        division_id = payload.get("divisionId")
        if match_num is None or division_id is None:
            return False
        try:
            from backend.modules.predictor.predictor import match_id_for

            flag = predictor.get_flag(match_id_for(division_id, match_num))
        except Exception:
            return False
        return bool(flag)


manager = ConnectionManager()


# ── /ws/events ────────────────────────────────────────────────────────


async def _ws_is_authenticated(websocket: WebSocket) -> bool:
    """Resolves whether `/ws/events` carries a valid session cookie,
    mirroring `_ws_has_prompter_control`'s cookie-branch of
    `dependencies.get_current_principal` (no API-key branch — a browser
    `WebSocket` cannot set an `Authorization` header on the handshake, so a
    session cookie is the only credential a WS client can ever present).

    `/ws/events` fans out the raw event bus to several pages gated behind
    different REST permissions (Dashboard, Field Monitor, Match Control,
    Timers, and the `settings:edit`-only Live Event Bus debug view), so
    this only requires *some* valid, non-expired session rather than one
    specific permission — matching every other `require_permission(...)`
    check in this codebase, which 401s on "no session" before ever
    evaluating which permission is missing. Page-level permission gating
    (e.g. `DebugEventBus.tsx` requiring `settings:edit`) stays the
    frontend's responsibility, same as which nav links are shown.
    """
    cookie_value = websocket.cookies.get(SESSION_COOKIE_NAME)
    if not cookie_value:
        return False
    session_id = unsign_session_id(cookie_value)
    if session_id is None:
        return False
    data = await get_session(redis_client, session_id)
    return data is not None


@router.websocket("/ws/events")
async def ws_events(websocket: WebSocket) -> None:
    if not await _ws_is_authenticated(websocket):
        await websocket.close(code=WS_CLOSE_POLICY_VIOLATION, reason="Authentication required")
        return
    await manager.connect(EVENTS_BUCKET, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(EVENTS_BUCKET, websocket)


# ── /ws/room/{slug} ───────────────────────────────────────────────────

#: Room slugs are admin-chosen `[a-z0-9-]`; anything else can't be a room.
_ROOM_SLUG_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789-")


@router.websocket("/ws/room/{slug}")
async def ws_room(websocket: WebSocket, slug: str) -> None:
    """Public (no auth) feed for a room display page: it only ever receives
    that room's `notice` messages, and sends nothing meaningful. The page is
    shown on a screen in the room, so the room slug is the only "credential"
    (like the overlay URL's entity_id); notices carry no private data."""
    if not slug or len(slug) > 50 or not set(slug) <= _ROOM_SLUG_CHARS:
        await websocket.close(code=WS_CLOSE_POLICY_VIOLATION, reason="Unknown room")
        return
    async with async_session_factory() as session:
        room = await session.get(Room, slug)
    if room is None:
        await websocket.close(code=WS_CLOSE_POLICY_VIOLATION, reason="Unknown room")
        return
    bucket = f"{ROOM_BUCKET_PREFIX}{slug}"
    await manager.connect(bucket, websocket)
    try:
        while True:
            await websocket.receive_text()  # keepalive pings from the page
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(bucket, websocket)


# ── /ws/prompter/{entity_id} ──────────────────────────────────────────


async def _cues_snapshot(entity_id: str) -> dict[str, Any]:
    """Active/next cue for the prompter's "cues" message. `PrompterCue` has
    no explicit "currently active pointer" column (plan §8) — this uses the
    simplest reasonable interpretation available from the existing schema:
    the first two `is_active` cues in `sort_order`. A richer "advance to
    next cue" pointer is a UI/product decision left to Wave 4's Timers page,
    which can push an explicit active cue id instead once designed.
    """
    async with async_session_factory() as session:
        result = await session.execute(
            select(PrompterCue)
            .where(PrompterCue.timer_entity_id == entity_id, PrompterCue.is_active.is_(True))
            .order_by(PrompterCue.sort_order, PrompterCue.created_at)
        )
        rows = list(result.scalars().all())
    active = rows[0] if rows else None
    nxt = rows[1] if len(rows) > 1 else None
    return {
        "active": {"id": str(active.id), "content": active.content, "type": active.type} if active else None,
        "next": {"id": str(nxt.id), "content": nxt.content, "type": nxt.type} if nxt else None,
    }


async def _ws_has_prompter_control(websocket: WebSocket) -> bool:
    """Resolves `prompter:control` for a `/ws/prompter` connection from its
    session cookie, mirroring `dependencies.get_current_principal`'s
    cookie-branch (there's no API-key branch here — a bare EMCEE iPad
    reaching this WS only ever carries a browser session cookie, never a
    `Bearer` header). The teleprompter *token* in the query string (per
    plan §5.10) only proves "this client was handed a link to this specific
    timer" — it's obscurity-based and never expires until manually rotated,
    so per §13 it must not, by itself, authorize `start_countdown`; a real
    `prompter:control` session is required on top of it.

    Deliberate tradeoff: if the connection carries no valid session at all
    (e.g. the iPad was opened straight from the token link, no login), this
    returns `False` and `start_countdown` is rejected with an explicit WS
    error rather than silently ignored — there is no established
    "viewer with valid token but no session" bypass anywhere else in this
    codebase (every other privileged action goes through
    `require_permission`), so none is invented here either.
    """
    cookie_value = websocket.cookies.get(SESSION_COOKIE_NAME)
    if not cookie_value:
        return False
    session_id = unsign_session_id(cookie_value)
    if session_id is None:
        return False
    data = await get_session(redis_client, session_id)
    if data is None:
        return False
    if data.get("is_admin_local"):
        return True
    permissions = set(data.get("permissions") or [])
    return ALL_PERMISSIONS in permissions or "prompter:control" in permissions


@router.websocket("/ws/prompter/{entity_id}")
async def ws_prompter(websocket: WebSocket, entity_id: str) -> None:
    token = websocket.query_params.get("token", "")

    async with async_session_factory() as session:
        result = await session.execute(select(TimerInstance).where(TimerInstance.entity_id == entity_id))
        row = result.scalar_one_or_none()

    if row is None or not validate_prompter_token(entity_id, row.token_nonce, token):
        await websocket.close(code=WS_CLOSE_POLICY_VIOLATION, reason="Invalid or expired teleprompter token")
        return

    timer_manager = manager.get_timer_manager() if manager.get_timer_manager else None
    if timer_manager is None:
        # Mirrors routers/timers.py's 503 ("TimerManager is not running on
        # this node / not the current leader") — there's no meaningful
        # WebSocket-level equivalent of a 503 response, so the connection is
        # rejected outright; the client's reconnect-with-backoff (Appendix
        # A.14, already built into prompter.html) will keep retrying until a
        # request lands on the leader node.
        await websocket.close(code=WS_CLOSE_SERVICE_UNAVAILABLE, reason="Not the current leader")
        return

    await manager.connect(entity_id, websocket)
    try:
        await websocket.send_json(
            {
                "entity_id": entity_id,
                "entity_tags": list(row.tags or []),
                "type": "timer_state",
                "timestamp": time.time(),
                "payload": timer_manager.get_state(entity_id),
            }
        )
        await websocket.send_json(
            {
                "entity_id": entity_id,
                "entity_tags": list(row.tags or []),
                "type": "cues",
                "timestamp": time.time(),
                "payload": await _cues_snapshot(entity_id),
            }
        )

        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("type") == "start_countdown":
                # Per plan §5.10/§13: only a caller holding `prompter:control`
                # (EMCEE) may trigger the countdown — possession of the
                # HMAC teleprompter token alone (already validated above,
                # at connect time) is not sufficient, since that token never
                # expires until manually rotated. See
                # `_ws_has_prompter_control`'s docstring for the "no session
                # at all" tradeoff.
                if not await _ws_has_prompter_control(websocket):
                    logger.warning(
                        "Denied start_countdown on %s: caller lacks prompter:control (or no valid session)",
                        entity_id,
                    )
                    await websocket.send_json(
                        {
                            "entity_id": entity_id,
                            "entity_tags": list(row.tags or []),
                            "type": "error",
                            "timestamp": time.time(),
                            "payload": {
                                "error": "forbidden",
                                "detail": "Missing required permission: 'prompter:control'",
                            },
                        }
                    )
                    continue
                current_manager = manager.get_timer_manager() if manager.get_timer_manager else None
                if current_manager is None:
                    await websocket.send_json(
                        {
                            "entity_id": entity_id,
                            "entity_tags": list(row.tags or []),
                            "type": "error",
                            "timestamp": time.time(),
                            "payload": {"error": "not_leader", "detail": "TimerManager is not running on this node"},
                        }
                    )
                    continue
                try:
                    await current_manager.start_countdown(entity_id)
                except ValueError as exc:
                    await websocket.send_json(
                        {
                            "entity_id": entity_id,
                            "entity_tags": list(row.tags or []),
                            "type": "error",
                            "timestamp": time.time(),
                            "payload": {"error": "invalid_request", "detail": str(exc)},
                        }
                    )
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(entity_id, websocket)


# ── /ws/overlay/{entity_id} ────────────────────────────────────────────


@router.websocket("/ws/overlay/{entity_id}")
async def ws_overlay(websocket: WebSocket, entity_id: str) -> None:
    """No auth (plan Appendix A.1: "security by obscurity of the
    entity_id"). Streams `upcoming_match`-shaped messages for the entity's
    bound field set — see `ConnectionManager._dispatch_upcoming_match`."""
    await manager.connect(entity_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(entity_id, websocket)
