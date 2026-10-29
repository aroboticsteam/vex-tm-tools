"""Rooms admin API: a room is a named group of teams with a public display
page (`/room/<slug>`), an optional livestream embed, and match/broadcast
notifications targeted at it. Thin CRUD over `rooms`/`room_teams`."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.audit import log_action
from backend.core.dependencies import CurrentPrincipal, get_db, require_permission
from backend.models.room import Room, RoomTeam
from backend.schemas.room import RoomCreate, RoomRead, RoomUpdate

router = APIRouter(prefix="/api/v1/rooms", tags=["rooms"])


async def _teams_by_room(db: AsyncSession, slugs: list[str] | None = None) -> dict[str, list[str]]:
    stmt = select(RoomTeam.room_slug, RoomTeam.team_number).order_by(RoomTeam.team_number)
    if slugs is not None:
        stmt = stmt.where(RoomTeam.room_slug.in_(slugs))
    grouped: dict[str, list[str]] = {}
    for slug, team in (await db.execute(stmt)).all():
        grouped.setdefault(slug, []).append(team)
    return grouped


def _read(room: Room, teams: list[str]) -> RoomRead:
    return RoomRead(slug=room.slug, name=room.name, stream_url=room.stream_url, teams=teams)


async def _get_or_404(db: AsyncSession, slug: str) -> Room:
    room = await db.get(Room, slug)
    if room is None:
        raise HTTPException(status_code=404, detail=f"Room {slug!r} not found")
    return room


@router.get("", response_model=list[RoomRead], dependencies=[Depends(require_permission("rooms:read"))])
async def list_rooms(db: Annotated[AsyncSession, Depends(get_db)]) -> list[RoomRead]:
    rooms = list((await db.execute(select(Room).order_by(Room.name))).scalars().all())
    teams = await _teams_by_room(db)
    return [_read(r, teams.get(r.slug, [])) for r in rooms]


@router.get("/{slug}", response_model=RoomRead, dependencies=[Depends(require_permission("rooms:read"))])
async def get_room(slug: str, db: Annotated[AsyncSession, Depends(get_db)]) -> RoomRead:
    room = await _get_or_404(db, slug)
    return _read(room, (await _teams_by_room(db, [slug])).get(slug, []))


@router.post("", response_model=RoomRead, status_code=201)
async def create_room(
    body: RoomCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("rooms:manage"))],
) -> RoomRead:
    db.add(Room(slug=body.slug, name=body.name, stream_url=body.stream_url))
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail=f"Room {body.slug!r} already exists")
    db.add_all(RoomTeam(room_slug=body.slug, team_number=t) for t in body.teams)
    await db.commit()
    await log_action(db, principal.subject, "create", "room", body.slug, changes=body.model_dump())
    return RoomRead(slug=body.slug, name=body.name, stream_url=body.stream_url, teams=sorted(body.teams))


@router.put("/{slug}", response_model=RoomRead)
async def update_room(
    slug: str,
    body: RoomUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("rooms:manage"))],
) -> RoomRead:
    room = await _get_or_404(db, slug)
    room.name = body.name
    room.stream_url = body.stream_url
    await db.execute(delete(RoomTeam).where(RoomTeam.room_slug == slug))
    db.add_all(RoomTeam(room_slug=slug, team_number=t) for t in body.teams)
    await db.commit()
    await log_action(db, principal.subject, "update", "room", slug, changes=body.model_dump())
    return RoomRead(slug=slug, name=body.name, stream_url=body.stream_url, teams=sorted(body.teams))


@router.delete("/{slug}", status_code=204, response_model=None)
async def delete_room(
    slug: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("rooms:manage"))],
) -> Response:
    room = await _get_or_404(db, slug)
    await db.delete(room)
    await db.commit()
    await log_action(db, principal.subject, "delete", "room", slug)
    return Response(status_code=204)
