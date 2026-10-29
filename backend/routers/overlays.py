"""Overlays API (plan §11 "Overlays" / §5.14).

Thin CRUD router over `overlay_instances`, plus a preview endpoint. Per
§C.2, no business logic lives here.
"""
from __future__ import annotations

import logging
import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend import loader
from backend.core.dependencies import get_db, require_permission
from backend.core.exceptions import ProcessingError
from backend.core.redis import get_redis
from backend.models.integration import IntegrationInstance
from backend.models.overlay import OverlayInstance
from backend.models.team import TeamProfile
from backend.modules.media import s3
from backend.schemas.events import EventBusMessage
from backend.schemas.overlay import (
    OverlayInstanceCreate,
    OverlayInstanceRead,
    OverlayInstanceUpdate,
    ShowTeamRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/overlays", tags=["overlays"])


async def _get_or_404(db: AsyncSession, entity_id: str) -> OverlayInstance:
    result = await db.execute(select(OverlayInstance).where(OverlayInstance.entity_id == entity_id))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Overlay {entity_id!r} not found")
    return row


async def _publish_config_change(redis_client: Any, entity_id: str, action: str, tags: list[str]) -> None:
    msg = EventBusMessage(
        entity_id=entity_id,
        entity_tags=tags,
        type="config_change",
        timestamp=time.time(),
        payload={"resource_type": "overlay_instance", "entity_id": entity_id, "action": action},
    )
    try:
        await redis_client.publish("qecomp:config_change", msg.model_dump_json())
    except Exception:
        logger.exception("Failed to publish config_change for %s", entity_id)


@router.get(
    "",
    response_model=list[OverlayInstanceRead],
    dependencies=[Depends(require_permission("overlays:read"))],
)
async def list_overlays(db: Annotated[AsyncSession, Depends(get_db)]) -> list[OverlayInstance]:
    result = await db.execute(select(OverlayInstance).order_by(OverlayInstance.display_name))
    return list(result.scalars().all())


@router.post(
    "",
    response_model=OverlayInstanceRead,
    status_code=201,
    dependencies=[Depends(require_permission("overlays:edit"))],
)
async def create_overlay(
    body: OverlayInstanceCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis_client: Any = Depends(get_redis),
) -> OverlayInstance:
    existing = await db.execute(select(OverlayInstance).where(OverlayInstance.entity_id == body.entity_id))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail=f"Overlay {body.entity_id!r} already exists")

    row = OverlayInstance(**body.model_dump())
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail=f"Overlay {body.entity_id!r} already exists") from None
    await db.refresh(row)

    await _publish_config_change(redis_client, row.entity_id, "created", row.tags)
    return row


@router.put(
    "/{entity_id}",
    response_model=OverlayInstanceRead,
    dependencies=[Depends(require_permission("overlays:edit"))],
)
async def update_overlay(
    entity_id: str,
    body: OverlayInstanceUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis_client: Any = Depends(get_redis),
) -> OverlayInstance:
    row = await _get_or_404(db, entity_id)
    for field_name, value in body.model_dump(exclude_unset=True).items():
        if value is None:
            continue
        setattr(row, field_name, value)
    await db.commit()
    await db.refresh(row)
    await _publish_config_change(redis_client, row.entity_id, "updated", row.tags)
    return row


@router.delete(
    "/{entity_id}",
    status_code=204,
    response_model=None,
    dependencies=[Depends(require_permission("overlays:edit"))],
)
async def delete_overlay(
    entity_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis_client: Any = Depends(get_redis),
) -> None:
    row = await _get_or_404(db, entity_id)
    tags = list(row.tags or [])
    await db.delete(row)
    await db.commit()
    await _publish_config_change(redis_client, entity_id, "deleted", tags)


