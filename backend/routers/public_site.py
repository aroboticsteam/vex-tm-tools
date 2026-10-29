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
import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.dependencies import get_db, get_redis
from backend.models.comms import AgendaItem
from backend.models.room import Room
from backend.models.settings import SystemSetting
from backend.models.team import TeamProfile
from backend.modules.notifications.logic import ScheduledMatch, parse_schedule
from backend.routers.team_clips import clip_urls_for

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/public/site", tags=["public-site"])

SITE_SETTING_KEY = "site"
SCHEDULE_KEY_PATTERN = "qecomp:tm:*:schedule"
MAX_MATCHES = 400


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


def _match_json(division: str, m: ScheduledMatch) -> dict[str, Any]:
    return {
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
    return {
        "title": config.get("title") or event_name or "Competition",
        "event": event_name,
        "stats": {
            "teams": len(profiles),
            "matches_played": sum(1 for ms in schedules.values() for m in ms if m.scored),
            "matches_total": sum(len(ms) for ms in schedules.values()),
            "awards": sum(len(_stats(p).get("awards") or []) for p in profiles),
        },
        "next_competition": config.get("next_competition"),
        "sponsors": config.get("sponsors") or [],
        "map": config.get("map"),
        "presented_by": config.get("presented_by"),
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

    all_matches = sorted(
        ((d, m) for d, ms in schedules.items() for m in ms),
        key=lambda dm: (dm[1].time_scheduled is None, dm[1].time_scheduled or 0, dm[1].idx),
    )[:MAX_MATCHES]
    agenda = [
        {"scope": a.scope, "start_time": a.start_time, "end_time": a.end_time, "title": a.title, "details": a.details}
        for a in (
            await db.execute(select(AgendaItem).order_by(AgendaItem.scope, AgendaItem.start_time, AgendaItem.position))
        ).scalars()
    ]
    return {
        "divisions": {d: {"programme": _programme(ms)} for d, ms in schedules.items()},
        "matches": [_match_json(d, m) for d, m in all_matches],
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
    schedule = [
        _match_json(d, m) for d, ms in (await _schedules(redis)).items() for m in ms if number in m.teams
    ]
    schedule.sort(key=lambda m: (m["scheduled_at"] is None, m["scheduled_at"] or 0))
    urls = (await clip_urls_for(db, [number], {number: profile}))[number]
    return {
        **team_row(profile),
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
    }


@router.get("/livestreams")
async def livestreams(db: Annotated[AsyncSession, Depends(get_db)]) -> list[dict[str, Any]]:
    rooms = (await db.execute(select(Room).where(Room.stream_url.is_not(None)).order_by(Room.name))).scalars()
    return [{"slug": r.slug, "name": r.name, "stream_url": r.stream_url} for r in rooms]
