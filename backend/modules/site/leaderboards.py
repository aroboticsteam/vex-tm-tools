"""Rotating leaderboards and season highlights for the public site (issue #28).

Pure functions over `TeamProfile`-like objects (anything with `team_number`
and `cached_stats`), so they can be tested without a database.
"""
from __future__ import annotations

from typing import Any

UK_COUNTRIES = frozenset(
    {"united kingdom", "uk", "gb", "great britain", "england", "scotland", "wales", "northern ireland"}
)
PROGRAMME_LABELS = {"iq": "VIQRC", "vrc": "V5RC"}
DEFAULT_LIMIT = 15
MAX_LIMIT = 50


def _num(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _stats(profile: Any) -> dict[str, Any]:
    return profile.cached_stats if isinstance(profile.cached_stats, dict) else {}


def best_skills(stats: dict[str, Any]) -> dict[str, Any] | None:
    entries = [e for e in (stats.get("skills") or []) if isinstance(e, dict)]
    return min(entries, key=lambda e: _num(e.get("rank")) or 10**9) if entries else None


def _programme_of(stats: dict[str, Any], division_programme: dict[str, str | None]) -> str | None:
    division = stats.get("division_id")
    return division_programme.get(str(division)) if division is not None else None


def _row_base(profile: Any, stats: dict[str, Any]) -> dict[str, Any]:
    return {"team": profile.team_number, "team_name": stats.get("team_name"), "division": stats.get("division_id")}


def _tournament(profiles: list[Any], limit: int) -> list[dict[str, Any]]:
    rows = []
    for p in profiles:
        stats = _stats(p)
        ranking = stats.get("ranking") if isinstance(stats.get("ranking"), dict) else {}
        rank = _num(ranking.get("rank"))
        if rank is None:
            continue
        rows.append(
            {
                **_row_base(p, stats),
                "rank": rank,
                "wins": _num(ranking.get("wins")),
                "losses": _num(ranking.get("losses")),
                "ties": _num(ranking.get("ties")),
                "avg_points": _num(ranking.get("avgPoints")),
            }
        )
    rows.sort(key=lambda r: (r["rank"], str(r["division"]), r["team"]))
    return rows[:limit]


def _skills(profiles: list[Any], limit: int) -> list[dict[str, Any]]:
    rows = []
    for p in profiles:
        stats = _stats(p)
        entry = best_skills(stats)
        if not entry or _num(entry.get("rank")) is None:
            continue
        rows.append(
            {
                **_row_base(p, stats),
                "rank": entry["rank"],
                "total": _num(entry.get("totalScore")),
                "driver": _num(entry.get("driverHighScore")),
                "prog": _num(entry.get("progHighScore")),
            }
        )
    rows.sort(key=lambda r: (r["rank"], str(r["division"]), r["team"]))
    return rows[:limit]


def _overall(profiles: list[Any], limit: int) -> list[dict[str, Any]]:
    """Overall = qualification rank + skills rank (lower is better); teams
    need both. Ties go to the better qualification rank."""
    rows = []
    for p in profiles:
        stats = _stats(p)
        ranking = stats.get("ranking") if isinstance(stats.get("ranking"), dict) else {}
        entry = best_skills(stats)
        qual = _num(ranking.get("rank"))
        skills = _num(entry.get("rank")) if entry else None
        if qual is None or skills is None:
            continue
        rows.append({**_row_base(p, stats), "qual_rank": qual, "skills_rank": skills, "score": qual + skills})
    rows.sort(key=lambda r: (r["score"], r["qual_rank"], r["team"]))
    for position, row in enumerate(rows, start=1):
        row["rank"] = position
    return rows[:limit]


def _awards(profiles: list[Any], limit: int) -> list[dict[str, Any]]:
    rows = []
    for p in profiles:
        stats = _stats(p)
        awards = [a for a in (stats.get("awards") or []) if isinstance(a, dict)]
        if not awards:
            continue
        latest = awards[-1]
        title = latest.get("title") or latest.get("name")
        rows.append({**_row_base(p, stats), "count": len(awards), "latest": title if isinstance(title, str) else None})
    rows.sort(key=lambda r: (-r["count"], r["team"]))
    for position, row in enumerate(rows, start=1):
        row["rank"] = position
    return rows[:limit]


def compute_leaderboards(
    profiles: list[Any], division_programme: dict[str, str | None], limit: int = DEFAULT_LIMIT
) -> list[dict[str, Any]]:
    """One group per programme (IQ / VRC), each with the four panels. When no
    team's programme is known everything is a single group (`programme: None`)."""
    limit = max(1, min(limit, MAX_LIMIT))
    grouped: dict[str | None, list[Any]] = {}
    for p in profiles:
        grouped.setdefault(_programme_of(_stats(p), division_programme), []).append(p)
    known = [k for k in grouped if k is not None]
    if known:
        # Teams with no programme join every group (e.g. a team whose division is unknown).
        unknown = grouped.pop(None, [])
        for key in known:
            grouped[key] = grouped[key] + unknown
        order = sorted(known, key=lambda k: (k != "iq", k))
    else:
        order = [None]
    return [
        {
            "programme": key,
            "label": PROGRAMME_LABELS.get(key, "All teams") if key else "All teams",
            "overall": _overall(grouped[key], limit),
            "tournament": _tournament(grouped[key], limit),
            "skills": _skills(grouped[key], limit),
            "awards": _awards(grouped[key], limit),
        }
        for key in order
    ]


def season_highlights(profiles: list[Any], programmes: list[str]) -> dict[str, Any]:
    """Season-dashboard numbers from the cached team data."""
    awards = sum(len(_stats(p).get("awards") or []) for p in profiles)
    world: list[tuple[int | float, str]] = []
    uk: list[tuple[int | float, str]] = []
    totals: list[tuple[int | float, str]] = []
    for p in profiles:
        stats = _stats(p)
        rank = _num(stats.get("skills_rank"))
        if rank is not None:
            world.append((rank, p.team_number))
            country = str(stats.get("country") or "").strip().lower()
            if country in UK_COUNTRIES:
                uk.append((rank, p.team_number))
        entry = best_skills(stats)
        total = _num(entry.get("totalScore")) if entry else None
        if total is not None:
            totals.append((total, p.team_number))
    best_world = min(world, default=None)
    best_uk = min(uk, default=None)
    best_total = max(totals, key=lambda t: (t[0], [-ord(c) for c in t[1]]), default=None)
    return {
        "active_programmes": [{"programme": p, "label": PROGRAMME_LABELS.get(p, p)} for p in programmes],
        "total_awards": awards,
        "best_world_skills": {"team": best_world[1], "rank": best_world[0]} if best_world else None,
        "best_uk_skills": {"team": best_uk[1], "rank": best_uk[0], "uk_teams_ranked": len(uk)} if best_uk else None,
        "top_combined_skills": {"team": best_total[1], "total": best_total[0]} if best_total else None,
    }
