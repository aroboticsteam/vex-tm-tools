"""Desk <-> iPad comms API (issue #8).

Staff (`comms:*`): manage stations (an iPad with a role), canned messages and
the agendas; send messages / push a screen to stations; read what stations
typed back.

Public (a station's slug is its only credential, like rooms): the iPad page
reads its config, next match and agendas, and can message the desk.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.audit import log_action
from backend.core.db import async_session_factory
from backend.core.dependencies import CurrentPrincipal, get_db, get_redis, require_permission
from backend.models.comms import ROLE_EMCEE, AgendaItem, CommsPreset, Station, StationMessage
from backend.models.team import TeamProfile
from backend.modules.comms import access, presence
from backend.modules.comms import timer as ctimer
from backend.modules.comms.next_match import find_next_match
from backend.modules.notifications import delivery
from backend.modules.notifications.logic import parse_schedule
from backend.routers.notifications import _rate_limit
from backend.routers.team_clips import _overlay_team
from backend.schemas.comms import (
    AgendaBase,
    AgendaImport,
    AgendaImportError,
    AgendaImportResult,
    AgendaRead,
    AgendaReorder,
    InboundMessage,
    LinkResult,
    NextMatch,
    PinRequest,
    PresetBase,
    PresetRead,
    PublicStation,
    ScreenRequest,
    SendRequest,
    SendResult,
    StationCreate,
    StationMessageRead,
    StationRead,
    StationUpdate,
    TimerRequest,
    TimerState,
    UnlockRequest,
    UnlockResult,
)
from backend.schemas.events import EventBusMessage

logger = logging.getLogger(__name__)

router = APIRouter(tags=["comms"])

SCHEDULE_KEY_PATTERN = "qecomp:tm:*:schedule"
SCREEN_KEY = "qecomp:station:{slug}:screen"
LAST_KEY = "qecomp:station:{slug}:last"
TIMER_KEY = "qecomp:station:{slug}:timer"
PIN_FAIL_KEY = "qecomp:station:{slug}:pinfail"
MAX_PIN_FAILURES = 10
PIN_LOCK_SECONDS = 600
DEFAULT_SCREEN = "messages"
MESSAGE_EVENT = "station_message"
SCREEN_EVENT = "station_screen"
TIMER_EVENT = "station_timer"
INBOUND_EVENT = "station_inbound"
COMMS_ENTITY_ID = "comms"
MAX_UNACKED_PER_STATION = 50


def screens_for(role: str) -> list[str]:
    base = ["messages", "timer", "agenda_day", "agenda_finals"]
    return ["messages", "countdown", "timer", "agenda_day", "agenda_finals"] if role == ROLE_EMCEE else base


async def _publish(redis: Any, event_type: str, payload: dict[str, Any]) -> None:
    event = EventBusMessage(
        entity_id=COMMS_ENTITY_ID, entity_tags=[], type=event_type, timestamp=time.time(), payload=payload
    )
    try:
        await redis.publish(delivery.EVENTS_CHANNEL, event.model_dump_json())
    except Exception:
        logger.exception("Failed to publish %s", event_type)


async def _resolve_targets(db: AsyncSession, stations: list[str] | None, roles: list[str] | None) -> list[str]:
    stmt = select(Station.slug)
    if stations or roles:
        clauses = []
        if stations:
            clauses.append(Station.slug.in_(stations))
        if roles:
            clauses.append(Station.role.in_(roles))
        from sqlalchemy import or_

        stmt = stmt.where(or_(*clauses))
    found = sorted((await db.execute(stmt)).scalars().all())
    if stations:
        missing = [s for s in stations if s not in set(found) and not roles]
        if missing:
            raise HTTPException(status_code=422, detail=f"Unknown stations: {', '.join(missing)}")
    return found


# ── stations ─────────────────────────────────────────────────────────


@router.get("/api/v1/comms/stations", response_model=list[StationRead], dependencies=[Depends(require_permission("comms:read"))])
async def list_stations(
    db: Annotated[AsyncSession, Depends(get_db)], redis: Annotated[Any, Depends(get_redis)]
) -> list[StationRead]:
    stations = list((await db.execute(select(Station).order_by(Station.role, Station.name))).scalars())
    seen = await presence.read_presence(redis, [s.slug for s in stations])
    out = []
    for station in stations:
        read = StationRead.model_validate(station)
        read.connected, read.last_seen = seen.get(station.slug, (False, None))
        out.append(read)
    return out


async def _station_or_404(db: AsyncSession, slug: str) -> Station:
    station = await db.get(Station, slug)
    if station is None:
        raise HTTPException(status_code=404, detail="Station not found")
    return station


def _link_result(station: Station) -> LinkResult:
    assert station.access_key is not None
    return LinkResult(access_key=station.access_key, path=f"/station/{station.slug}?k={station.access_key}")


@router.get("/api/v1/comms/stations/{slug}/link", response_model=LinkResult)
async def station_link(
    slug: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _principal: Annotated[CurrentPrincipal, Depends(require_permission("comms:manage"))],
) -> LinkResult:
    """The current secured link (so the desk can copy it again)."""
    station = await _station_or_404(db, slug)
    if station.access_key is None:
        raise HTTPException(status_code=404, detail="Station is not secured; rotate its link to secure it")
    return _link_result(station)


@router.post("/api/v1/comms/stations/{slug}/rotate-link", response_model=LinkResult)
async def rotate_station_link(
    slug: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("comms:manage"))],
) -> LinkResult:
    """Secures the station (if it isn't) and issues a fresh key; every
    previously shared link and unlock token stops working."""
    station = await _station_or_404(db, slug)
    station.access_key = access.new_access_key()
    station.key_version += 1
    await db.commit()
    await log_action(db, principal.subject, "rotate_link", "station", slug)
    return _link_result(station)


@router.put("/api/v1/comms/stations/{slug}/pin", response_model=StationRead)
async def set_station_pin(
    slug: str,
    body: PinRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("comms:manage"))],
) -> Station:
    """Sets (or with `pin: null` clears) the PIN. A PIN needs a secured
    station, so one is secured with a new key if it wasn't."""
    station = await _station_or_404(db, slug)
    if body.pin is not None and station.access_key is None:
        station.access_key = access.new_access_key()
    station.pin_hash = access.hash_pin(body.pin) if body.pin is not None else None
    station.key_version += 1
    await db.commit()
    await log_action(db, principal.subject, "set_pin" if body.pin else "clear_pin", "station", slug)
    return station


@router.delete("/api/v1/comms/stations/{slug}/link", status_code=204, response_model=None)
async def unsecure_station(
    slug: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("comms:manage"))],
) -> Response:
    """Removes the key and PIN: the plain `/station/{slug}` link works again."""
    station = await _station_or_404(db, slug)
    station.access_key, station.pin_hash = None, None
    station.key_version += 1
    await db.commit()
    await log_action(db, principal.subject, "unsecure", "station", slug)
    return Response(status_code=204)