@router.post(
    "/{entity_id}/show-team",
    status_code=204,
    response_model=None,
    dependencies=[Depends(require_permission("overlays:edit"))],
)
async def show_team(
    entity_id: str,
    body: ShowTeamRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis_client: Any = Depends(get_redis),
) -> None:
    """Finals "team spotlight": tells the overlay to show one team (team clip
    + robot 360 + info) instead of the match grid; `team: null` reverts."""
    row = await _get_or_404(db, entity_id)
    msg = EventBusMessage(
        entity_id=entity_id,
        entity_tags=list(row.tags or []),
        type="overlay_show_team",
        timestamp=time.time(),
        payload={"team": body.team.upper() if body.team else None},
    )
    try:
        await redis_client.publish("qecomp:events", msg.model_dump_json())
    except Exception:
        logger.exception("Failed to publish overlay_show_team for %s", entity_id)
        raise HTTPException(status_code=503, detail="Event bus unavailable")


async def _find_upcoming_match_for_field_set(
    db: AsyncSession, field_set_id: int
) -> tuple[str | None, dict[str, Any] | None]:
    """Best-effort lookup of the current/next queued match for a field set,
    reusing the exact same source of truth `ws.py`'s
    `ConnectionManager._dispatch_upcoming_match` relies on transitively:
    each `vex_tm` `Integration` instance keeps the raw last-received TM
    WebSocket event on `self._last_event` (see
    `VexTmIntegration._handle_ws_message`), surfaced read-only via the
    frozen `get_state()`/`backend.loader` public API (plan §C.2) rather than
    a private attribute reach-through.

    There is no dedicated "last queued match" cache in Postgres/Redis (the
    live path is purely event-driven, pushed straight from
    `_dispatch_upcoming_match` to connected WebSocket clients) — so a REST
    preview endpoint has no persisted state to query after the fact. This
    reads the same in-memory value the live WS dispatch path reads from,
    which is only available on the current leader node (a passive node's
    `loader.get_instance()` returns `None` for every entity_id, same
    degradation as everywhere else in the app that reaches leader-only
    state via `backend.loader`).
    """
    result = await db.execute(select(IntegrationInstance).where(IntegrationInstance.domain == "vex_tm"))
    candidate_entity_ids = [
        row.entity_id for row in result.scalars().all() if (row.config or {}).get("field_set_id") == field_set_id
    ]
    for tm_entity_id in candidate_entity_ids:
        integration = loader.get_instance(tm_entity_id)
        if integration is None:
            continue
        try:
            state = await integration.get_state()
        except Exception:
            logger.exception("Failed to read get_state() for vex_tm instance %s", tm_entity_id)
            continue
        last_event = (state or {}).get("last_event") or {}
        if last_event.get("type") == "fieldMatchAssigned":
            return tm_entity_id, last_event
    return None, None


def _resolve_high_potential(request: Request, payload: dict[str, Any]) -> bool:
    """Mirrors `ConnectionManager._resolve_high_potential` (never exposes
    `predicted_red`/`predicted_blue`, only the sanitized boolean flag —
    plan §5.12). Reached via `request.app.state.predictor` rather than
    `ws_manager.get_predictor` since this is a plain HTTP router, not the
    WS module — same leader-only degradation (`None` on a passive node)."""
    predictor = getattr(request.app.state, "predictor", None)
    if predictor is None:
        return False
    match_num = payload.get("matchNum")
    division_id = payload.get("divisionId")
    if match_num is None or division_id is None:
        return False
    try:
        from backend.modules.predictor.predictor import match_id_for

        return bool(predictor.get_flag(match_id_for(division_id, match_num)))
    except Exception:
        return False


async def _team_profiles_by_number(db: AsyncSession, numbers: list[str]) -> dict[str, TeamProfile]:
    if not numbers:
        return {}
    result = await db.execute(select(TeamProfile).where(TeamProfile.team_number.in_(numbers)))
    return {row.team_number: row for row in result.scalars().all()}


