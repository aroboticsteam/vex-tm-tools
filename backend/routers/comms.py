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
from backend.modules.comms.next_match import find_next_match
from backend.modules.notifications import delivery
from backend.modules.notifications.logic import parse_schedule
from backend.routers.notifications import _rate_limit
from backend.routers.team_clips import _overlay_team
from backend.schemas.comms import (
    AgendaBase,
    AgendaRead,
    InboundMessage,
    NextMatch,
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
)
from backend.schemas.events import EventBusMessage

logger = logging.getLogger(__name__)

router = APIRouter(tags=["comms"])

SCHEDULE_KEY_PATTERN = "qecomp:tm:*:schedule"
SCREEN_KEY = "qecomp:station:{slug}:screen"
LAST_KEY = "qecomp:station:{slug}:last"
DEFAULT_SCREEN = "messages"
MESSAGE_EVENT = "station_message"
SCREEN_EVENT = "station_screen"
INBOUND_EVENT = "station_inbound"
COMMS_ENTITY_ID = "comms"
MAX_UNACKED_PER_STATION = 50


def screens_for(role: str) -> list[str]:
    base = ["messages", "agenda_day", "agenda_finals"]
    return ["messages", "countdown", "agenda_day", "agenda_finals"] if role == ROLE_EMCEE else base


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
async def list_stations(db: Annotated[AsyncSession, Depends(get_db)]) -> list[Station]:
    return list((await db.execute(select(Station).order_by(Station.role, Station.name))).scalars())


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
    stmt = select(AgendaItem).order_by(AgendaItem.scope, AgendaItem.start_time, AgendaItem.position)
    if scope:
        stmt = stmt.where(AgendaItem.scope == scope)
    return list((await db.execute(stmt)).scalars())


@router.get("/api/v1/comms/agenda", response_model=list[AgendaRead], dependencies=[Depends(require_permission("comms:read"))])
async def list_agenda(db: Annotated[AsyncSession, Depends(get_db)]) -> list[AgendaItem]:
    return await _agenda_items(db)


@router.post("/api/v1/comms/agenda", response_model=AgendaRead, status_code=201)
async def create_agenda_item(
    body: AgendaBase,
    db: Annotated[AsyncSession, Depends(get_db)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("comms:manage"))],
) -> AgendaItem:
    item = AgendaItem(**body.model_dump())
    db.add(item)
    await db.commit()
    await db.refresh(item)
    await log_action(db, principal.subject, "create", "agenda_item", str(item.id), changes={"title": body.title})
    return item


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
    for key, value in body.model_dump().items():
        setattr(item, key, value)
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


@router.get("/api/v1/public/stations/{slug}", response_model=PublicStation)
async def public_station(
    slug: str, db: Annotated[AsyncSession, Depends(get_db)], redis: Annotated[Any, Depends(get_redis)]
) -> PublicStation:
    station = await _public_station(db, slug)
    screen, last = DEFAULT_SCREEN, None
    try:
        stored = await redis.get(SCREEN_KEY.format(slug=slug))
        if stored in screens_for(station.role):
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
    )


@router.get("/api/v1/public/stations/{slug}/agenda", response_model=list[AgendaRead])
async def public_agenda(slug: str, db: Annotated[AsyncSession, Depends(get_db)], scope: str | None = None) -> list[AgendaItem]:
    await _public_station(db, slug)
    if scope not in (None, "day", "finals"):
        raise HTTPException(status_code=422, detail="scope must be 'day' or 'finals'")
    return await _agenda_items(db, scope)


@router.get("/api/v1/public/stations/{slug}/next-match", response_model=NextMatch)
async def public_next_match(
    slug: str, db: Annotated[AsyncSession, Depends(get_db)], redis: Annotated[Any, Depends(get_redis)]
) -> NextMatch:
    """Countdown data for emcees: the next unscored match, its teams and
    their display stats. Other roles have no countdown screen."""
    station = await _public_station(db, slug)
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
    slug: str,
    body: InboundMessage,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Any, Depends(get_redis)],
) -> None:
    await _rate_limit(request, redis, "station-msg")
    station = await _public_station(db, slug)
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