@router.post("/api/v1/comms/stations", response_model=StationRead, status_code=201)
async def create_station(
    body: StationCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("comms:manage"))],
) -> Station:
    station = Station(slug=body.slug, name=body.name, role=body.role)
    db.add(station)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail=f"Station {body.slug!r} already exists")
    await log_action(db, principal.subject, "create", "station", body.slug, changes=body.model_dump())
    return station


@router.put("/api/v1/comms/stations/{slug}", response_model=StationRead)
async def update_station(
    slug: str,
    body: StationUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("comms:manage"))],
) -> Station:
    station = await db.get(Station, slug)
    if station is None:
        raise HTTPException(status_code=404, detail="Station not found")
    station.name, station.role = body.name, body.role
    await db.commit()
    await log_action(db, principal.subject, "update", "station", slug, changes=body.model_dump())
    return station


@router.delete("/api/v1/comms/stations/{slug}", status_code=204, response_model=None)
async def delete_station(
    slug: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("comms:manage"))],
) -> Response:
    station = await db.get(Station, slug)
    if station is None:
        raise HTTPException(status_code=404, detail="Station not found")
    await db.delete(station)
    await db.commit()
    await log_action(db, principal.subject, "delete", "station", slug)
    return Response(status_code=204)


# ── presets & agenda (small CRUD, same shape) ────────────────────────


@router.get("/api/v1/comms/presets", response_model=list[PresetRead], dependencies=[Depends(require_permission("comms:read"))])
async def list_presets(db: Annotated[AsyncSession, Depends(get_db)]) -> list[CommsPreset]:
    return list((await db.execute(select(CommsPreset).order_by(CommsPreset.position, CommsPreset.label))).scalars())


