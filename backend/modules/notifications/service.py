"""Leader-only notification engine (issue #5).

Every `TICK_SECONDS` it reads the VEX TM schedule that the `vex_tm`
integration caches in Redis and works out, per division:

- which upcoming matches are within `lead_matches_x` matches / `lead_minutes_y`
  minutes of starting ("arrival alerts"), and
- which matches have newly been scored ("score alerts"; skipped for the
  rounds in `scores_excluded_rounds`, e.g. finals).

Each alert goes to (a) the room displays of rooms containing a team in the
match, and (b) Web Push subscribers following one of those teams. A row in
`notification_log` is claimed *before* sending, so a restart or leader
failover can't send the same alert twice.

It also listens on the event bus (`fieldMatchAssigned`, to know which match
is currently on the field) and on the staff-alert channel (operator alerts,
forwarded to staff push). Only the leader runs it (started/stopped from
`main.py`), the same as the other event-driven managers.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from backend.models.comms import AgendaItem
from backend.models.notification import NotificationLog
from backend.models.room import RoomTeam
from backend.models.settings import SystemSetting
from backend.modules.notifications import delivery
from backend.modules.notifications.logic import (
    AgendaAnnouncement,
    ArrivalAlert,
    LateAlert,
    ScheduledMatch,
    agenda_text,
    arrival_text,
    current_index,
    due_agenda_announcements,
    due_arrival_alerts,
    due_late_alerts,
    late_text,
    match_key_from_tuple,
    newly_scored,
    parse_schedule,
    score_text,
)

logger = logging.getLogger(__name__)

TICK_SECONDS = 10
SCHEDULE_KEY_PATTERN = "qecomp:tm:*:schedule"
ASSIGNED_HASH_KEY = "qecomp:notif:assigned"
SETTINGS_KEY = "notifications"

#: Room-display toast length for automatic alerts.
ROOM_ALERT_DURATION_SECONDS = 30

_MAX_LEAD_MATCHES = 20
_MAX_LEAD_MINUTES = 120
_MAX_LATE_MINUTES = 60


@dataclass(frozen=True)
class AlertSettings:
    team_alerts_enabled: bool = True
    lead_matches: int = 3
    lead_minutes: int = 15
    scores_enabled: bool = True
    scores_excluded_rounds: frozenset[str] = field(default_factory=lambda: frozenset({"F"}))
    #: Warn teams whose match is this many minutes overdue and not yet on the
    #: field (0 = off).
    late_minutes: int = 5
    #: Announce agenda items (lunch, finals, driver's meeting…) to every room.
    agenda_enabled: bool = True
    agenda_lead_minutes: int = 10
    #: IANA zone the agenda's "HH:MM" times are written in.
    timezone: str = "UTC"

    @classmethod
    def from_value(cls, value: dict[str, Any] | None) -> "AlertSettings":
        value = value if isinstance(value, dict) else {}

        def _int(name: str, default: int, maximum: int) -> int:
            try:
                return max(0, min(int(value.get(name, default)), maximum))
            except (TypeError, ValueError):
                return default

        excluded = value.get("scores_excluded_rounds", ["F"])
        return cls(
            team_alerts_enabled=bool(value.get("team_alerts_enabled", True)),
            lead_matches=_int("lead_matches_x", 3, _MAX_LEAD_MATCHES),
            lead_minutes=_int("lead_minutes_y", 15, _MAX_LEAD_MINUTES),
            scores_enabled=bool(value.get("scores_enabled", True)),
            late_minutes=_int("late_minutes", 5, _MAX_LATE_MINUTES),
            agenda_enabled=bool(value.get("agenda_announcements_enabled", True)),
            agenda_lead_minutes=_int("agenda_lead_minutes", 10, 240),
            timezone=str(value.get("timezone") or "UTC"),
            scores_excluded_rounds=frozenset(str(r).upper() for r in excluded)
            if isinstance(excluded, list)
            else frozenset({"F"}),
        )


class NotificationService:
    def __init__(self, redis: Any, session_factory: Any) -> None:
        self._redis = redis
        self._session_factory = session_factory
        self._tasks: list[asyncio.Task] = []
        self._shutdown = asyncio.Event()
        self._assigned: dict[str, str] = {}  # division id -> match key on the field

    # ── lifecycle ────────────────────────────────────────────────────

    async def start(self) -> None:
        self._shutdown.clear()
        await self._load_assigned()
        self._tasks = [
            asyncio.create_task(self._tick_loop(), name="notifications-tick"),
            asyncio.create_task(self._events_loop(), name="notifications-events"),
        ]

    async def stop(self) -> None:
        self._shutdown.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await task
        self._tasks = []

    # ── event bus ────────────────────────────────────────────────────

    async def _load_assigned(self) -> None:
        try:
            self._assigned = dict(await self._redis.hgetall(ASSIGNED_HASH_KEY) or {})
        except Exception:
            logger.warning("Could not restore assigned-match state from Redis", exc_info=True)

    async def _events_loop(self) -> None:
        pubsub = self._redis.pubsub()
        try:
            await pubsub.subscribe(delivery.EVENTS_CHANNEL, delivery.STAFF_ALERTS_CHANNEL)
            while not self._shutdown.is_set():
                try:
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Error reading notification pub/sub")
                    await asyncio.sleep(1)
                    continue
                if message is None:
                    continue
                try:
                    await self.handle_message(message.get("channel"), message.get("data"))
                except Exception:
                    logger.exception("Error handling notification message")
        finally:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe()
                await pubsub.aclose()

    async def handle_message(self, channel: str | None, raw: Any) -> None:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return
        if channel == delivery.STAFF_ALERTS_CHANNEL:
            await delivery.push_to_staff(
                self._session_factory,
                str(data.get("title") or "Alert"),
                str(data.get("message") or ""),
                delivery.CATEGORY_SYSTEM,
            )
        elif data.get("type") == "fieldMatchAssigned":
            await self._record_assignment(data.get("payload") or {})

    async def _record_assignment(self, payload: dict[str, Any]) -> None:
        match_tuple = payload.get("match")
        if not isinstance(match_tuple, dict) or match_tuple.get("division") is None:
            return
        division = str(match_tuple["division"])
        key = match_key_from_tuple(match_tuple)
        self._assigned[division] = key
        try:
            await self._redis.hset(ASSIGNED_HASH_KEY, division, key)
        except Exception:
            logger.warning("Could not persist assigned match for division %s", division, exc_info=True)

    # ── schedule tick ────────────────────────────────────────────────

    async def _tick_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Notification tick failed")
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=TICK_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def _load_settings(self) -> AlertSettings:
        async with self._session_factory() as session:
            row = await session.get(SystemSetting, SETTINGS_KEY)
        return AlertSettings.from_value(row.value if row else None)

    async def _load_schedules(self) -> dict[str, dict[str, list[ScheduledMatch]]]:
        """`{redis_key: {division_id: [matches]}}` for every cached TM schedule."""
        out: dict[str, dict[str, list[ScheduledMatch]]] = {}
        async for key in self._redis.scan_iter(match=SCHEDULE_KEY_PATTERN):
            raw = await self._redis.get(key)
            if not raw:
                continue
            try:
                out[key] = parse_schedule(json.loads(raw))
            except (ValueError, TypeError):
                logger.warning("Ignoring unparseable cached schedule at %s", key)
        return out

    async def tick(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        settings = await self._load_settings()
        if settings.agenda_enabled:
            await self._announce_agenda(settings, now)
        if not settings.team_alerts_enabled:
            return
        for schedule_key, divisions in (await self._load_schedules()).items():
            primed = await self._prime_scores(schedule_key, divisions)
            for division_id, matches in divisions.items():
                if settings.lead_matches > 0 or settings.lead_minutes > 0 or settings.late_minutes > 0:
                    current = current_index(matches, self._assigned.get(division_id))
                    for alert in due_arrival_alerts(
                        matches, current, now, settings.lead_matches, settings.lead_minutes
                    ):
                        if await self._claim(alert.dedupe_key):
                            await self._send_arrival(alert)
                    for late in due_late_alerts(matches, current, now, settings.late_minutes):
                        if await self._claim(late.dedupe_key):
                            await self._send_late(late)
                if settings.scores_enabled and primed:
                    for match in newly_scored(matches):
                        if match.round.upper() in settings.scores_excluded_rounds:
                            continue
                        if await self._claim(f"score:{match.key}"):
                            await self._send_score(match)

    async def _announce_agenda(self, settings: AlertSettings, now: float) -> None:
        try:
            zone = ZoneInfo(settings.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning("Unknown notifications timezone %r; using UTC", settings.timezone)
            zone = ZoneInfo("UTC")
        async with self._session_factory() as session:
            items = list((await session.execute(select(AgendaItem))).scalars())
        for announcement in due_agenda_announcements(items, datetime.fromtimestamp(now, zone), settings.agenda_lead_minutes):
            if await self._claim(announcement.dedupe_key):
                await self._send_agenda(announcement)

    async def _prime_scores(self, schedule_key: str, divisions: dict[str, list[ScheduledMatch]]) -> bool:
        """First time a schedule is seen (fresh deploy, or the service is
        enabled mid-event) every already-scored match is logged as "sent"
        without sending, so teams aren't spammed with the whole history.
        Returns True once the baseline exists (this tick may send scores)."""
        if not await self._claim(f"baseline:{schedule_key}"):
            return True
        for matches in divisions.values():
            for match in newly_scored(matches):
                await self._claim(f"score:{match.key}")
        return False  # nothing new can exist this tick; scores resume next tick

    async def _claim(self, key: str) -> bool:
        """Atomically records `key` in `notification_log`; True if this call
        created it (i.e. the alert hasn't been sent before)."""
        async with self._session_factory() as session:
            result = await session.execute(
                pg_insert(NotificationLog)
                .values(key=key[:200])
                .on_conflict_do_nothing(index_elements=["key"])
                .returning(NotificationLog.key)
            )
            claimed = result.first() is not None
            await session.commit()
        return claimed

    # ── fan-out ──────────────────────────────────────────────────────

    async def _rooms_for_teams(self, teams: tuple[str, ...]) -> list[str]:
        if not teams:
            return []
        async with self._session_factory() as session:
            result = await session.execute(
                select(RoomTeam.room_slug).where(RoomTeam.team_number.in_(list(teams))).distinct()
            )
            return [row[0] for row in result.all()]

    async def _send_arrival(self, alert: ArrivalAlert) -> None:
        m = alert.match
        title, body = arrival_text(alert)
        await delivery.publish_room_notice(
            self._redis,
            await self._rooms_for_teams(m.teams),
            title,
            body,
            duration=ROOM_ALERT_DURATION_SECONDS,
            source="match_alert",
        )
        await self._push_to_followers(
            m.teams, lambda own: arrival_text(alert, own), tag=alert.dedupe_key, kind="arrival",
            category=delivery.CATEGORY_ARRIVAL,
        )

    async def _send_late(self, alert: LateAlert) -> None:
        m = alert.match
        title, body = late_text(alert)
        await delivery.publish_room_notice(
            self._redis,
            await self._rooms_for_teams(m.teams),
            title,
            body,
            duration=ROOM_ALERT_DURATION_SECONDS,
            source="late_alert",
        )
        await self._push_to_followers(
            m.teams, lambda own: late_text(alert, own), tag=alert.dedupe_key, kind="late",
            category=delivery.CATEGORY_LATE,
        )

    async def _send_agenda(self, announcement: AgendaAnnouncement) -> None:
        title, body = agenda_text(announcement)
        await delivery.publish_room_notice(
            self._redis,
            await delivery.all_room_slugs(self._session_factory),
            title,
            body,
            duration=60,
            source="announcement",
        )
        if not delivery.push_configured():
            return
        targets = [
            delivery.PushTarget(
                sub.id, sub.endpoint, sub.p256dh, sub.auth,
                delivery.team_payload(sub, title, body, tag=announcement.dedupe_key, kind="announcement"),
            )
            for sub in await delivery.all_team_subscriptions(
                self._session_factory, category=delivery.CATEGORY_ANNOUNCEMENT
            )
        ]
        await delivery.send_push(self._session_factory, targets)

    async def _send_score(self, match: ScheduledMatch) -> None:
        title, body = score_text(match)
        await delivery.publish_room_notice(
            self._redis,
            await self._rooms_for_teams(match.teams),
            title,
            body,
            duration=ROOM_ALERT_DURATION_SECONDS,
            source="score",
        )
        await self._push_to_followers(
            match.teams, lambda own: score_text(match, own), tag=f"score:{match.key}", kind="score",
            category=delivery.CATEGORY_SCORE,
        )

    async def _push_to_followers(
        self, teams: tuple[str, ...], text_for: Any, *, tag: str, kind: str, category: str
    ) -> None:
        if not delivery.push_configured():
            return
        targets = []
        for sub, own_teams in await delivery.team_subscriptions(
            self._session_factory, teams, category=category
        ):
            title, body = text_for(own_teams)
            payload = delivery.team_payload(sub, title, body, tag=tag, kind=kind)
            targets.append(delivery.PushTarget(sub.id, sub.endpoint, sub.p256dh, sub.auth, payload))
        await delivery.send_push(self._session_factory, targets)
