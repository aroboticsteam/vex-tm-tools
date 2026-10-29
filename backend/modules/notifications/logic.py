"""Pure scheduling logic for team notifications (no I/O).

Turns a cached `/api/matches/{division}` schedule (see the VEX TM API
"Match List Resource") into the alerts that are due *right now*:

- "matches" alerts — the match is `x` or fewer matches behind the one
  currently on/after the field;
- "minutes" alerts — the match's scheduled time is `y` or fewer minutes away;
- score alerts — a match has newly moved to `SCORED`.

Dedupe is the caller's job (see `service.py`'s `notification_log` claims);
everything here is a deterministic function of its inputs.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

STATE_SCORED = "SCORED"

#: Rounds written as `<prefix><match>` (e.g. Q12); everything else is
#: `<round> <instance>-<match>` (e.g. `SF 2-1`).
_SIMPLE_LABEL_PREFIX = {"QUAL": "Q", "PRACTICE": "P"}

#: A "minutes" alert may still fire for a match this many seconds past its
#: scheduled time (it's evidently running late and hasn't been played), but
#: not for ones that were due long ago — keeps a restart mid-event quiet.
_LATE_GRACE_SECONDS = 300


def match_key(division: Any, session: Any, round_: Any, instance: Any, match: Any) -> str:
    return f"{division}:{session}:{round_}:{instance}:{match}"


def match_key_from_tuple(match_tuple: dict[str, Any]) -> str:
    return match_key(
        match_tuple.get("division"),
        match_tuple.get("session", 0),
        match_tuple.get("round"),
        match_tuple.get("instance", 1),
        match_tuple.get("match"),
    )


def match_label(round_: str, instance: Any, match_num: Any) -> str:
    prefix = _SIMPLE_LABEL_PREFIX.get(round_)
    if prefix:
        return f"{prefix}{match_num}"
    return f"{round_} {instance}-{match_num}"


@dataclass(frozen=True)
class ScheduledMatch:
    key: str
    division: Any
    round: str
    instance: Any
    match_num: Any
    time_scheduled: float | None
    state: str
    red: tuple[str, ...]
    blue: tuple[str, ...]
    final_score: tuple[int, int] | None
    idx: int  # position within its division's chronological list

    @property
    def teams(self) -> tuple[str, ...]:
        return (*self.red, *self.blue)

    @property
    def label(self) -> str:
        return match_label(self.round, self.instance, self.match_num)

    @property
    def scored(self) -> bool:
        return self.state == STATE_SCORED


def _alliance_teams(alliances: list[dict[str, Any]], index: int) -> tuple[str, ...]:
    if len(alliances) <= index:
        return ()
    return tuple(
        str(t["number"]) for t in (alliances[index].get("teams") or []) if t.get("number") not in (None, "")
    )


def parse_division(division_id: Any, matches: list[dict[str, Any]]) -> list[ScheduledMatch]:
    """Parses one division's raw match list into chronologically ordered
    `ScheduledMatch`es. Entries missing a match tuple are skipped."""
    parsed: list[tuple[float | None, int, dict[str, Any]]] = []
    for position, raw in enumerate(matches or []):
        info = raw.get("matchInfo") or {}
        if not info.get("matchTuple"):
            continue
        ts = info.get("timeScheduled")
        parsed.append((float(ts) if isinstance(ts, (int, float)) else None, position, raw))
    # Chronological when times are known; otherwise keep TM's own order.
    parsed.sort(key=lambda item: (item[0] is None, item[0] or 0, item[1]))

    out: list[ScheduledMatch] = []
    for idx, (ts, _pos, raw) in enumerate(parsed):
        info = raw["matchInfo"]
        mt = info["matchTuple"]
        alliances = info.get("alliances") or []
        score = raw.get("finalScore")
        final = (
            (int(score[0]), int(score[1]))
            if isinstance(score, (list, tuple)) and len(score) >= 2 and all(isinstance(s, (int, float)) for s in score[:2])
            else None
        )
        out.append(
            ScheduledMatch(
                key=match_key_from_tuple({**mt, "division": mt.get("division", division_id)}),
                division=mt.get("division", division_id),
                round=str(mt.get("round") or ""),
                instance=mt.get("instance", 1),
                match_num=mt.get("match"),
                time_scheduled=ts,
                state=str(info.get("state") or ""),
                red=_alliance_teams(alliances, 0),
                blue=_alliance_teams(alliances, 1),
                final_score=final,
                idx=idx,
            )
        )
    return out


def parse_schedule(schedule: dict[str, Any]) -> dict[str, list[ScheduledMatch]]:
    """`{"divisions": [{"id", "matches": [...]}]}` (the Redis-cached shape)
    -> `{str(division_id): [ScheduledMatch, ...]}`."""
    result: dict[str, list[ScheduledMatch]] = {}
    for division in (schedule or {}).get("divisions", []):
        div_id = division.get("id")
        result[str(div_id)] = parse_division(div_id, division.get("matches") or [])
    return result


def current_index(matches: list[ScheduledMatch], assigned_key: str | None) -> int:
    """Index of the match "currently in play": the later of the one most
    recently assigned to a field and the last scored one; -1 if neither."""
    current = -1
    for m in matches:
        if m.scored:
            current = max(current, m.idx)
        elif assigned_key is not None and m.key == assigned_key:
            current = max(current, m.idx)
    return current


@dataclass(frozen=True)
class ArrivalAlert:
    kind: str  # "matches" | "minutes"
    match: ScheduledMatch
    matches_ahead: int | None = None
    minutes_away: int | None = None

    @property
    def dedupe_key(self) -> str:
        return f"{self.kind}:{self.match.key}"


def due_arrival_alerts(
    matches: list[ScheduledMatch], current_idx: int, now: float, lead_matches: int, lead_minutes: int
) -> list[ArrivalAlert]:
    alerts: list[ArrivalAlert] = []
    for m in matches:
        if m.scored or m.idx <= current_idx:
            continue
        ahead = m.idx - current_idx
        if lead_matches > 0 and ahead <= lead_matches:
            alerts.append(ArrivalAlert("matches", m, matches_ahead=ahead))
        if lead_minutes > 0 and m.time_scheduled is not None:
            delta = m.time_scheduled - now
            if -_LATE_GRACE_SECONDS <= delta <= lead_minutes * 60:
                alerts.append(ArrivalAlert("minutes", m, minutes_away=max(0, math.ceil(delta / 60))))
    return alerts


#: A late warning stops once a match is this many seconds overdue beyond the
#: configured threshold — a long-abandoned match shouldn't page anyone, and a
#: restart mid-event stays quiet about ancient history.
_LATE_WINDOW_SECONDS = 1800


@dataclass(frozen=True)
class LateAlert:
    match: ScheduledMatch
    minutes_late: int

    @property
    def dedupe_key(self) -> str:
        return f"late:{self.match.key}"


def due_late_alerts(matches: list[ScheduledMatch], current_idx: int, now: float, late_minutes: int) -> list[LateAlert]:
    """Matches that are `late_minutes` or more past their scheduled time and
    still haven't reached the field (the field is behind them) or been
    scored. `late_minutes` of 0 disables the warning."""
    if late_minutes <= 0:
        return []
    threshold = late_minutes * 60
    alerts: list[LateAlert] = []
    for m in matches:
        if m.scored or m.idx <= current_idx or m.time_scheduled is None:
            continue
        overdue = now - m.time_scheduled
        if threshold <= overdue <= threshold + _LATE_WINDOW_SECONDS:
            alerts.append(LateAlert(m, int(overdue // 60)))
    return alerts


def late_text(alert: LateAlert, own_teams: list[str] | None = None) -> tuple[str, str]:
    m = alert.match
    who = ", ".join(own_teams) if own_teams else ", ".join(m.teams)
    return f"{m.label} is running late", f"{who} was due {alert.minutes_late} min ago. Head to the field now."


@dataclass(frozen=True)
class AgendaAnnouncement:
    item_id: str
    title: str
    details: str | None
    start_time: str
    minutes_away: int
    day: str  # YYYY-MM-DD in the event timezone

    @property
    def dedupe_key(self) -> str:
        return f"agenda:{self.item_id}:{self.day}"


def _minutes_of(hhmm: str) -> int | None:
    try:
        hours, minutes = hhmm.split(":")
        return int(hours) * 60 + int(minutes)
    except (ValueError, AttributeError):
        return None


def due_agenda_announcements(
    items: list[Any], now_local: datetime, default_lead_minutes: int
) -> list[AgendaAnnouncement]:
    """Agenda items (anything with `id`, `title`, `details`, `start_time` as
    "HH:MM" and `announce_lead_minutes`) whose lead window is open right now.

    The window runs from `start - lead` until the start time has passed by a
    short grace, so a tick that lands a little late still announces once."""
    now_minutes = now_local.hour * 60 + now_local.minute + now_local.second / 60
    out: list[AgendaAnnouncement] = []
    for item in items:
        lead = item.announce_lead_minutes if item.announce_lead_minutes is not None else default_lead_minutes
        start = _minutes_of(item.start_time)
        if lead <= 0 or start is None:
            continue
        away = start - now_minutes
        if -2 <= away <= lead:
            out.append(
                AgendaAnnouncement(
                    str(item.id), item.title, item.details, item.start_time, max(0, math.ceil(away)),
                    now_local.date().isoformat(),
                )
            )
    return out


def agenda_text(a: AgendaAnnouncement) -> tuple[str, str]:
    when = "starting now" if a.minutes_away <= 0 else f"at {a.start_time} (in ~{a.minutes_away} min)"
    body = f"{a.title} {when}."
    if a.details:
        body += f" {a.details}"
    return f"Coming up: {a.title}", body


def newly_scored(matches: list[ScheduledMatch]) -> list[ScheduledMatch]:
    return [m for m in matches if m.scored and m.final_score is not None]


def arrival_text(alert: ArrivalAlert, own_teams: list[str] | None = None) -> tuple[str, str]:
    """(title, body) for an arrival alert. `own_teams` narrows the body to
    the recipient's teams; None lists the whole match (room display)."""
    m = alert.match
    who = ", ".join(own_teams) if own_teams else ", ".join(m.teams)
    if alert.kind == "matches":
        ahead = alert.matches_ahead or 0
        when = "is up next" if ahead <= 1 else f"is {ahead} matches away"
        return f"{m.label} coming up", f"{who} {when}. Head to the field queue."
    minutes = alert.minutes_away or 0
    if minutes <= 0:
        return f"{m.label} due now", f"{who} is scheduled now."
    unit = "minute" if minutes == 1 else "minutes"
    return f"{m.label} in ~{minutes} min", f"{who} is scheduled in about {minutes} {unit}."


def score_text(m: ScheduledMatch, own_teams: list[str] | None = None) -> tuple[str, str]:
    assert m.final_score is not None
    red, blue = m.final_score
    teams = f" ({', '.join(own_teams)})" if own_teams else ""
    body = f"Red {red} – Blue {blue}"
    if not own_teams:
        body += f"  |  Red: {', '.join(m.red)}  Blue: {', '.join(m.blue)}"
    return f"{m.label} final{teams}", body