@router.post("/api/v1/comms/presets", response_model=PresetRead, status_code=201)
async def create_preset(
    body: PresetBase,
    db: Annotated[AsyncSession, Depends(get_db)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("comms:manage"))],
) -> CommsPreset:
    preset = CommsPreset(**body.model_dump())
    db.add(preset)
    await db.commit()
    await db.refresh(preset)
    await log_action(db, principal.subject, "create", "comms_preset", str(preset.id), changes={"label": body.label})
    return preset


@router.put("/api/v1/comms/presets/{preset_id}", response_model=PresetRead)
async def update_preset(
    preset_id: UUID,
    body: PresetBase,
    db: Annotated[AsyncSession, Depends(get_db)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("comms:manage"))],
) -> CommsPreset:
    preset = await db.get(CommsPreset, preset_id)
    if preset is None:
        raise HTTPException(status_code=404, detail="Preset not found")
    for key, value in body.model_dump().items():
        setattr(preset, key, value)
    await db.commit()
    await log_action(db, principal.subject, "update", "comms_preset", str(preset_id))
    return preset


@router.delete("/api/v1/comms/presets/{preset_id}", status_code=204, response_model=None)
async def delete_preset(
    preset_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("comms:manage"))],
) -> Response:
    preset = await db.get(CommsPreset, preset_id)
    if preset is None:
        raise HTTPException(status_code=404, detail="Preset not found")
    await db.delete(preset)
    await db.commit()
    await log_action(db, principal.subject, "delete", "comms_preset", str(preset_id))
    return Response(status_code=204)


async def _agenda_items(db: AsyncSession, scope: str | None = None) -> list[AgendaItem]:
    stmt = select(AgendaItem).order_by(AgendaItem.scope, AgendaItem.position, AgendaItem.start_time)
    if scope:
        stmt = stmt.where(AgendaItem.scope == scope)
    return list((await db.execute(stmt)).scalars())


@router.get("/api/v1/comms/agenda", response_model=list[AgendaRead], dependencies=[Depends(require_permission("comms:read"))])
async def list_agenda(db: Annotated[AsyncSession, Depends(get_db)]) -> list[AgendaItem]:
    return await _agenda_items(db)


def _place(siblings: list[AgendaItem], item: AgendaItem, position: int | None) -> None:
    """Puts `item` into `siblings` (its scope's other items, in order) at
    `position` — or, when none is given, chronologically by start time — and
    renumbers the scope 0..n."""
    if position is None:
        index = next((i for i, s in enumerate(siblings) if s.start_time > item.start_time), len(siblings))
    else:
        index = min(position, len(siblings))
    for number, member in enumerate([*siblings[:index], item, *siblings[index:]]):
        member.position = number


@router.post("/api/v1/comms/agenda", response_model=AgendaRead, status_code=201)
async def create_agenda_item(
    body: AgendaBase,
    db: Annotated[AsyncSession, Depends(get_db)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("comms:manage"))],
) -> AgendaItem:
    data = body.model_dump()
    position = data.pop("position")
    item = AgendaItem(**data, position=0)
    siblings = await _agenda_items(db, body.scope)
    db.add(item)
    _place(siblings, item, position)
    await db.commit()
    await db.refresh(item)
    await log_action(db, principal.subject, "create", "agenda_item", str(item.id), changes={"title": body.title})
    return item


@router.put("/api/v1/comms/agenda/order", status_code=204, response_model=None)
async def reorder_agenda(
    body: AgendaReorder,
    db: Annotated[AsyncSession, Depends(get_db)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("comms:manage"))],
) -> Response:
    """Drag-and-drop ordering: `ids` first (in that order), unlisted items after."""
    items = {i.id: i for i in await _agenda_items(db, body.scope)}
    unknown = [str(i) for i in body.ids if i not in items]
    if unknown:
        raise HTTPException(status_code=422, detail=f"Not in this agenda: {', '.join(unknown)}")
    if len(set(body.ids)) != len(body.ids):
        raise HTTPException(status_code=422, detail="Duplicate ids")
    rest = [i for key, i in items.items() if key not in set(body.ids)]
    for number, item in enumerate([items[i] for i in body.ids] + rest):
        item.position = number
    await db.commit()
    await log_action(db, principal.subject, "reorder", "agenda_item", body.scope)
    return Response(status_code=204)