def _team_object(number: str, profile: TeamProfile | None) -> dict[str, Any]:
    """Builds the enriched per-team shape (plan §5.10/§7's team-profile
    join — team name, robot name, qual rank, awards), matching the exact
    contract `ws.py`'s `_dispatch_upcoming_match` enriches `redTeams`/
    `blueTeams` with for `/ws/prompter/{entity_id}` and
    `/ws/overlay/{entity_id}` alike: `{team_number, team_name, robot_name,
    rank, awards}`. Every field besides `team_number` gracefully degrades to
    `None`/`[]` when profile data hasn't been scraped yet.

    NOTE (flagged in the audit report): `TeamProfile`/the scraper's
    `cached_stats` currently has no dedicated "team name" field (only
    `robot_name` is a first-class column) even though the TM `/api/teams`
    payload the scraper already fetches includes one (`team_row["name"]`,
    see `mock_tm_server.py`) — this reads `cached_stats.get("team_name")` as
    a forward-compatible key in case that gap is closed, but it will be
    `None` until then.
    """
    stats = (profile.cached_stats if profile else None) or {}
    ranking = stats.get("ranking") or {}
    return {
        "team_number": number,
        "team_name": stats.get("team_name") or stats.get("name"),
        "robot_name": profile.robot_name if profile else None,
        "rank": ranking.get("rank") if isinstance(ranking, dict) else None,
        "awards": stats.get("awards") or [],
    }


async def _video_url_for(profile: TeamProfile | None) -> str | None:
    if profile is None or not profile.video_360_s3_key or profile.video_processing_status != "DONE":
        return None
    try:
        return await s3.generate_presigned_url(profile.video_360_s3_key)
    except ProcessingError:
        logger.exception("Failed to presign video URL for team %s", profile.team_number)
        return None


@router.get(
    "/{entity_id}/preview",
    dependencies=[Depends(require_permission("overlays:read"))],
)
async def preview_overlay(
    entity_id: str, request: Request, db: Annotated[AsyncSession, Depends(get_db)]
) -> dict[str, Any]:
    """Returns exactly what `/ws/overlay/{entity_id}` would currently push
    to a connected OBS browser source (plan §5.14: "a preview panel showing
    exactly what the OBS browser source will render for the next queued
    match, without triggering the live OBS transition") — the real join
    that finding 2.1 flagged as an unconditional empty-shape stub.

    Resolution: overlay -> `field_set_id` -> the bound `vex_tm` instance's
    last `fieldMatchAssigned` event (see `_find_upcoming_match_for_field_set`)
    -> team profile + presigned video URL join (same pattern as
    `routers/teams.py`'s `GET /batch/videos`).

    When no match is currently queued (or this node isn't the leader, so no
    live `vex_tm` instance state is reachable), returns the same
    "transparent/empty" shape `overlay.html` renders as nothing per
    Appendix A.6.
    """
    row = await _get_or_404(db, entity_id)

    _tm_entity_id, last_event = await _find_upcoming_match_for_field_set(db, row.field_set_id)
    if not last_event:
        return {"entity_id": entity_id, "match": None, "teams": []}

    red_numbers = [str(t) for t in (last_event.get("redTeams") or [])]
    blue_numbers = [str(t) for t in (last_event.get("blueTeams") or [])]
    profiles_by_number = await _team_profiles_by_number(db, red_numbers + blue_numbers)

    match = {
        "matchNum": last_event.get("matchNum"),
        "round": last_event.get("round"),
        "redTeams": [_team_object(n, profiles_by_number.get(n)) for n in red_numbers],
        "blueTeams": [_team_object(n, profiles_by_number.get(n)) for n in blue_numbers],
        "high_potential": _resolve_high_potential(request, last_event),
    }

    teams = [
        {
            "team_number": number,
            "video_url": await _video_url_for(profiles_by_number.get(number)),
        }
        for number in red_numbers + blue_numbers
    ]

    return {"entity_id": entity_id, "match": match, "teams": teams}
