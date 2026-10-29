"""Staff tools behind the public site (issue #28): uploaded site images
(event map, sponsor logos) and links from completed matches to recordings.

Image uploads and recording links need `settings:edit` (they change what the
public site shows); listing needs `settings:read`.
"""
from __future__ import annotations

import logging
import re
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.audit import log_action
from backend.core.dependencies import CurrentPrincipal, get_db, get_redis, require_permission
from backend.core.exceptions import ProcessingError
from backend.models.match_recording import MatchRecording
from backend.modules.media import images, s3
from backend.modules.notifications.logic import match_label
from backend.routers.public_site import _schedules, safe_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["site-admin"])


class ImageUploaded(BaseModel):
    #: Store this in the site config (`image_key` / `logo_key`).
    key: str
    url: str | None = None


@router.post("/site/images", response_model=ImageUploaded, status_code=201)
async def upload_site_image(
    db: Annotated[AsyncSession, Depends(get_db)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("settings:edit"))],
    file: UploadFile = File(...),
) -> ImageUploaded:
    data, ext = await images.read_validated_image(file)
    name = f"{uuid.uuid4().hex}.{ext}"
    try:
        await images.store_image(data, s3.site_image_key(name))
    except ProcessingError as exc:
        raise HTTPException(status_code=502, detail="Could not store the image") from exc
    await log_action(db, principal.subject, "upload", "site_image", name, changes={"bytes": len(data)})
    try:
        url = await s3.generate_presigned_url(s3.site_image_key(name), expires_in=3600)
    except Exception:
        url = None
    return ImageUploaded(key=name, url=url)


class RecordingIn(BaseModel):
    division: str = Field(..., min_length=1, max_length=20)
    label: str = Field(..., min_length=1, max_length=40)
    url: str = Field(..., max_length=500)
    title: str | None = Field(default=None, max_length=100)

    @field_validator("url")
    @classmethod
    def _http(cls, value: str) -> str:
        if safe_url(value) is None:
            raise ValueError("url must be an http:// or https:// link")
        return value.strip()


class RecordingRead(BaseModel):
    match_key: str
    division: str
    label: str
    url: str
    title: str | None = None


class RecordingImport(BaseModel):
    text: str = Field(..., min_length=1, max_length=50000)


class RecordingImportResult(BaseModel):
    saved: int
    errors: list[dict[str, Any]] = Field(default_factory=list)


def _read(row: MatchRecording) -> RecordingRead:
    parts = row.match_key.split(":")
    division, label = parts[0], row.match_key
    if len(parts) == 5:
        label = match_label(parts[2], parts[3], parts[4])
    return RecordingRead(match_key=row.match_key, division=division, label=label, url=row.url, title=row.title)


async def _resolve_key(redis: Any, division: str, label: str) -> str | None:
    wanted = label.strip().lower()
    for div, matches in (await _schedules(redis)).items():
        if str(div) != division.strip():
            continue
        for m in matches:
            if m.label.lower() == wanted:
                return m.key
    return None


async def _upsert(db: AsyncSession, key: str, url: str, title: str | None) -> None:
    stmt = pg_insert(MatchRecording).values(match_key=key, url=url, title=title)
    await db.execute(stmt.on_conflict_do_update(index_elements=["match_key"], set_={"url": url, "title": title}))


@router.get(
    "/match-recordings", response_model=list[RecordingRead], dependencies=[Depends(require_permission("settings:read"))]
)
async def list_recordings(db: Annotated[AsyncSession, Depends(get_db)]) -> list[RecordingRead]:
    rows = (await db.execute(select(MatchRecording).order_by(MatchRecording.match_key))).scalars()
    return [_read(r) for r in rows]


@router.put("/match-recordings", response_model=RecordingRead)
async def set_recording(
    body: RecordingIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Any, Depends(get_redis)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("settings:edit"))],
) -> RecordingRead:
    """Links a match (by division + label such as `Q12`) to its recording."""
    key = await _resolve_key(redis, body.division, body.label)
    if key is None:
        raise HTTPException(status_code=404, detail=f"No match {body.label!r} in division {body.division!r} of the cached schedule")
    await _upsert(db, key, body.url, body.title)
    await db.commit()
    await log_action(db, principal.subject, "set", "match_recording", key, changes={"url": body.url})
    return _read(MatchRecording(match_key=key, url=body.url, title=body.title))


_IMPORT_LINE = re.compile(r"^\s*(\S+)\s+(\S+(?:\s+\d+-\d+)?)\s+(https?://\S+)\s*(.*?)\s*$", re.IGNORECASE)


@router.post("/match-recordings/import", response_model=RecordingImportResult)
async def import_recordings(
    body: RecordingImport,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Any, Depends(get_redis)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("settings:edit"))],
) -> RecordingImportResult:
    """Bulk links, one per line: `<division> <label> <url> [title]`, e.g.
    `1 Q12 https://youtu.be/abc?t=95 Qualification 12`. All-or-nothing."""
    parsed: list[tuple[str, str, str | None]] = []
    errors: list[dict[str, Any]] = []
    for number, raw in enumerate(body.text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _IMPORT_LINE.match(line)
        if match is None:
            errors.append({"line": number, "error": "Expected '<division> <label> <url> [title]'"})
            continue
        division, label, url, title = match.groups()
        key = await _resolve_key(redis, division, label)
        if key is None:
            errors.append({"line": number, "error": f"No match {label!r} in division {division!r}"})
        else:
            parsed.append((key, url, title[:100] or None))
    if errors or not parsed:
        return RecordingImportResult(saved=0, errors=errors or [{"line": 1, "error": "No recording lines found"}])
    for key, url, title in parsed:
        await _upsert(db, key, url, title)
    await db.commit()
    await log_action(db, principal.subject, "import", "match_recording", "*", changes={"count": len(parsed)})
    return RecordingImportResult(saved=len(parsed))


@router.delete("/match-recordings/{match_key}", status_code=204, response_model=None)
async def delete_recording(
    match_key: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("settings:edit"))],
) -> Response:
    result = await db.execute(delete(MatchRecording).where(MatchRecording.match_key == match_key))
    await db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Recording not found")
    await log_action(db, principal.subject, "delete", "match_recording", match_key)
    return Response(status_code=204)