_IMPORT_LINE = re.compile(
    r"^\s*(\d{1,2}:\d{2})\s*(?:[-–—]\s*(\d{1,2}:\d{2}))?\s+(.+?)\s*(?:\|\s*(.*?))?\s*$"
)


def _norm_time(value: str) -> str | None:
    hours, minutes = value.split(":")
    if not (0 <= int(hours) <= 23 and 0 <= int(minutes) <= 59):
        return None
    return f"{int(hours):02d}:{minutes}"


def parse_agenda_import(scope: str, text: str) -> tuple[list[AgendaBase], list[AgendaImportError]]:
    items: list[AgendaBase] = []
    errors: list[AgendaImportError] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _IMPORT_LINE.match(line)
        if match is None:
            errors.append(AgendaImportError(line=number, error="Expected 'HH:MM[-HH:MM] Title [| details]'"))
            continue
        start, end, title, details = match.groups()
        start_n, end_n = _norm_time(start), (_norm_time(end) if end else None)
        if start_n is None or (end and end_n is None):
            errors.append(AgendaImportError(line=number, error="Time must be between 00:00 and 23:59"))
        elif len(title) > 200 or (details and len(details) > 2000):
            errors.append(AgendaImportError(line=number, error="Title or details too long"))
        else:
            items.append(AgendaBase(scope=scope, start_time=start_n, end_time=end_n, title=title, details=details or None))
    return items, errors


@router.post("/api/v1/comms/agenda/import", response_model=AgendaImportResult)
async def import_agenda(
    body: AgendaImport,
    db: Annotated[AsyncSession, Depends(get_db)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("comms:manage"))],
) -> AgendaImportResult:
    """Bulk-adds agenda items from pasted text, all-or-nothing: any bad line
    rejects the whole import (nothing is written) and is reported by line."""
    items, errors = parse_agenda_import(body.scope, body.text)
    if not items and not errors:
        errors.append(AgendaImportError(line=1, error="No agenda lines found"))
    if errors or body.dry_run:
        return AgendaImportResult(created=0, errors=errors, items=items)
    existing = [] if body.replace else await _agenda_items(db, body.scope)
    if body.replace:
        await db.execute(delete(AgendaItem).where(AgendaItem.scope == body.scope))
    start = len(existing)
    for offset, item in enumerate(items):
        data = item.model_dump()
        data.pop("position")
        db.add(AgendaItem(**data, position=start + offset))
    await db.commit()
    await log_action(
        db, principal.subject, "import", "agenda_item", body.scope, changes={"count": len(items), "replace": body.replace}
    )
    return AgendaImportResult(created=len(items), items=items)


@router.put("/api/v1/comms/agenda/{item_id}", response_model=AgendaRead)
async def update_agenda_item(
    item_id: UUID,
    body: AgendaBase,
    db: Annotated[AsyncSession, Depends(get_db)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("comms:manage"))],
) -> AgendaItem:
    item = await db.get(AgendaItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Agenda item not found")
    moved_scope = item.scope != body.scope
    for key, value in body.model_dump(exclude={"position"}).items():
        setattr(item, key, value)
    if moved_scope or body.position is not None:
        siblings = [i for i in await _agenda_items(db, body.scope) if i.id != item.id]
        _place(siblings, item, body.position)
    await db.commit()
    await log_action(db, principal.subject, "update", "agenda_item", str(item_id))
    return item


@router.delete("/api/v1/comms/agenda/{item_id}", status_code=204, response_model=None)
async def delete_agenda_item(
    item_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("comms:manage"))],
) -> Response:
    item = await db.get(AgendaItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Agenda item not found")
    await db.delete(item)
    await db.commit()
    await log_action(db, principal.subject, "delete", "agenda_item", str(item_id))
    return Response(status_code=204)


# ── desk -> stations ─────────────────────────────────────────────────


@router.post("/api/v1/comms/send", response_model=SendResult)
async def send_message(
    body: SendRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Any, Depends(get_redis)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("comms:send"))],
) -> SendResult:
    targets = await _resolve_targets(db, body.stations, body.roles)
    if not targets:
        raise HTTPException(status_code=422, detail="No matching stations")
    message = {"id": str(uuid.uuid4()), "text": body.text, "flash": body.flash, "sent_at": time.time()}
    for slug in targets:
        try:
            await redis.set(LAST_KEY.format(slug=slug), json.dumps(message), ex=6 * 3600)
        except Exception:
            logger.warning("Could not persist last message for station %s", slug, exc_info=True)
    await _publish(redis, MESSAGE_EVENT, {"stations": targets, **message})
    await log_action(db, principal.subject, "send", "comms", ",".join(targets), changes={"text": body.text, "flash": body.flash})
    return SendResult(stations=targets)


