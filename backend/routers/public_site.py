"""Public, unauthenticated team-facing website data (issue #10).

Everything here is display-safe: no staff notes, no credentials. The site's
own configuration (next competition, sponsors, map, title) lives in the
`site` system setting, edited from Settings.

Event data (matches) comes from the cached TM schedule; rankings, skills and
team info from the scraper's `team_profiles.cached_stats`.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import date
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.dependencies import get_db, get_redis
from backend.models.comms import AgendaItem
from backend.models.capture import CaptureRecording
from backend.models.match_recording import MatchRecording
from backend.models.room import Room
from backend.models.settings import SystemSetting
from backend.models.team import TeamProfile
from backend.models.team_clip import TeamClip
from backend.modules.media import s3
from backend.modules.notifications.logic import ScheduledMatch, match_key, parse_schedule
from backend.modules.site import leaderboards as lb
from backend.modules.site.checklist import checklist_for
from backend.routers.team_clips import clip_urls_for

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/public/site", tags=["public-site"])

SITE_SETTING_KEY = "site"
SCHEDULE_KEY_PATTERN = "qecomp:tm:*:schedule"
MAX_MATCHES = 400
IMAGE_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(png|jpg|gif|webp)$")
_IMAGE_URL_TTL = 6 * 3600
DEFAULT_PANEL_SECONDS = 60
DEFAULT_PROGRAMME_SECONDS = 240


def _stats(profile: TeamProfile) -> dict[str, Any]:
    return profile.cached_stats if isinstance(profile.cached_stats, dict) else {}


def _num(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _skills_entry(stats: dict[str, Any]) -> dict[str, Any] | None:
    entries = [e for e in (stats.get("skills") or []) if isinstance(e, dict)]
    return min(entries, key=lambda e: _num(e.get("rank")) or 10**9) if entries else None


def team_row(profile: TeamProfile) -> dict[str, Any]:
    stats = _stats(profile)
    ranking = stats.get("ranking") if isinstance(stats.get("ranking"), dict) else {}
    skills = _skills_entry(stats) or {}
    return {
        "team_number": profile.team_number,
        "team_name": stats.get("team_name"),
        "organisation": stats.get("organisation"),
        "location": stats.get("location"),
        "country": stats.get("country"),
        "division": stats.get("division_id"),
        "pit_location": profile.pit_location,
        "robot_name": profile.robot_name,
        "qual_rank": _num(ranking.get("rank")),
        "skills_rank": _num(skills.get("rank")) or _num(stats.get("skills_rank")),
    }


async def _site_config(db: AsyncSession) -> dict[str, Any]:
    row = await db.get(SystemSetting, SITE_SETTING_KEY)
    return row.value if row and isinstance(row.value, dict) else {}


def safe_url(value: Any) -> str | None:
    """Only plain http(s) links reach the public page."""
    if isinstance(value, str) and re.match(r"^https?://[^\s]+$", value.strip(), re.IGNORECASE):
        return value.strip()
    return None


def safe_image_url(value: Any) -> str | None:
    """An http(s) link or a root-relative path (an image served by this site)."""
    if isinstance(value, str) and re.match(r"^/(?!/)[^\s]*$", value.strip()):
        return value.strip()
    return safe_url(value)


def _text(value: Any, limit: int = 300) -> str | None:
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


async def _image_url(key: Any) -> str | None:
    """Presigned URL for an uploaded site image (config stores only its name)."""
    if not isinstance(key, str) or not IMAGE_NAME_RE.match(key):
        return None
    try:
        return await s3.generate_presigned_url(s3.site_image_key(key), expires_in=_IMAGE_URL_TTL)
    except Exception:
        logger.warning("Could not presign site image %s", key, exc_info=True)
        return None


def _days_until(value: Any) -> int | None:
    try:
        return (date.fromisoformat(str(value)) - date.today()).days
    except ValueError:
        return None


async def _public_next_competition(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not _text(raw.get("name")):
        return None
    return {
        "name": _text(raw.get("name")),
        "date": _text(raw.get("date"), 10),
        "days_until": _days_until(raw.get("date")),
        "location": _text(raw.get("location")),
        "description": _text(raw.get("description"), 1000),
        "url": safe_url(raw.get("url")),
        "registration_url": safe_url(raw.get("registration_url")),
        "teams": [str(t)[:20] for t in (raw.get("teams") or [])[:200]] if isinstance(raw.get("teams"), list) else [],
    }


async def _public_sponsors(raw: Any) -> list[dict[str, Any]]:
    out = []
    for item in (raw if isinstance(raw, list) else [])[:30]:
        if not isinstance(item, dict):
            continue
        logo = await _image_url(item.get("logo_key")) or safe_image_url(item.get("logo_url"))
        out.append({"name": _text(item.get("name"), 100), "url": safe_url(item.get("url")), "logo_url": logo})
    return out


async def _public_map(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    image = await _image_url(raw.get("image_key")) or safe_image_url(raw.get("image_url"))
    if not image:
        return None
    rooms = []
    for r in (raw.get("rooms") or [])[:100] if isinstance(raw.get("rooms"), list) else []:
        if isinstance(r, dict) and _text(r.get("name"), 100) and _num(r.get("x")) is not None and _num(r.get("y")) is not None:
            rooms.append(
                {
                    "name": _text(r.get("name"), 100),
                    "x": max(0, min(100, r["x"])),
                    "y": max(0, min(100, r["y"])),
                    "description": _text(r.get("description"), 500),
                    "teams": [str(t)[:20] for t in (r.get("teams") or [])[:100]] if isinstance(r.get("teams"), list) else [],
                }
            )
    return {"image_url": image, "rooms": rooms}


def _public_bus_tracker(raw: Any) -> dict[str, Any] | None:
    """Embedded tracker page and/or a manual status board of buses."""
    if not isinstance(raw, dict):
        return None
    buses = [
        {"name": _text(b.get("name"), 60), "status": _text(b.get("status"), 100), "eta": _text(b.get("eta"), 40)}
        for b in (raw.get("buses") or [])[:20]
        if isinstance(b, dict) and _text(b.get("name"), 60)
    ] if isinstance(raw.get("buses"), list) else []
    tracker = {
        "title": _text(raw.get("title"), 80) or "Bus tracker",
        "embed_url": safe_url(raw.get("embed_url")),
        "link_url": safe_url(raw.get("link_url")),
        "note": _text(raw.get("note"), 500),
        "buses": buses,
    }
    return tracker if tracker["embed_url"] or tracker["link_url"] or buses or tracker["note"] else None


def _leaderboard_timing(config: dict[str, Any]) -> dict[str, int]:
    raw = config.get("leaderboard") if isinstance(config.get("leaderboard"), dict) else {}

    def clamp(name: str, default: int) -> int:
        value = raw.get(name)
        return max(5, min(int(value), 3600)) if isinstance(value, (int, float)) and not isinstance(value, bool) else default

    return {
        "panel_seconds": clamp("panel_seconds", DEFAULT_PANEL_SECONDS),
        "programme_seconds": clamp("programme_seconds", DEFAULT_PROGRAMME_SECONDS),
    }


async def _recordings(db: AsyncSession) -> dict[str, str]:
    """Match key -> video link: videos captured by the agent (issue #42), with
    an explicitly imported link taking precedence."""
    captured = (
        await db.execute(
            select(CaptureRecording.match_key, CaptureRecording.id).where(
                CaptureRecording.status == "READY", CaptureRecording.object_key.is_not(None)
            )
        )
    ).all()
    out = {key: f"/api/v1/public/recordings/{rid}/video" for key, rid in captured}
    rows = (await db.execute(select(MatchRecording.match_key, MatchRecording.url))).all()
    out.update({key: url for key, url in rows})
    return out


def _recording_url(
    division: str, m: ScheduledMatch, recordings: dict[str, str], template: str | None
) -> str | None:
    """An explicit link wins; otherwise a scored match gets the site's URL
    template (`{division}`, `{label}`, `{round}`, `{instance}`, `{match}`)."""
    explicit = recordings.get(m.key)
    if explicit:
        return explicit
    if template and m.scored:
        filled = template
        for name, value in (
            ("division", division),
            ("label", m.label),
            ("round", m.round),
            ("instance", m.instance),
            ("match", m.match_num),
        ):
            filled = filled.replace("{" + name + "}", quote(str(value), safe=""))
        return safe_url(filled)
    return None


def _recording_template(config: dict[str, Any]) -> str | None:
    value = config.get("recording_url_template")
    return value if isinstance(value, str) and value.strip().lower().startswith(("http://", "https://")) else None


async def _schedules(redis: Any) -> dict[str, list[ScheduledMatch]]:
    out: dict[str, list[ScheduledMatch]] = {}
    try:
        async for key in redis.scan_iter(match=SCHEDULE_KEY_PATTERN):
            raw = await redis.get(key)
            if raw:
                for division, matches in parse_schedule(json.loads(raw)).items():
                    out.setdefault(division, []).extend(matches)
    except Exception:
        logger.warning("Could not read cached schedules", exc_info=True)
    return out


def _programme(matches: list[ScheduledMatch]) -> str | None:
    biggest = max((len(m.teams) for m in matches), default=0)
    return "iq" if biggest == 2 else "vrc" if biggest >= 4 else None


def _match_json(
    division: str, m: ScheduledMatch, recordings: dict[str, str] | None = None, template: str | None = None
) -> dict[str, Any]:
    return {
        "recording_url": _recording_url(division, m, recordings or {}, template),
        "division": division,
        "label": m.label,
        "state": m.state,
        "scheduled_at": m.time_scheduled,
        "red": list(m.red),
        "blue": list(m.blue),
        "score": list(m.final_score) if m.final_score else None,
        "scored": m.scored,
    }


@router.get("/overview")
async def overview(
    db: Annotated[AsyncSession, Depends(get_db)], redis: Annotated[Any, Depends(get_redis)]
) -> dict[str, Any]:
    profiles = list((await db.execute(select(TeamProfile))).scalars())
    schedules = await _schedules(redis)
    event_name = next(
        (e["name"] for p in profiles if isinstance((e := _stats(p).get("event")), dict) and e.get("name")), None
    )
    config = await _site_config(db)
    programmes = sorted({pr for ms in schedules.values() if (pr := _programme(ms))}, key=lambda k: (k != "iq", k))
    return {
        "title": config.get("title") or event_name or "Competition",
        "event": event_name,
        "stats": {
            "teams": len(profiles),
            "matches_played": sum(1 for ms in schedules.values() for m in ms if m.scored),
            "matches_total": sum(len(ms) for ms in schedules.values()),
            "awards": sum(len(_stats(p).get("awards") or []) for p in profiles),
        },
        "highlights": lb.season_highlights(profiles, programmes),
        "next_competition": await _public_next_competition(config.get("next_competition")),
        "sponsors": await _public_sponsors(config.get("sponsors")),
        "map": await _public_map(config.get("map")),
        "presented_by": _text(config.get("presented_by"), 200),
        "bus_tracker": _public_bus_tracker(config.get("bus_tracker")),
    }


@router.get("/leaderboards")
async def leaderboards(
    db: Annotated[AsyncSession, Depends(get_db)], redis: Annotated[Any, Depends(get_redis)], limit: int = lb.DEFAULT_LIMIT
) -> dict[str, Any]:
    """Overall / tournament / skills / awards leaderboards per programme, with
    the rotation timing (`panel_seconds` per panel, `programme_seconds` before
    switching IQ <-> VRC) that the `/site/leaderboard` display page follows."""
    profiles = list((await db.execute(select(TeamProfile))).scalars())
    schedules = await _schedules(redis)
    division_programme = {d: _programme(ms) for d, ms in schedules.items()}
    config = await _site_config(db)
    return {
        "groups": lb.compute_leaderboards(profiles, division_programme, limit),
        **_leaderboard_timing(config),
        "server_time": time.time(),
    }


@router.get("/event")
async def event(
    db: Annotated[AsyncSession, Depends(get_db)], redis: Annotated[Any, Depends(get_redis)]
) -> dict[str, Any]:
    profiles = list((await db.execute(select(TeamProfile))).scalars())
    schedules = await _schedules(redis)
    names = {p.team_number: _stats(p).get("team_name") for p in profiles}

    rankings, skills = [], []
    for p in profiles:
        stats = _stats(p)
        ranking = stats.get("ranking") if isinstance(stats.get("ranking"), dict) else None
        if ranking and _num(ranking.get("rank")) is not None:
            rankings.append(
                {
                    "rank": ranking["rank"],
                    "team": p.team_number,
                    "team_name": names.get(p.team_number),
                    "division": stats.get("division_id"),
                    "wins": _num(ranking.get("wins")),
                    "losses": _num(ranking.get("losses")),
                    "ties": _num(ranking.get("ties")),
                    "avg_points": _num(ranking.get("avgPoints")),
                    "high_score": _num(ranking.get("highScore")),
                }
            )
        entry = _skills_entry(stats)
        if entry and _num(entry.get("rank")) is not None:
            skills.append(
                {
                    "rank": entry["rank"],
                    "team": p.team_number,
                    "team_name": names.get(p.team_number),
                    "division": stats.get("division_id"),
                    "total": _num(entry.get("totalScore")),
                    "driver": _num(entry.get("driverHighScore")),
                    "prog": _num(entry.get("progHighScore")),
                    "driver_attempts": _num(entry.get("driverAttempts")),
                    "prog_attempts": _num(entry.get("progAttempts")),
                }
            )
    rankings.sort(key=lambda r: (str(r["division"]), r["rank"]))
    skills.sort(key=lambda r: (str(r["division"]), r["rank"]))

    recordings = await _recordings(db)
    template = _recording_template(await _site_config(db))
    all_matches = sorted(
        ((d, m) for d, ms in schedules.items() for m in ms),
        key=lambda dm: (dm[1].time_scheduled is None, dm[1].time_scheduled or 0, dm[1].idx),
    )[:MAX_MATCHES]
    agenda = [
        {"scope": a.scope, "start_time": a.start_time, "end_time": a.end_time, "title": a.title, "details": a.details}
        for a in (
            await db.execute(select(AgendaItem).order_by(AgendaItem.scope, AgendaItem.position, AgendaItem.start_time))
        ).scalars()
    ]
    return {
        "divisions": {d: {"programme": _programme(ms)} for d, ms in schedules.items()},
        "matches": [_match_json(d, m, recordings, template) for d, m in all_matches],
        "rankings": rankings,
        "skills": skills,
        "agenda": agenda,
        "server_time": time.time(),
    }


@router.get("/teams")
async def teams(db: Annotated[AsyncSession, Depends(get_db)]) -> list[dict[str, Any]]:
    profiles = (await db.execute(select(TeamProfile).order_by(TeamProfile.team_number))).scalars()
    return [team_row(p) for p in profiles]


@router.get("/teams/{number}")
async def team(
    number: str, db: Annotated[AsyncSession, Depends(get_db)], redis: Annotated[Any, Depends(get_redis)]
) -> dict[str, Any]:
    profile = await db.get(TeamProfile, number)
    if profile is None:
        raise HTTPException(status_code=404, detail="Team not found")
    stats = _stats(profile)
    ranking = stats.get("ranking") if isinstance(stats.get("ranking"), dict) else {}
    skills = _skills_entry(stats) or {}
    recordings = await _recordings(db)
    template = _recording_template(await _site_config(db))
    schedule = [
        _match_json(d, m, recordings, template)
        for d, ms in (await _schedules(redis)).items()
        for m in ms
        if number in m.teams
    ]
    schedule.sort(key=lambda m: (m["scheduled_at"] is None, m["scheduled_at"] or 0))
    urls = (await clip_urls_for(db, [number], {number: profile}))[number]
    clip_rows = {
        c.kind: c for c in (await db.execute(select(TeamClip).where(TeamClip.team_number == number))).scalars()
    }
    logo_url = None
    if profile.logo_s3_key:
        try:
            logo_url = await s3.generate_presigned_url(profile.logo_s3_key, expires_in=_IMAGE_URL_TTL)
        except Exception:
            logger.warning("Could not presign logo for %s", number, exc_info=True)
    return {
        **team_row(profile),
        "website_url": safe_url(profile.website_url),
        "logo_url": logo_url,
        "current": {
            "qual_rank": _num(ranking.get("rank")),
            "wins": _num(ranking.get("wins")),
            "losses": _num(ranking.get("losses")),
            "ties": _num(ranking.get("ties")),
            "avg_points": _num(stats.get("qual_avg")) if _num(stats.get("qual_avg")) is not None else _num(ranking.get("avgPoints")),
            "high_score": _num(stats.get("qual_high")),
            "skills_rank": _num(skills.get("rank")),
            "skills_total": _num(skills.get("totalScore")),
            "skills_driver": _num(skills.get("driverHighScore")),
            "skills_prog": _num(skills.get("progHighScore")),
            "skills_driver_attempts": _num(skills.get("driverAttempts")),
            "skills_prog_attempts": _num(skills.get("progAttempts")),
        },
        "season": {
            "events_attended": len(stats.get("previous_rankings") or []),
            "awards": len(stats.get("awards") or []),
            "world_skills_rank": _num(stats.get("skills_rank")),
            "previous_rankings": [r for r in (stats.get("previous_rankings") or []) if isinstance(r, dict)][:30],
            "award_list": [a for a in (stats.get("awards") or []) if isinstance(a, dict)][:50],
        },
        "schedule": schedule,
        "clips": {"bot": urls.get("bot"), "team": urls.get("team")},
        "checklist": checklist_for(profile, clip_rows),
    }


@router.get("/livestreams")
async def livestreams(db: Annotated[AsyncSession, Depends(get_db)]) -> list[dict[str, Any]]:
    rooms = (await db.execute(select(Room).where(Room.stream_url.is_not(None)).order_by(Room.name))).scalars()
    return [{"slug": r.slug, "name": r.name, "stream_url": r.stream_url} for r in rooms]
