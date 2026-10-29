"""Picks the next match to show on emcee countdown screens (issue #8)."""
from __future__ import annotations

from backend.modules.notifications.logic import ScheduledMatch

#: A match this far past its scheduled time that is still unscored is
#: treated as "running late" and still the next match, not skipped.
LATE_GRACE_SECONDS = 15 * 60


def find_next_match(schedules: dict[str, list[ScheduledMatch]], now: float) -> tuple[str, ScheduledMatch] | None:
    """Earliest not-yet-scored match across all divisions, ignoring ones that
    are long overdue (stale entries). Matches without a time sort last."""
    candidates: list[tuple[str, ScheduledMatch]] = [
        (division, m) for division, matches in schedules.items() for m in matches if not m.scored
    ]
    if not candidates:
        return None
    recent = [
        c for c in candidates if c[1].time_scheduled is None or c[1].time_scheduled >= now - LATE_GRACE_SECONDS
    ]
    pool = recent or candidates
    return min(pool, key=lambda c: (c[1].time_scheduled is None, c[1].time_scheduled or 0, c[1].idx))