@router.post("/api/v1/comms/screen", response_model=SendResult)
async def push_screen(
    body: ScreenRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Any, Depends(get_redis)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("comms:send"))],
) -> SendResult:
    targets = await _resolve_targets(db, body.stations, body.roles)
    roles = {
        s.slug: s.role for s in (await db.execute(select(Station).where(Station.slug.in_(targets)))).scalars()
    }
    # A station only ever shows screens its role may see (e.g. no countdown
    # on floor manager iPads); the rest are skipped, not errored.
    allowed = [slug for slug in targets if body.screen in screens_for(roles[slug])]
    if not allowed:
        raise HTTPException(status_code=422, detail=f"Screen {body.screen!r} is not available to those stations")
    for slug in allowed:
        try:
            await redis.set(SCREEN_KEY.format(slug=slug), body.screen, ex=24 * 3600)
        except Exception:
            logger.warning("Could not persist screen for station %s", slug, exc_info=True)
    await _publish(redis, SCREEN_EVENT, {"stations": allowed, "screen": body.screen})
    await log_action(db, principal.subject, "screen", "comms", ",".join(allowed), changes={"screen": body.screen})
    return SendResult(stations=allowed)


async def _read_timer(redis: Any, slug: str) -> dict[str, Any] | None:
    try:
        raw = await redis.get(TIMER_KEY.format(slug=slug))
        return json.loads(raw) if raw else None
    except Exception:
        logger.warning("Timer state unavailable for station %s", slug, exc_info=True)
        return None


@router.post("/api/v1/comms/timer", response_model=SendResult)
async def control_timer(
    body: TimerRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Any, Depends(get_redis)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("comms:send"))],
) -> SendResult:
    """Starts / pauses / resumes / adjusts / stops a stagetimer-style cue on
    stations. The timer lives in Redis as 'run started at' + 'elapsed before',
    so every pod and every iPad derives the same display from the clock."""
    targets = await _resolve_targets(db, body.stations, body.roles)
    if not targets:
        raise HTTPException(status_code=422, detail="No matching stations")
    now = time.time()
    affected: list[str] = []
    for slug in targets:
        current = await _read_timer(redis, slug)
        if body.action == "start":
            state = ctimer.start(
                mode=body.mode,
                duration=body.duration_seconds,
                label=body.label,
                warn=body.warn_seconds,
                critical=body.critical_seconds,
                now=now,
            )
        elif body.action == "stop":
            state = None
        elif current is None:
            continue
        elif body.action == "pause":
            state = ctimer.pause(current, now)
        elif body.action == "resume":
            state = ctimer.resume(current, now)
        else:
            state = ctimer.adjust(current, body.adjust_seconds or 0)
        if body.action == "stop" and current is None:
            continue
        try:
            if state is None:
                await redis.delete(TIMER_KEY.format(slug=slug))
                if await redis.get(SCREEN_KEY.format(slug=slug)) == "timer":
                    await redis.set(SCREEN_KEY.format(slug=slug), DEFAULT_SCREEN, ex=24 * 3600)
            else:
                await redis.set(TIMER_KEY.format(slug=slug), json.dumps(state), ex=24 * 3600)
                if body.action == "start":
                    await redis.set(SCREEN_KEY.format(slug=slug), "timer", ex=24 * 3600)
        except Exception:
            logger.warning("Could not persist timer for station %s", slug, exc_info=True)
        await _publish(redis, TIMER_EVENT, {"stations": [slug], "timer": state, "server_time": now})
        if body.action == "start":
            await _publish(redis, SCREEN_EVENT, {"stations": [slug], "screen": "timer"})
        affected.append(slug)
    if not affected:
        raise HTTPException(status_code=409, detail="No running timer on those stations")
    await log_action(
        db, principal.subject, f"timer_{body.action}", "comms", ",".join(affected),
        changes={"mode": body.mode, "duration": body.duration_seconds, "label": body.label},
    )
    return SendResult(stations=affected)


