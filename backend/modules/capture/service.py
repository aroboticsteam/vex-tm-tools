"""Recording / moment lifecycle (issues #42, #38, #45).

Shared by the REST routes, the `capture` integration (automations, button
pages) and the `/ws/agent` handler. Every function takes the Redis client and
a session factory so tests can inject fakes.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.core.exceptions import IntegrationError
from backend.models.capture import CaptureMoment, CaptureRecording
from backend.modules.capture import hub, keys
from backend.modules.media import s3
from backend.schemas.capture import MAX_MARK_SECONDS

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], Any]

UPLOAD_URL_TTL_S = 3600


class CaptureConflict(ValueError):
    """The request is valid but not possible in the current state (HTTP 409)."""


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def team_number(entry: Any) -> str:
    raw = entry.get("team_number") or entry.get("number") if isinstance(entry, dict) else entry
    return str(raw or "").strip().upper()


def parse_match(params: dict[str, Any]) -> dict[str, Any]:
    """Normalises the start request: a TM match tuple (as carried by a
    `matchStarted` event) or explicit fields, plus the scheduled teams."""
    tup = params.get("match") if isinstance(params.get("match"), dict) else {}
    round_ = tup.get("round", params.get("round"))
    instance = tup.get("instance", params.get("instance", 1))
    match = tup.get("match", params.get("match_num", params.get("matchNum")))
    if round_ is None or match is None:
        raise ValueError("A round and match number are required to start a recording")
    division = tup.get("division", params.get("division", 1))
    session = tup.get("session", params.get("session", 0))
    match_key = params.get("match_key") or f"{division}:{session}:{round_}:{instance}:{match}"
    red = [t for t in (team_number(x) for x in params.get("red_teams") or params.get("redTeams") or []) if t]
    blue = [t for t in (team_number(x) for x in params.get("blue_teams") or params.get("blueTeams") or []) if t]
    teams: list[str] = []
    for t in red + blue:
        if t not in teams:
            teams.append(t)
    return {
        "match_key": str(match_key)[:200],
        "match_type": keys.match_type_slug(round_),
        "match_number": keys.match_number_slug(round_, instance, match),
        "teams": teams,
        "alliances": {"red": red, "blue": blue},
    }


def recording_summary(row: CaptureRecording) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "match_key": row.match_key,
        "match_type": row.match_type,
        "match_number": row.match_number,
        "teams": list(row.teams or []),
        "status": row.status,
    }


def moment_summary(row: CaptureMoment, recording: CaptureRecording | None = None) -> dict[str, Any]:
    out = {
        "id": str(row.id),
        "recording_id": str(row.recording_id),
        "number": row.number,
        "length_s": row.length_s,
        "status": row.status,
        "ready_ms": row.ready_ms,
    }
    if recording is not None:
        out["match_key"] = recording.match_key
    return out


async def active_recording(session: AsyncSession) -> CaptureRecording | None:
    result = await session.execute(
        select(CaptureRecording)
        .where(CaptureRecording.status == "RECORDING")
        .order_by(CaptureRecording.started_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _drop_objects(keys_: list[str]) -> int:
    deleted = 0
    for key in keys_:
        try:
            await s3.delete_object(key)
            deleted += 1
        except Exception:
            logger.exception("Could not delete capture object %s", key)
    return deleted


async def start_recording(redis: Any, session_factory: SessionFactory, params: dict[str, Any]) -> dict[str, Any]:
    info = parse_match(params)
    if await hub.get_presence(redis) is None:
        raise IntegrationError("The capture agent is not connected")

    async with session_factory() as session:
        previous = await active_recording(session)
        if previous is not None and previous.match_key != info["match_key"]:
            previous.status = "STOPPED"
            previous.stopped_at = _now()
            await hub.send_command(redis, {"type": "stop_recording", "recording_id": str(previous.id)})
            await hub.publish(redis, "capture_recording", recording_summary(previous))

        row = (
            await session.execute(select(CaptureRecording).where(CaptureRecording.match_key == info["match_key"]))
        ).scalar_one_or_none()
        stale_keys: list[str] = []
        if row is None:
            row = CaptureRecording(match_key=info["match_key"])
            session.add(row)
        else:
            # A replayed match reuses its key: start over, dropping the earlier take.
            moment_keys = await session.execute(
                select(CaptureMoment.object_key).where(
                    CaptureMoment.recording_id == row.id, CaptureMoment.object_key.is_not(None)
                )
            )
            stale_keys.extend(k for (k,) in moment_keys.all())
            if row.object_key:
                stale_keys.append(row.object_key)
            await session.execute(delete(CaptureMoment).where(CaptureMoment.recording_id == row.id))
        row.match_type = info["match_type"]
        row.match_number = info["match_number"]
        row.teams = info["teams"]
        row.alliances = info["alliances"]
        row.status = "RECORDING"
        row.object_key = None
        row.started_at = _now()
        row.stopped_at = None
        row.duration_s = None
        row.size_bytes = None
        row.error = None
        await session.commit()
        await session.refresh(row)
        summary = recording_summary(row)
        recording_id = str(row.id)
        match_type, match_number = row.match_type, row.match_number

    if stale_keys:
        await _drop_objects(stale_keys)
    await hub.send_command(
        redis,
        {
            "type": "start_recording",
            "recording_id": recording_id,
            "match_key": info["match_key"],
            "match_type": match_type,
            "match_number": match_number,
        },
    )
    await hub.publish(redis, "capture_recording", summary)
    return {"ok": True, "recording": summary}


async def stop_recording(redis: Any, session_factory: SessionFactory) -> dict[str, Any]:
    """Stops the active recording. Idempotent: a stop with nothing recording is a no-op."""
    async with session_factory() as session:
        row = await active_recording(session)
        if row is None:
            return {"ok": True, "recording": None}
        row.status = "STOPPED"
        row.stopped_at = _now()
        await session.commit()
        await session.refresh(row)
        summary = recording_summary(row)
    await hub.send_command(redis, {"type": "stop_recording", "recording_id": summary["id"]})
    await hub.publish(redis, "capture_recording", summary)
    return {"ok": True, "recording": summary}


async def mark_moment(redis: Any, session_factory: SessionFactory, length_s: float) -> dict[str, Any]:
    if not 0 < length_s <= MAX_MARK_SECONDS:
        raise ValueError(f"length_s must be between 0 and {MAX_MARK_SECONDS} seconds")
    if await hub.get_presence(redis) is None:
        raise IntegrationError("The capture agent is not connected")
    async with session_factory() as session:
        recording = await active_recording(session)
        if recording is None:
            raise CaptureConflict("No recording is running, so there is nothing to mark")
        # Serialise concurrent marks on the same recording so numbers stay unique.
        await session.execute(
            select(CaptureRecording.id).where(CaptureRecording.id == recording.id).with_for_update()
        )
        next_number = (
            await session.execute(
                select(func.coalesce(func.max(CaptureMoment.number), 0) + 1).where(
                    CaptureMoment.recording_id == recording.id
                )
            )
        ).scalar_one()
        moment = CaptureMoment(recording_id=recording.id, number=next_number, length_s=float(length_s))
        session.add(moment)
        await session.commit()
        await session.refresh(moment)
        summary = moment_summary(moment, recording)
        command = {
            "type": "mark",
            "recording_id": str(recording.id),
            "moment_id": str(moment.id),
            "number": moment.number,
            "length_s": moment.length_s,
        }
    await hub.send_command(redis, command)
    await hub.publish(redis, "capture_moment", summary)
    return {"ok": True, "moment": summary}


async def handle_agent_message(redis: Any, session_factory: SessionFactory, msg: dict[str, Any]) -> None:
    """Applies a status report from the agent (`recording_stopped`, `moment_cut`, `error`)."""
    kind = msg.get("type")
    async with session_factory() as session:
        if kind == "recording_stopped":
            row = await _recording(session, msg.get("recording_id"))
            if row is None:
                return
            if row.status == "RECORDING":
                row.status = "STOPPED"
                row.stopped_at = row.stopped_at or _now()
            if isinstance(msg.get("duration_s"), (int, float)):
                row.duration_s = float(msg["duration_s"])
            await session.commit()
            await session.refresh(row)
            await hub.publish(redis, "capture_recording", recording_summary(row))
        elif kind == "moment_cut":
            moment = await _moment(session, msg.get("moment_id"))
            if moment is None:
                return
            if moment.status == "REQUESTED":
                moment.status = "CUT"
            if isinstance(msg.get("end_offset_ms"), int):
                moment.end_offset_ms = msg["end_offset_ms"]
            await session.commit()
            await session.refresh(moment)
            await hub.publish(redis, "capture_moment", moment_summary(moment))
        elif kind == "error":
            detail = str(msg.get("detail") or "Unknown capture error")[:500]
            moment = await _moment(session, msg.get("moment_id")) if msg.get("moment_id") else None
            if moment is not None:
                moment.status, moment.error = "ERROR", detail
                await session.commit()
                await session.refresh(moment)
                await hub.publish(redis, "capture_moment", moment_summary(moment))
                return
            row = await _recording(session, msg.get("recording_id")) if msg.get("recording_id") else None
            if row is not None:
                row.status, row.error = "ERROR", detail
                await session.commit()
                await session.refresh(row)
                await hub.publish(redis, "capture_recording", recording_summary(row))


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


async def _recording(session: AsyncSession, raw_id: Any) -> CaptureRecording | None:
    rid = _uuid(raw_id)
    return await session.get(CaptureRecording, rid) if rid else None


async def _moment(session: AsyncSession, raw_id: Any) -> CaptureMoment | None:
    mid = _uuid(raw_id)
    return await session.get(CaptureMoment, mid) if mid else None


async def request_upload(
    session_factory: SessionFactory, kind: str, recording_id: uuid.UUID, moment_id: uuid.UUID | None
) -> dict[str, Any]:
    async with session_factory() as session:
        recording = await session.get(CaptureRecording, recording_id)
        if recording is None:
            raise LookupError("Unknown recording")
        if kind == "moment":
            moment = await session.get(CaptureMoment, moment_id) if moment_id else None
            if moment is None or moment.recording_id != recording.id:
                raise LookupError("Unknown moment for this recording")
            key = keys.moment_key(recording.match_type, recording.match_number, moment.number)
            upload_id = moment.id
        else:
            key = keys.recording_key(recording.match_type, recording.match_number)
            upload_id = recording.id
    content_type = "video/mp4"
    url = await s3.generate_presigned_put_url(key, expires_in=UPLOAD_URL_TTL_S, content_type=content_type)
    return {
        "upload_id": upload_id,
        "kind": kind,
        "url": url,
        "method": "PUT",
        "headers": {"Content-Type": content_type},
        "object_key": key,
        "expires_in": UPLOAD_URL_TTL_S,
    }


async def complete_upload(
    redis: Any,
    session_factory: SessionFactory,
    upload_id: uuid.UUID,
    kind: str,
    ready_ms: int | None,
    duration_s: float | None,
) -> dict[str, Any]:
    """Confirms the object is in the store, then marks the recording/moment READY."""
    async with session_factory() as session:
        if kind == "moment":
            moment = await session.get(CaptureMoment, upload_id)
            if moment is None:
                raise LookupError("Unknown moment")
            recording = await session.get(CaptureRecording, moment.recording_id)
            key = keys.moment_key(recording.match_type, recording.match_number, moment.number)
            size = await s3.object_size(key)
            if size is None:
                raise CaptureConflict("The uploaded file is not in the object store")
            moment.status, moment.object_key, moment.error = "READY", key, None
            if ready_ms is not None:
                moment.ready_ms = ready_ms
            await session.commit()
            await session.refresh(moment)
            summary = moment_summary(moment, recording)
            await hub.publish(redis, "capture_moment", summary)
            return {"ok": True, "moment": summary}
        recording = await session.get(CaptureRecording, upload_id)
        if recording is None:
            raise LookupError("Unknown recording")
        key = keys.recording_key(recording.match_type, recording.match_number)
        size = await s3.object_size(key)
        if size is None:
            raise CaptureConflict("The uploaded file is not in the object store")
        recording.object_key, recording.size_bytes, recording.error = key, size, None
        if recording.status != "RECORDING":
            recording.status = "READY"
        if duration_s is not None:
            recording.duration_s = duration_s
        await session.commit()
        await session.refresh(recording)
        summary = recording_summary(recording)
        await hub.publish(redis, "capture_recording", summary)
        return {"ok": True, "recording": summary}


async def wipe(session_factory: SessionFactory, match_type: str | None = None) -> dict[str, int]:
    """Deletes recordings (all, or one match type) and their stored files, for clearing between events."""
    async with session_factory() as session:
        stmt = select(CaptureRecording).options(selectinload(CaptureRecording.moments))
        if match_type:
            stmt = stmt.where(CaptureRecording.match_type == keys.match_type_slug(match_type))
        rows = list((await session.execute(stmt)).scalars().all())
        object_keys: list[str] = []
        for row in rows:
            if row.object_key:
                object_keys.append(row.object_key)
            object_keys.extend(m.object_key for m in row.moments if m.object_key)
            await session.delete(row)
        await session.commit()
    return {"recordings": len(rows), "objects_deleted": await _drop_objects(object_keys)}
