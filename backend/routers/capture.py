"""Capture agent API: recordings, moments, upload URLs (issues #42, #38, #45).

Two kinds of caller: the analysis deck / automations (`capture:control`,
`analysis:view`) and the capture agent's scoped API key (`capture:upload`).
"""
from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.core.audit import log_action
from backend.core.db import async_session_factory
from backend.core.dependencies import CurrentPrincipal, get_db, get_redis, require_permission
from backend.core.exceptions import IntegrationError
from backend.models.capture import CaptureMoment, CaptureRecording
from backend.modules.capture import hub, keys, replay, service
from backend.modules.media import s3
from backend.schemas.capture import (
    AgentStatus,
    MarkRequest,
    ReplayControl,
    ReplayPlay,
    RecordingRead,
    StartRecordingRequest,
    UploadComplete,
    UploadGrant,
    UploadRequest,
    WipeResult,
)

router = APIRouter(prefix="/api/v1", tags=["capture"])

PLAYBACK_URL_TTL_S = 3600


def _http(exc: Exception) -> HTTPException:
    if isinstance(exc, IntegrationError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, service.CaptureConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


@router.get("/capture/status", response_model=AgentStatus)
async def capture_status(
    redis: Annotated[Any, Depends(get_redis)],
    _: Annotated[CurrentPrincipal, Depends(require_permission("analysis:view"))],
) -> AgentStatus:
    presence = await hub.get_presence(redis)
    async with async_session_factory() as session:
        row = (
            await session.execute(
                select(CaptureRecording)
                .options(selectinload(CaptureRecording.moments))
                .where(CaptureRecording.status == "RECORDING")
                .order_by(CaptureRecording.started_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        recording = RecordingRead.model_validate(row) if row else None
    return AgentStatus(connected=presence is not None, agent=presence, recording=recording)


@router.get("/capture/recordings", response_model=list[RecordingRead])
async def list_recordings(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[CurrentPrincipal, Depends(require_permission("analysis:view"))],
    team: str | None = Query(default=None, max_length=20),
    match_type: str | None = Query(default=None, max_length=40),
    match_key: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[CaptureRecording]:
    stmt = select(CaptureRecording).options(selectinload(CaptureRecording.moments))
    if team:
        stmt = stmt.where(CaptureRecording.teams.any(team.strip().upper()))
    if match_type:
        stmt = stmt.where(CaptureRecording.match_type == keys.match_type_slug(match_type))
    if match_key:
        stmt = stmt.where(CaptureRecording.match_key == match_key)
    result = await db.execute(stmt.order_by(CaptureRecording.started_at.desc()).limit(limit))
    return list(result.scalars().all())


@router.get("/capture/recordings/{recording_id}", response_model=RecordingRead)
async def get_recording(
    recording_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[CurrentPrincipal, Depends(require_permission("analysis:view"))],
) -> CaptureRecording:
    row = (
        await db.execute(
            select(CaptureRecording).options(selectinload(CaptureRecording.moments)).where(CaptureRecording.id == recording_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Recording not found")
    return row


@router.post("/capture/recordings/start")
async def start_recording(
    body: StartRecordingRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Any, Depends(get_redis)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("capture:control"))],
) -> dict[str, Any]:
    try:
        result = await service.start_recording(redis, async_session_factory, body.model_dump())
    except (ValueError, LookupError, IntegrationError) as exc:
        raise _http(exc) from exc
    await log_action(db, principal.subject, "start", "capture_recording", result["recording"]["match_key"])
    return result


@router.post("/capture/recordings/stop")
async def stop_recording(
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Any, Depends(get_redis)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("capture:control"))],
) -> dict[str, Any]:
    try:
        result = await service.stop_recording(redis, async_session_factory)
    except (ValueError, LookupError, IntegrationError) as exc:
        raise _http(exc) from exc
    if result["recording"]:
        await log_action(db, principal.subject, "stop", "capture_recording", result["recording"]["match_key"])
    return result


@router.post("/capture/moments")
async def mark_moment(
    body: MarkRequest,
    redis: Annotated[Any, Depends(get_redis)],
    _: Annotated[CurrentPrincipal, Depends(require_permission("capture:control"))],
) -> dict[str, Any]:
    try:
        return await service.mark_moment(redis, async_session_factory, body.length_s)
    except (ValueError, LookupError, IntegrationError) as exc:
        raise _http(exc) from exc


@router.delete("/capture/recordings", response_model=WipeResult)
async def wipe_recordings(
    db: Annotated[AsyncSession, Depends(get_db)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("capture:control"))],
    match_type: str | None = Query(default=None, max_length=40),
) -> WipeResult:
    """Clears recordings and their stored files (between events)."""
    result = await service.wipe(async_session_factory, match_type)
    await log_action(db, principal.subject, "wipe", "capture_recording", match_type or "*", changes=result)
    return WipeResult(**result)


# ── Playback URLs (deck preview / send to program) ────────────────────


async def playback_url_for(session: AsyncSession, kind: str, item_id: uuid.UUID) -> tuple[str, str]:
    """Presigned GET URL and a label for a READY recording or moment."""
    if kind == "moment":
        moment = await session.get(CaptureMoment, item_id)
        recording = await session.get(CaptureRecording, moment.recording_id) if moment else None
        if moment is None or recording is None or moment.status != "READY" or not moment.object_key:
            raise HTTPException(status_code=409, detail="That moment is not uploaded yet")
        label = f"{recording.match_type} {recording.match_number} · moment {moment.number}"
        key = moment.object_key
    else:
        recording = await session.get(CaptureRecording, item_id)
        if recording is None:
            raise HTTPException(status_code=404, detail="Recording not found")
        if recording.status != "READY" or not recording.object_key:
            raise HTTPException(status_code=409, detail="That recording is not uploaded yet")
        label = f"{recording.match_type} {recording.match_number}"
        key = recording.object_key
    return await s3.generate_presigned_url(key, expires_in=PLAYBACK_URL_TTL_S), label


@router.get("/capture/{kind}/{item_id}/url")
async def playback_url(
    kind: str,
    item_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[CurrentPrincipal, Depends(require_permission("analysis:view"))],
) -> dict[str, str]:
    if kind not in ("recordings", "moments"):
        raise HTTPException(status_code=404, detail="Not found")
    url, label = await playback_url_for(db, "moment" if kind == "moments" else "recording", item_id)
    return {"url": url, "label": label}


@router.get("/public/recordings/{recording_id}/video", include_in_schema=False)
async def public_recording_video(recording_id: uuid.UUID, db: Annotated[AsyncSession, Depends(get_db)]) -> RedirectResponse:
    """Match videos are shown on the public site, like the imported recording links."""
    url, _label = await playback_url_for(db, "recording", recording_id)
    return RedirectResponse(url, status_code=302)


# ── Agent upload API (scoped `qec_` key with `capture:upload`) ────────


@router.post("/capture/uploads", response_model=UploadGrant)
async def request_upload(
    body: UploadRequest,
    _: Annotated[CurrentPrincipal, Depends(require_permission("capture:upload"))],
) -> dict[str, Any]:
    try:
        return await service.request_upload(async_session_factory, body.kind, body.recording_id, body.moment_id)
    except (ValueError, LookupError) as exc:
        raise _http(exc) from exc


@router.post("/capture/uploads/{upload_id}/complete")
async def complete_upload(
    upload_id: uuid.UUID,
    body: UploadComplete,
    redis: Annotated[Any, Depends(get_redis)],
    _: Annotated[CurrentPrincipal, Depends(require_permission("capture:upload"))],
) -> dict[str, Any]:
    try:
        return await service.complete_upload(
            redis, async_session_factory, upload_id, body.kind, body.ready_ms, body.duration_s
        )
    except (ValueError, LookupError) as exc:
        raise _http(exc) from exc


# ── Replay player (send to program) ───────────────────────────────────


def _replay_entity(entity_id: str) -> str:
    if not replay.ENTITY_ID_RE.match(entity_id):
        raise HTTPException(status_code=422, detail="Invalid replay id")
    return entity_id


@router.get("/capture/replay/{entity_id}")
async def replay_state(
    entity_id: str,
    redis: Annotated[Any, Depends(get_redis)],
    _: Annotated[CurrentPrincipal, Depends(require_permission("analysis:view"))],
) -> dict[str, Any]:
    return await replay.get_state(redis, _replay_entity(entity_id))


@router.post("/capture/replay/{entity_id}/play")
async def replay_play(
    entity_id: str,
    body: ReplayPlay,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Any, Depends(get_redis)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("capture:control"))],
) -> dict[str, Any]:
    """Send a recording or moment to the replay player page. Switching OBS to
    the scene that holds the page is a separate `obs.switch_scene` call."""
    entity_id = _replay_entity(entity_id)
    url, label = await playback_url_for(db, body.kind, body.id)
    state = await replay.play(redis, entity_id, url, label, body.rate, body.loop)
    await log_action(db, principal.subject, "send_to_program", "replay", entity_id, changes={"kind": body.kind, "id": str(body.id)})
    return {"ok": True, "label": label, "state": {k: v for k, v in state.items() if k != "url"}}


@router.post("/capture/replay/{entity_id}/control")
async def replay_control(
    entity_id: str,
    body: ReplayControl,
    redis: Annotated[Any, Depends(get_redis)],
    _: Annotated[CurrentPrincipal, Depends(require_permission("capture:control"))],
) -> dict[str, Any]:
    try:
        state = await replay.control(redis, _replay_entity(entity_id), body.action, body.value)
    except ValueError as exc:
        raise HTTPException(status_code=409 if "Nothing" in str(exc) else 422, detail=str(exc)) from exc
    return {"ok": True, "state": {k: v for k, v in state.items() if k != "url"}}