# ── stations -> desk ─────────────────────────────────────────────────


@router.get(
    "/api/v1/comms/inbox", response_model=list[StationMessageRead], dependencies=[Depends(require_permission("comms:read"))]
)
async def inbox(db: Annotated[AsyncSession, Depends(get_db)], unacknowledged: bool = False) -> list[StationMessage]:
    stmt = select(StationMessage).order_by(StationMessage.created_at.desc()).limit(100)
    if unacknowledged:
        stmt = stmt.where(StationMessage.acknowledged.is_(False))
    return list((await db.execute(stmt)).scalars())


@router.post("/api/v1/comms/inbox/{message_id}/ack", status_code=204, response_model=None)
async def ack_message(
    message_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _principal: Annotated[CurrentPrincipal, Depends(require_permission("comms:send"))],
) -> Response:
    result = await db.execute(update(StationMessage).where(StationMessage.id == message_id).values(acknowledged=True))
    await db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Message not found")
    return Response(status_code=204)


@router.delete("/api/v1/comms/inbox", status_code=204, response_model=None)
async def clear_inbox(
    db: Annotated[AsyncSession, Depends(get_db)],
    _principal: Annotated[CurrentPrincipal, Depends(require_permission("comms:send"))],
) -> Response:
    await db.execute(delete(StationMessage).where(StationMessage.acknowledged.is_(True)))
    await db.commit()
    return Response(status_code=204)


# ── public (station iPad) ────────────────────────────────────────────


async def _public_station(db: AsyncSession, slug: str) -> Station:
    station = await db.get(Station, slug)
    if station is None:
        raise HTTPException(status_code=404, detail="Station not found")
    return station


def _credentials(request: Request) -> tuple[str | None, str | None]:
    key = request.headers.get("x-station-key") or request.query_params.get("k")
    token = request.headers.get("x-station-token") or request.query_params.get("t")
    return key, token


async def station_access(slug: str, request: Request, db: Annotated[AsyncSession, Depends(get_db)]) -> Station:
    """Dependency for every public station endpoint: 404 for unknown
    stations, 401 `key` / `pin` when a secured station's credentials are
    missing or wrong (the page uses the detail to know what to ask for)."""
    station = await _public_station(db, slug)
    missing = access.check_access(station, *_credentials(request))
    if missing:
        raise HTTPException(status_code=401, detail=missing)
    return station


StationAccess = Annotated[Station, Depends(station_access)]


@router.post("/api/v1/public/stations/{slug}/unlock", response_model=UnlockResult)
async def unlock_station(
    slug: str,
    body: UnlockRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Any, Depends(get_redis)],
) -> UnlockResult:
    """Exchanges the station key + PIN for a 12 h unlock token. Wrong PINs are
    counted per station; after `MAX_PIN_FAILURES` the station locks for ten
    minutes so a short PIN can't be brute-forced."""
    station = await _public_station(db, slug)
    key, _ = _credentials(request)
    if station.access_key is None or station.pin_hash is None:
        raise HTTPException(status_code=409, detail="This station has no PIN")
    if access.check_access(station, key, "x") == "key":  # the key must be right before a PIN is even tried
        raise HTTPException(status_code=401, detail="key")
    fail_key = PIN_FAIL_KEY.format(slug=slug)
    try:
        if int(await redis.get(fail_key) or 0) >= MAX_PIN_FAILURES:
            raise HTTPException(status_code=429, detail="Too many wrong PINs; try again in a few minutes")
    except HTTPException:
        raise
    except Exception:
        logger.warning("PIN lockout state unavailable for %s", slug, exc_info=True)
    if not access.verify_pin(body.pin, station.pin_hash):
        try:
            if await redis.incr(fail_key) == 1:
                await redis.expire(fail_key, PIN_LOCK_SECONDS)
        except Exception:
            logger.warning("Could not record PIN failure for %s", slug, exc_info=True)
        raise HTTPException(status_code=401, detail="pin")
    try:
        await redis.delete(fail_key)
    except Exception:
        pass
    return UnlockResult(token=access.issue_token(slug, station.key_version), expires_in=access.TOKEN_TTL_SECONDS)


