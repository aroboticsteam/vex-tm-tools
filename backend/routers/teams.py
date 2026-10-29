"""Teams API (plan §11 Teams section / §5.13 Video Upload pipeline).

Per §C.2, routers are thin — this module only orchestrates
`backend.modules.media.processor`/`s3` and the `TeamProfile` ORM model. It
does not contain FFmpeg/S3 logic itself.

── Note on background video processing (coordination note) ────────────────
§C.5 rule 6 says routers must be stateless and must not spawn background
asyncio tasks ("use the pre-started managers in main.py instead"). However,
the plan's own §5.13 pipeline explicitly calls for
`POST /api/v1/teams/<number>/video` to "trigger process_video + upload as a
background task" — video processing is a slow (FFmpeg + S3), per-request,
idempotent operation with its progress tracked entirely in Postgres
(`video_processing_status`), not the kind of persistent cross-pod state rule
6 is protecting against (that's aimed at things like the automation engine).
This router resolves the tension by using FastAPI's own `BackgroundTasks`
primitive (not a bare `asyncio.create_task()` orphaned after the response) —
the sanctioned, framework-native mechanism for deferred post-response work.
Flagging this explicitly for the coordinator in case a different resolution
(e.g. a dedicated video-processing worker managed from `main.py`) is
preferred once Wave 3/4 wire up the rest of the router layer.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from typing import Annotated

import aiofiles
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.audit import log_action
from backend.core.dependencies import CurrentPrincipal, get_db, require_permission
from backend.core.exceptions import ProcessingError
from backend.models.team import TeamProfile
from backend.modules.media import images as site_images
from backend.modules.media import s3
from backend.modules.media.processor import process_video
from backend.schemas.team import (
    BatchVideoUrlsResponse,
    TeamProfileRead,
    TeamProfileUpdate,
    VideoProcessingStatus,
    VideoUploadAccepted,
)

logger = logging.getLogger(__name__)

_MAX_PROCESSING_ERROR_LEN = 300


def _sanitize_processing_error(exc: BaseException) -> str:
    """Reduces an exception to a short, single-line, caller-safe message.

    The full exception (e.g. `botocore.errorfactory.NoSuchBucket`) is always
    logged server-side with `logger.exception(...)`; only this condensed
    form is ever persisted to `video_processing_error` / returned from
    `/video/status`, so multi-line tracebacks and local filesystem paths
    (e.g. the `tempfile.mkdtemp()` dir used during processing) never reach
    the API response.
    """
    message = str(exc).strip() or type(exc).__name__
    message = message.splitlines()[0].strip()
    if len(message) > _MAX_PROCESSING_ERROR_LEN:
        message = message[: _MAX_PROCESSING_ERROR_LEN - 3] + "..."
    return message

router = APIRouter(prefix="/api/v1/teams", tags=["teams"])

_ALLOWED_VIDEO_EXTENSIONS = {"mp4", "mov"}


@router.get("", response_model=list[TeamProfileRead], dependencies=[Depends(require_permission("teams:read"))])
async def list_teams(db: Annotated[AsyncSession, Depends(get_db)]) -> list[TeamProfile]:
    result = await db.execute(select(TeamProfile).order_by(TeamProfile.team_number))
    return list(result.scalars())


@router.get(
    "/batch/videos",
    response_model=BatchVideoUrlsResponse,
    dependencies=[Depends(require_permission("teams:read"))],
)
async def batch_video_urls(
    teams: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BatchVideoUrlsResponse:
    """Batch presigned processed-video URLs for overlay use (plan §5.14).

    `teams` is a comma-separated list of team numbers, e.g.
    `?teams=1234A,5678B,9101C,1121D`.
    """
    numbers = [t.strip() for t in teams.split(",") if t.strip()]
    urls: dict[str, str | None] = {}
    if numbers:
        result = await db.execute(select(TeamProfile).where(TeamProfile.team_number.in_(numbers)))
        rows_by_number = {row.team_number: row for row in result.scalars()}
        for number in numbers:
            row = rows_by_number.get(number)
            if row is None or not row.video_360_s3_key or row.video_processing_status != "DONE":
                urls[number] = None
                continue
            try:
                urls[number] = await s3.generate_presigned_url(row.video_360_s3_key)
            except ProcessingError:
                logger.exception("Failed to presign video URL for team %s", number)
                urls[number] = None
    return BatchVideoUrlsResponse(videos=urls)


@router.get(
    "/{number}",
    response_model=TeamProfileRead,
    dependencies=[Depends(require_permission("teams:read"))],
)
async def get_team(number: str, db: Annotated[AsyncSession, Depends(get_db)]) -> TeamProfile:
    team = await db.get(TeamProfile, number)
    if team is None:
        raise HTTPException(status_code=404, detail=f"Team {number!r} not found")
    return team


@router.put(
    "/{number}",
    response_model=TeamProfileRead,
)
async def update_team(
    number: str,
    body: TeamProfileUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("teams:edit"))],
) -> TeamProfile:
    team = await db.get(TeamProfile, number)
    if team is None:
        raise HTTPException(status_code=404, detail=f"Team {number!r} not found")

    changed_fields: dict[str, object] = {}
    if body.pit_location is not None:
        team.pit_location = body.pit_location
        changed_fields["pit_location"] = body.pit_location
    if body.extra_notes is not None:
        team.extra_notes = body.extra_notes
        changed_fields["extra_notes"] = body.extra_notes
    if body.website_url is not None:
        team.website_url = body.website_url or None
        changed_fields["website_url"] = team.website_url

    await db.commit()
    await db.refresh(team)
    await log_action(db, principal.subject, "update", "team", number, changes=changed_fields)
    return team


@router.post("/{number}/logo", response_model=TeamProfileRead)
async def upload_team_logo(
    number: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("teams:edit"))],
    file: UploadFile = File(...),
) -> TeamProfile:
    """Public-page logo (PNG/JPEG/GIF/WebP, max 4 MB, content-checked)."""
    team = await db.get(TeamProfile, number)
    if team is None:
        raise HTTPException(status_code=404, detail=f"Team {number!r} not found")
    data, ext = await site_images.read_validated_image(file)
    key = s3.team_logo_key(number, ext)
    try:
        await site_images.store_image(data, key)
    except ProcessingError as exc:
        raise HTTPException(status_code=502, detail=_sanitize_processing_error(exc)) from exc
    if team.logo_s3_key and team.logo_s3_key != key:
        try:
            await s3.delete_object(team.logo_s3_key)
        except Exception:
            logger.warning("Could not delete previous logo %s", team.logo_s3_key, exc_info=True)
    team.logo_s3_key = key
    await db.commit()
    await db.refresh(team)
    await log_action(db, principal.subject, "upload", "team_logo", number, changes={"key": key})
    return team


@router.delete("/{number}/logo", response_model=TeamProfileRead)
async def delete_team_logo(
    number: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("teams:edit"))],
) -> TeamProfile:
    team = await db.get(TeamProfile, number)
    if team is None:
        raise HTTPException(status_code=404, detail=f"Team {number!r} not found")
    if team.logo_s3_key:
        try:
            await s3.delete_object(team.logo_s3_key)
        except Exception:
            logger.warning("Could not delete logo %s", team.logo_s3_key, exc_info=True)
        team.logo_s3_key = None
        await db.commit()
        await db.refresh(team)
        await log_action(db, principal.subject, "delete", "team_logo", number)
    return team


@router.get(
    "/{number}/video/status",
    response_model=VideoProcessingStatus,
    dependencies=[Depends(require_permission("teams:read"))],
)
async def get_video_status(number: str, db: Annotated[AsyncSession, Depends(get_db)]) -> VideoProcessingStatus:
    team = await db.get(TeamProfile, number)
    if team is None:
        raise HTTPException(status_code=404, detail=f"Team {number!r} not found")
    return VideoProcessingStatus(
        team_number=number,
        video_processing_status=team.video_processing_status,
        video_360_s3_key=team.video_360_s3_key,
        error=team.video_processing_error,
    )


@router.post(
    "/{number}/video",
    response_model=VideoUploadAccepted,
    status_code=202,
)
async def upload_team_video(
    number: str,
    background_tasks: BackgroundTasks,
    db: Annotated[AsyncSession, Depends(get_db)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("video:upload"))],
    file: UploadFile = File(...),
    key_colour: str = Form("#00B140"),
    similarity: float = Form(0.1),
    blend: float = Form(0.05),
) -> VideoUploadAccepted:
    ext = (file.filename or "").rsplit(".", 1)[-1].lower() if file.filename and "." in file.filename else ""
    if ext not in _ALLOWED_VIDEO_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file extension {ext!r}; expected one of {_ALLOWED_VIDEO_EXTENSIONS}",
        )

    team = await db.get(TeamProfile, number)
    if team is None:
        team = TeamProfile(team_number=number)
        db.add(team)

    tmp_dir = tempfile.mkdtemp(prefix=f"qecomp-video-{number}-")
    input_path = os.path.join(tmp_dir, f"raw.{ext}")
    async with aiofiles.open(input_path, "wb") as out:
        while chunk := await file.read(8 * 1024 * 1024):
            await out.write(chunk)

    team.video_processing_status = "PROCESSING"
    team.video_processing_error = None
    await db.commit()

    await log_action(
        db,
        principal.subject,
        "upload",
        "team_video",
        number,
        changes={"filename": file.filename, "key_colour": key_colour, "similarity": similarity, "blend": blend},
    )

    background_tasks.add_task(
        _process_and_upload_video,
        team_number=number,
        input_path=input_path,
        ext=ext,
        key_colour=key_colour,
        similarity=similarity,
        blend=blend,
        tmp_dir=tmp_dir,
    )

    return VideoUploadAccepted(team_number=number, video_processing_status="PROCESSING")


async def _process_and_upload_video(
    team_number: str,
    input_path: str,
    ext: str,
    key_colour: str,
    similarity: float,
    blend: float,
    tmp_dir: str,
) -> None:
    """Background task body: FFmpeg key + multipart-upload raw & processed
    videos to S3, then update `team_profiles` with the result.

    Runs after the HTTP response has already been sent (via FastAPI
    `BackgroundTasks` — see module docstring), on its own DB session since
    the request-scoped session from `get_db()` is closed by then.
    """
    from backend.core.db import async_session_factory

    output_path = os.path.join(tmp_dir, "processed.webm")
    raw_key = s3.raw_video_key(team_number, ext)
    processed_key = s3.processed_video_key(team_number)

    try:
        # Raw retention (permanent, per Appendix A.5) and FFmpeg keying both
        # only read `input_path` and don't depend on each other's result, so
        # run them concurrently rather than serializing two potentially
        # slow operations.
        raw_result, process_result = await asyncio.gather(
            s3.upload_multipart(input_path, raw_key),
            process_video(input_path, output_path, key_colour, similarity, blend),
            return_exceptions=True,
        )

        raw_upload_ok = not isinstance(raw_result, BaseException)
        if raw_upload_ok:
            # Persist the raw key as soon as it's known to be in S3,
            # independent of whether processing itself succeeds — otherwise
            # a processing failure would orphan an uploaded raw video with
            # no DB reference to it (defeating "Re-process" per Appendix A.5).
            async with async_session_factory() as session:
                team = await session.get(TeamProfile, team_number)
                if team is not None:
                    team.raw_video_s3_key = raw_key
                    await session.commit()
        else:
            logger.exception("Raw video upload failed for team %s", team_number, exc_info=raw_result)

        if isinstance(process_result, BaseException):
            logger.exception(
                "FFmpeg processing failed for team %s", team_number, exc_info=process_result
            )
            raise process_result
        if not raw_upload_ok:
            raise raw_result

        await s3.upload_multipart(output_path, processed_key)

        async with async_session_factory() as session:
            team = await session.get(TeamProfile, team_number)
            if team is not None:
                team.video_360_s3_key = processed_key
                team.video_processing_status = "DONE"
                await session.commit()
    except Exception as exc:
        logger.exception("Video processing failed for team %s", team_number)
        async with async_session_factory() as session:
            team = await session.get(TeamProfile, team_number)
            if team is not None:
                team.video_processing_status = "FAILED"
                team.video_processing_error = _sanitize_processing_error(exc)
                await session.commit()
    finally:
        for path in (input_path, output_path):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass
