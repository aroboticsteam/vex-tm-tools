"""Recorded green-screen clips (issues #1/#12).

Staff record two short clips per team in the browser ("bot" and "team" in
front of the green screen); each is chroma-keyed to a transparent WebM
(square framing and frame rate preserved) and stored in S3.

`GET /api/v1/public/overlay/teams` is the unauthenticated, display-safe feed
the OBS overlay page reads (no auth on overlays, by design — see
`overlay.html`): team info plus presigned clip URLs, nothing else.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from typing import Annotated, Any

import aiofiles
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.audit import log_action
from backend.core.dependencies import CurrentPrincipal, get_db, require_permission
from backend.core.exceptions import ProcessingError
from backend.models.team import TeamProfile
from backend.models.team_clip import TeamClip
from backend.modules.media import s3
from backend.modules.media.processor import process_video
from backend.routers.teams import _sanitize_processing_error
from backend.schemas.team import OverlayTeam, OverlayTeamsResponse, TeamClipsRead, TeamClipStatus, VideoUploadAccepted

logger = logging.getLogger(__name__)

router = APIRouter(tags=["team-clips"])

ALLOWED_CLIP_EXTENSIONS = {"webm", "mp4", "mov"}
MAX_CLIP_BYTES = 200 * 1024 * 1024
MAX_OVERLAY_TEAMS = 12


def _check_kind(kind: str) -> str:
    if kind not in s3.CLIP_KINDS:
        raise HTTPException(status_code=404, detail=f"Unknown clip kind {kind!r}; expected one of {s3.CLIP_KINDS}")
    return kind


def _status(number: str, kind: str, row: TeamClip | None) -> TeamClipStatus:
    if row is None:
        return TeamClipStatus(team_number=number, kind=kind)
    return TeamClipStatus(
        team_number=number, kind=kind, status=row.status, processed_s3_key=row.processed_s3_key, error=row.error
    )


@router.get(
    "/api/v1/teams/{number}/clips",
    response_model=TeamClipsRead,
    dependencies=[Depends(require_permission("teams:read"))],
)
async def get_team_clips(number: str, db: Annotated[AsyncSession, Depends(get_db)]) -> TeamClipsRead:
    rows = {
        r.kind: r for r in (await db.execute(select(TeamClip).where(TeamClip.team_number == number))).scalars()
    }
    return TeamClipsRead(team_number=number, clips={k: _status(number, k, rows.get(k)) for k in s3.CLIP_KINDS})


@router.post("/api/v1/teams/{number}/clips/{kind}", response_model=VideoUploadAccepted, status_code=202)
async def upload_team_clip(
    number: str,
    kind: str,
    background_tasks: BackgroundTasks,
    db: Annotated[AsyncSession, Depends(get_db)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("video:upload"))],
    file: UploadFile = File(...),
    key_colour: str = Form("#00B140"),
    similarity: float = Form(0.1),
    blend: float = Form(0.05),
) -> VideoUploadAccepted:
    _check_kind(kind)
    ext = (file.filename or "").rsplit(".", 1)[-1].lower() if file.filename and "." in file.filename else ""
    if ext not in ALLOWED_CLIP_EXTENSIONS:
        raise HTTPException(
            status_code=400, detail=f"Unsupported file extension {ext!r}; expected one of {sorted(ALLOWED_CLIP_EXTENSIONS)}"
        )

    tmp_dir = tempfile.mkdtemp(prefix=f"qecomp-clip-{number}-{kind}-")
    input_path = os.path.join(tmp_dir, f"raw.{ext}")
    written = 0
    try:
        async with aiofiles.open(input_path, "wb") as out:
            while chunk := await file.read(8 * 1024 * 1024):
                written += len(chunk)
                if written > MAX_CLIP_BYTES:
                    raise HTTPException(status_code=413, detail="Clip is too large")
                await out.write(chunk)
    except BaseException:
        _cleanup(input_path, None, tmp_dir)
        raise

    if await db.get(TeamProfile, number) is None:
        db.add(TeamProfile(team_number=number))
    row = await db.get(TeamClip, (number, kind))
    if row is None:
        row = TeamClip(team_number=number, kind=kind)
        db.add(row)
    row.status, row.error = "PROCESSING", None
    await db.commit()
    await log_action(db, principal.subject, "upload", "team_clip", f"{number}/{kind}", changes={"filename": file.filename})

    background_tasks.add_task(_process_and_upload_clip, number, kind, input_path, ext, key_colour, similarity, blend, tmp_dir)
    return VideoUploadAccepted(team_number=number, video_processing_status="PROCESSING")


def _cleanup(input_path: str, output_path: str | None, tmp_dir: str) -> None:
    for path in (input_path, output_path):
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError:
            pass
    try:
        os.rmdir(tmp_dir)
    except OSError:
        pass


async def _set_clip_state(number: str, kind: str, **fields: Any) -> None:
    from backend.core.db import async_session_factory

    async with async_session_factory() as session:
        row = await session.get(TeamClip, (number, kind))
        if row is not None:
            for key, value in fields.items():
                setattr(row, key, value)
            await session.commit()


async def _process_and_upload_clip(
    number: str, kind: str, input_path: str, ext: str, key_colour: str, similarity: float, blend: float, tmp_dir: str
) -> None:
    output_path = os.path.join(tmp_dir, "processed.webm")
    raw_key = s3.clip_raw_key(number, kind, ext)
    processed_key = s3.clip_processed_key(number, kind)
    try:
        raw_result, process_result = await asyncio.gather(
            s3.upload_multipart(input_path, raw_key),
            process_video(input_path, output_path, key_colour, similarity, blend, clip=True),
            return_exceptions=True,
        )
        if not isinstance(raw_result, BaseException):
            await _set_clip_state(number, kind, raw_s3_key=raw_key)
        for result in (process_result, raw_result):
            if isinstance(result, BaseException):
                raise result
        await s3.upload_multipart(output_path, processed_key)
        await _set_clip_state(number, kind, processed_s3_key=processed_key, status="DONE", error=None)
    except Exception as exc:
        logger.exception("Clip processing failed for %s/%s", number, kind)
        await _set_clip_state(number, kind, status="FAILED", error=_sanitize_processing_error(exc))
    finally:
        _cleanup(input_path, output_path, tmp_dir)


def _clean_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None and not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None


def _overlay_team(number: str, profile: TeamProfile | None, urls: dict[str, str | None]) -> OverlayTeam:
    stats: dict[str, Any] = (profile.cached_stats if profile and isinstance(profile.cached_stats, dict) else {}) or {}
    ranking = stats.get("ranking")
    location = stats.get("location")
    country = None
    if isinstance(location, str) and "," in location:
        country = location.rsplit(",", 1)[1].strip() or None
    return OverlayTeam(
        team_number=number,
        team_name=stats.get("team_name"),
        organisation=stats.get("organisation"),
        location=location if isinstance(location, str) else None,
        country=country,
        qual_rank=_clean_int(ranking.get("rank")) if isinstance(ranking, dict) else None,
        skills_rank=_clean_int(stats.get("skills_rank")),
        robot_name=profile.robot_name if profile else None,
        bot_url=urls.get("bot"),
        team_url=urls.get("team"),
    )


@router.get("/api/v1/public/overlay/teams", response_model=OverlayTeamsResponse)
async def public_overlay_teams(teams: str, db: Annotated[AsyncSession, Depends(get_db)]) -> OverlayTeamsResponse:
    """Display info + presigned clip URLs for `?teams=1234A,5678B` (max 12)."""
    numbers = list(dict.fromkeys(t.strip() for t in teams.split(",") if t.strip()))
    if len(numbers) > MAX_OVERLAY_TEAMS:
        raise HTTPException(status_code=422, detail=f"At most {MAX_OVERLAY_TEAMS} teams per request")
    profiles = {
        p.team_number: p for p in (await db.execute(select(TeamProfile).where(TeamProfile.team_number.in_(numbers)))).scalars()
    }
    clips: dict[tuple[str, str], TeamClip] = {
        (c.team_number, c.kind): c
        for c in (await db.execute(select(TeamClip).where(TeamClip.team_number.in_(numbers)))).scalars()
    }
    out: dict[str, OverlayTeam] = {}
    for number in numbers:
        urls: dict[str, str | None] = {}
        for kind in s3.CLIP_KINDS:
            clip = clips.get((number, kind))
            key = clip.processed_s3_key if clip and clip.status == "DONE" else None
            if key is None and kind == "bot":
                # Fall back to the legacy single "robot 360" upload.
                profile = profiles.get(number)
                if profile and profile.video_360_s3_key and profile.video_processing_status == "DONE":
                    key = profile.video_360_s3_key
            try:
                urls[kind] = await s3.generate_presigned_url(key) if key else None
            except ProcessingError:
                logger.exception("Failed to presign %s clip for team %s", kind, number)
                urls[kind] = None
        out[number] = _overlay_team(number, profiles.get(number), urls)
    return OverlayTeamsResponse(teams=out)