@router.get("/api/v1/public/stations/{slug}", response_model=PublicStation)
async def public_station(station: StationAccess, redis: Annotated[Any, Depends(get_redis)]) -> PublicStation:
    slug = station.slug
    screen, last = DEFAULT_SCREEN, None
    timer_state = await _read_timer(redis, slug)
    try:
        stored = await redis.get(SCREEN_KEY.format(slug=slug))
        if stored in screens_for(station.role) and (stored != "timer" or timer_state is not None):
            screen = stored
        raw = await redis.get(LAST_KEY.format(slug=slug))
        last = json.loads(raw) if raw else None
    except Exception:
        logger.warning("Station state unavailable for %s", slug, exc_info=True)
    return PublicStation(
        slug=slug,
        name=station.name,
        role=station.role,
        screens=screens_for(station.role),
        screen=screen,
        last_message=last,
        timer=TimerState(**timer_state) if timer_state else None,
        server_time=time.time(),
    )


@router.get("/api/v1/public/stations/{slug}/agenda", response_model=list[AgendaRead])
async def public_agenda(station: StationAccess, db: Annotated[AsyncSession, Depends(get_db)], scope: str | None = None) -> list[AgendaItem]:
    if scope not in (None, "day", "finals"):
        raise HTTPException(status_code=422, detail="scope must be 'day' or 'finals'")
    return await _agenda_items(db, scope)


@router.get("/api/v1/public/stations/{slug}/next-match", response_model=NextMatch)
async def public_next_match(
    station: StationAccess, db: Annotated[AsyncSession, Depends(get_db)], redis: Annotated[Any, Depends(get_redis)]
) -> NextMatch:
    """Countdown data for emcees: the next unscored match, its teams and
    their display stats. Other roles have no countdown screen."""
    if station.role != ROLE_EMCEE:
        raise HTTPException(status_code=403, detail="Countdown is only available to emcee stations")
    schedules: dict[str, list] = {}
    try:
        async for key in redis.scan_iter(match=SCHEDULE_KEY_PATTERN):
            raw = await redis.get(key)
            if raw:
                for division, matches in parse_schedule(json.loads(raw)).items():
                    schedules.setdefault(division, []).extend(matches)
    except Exception:
        logger.warning("Could not read cached schedules", exc_info=True)
        return NextMatch()
    found = find_next_match(schedules, time.time())
    if found is None:
        return NextMatch()
    division, match = found
    numbers = list(dict.fromkeys(match.teams))
    profiles = {
        p.team_number: p
        for p in (await db.execute(select(TeamProfile).where(TeamProfile.team_number.in_(numbers)))).scalars()
    }

    def info(number: str) -> dict:
        return _overlay_team(number, profiles.get(number), {}).model_dump(exclude={"bot_url", "team_url"})

    return NextMatch(
        available=True,
        label=match.label,
        division=division,
        scheduled_at=match.time_scheduled,
        red=[info(t) for t in match.red],
        blue=[info(t) for t in match.blue],
    )


@router.post("/api/v1/public/stations/{slug}/messages", status_code=204, response_model=None)
async def post_station_message(
    station: StationAccess,
    body: InboundMessage,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Any, Depends(get_redis)],
) -> None:
    await _rate_limit(request, redis, "station-msg")
    slug = station.slug
    pending = (
        await db.execute(
            select(StationMessage.id).where(StationMessage.station_slug == slug, StationMessage.acknowledged.is_(False))
        )
    ).all()
    if len(pending) >= MAX_UNACKED_PER_STATION:
        raise HTTPException(status_code=429, detail="Too many unread messages; wait for the desk to catch up")
    db.add(StationMessage(station_slug=slug, text=body.text))
    await db.commit()
    await _publish(redis, INBOUND_EVENT, {"station": slug, "name": station.name, "text": body.text})
    if delivery.push_configured():
        try:
            await delivery.push_to_staff(async_session_factory, f"Message from {station.name}", body.text, delivery.CATEGORY_SYSTEM)
        except Exception:
            logger.exception("Staff push for station message failed")
