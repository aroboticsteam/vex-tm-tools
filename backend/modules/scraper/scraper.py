"""Team Profile & Stats Scraper (plan §5.11 / §C.2).

Owns: triggering team data fetches when `fieldMatchAssigned` fires, fetching
from the TM REST API and the Robot Events API, computing derived stats,
writing to `team_profiles` Postgres table, Redis caching.

May import from: `backend.core` (DB, Redis), `backend.models` (team_profiles,
system_settings), `backend.schemas.events` (the frozen `EventBusMessage`).

Must NOT import from: `routers/`, `modules/automation/`, `modules/timer/`,
`modules/media/`, `modules/predictor/`.

── Note on TM REST access (coordination note for Wave 2a — Loader & Integrations) ──
Per §C.2 this module "may import from `backend.loader` (to get the vex_tm
instance for REST calls)". `backend.loader` and the `vex_tm` integration are
being built concurrently in a sibling worktree and are not available here.
The `Integration` ABC (`backend.modules.integrations.base.Integration`) only
exposes `call_service()` (for commands) and `get_state()` (telemetry) — no
read-only "give me the raw REST data" method. Rather than hard-depend on
Wave 2a's exact (not-yet-frozen) internals, this module fetches TM REST data
via its own lightweight `httpx.AsyncClient`, using:
  * `base_url` read directly from the `integration_instances` row for the
    given `tm_entity_id` (a non-secret config field per the vex_tm
    manifest's `config_schema`, so no Fernet decryption is required here).
  * the bearer token from the frozen Redis keyspace key
    `qecomp:tm:<entity_id>:token` (§8's Redis key space table — written by
    the vex_tm integration's `setup()`, already plaintext per that table).
This means the Scraper only depends on the DB row shape and the documented
Redis key convention, not on Wave 2a's class internals. If Wave 2a's
`loader.get_instance()` later grows a read-only data-fetch method, this
module can be switched to call it without changing its public API — but
that is a coordination decision for the maintainers, not made here.
"""
from __future__ import annotations

import asyncio
import json
import logging
import statistics
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.core.redis import redis_client as default_redis_client
from backend.core.security import decrypt
from backend.models.integration import IntegrationInstance
from backend.models.settings import SystemSetting
from backend.models.team import TeamProfile
from backend.modules.integrations.vex_tm.integration import _host_header, _sign, _string_to_sign
from backend.schemas.events import EventBusMessage

logger = logging.getLogger(__name__)

EVENTS_CHANNEL = "qecomp:events"
# Published by the vex_tm integration after each schedule poll (see
# `_fetch_and_cache_schedule`): {"entity_id": ..., "teams": [...]}.
TEAMS_DISCOVERED_CHANNEL = "qecomp:tm_teams_discovered"
TEAM_PROFILE_CACHE_TTL = 60 * 60 * 24  # 24h, per plan §5.11 / §8.
# The VEX Events API is strictly rate-limited, so the resolved "current
# season" id for a program is cached rather than re-fetched on every team
# lookup — a season practically never changes mid-cache-window, and this
# turns what would otherwise be one extra `/seasons` call per uncached team
# fetch into (at most) one per program per day.
CURRENT_SEASON_CACHE_TTL = 60 * 60 * 24  # 24h
# RobotEvents was renamed/moved to the VEX Events API; the resource paths,
# "data" response envelope, and Bearer-token auth are unchanged (verified
# against https://events.vex.com/api/v2/swagger.yml) — only the host moved.
ROBOT_EVENTS_BASE_URL = "https://events.vex.com/api/v2"


def _team_cache_key(team_number: str) -> str:
    return f"qecomp:team:{team_number}:profile"


def _tm_token_key(entity_id: str) -> str:
    return f"qecomp:tm:{entity_id}:token"



def _team_numbers_in(entry: Any) -> set[str]:
    """Every team number an entry from a TM rankings/skills list refers to.
    Handles the documented shapes (`alliance.teams[].number` for rankings,
    top-level `number` for skills) and the older `team`/`team.number` ones."""
    if not isinstance(entry, dict):
        return set()
    found: set[str] = set()
    if entry.get("number") not in (None, ""):
        found.add(str(entry["number"]))
    team = entry.get("team")
    if isinstance(team, dict) and team.get("number") not in (None, ""):
        found.add(str(team["number"]))
    elif isinstance(team, str) and team:
        found.add(team)
    alliance = entry.get("alliance")
    if isinstance(alliance, dict):
        for t in alliance.get("teams") or []:
            if isinstance(t, dict) and t.get("number") not in (None, ""):
                found.add(str(t["number"]))
    return found


def _team_location(team_row: dict[str, Any]) -> str | None:
    """`location` if TM provides it, else "City, State, Country" from the
    documented separate fields."""
    if team_row.get("location"):
        return str(team_row["location"])
    parts = [str(team_row[k]) for k in ("city", "state", "country") if team_row.get(k)]
    return ", ".join(parts) or None


def _country_from_location(location: Any) -> str | None:
    if isinstance(location, str) and "," in location:
        return location.rsplit(",", 1)[1].strip() or None
    return None


class Scraper:
    """Subscribes to `qecomp:events`, auto-creates/enriches `team_profiles`
    rows from TM + Robot Events data, and caches the result in Redis."""

    def __init__(
        self,
        redis: Any = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._redis = redis if redis is not None else default_redis_client
        if session_factory is None:
            from backend.core.db import async_session_factory as _default_factory

            session_factory = _default_factory
        self._session_factory = session_factory

        self._task: asyncio.Task | None = None
        self._backfill_task: asyncio.Task | None = None
        self._pubsub = None
        self._shutdown = asyncio.Event()

        # Reused per C.7 (single persistent client, created once).
        self._re_http = httpx.AsyncClient(base_url=ROBOT_EVENTS_BASE_URL, timeout=10.0)
        # TM base_url varies per instance, so this client carries no base_url.
        self._tm_http = httpx.AsyncClient(timeout=10.0)

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Subscribe to `qecomp:events` and begin listening for
        `fieldMatchAssigned` events in a background task."""
        self._shutdown.clear()
        self._pubsub = self._redis.pubsub()
        await self._pubsub.subscribe(EVENTS_CHANNEL, TEAMS_DISCOVERED_CHANNEL)
        self._task = asyncio.create_task(self._listen_loop())

    async def stop(self) -> None:
        self._shutdown.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._backfill_task is not None:
            self._backfill_task.cancel()
            try:
                await self._backfill_task
            except (asyncio.CancelledError, Exception):
                pass
            self._backfill_task = None
        if self._pubsub is not None:
            try:
                await self._pubsub.unsubscribe(EVENTS_CHANNEL, TEAMS_DISCOVERED_CHANNEL)
                await self._pubsub.aclose()
            except Exception:
                logger.exception("Error closing scraper pubsub connection")
            self._pubsub = None
        try:
            await self._re_http.aclose()
            await self._tm_http.aclose()
        except Exception:
            logger.exception("Error closing scraper HTTP clients")

    async def _listen_loop(self) -> None:
        assert self._pubsub is not None
        try:
            async for message in self._pubsub.listen():
                if self._shutdown.is_set():
                    break
                if message is None or message.get("type") != "message":
                    continue
                channel = message.get("channel")
                if isinstance(channel, bytes):
                    channel = channel.decode("utf-8")
                if channel == TEAMS_DISCOVERED_CHANNEL:
                    await self._handle_teams_discovered(message.get("data"))
                else:
                    await self._handle_raw_message(message.get("data"))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Scraper event listen loop crashed")

    async def _handle_raw_message(self, data: Any) -> None:
        try:
            payload = json.loads(data) if isinstance(data, (str, bytes)) else data
            event = EventBusMessage.model_validate(payload)
        except Exception:
            logger.warning("Scraper received malformed event bus message: %r", data)
            return

        if event.type != "fieldMatchAssigned":
            return

        tm_entity_id = event.entity_id
        red_teams = event.payload.get("redTeams") or []
        blue_teams = event.payload.get("blueTeams") or []
        for team_number in [*red_teams, *blue_teams]:
            try:
                await self._auto_create_if_missing(str(team_number), tm_entity_id)
            except Exception:
                logger.exception(
                    "Failed to auto-create/refresh team profile for %s", team_number
                )

    async def _handle_teams_discovered(self, data: Any) -> None:
        """Create profiles for every team in a freshly polled schedule that
        doesn't have one yet. Runs in a background task (one at a time — a
        poll that lands mid-backfill is skipped, the next poll retries) so a
        big event's worth of TM/Vex Events lookups never blocks the listen
        loop that handles live `fieldMatchAssigned` events."""
        try:
            payload = json.loads(data) if isinstance(data, (str, bytes)) else data
            tm_entity_id = str(payload["entity_id"])
            teams = [str(t) for t in payload["teams"]]
        except Exception:
            logger.warning("Scraper received malformed teams-discovered message: %r", data)
            return
        if self._backfill_task is not None and not self._backfill_task.done():
            return
        self._backfill_task = asyncio.create_task(self._backfill_teams(tm_entity_id, teams))

    async def _backfill_teams(self, tm_entity_id: str, teams: list[str]) -> None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(TeamProfile.team_number).where(TeamProfile.team_number.in_(teams))
            )
            existing = set(result.scalars().all())
        missing = [t for t in teams if t not in existing]
        if missing:
            logger.info("Creating %d team profile(s) from schedule for %s", len(missing), tm_entity_id)
        for team_number in missing:
            if self._shutdown.is_set():
                return
            try:
                await self.fetch_team(team_number, tm_entity_id)
            except Exception:
                logger.exception("Failed to create team profile for %s from schedule", team_number)

    async def _auto_create_if_missing(self, team_number: str, tm_entity_id: str) -> None:
        async with self._session_factory() as session:
            row = await session.get(TeamProfile, team_number)
            exists = row is not None
        if not exists:
            await self.fetch_team(team_number, tm_entity_id)

    # ── Public API ───────────────────────────────────────────────────────

    async def invalidate_cache(self, team_number: str) -> None:
        await self._redis.delete(_team_cache_key(team_number))

    async def fetch_team(self, team_number: str, tm_entity_id: str) -> dict[str, Any]:
        """Fetch team data from the TM instance + Robot Events, compute
        derived stats, upsert `team_profiles`, cache in Redis (24h TTL), and
        publish a `team_profile_updated` event. Returns the profile dict."""
        cache_key = _team_cache_key(team_number)
        cached = await self._redis.get(cache_key)
        if cached:
            try:
                return json.loads(cached)
            except (TypeError, json.JSONDecodeError):
                pass  # fall through and refetch on corrupt cache

        tm_data = await self._fetch_from_tm(team_number, tm_entity_id)
        re_data = await self._fetch_from_robot_events(team_number)

        profile = self._merge_profile(team_number, tm_data, re_data)

        await self._upsert_team_profile(profile)
        await self._redis.set(cache_key, json.dumps(profile), ex=TEAM_PROFILE_CACHE_TTL)
        await self._publish_team_profile_updated(team_number)
        return profile

    # ── TM REST fetching ─────────────────────────────────────────────────

    async def _get_tm_base_url_and_token(
        self, tm_entity_id: str
    ) -> tuple[str | None, str | None, str | None]:
        base_url: str | None = None
        api_key: str | None = None
        async with self._session_factory() as session:
            result = await session.execute(
                select(IntegrationInstance).where(IntegrationInstance.entity_id == tm_entity_id)
            )
            instance = result.scalar_one_or_none()
            if instance is not None:
                base_url = instance.config.get("base_url")
                # `api_key` is a `secret: true` manifest field (see
                # manifest.yaml) — it's stored encrypted in
                # `IntegrationInstance.config` and only decrypted by
                # `loader.py` when constructing the live `VexTmIntegration`
                # instance. This client reads the DB row directly, so it
                # must decrypt it itself before using it to sign requests.
                raw_api_key = instance.config.get("api_key")
                if raw_api_key:
                    try:
                        api_key = decrypt(raw_api_key)
                    except Exception:
                        logger.exception(
                            "Failed to decrypt api_key for TM instance %s", tm_entity_id
                        )
        token = await self._redis.get(_tm_token_key(tm_entity_id))
        return base_url, token, api_key

    async def _tm_get(
        self,
        base_url: str,
        token: str | None,
        api_key: str | None,
        path: str,
        envelope_key: str | None = None,
    ) -> Any:
        """Signed TM REST GET (per docs/modules/VEX_API_DOCS.md "Request
        Signing"): every TM API request must carry `x-tm-date`/
        `x-tm-signature` headers, not just the bearer token. Reuses the same
        HMAC helpers `VexTmIntegration._get` uses rather than duplicating the
        signing logic.

        Every TM REST resource wraps its payload in an envelope object keyed
        by the resource name (`{"teams": [...]}`, `{"rankings": [...]}`,
        `{"skillsRankings": [...]}`, `{"matches": [...]}`, `{"event": {...}}`)
        per the docs — pass `envelope_key` to unwrap it; the caller gets the
        actual list/object rather than the envelope dict itself.
        """
        url = base_url.rstrip("/") + path
        parsed = urlparse(base_url)
        host = _host_header(parsed.hostname, parsed.scheme, parsed.port)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if api_key and token:
            date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
            signature = _sign(api_key, _string_to_sign("GET", path, token, host, date))
            headers["x-tm-date"] = date
            headers["x-tm-signature"] = signature
        resp = await self._tm_http.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        if envelope_key is not None and isinstance(data, dict):
            return data.get(envelope_key)
        return data

    async def _fetch_from_tm(self, team_number: str, tm_entity_id: str) -> dict[str, Any]:
        base_url, token, api_key = await self._get_tm_base_url_and_token(tm_entity_id)
        if not base_url:
            logger.warning(
                "No base_url configured for TM instance %s; skipping TM fetch for team %s",
                tm_entity_id,
                team_number,
            )
            return {}

        try:
            teams = await self._tm_get(base_url, token, api_key, "/api/teams", envelope_key="teams") or []
        except Exception:
            logger.exception("Failed to fetch /api/teams from %s", tm_entity_id)
            teams = []

        team_row = next(
            (t for t in teams if str(t.get("number")) == str(team_number)), None
        )
        division_id = None
        if team_row:
            division = team_row.get("division")
            if isinstance(division, dict):
                division_id = division.get("id")
            else:
                # TM's documented `/api/teams` row carries `divId`.
                division_id = team_row.get("divId") or team_row.get("divisionId") or division

        result: dict[str, Any] = {"team_row": team_row or {}, "division_id": division_id}

        try:
            result["event"] = (
                await self._tm_get(base_url, token, api_key, "/api/event", envelope_key="event") or {}
            )
        except Exception:
            logger.exception("Failed to fetch /api/event from %s", tm_entity_id)
            result["event"] = {}

        if division_id is not None:
            try:
                rankings = (
                    await self._tm_get(
                        base_url,
                        token,
                        api_key,
                        f"/api/rankings/{division_id}/QUAL",
                        envelope_key="rankings",
                    )
                    or []
                )
                result["ranking"] = next(
                    (r for r in rankings if str(team_number) in _team_numbers_in(r)),
                    None,
                )
            except Exception:
                logger.exception("Failed to fetch rankings from %s", tm_entity_id)
                result["ranking"] = None

            try:
                matches = (
                    await self._tm_get(
                        base_url, token, api_key, f"/api/matches/{division_id}", envelope_key="matches"
                    )
                    or []
                )
                result["matches"] = self._extract_team_matches(matches, team_number)
            except Exception:
                logger.exception("Failed to fetch matches from %s", tm_entity_id)
                result["matches"] = []
        else:
            result["ranking"] = None
            result["matches"] = []

        try:
            skills = (
                await self._tm_get(base_url, token, api_key, "/api/skills", envelope_key="skillsRankings")
                or []
            )
            result["skills"] = [s for s in skills if str(team_number) in _team_numbers_in(s)]
        except Exception:
            logger.exception("Failed to fetch skills from %s", tm_entity_id)
            result["skills"] = []

        return result

    @staticmethod
    def _extract_team_matches(matches: list[dict[str, Any]], team_number: str) -> list[dict[str, Any]]:
        """Filter the division's match list down to matches this team played
        in (qualification matches only, per the `/api/matches/<div_id>`
        endpoint — skills runs are fetched separately via `/api/skills` and
        are naturally excluded here).

        Returns a compact per-match record: matchNum, round, redTeams,
        blueTeams, redScore, blueScore — enough for the predictor's OPR
        regression to reconstruct alliance compositions without re-fetching.
        """
        out = []
        for m in matches or []:
            red_teams = [str(t) for t in (m.get("redTeams") or m.get("red", {}).get("teams") or [])]
            blue_teams = [str(t) for t in (m.get("blueTeams") or m.get("blue", {}).get("teams") or [])]
            if str(team_number) not in red_teams and str(team_number) not in blue_teams:
                continue
            red_score = m.get("redScore", m.get("red", {}).get("score"))
            blue_score = m.get("blueScore", m.get("blue", {}).get("score"))
            if red_score is None or blue_score is None:
                continue  # unscored/future match — excluded from stats.
            out.append(
                {
                    "matchNum": m.get("matchNum") or m.get("match_num") or m.get("number"),
                    "round": m.get("round"),
                    "redTeams": red_teams,
                    "blueTeams": blue_teams,
                    "redScore": red_score,
                    "blueScore": blue_score,
                }
            )
        return out

    # ── Robot Events fetching ────────────────────────────────────────────

    async def _get_robot_events_token(self) -> str | None:
        async with self._session_factory() as session:
            setting = await session.get(SystemSetting, "robot_events_api")
            if setting is None:
                return None
            return (setting.value or {}).get("token") or None

    async def _get_current_season_id(self, program_id: int, headers: dict[str, str]) -> int | None:
        """Resolves the active season id for a program, via
        `GET /seasons?program[]=<id>&active=true` — Redis-cached (see
        `CURRENT_SEASON_CACHE_TTL`) since this only needs to change once a
        season, not once per team fetch, and the API is strictly
        rate-limited."""
        cache_key = f"qecomp:re:season:{program_id}"
        cached = await self._redis.get(cache_key)
        if cached:
            try:
                return int(cached)
            except (TypeError, ValueError):
                pass

        try:
            resp = await self._re_http.get(
                "/seasons",
                params={"program[]": program_id, "active": "true"},
                headers=headers,
            )
            resp.raise_for_status()
            seasons = resp.json().get("data", [])
        except Exception:
            logger.exception("Failed to resolve current season for program %s", program_id)
            return None

        season_id = seasons[0].get("id") if seasons else None
        if season_id is not None:
            await self._redis.set(cache_key, str(season_id), ex=CURRENT_SEASON_CACHE_TTL)
        return season_id

    async def _fetch_from_robot_events(self, team_number: str) -> dict[str, Any]:
        token = await self._get_robot_events_token()
        if not token:
            logger.debug("No Robot Events API token configured; skipping RE fetch")
            return {}

        headers = {"Authorization": f"Bearer {token}"}
        try:
            resp = await self._re_http.get(
                "/teams", params={"number[]": team_number}, headers=headers
            )
            resp.raise_for_status()
            teams = resp.json().get("data", [])
        except Exception:
            logger.exception("Failed to fetch Robot Events team lookup for %s", team_number)
            return {}

        if not teams:
            return {}
        re_team = teams[0]
        team_id = re_team.get("id")

        result: dict[str, Any] = {
            # The VEX Events API's `Team` schema (the RobotEvents ->
            # events.vex.com migration) has no `bio`/`description` field at
            # all — it was dropped, not renamed. Left as a no-op lookup so
            # this starts working again for free if the field is ever added
            # back, rather than silently deleting the column/feature.
            "bio": re_team.get("description") or re_team.get("bio"),
            "robot_name": re_team.get("robot_name"),
        }

        if team_id is not None:
            program_id = (re_team.get("program") or {}).get("id")
            season_id = (
                await self._get_current_season_id(program_id, headers)
                if program_id is not None
                else None
            )
            if season_id is None:
                # Can't scope to the current season without a resolved id —
                # skip rather than pulling this team's full multi-season
                # history (violates "current season only" and needlessly
                # burns calls/pagination against a strictly rate-limited
                # API).
                logger.warning(
                    "Could not resolve current season for RE team %s (program %s); "
                    "skipping rankings/awards/skills fetch",
                    team_id,
                    program_id,
                )
                result["previous_rankings"] = []
                result["awards"] = []
                result["skills_rank"] = None
                return result

            season_params = {"season[]": season_id}
            # None of these three depend on each other's result — fetch
            # concurrently instead of paying for 3 sequential round-trips
            # per team (this runs once per auto-created team per match).
            # `season[]` scopes each to the current season only, which also
            # keeps response sizes (and pagination) down against a strictly
            # rate-limited API.
            rankings_result, awards_result, skills_result = await asyncio.gather(
                self._re_http.get(f"/teams/{team_id}/rankings", params=season_params, headers=headers),
                self._re_http.get(f"/teams/{team_id}/awards", params=season_params, headers=headers),
                self._re_http.get(f"/teams/{team_id}/skills", params=season_params, headers=headers),
                return_exceptions=True,
            )

            if isinstance(rankings_result, BaseException):
                logger.exception("Failed to fetch RE rankings for team %s", team_id, exc_info=rankings_result)
                result["previous_rankings"] = []
            else:
                try:
                    rankings_result.raise_for_status()
                    result["previous_rankings"] = rankings_result.json().get("data", [])
                except Exception:
                    logger.exception("Failed to fetch RE rankings for team %s", team_id)
                    result["previous_rankings"] = []

            if isinstance(awards_result, BaseException):
                logger.exception("Failed to fetch RE awards for team %s", team_id, exc_info=awards_result)
                result["awards"] = []
            else:
                try:
                    awards_result.raise_for_status()
                    result["awards"] = awards_result.json().get("data", [])
                except Exception:
                    logger.exception("Failed to fetch RE awards for team %s", team_id)
                    result["awards"] = []

            if isinstance(skills_result, BaseException):
                logger.exception("Failed to fetch RE skills for team %s", team_id, exc_info=skills_result)
            else:
                try:
                    skills_result.raise_for_status()
                    skills_data = skills_result.json().get("data", [])
                    # The VEX Events API's `Skill` schema (the RobotEvents ->
                    # events.vex.com migration) dropped both `region` and the
                    # nested `season.program` this used to split ranks into
                    # "world" vs "UK" championship standings — neither field
                    # exists on a skills entry any more (only
                    # id/event/team/type/season/division/rank/score/attempts),
                    # and recovering the region would mean an extra
                    # `GET /events/{id}` call per entry. Report a single best
                    # (lowest = highest-placing) rank across all of this
                    # team's skills entries instead of a region split.
                    ranks = [s.get("rank") for s in skills_data if s.get("rank") is not None]
                    result["skills_rank"] = min(ranks) if ranks else None
                except Exception:
                    logger.exception("Failed to fetch RE skills for team %s", team_id)

        return result

    # ── Stats + persistence ──────────────────────────────────────────────

    @staticmethod
    def _merge_profile(
        team_number: str, tm_data: dict[str, Any], re_data: dict[str, Any]
    ) -> dict[str, Any]:
        team_row = tm_data.get("team_row", {}) or {}
        matches = tm_data.get("matches", []) or []

        scores: list[float] = []
        for m in matches:
            red_teams = m.get("redTeams", [])
            score = m["redScore"] if str(team_number) in red_teams else m["blueScore"]
            scores.append(float(score))

        cached_stats: dict[str, Any] = {
            "qual_avg": round(statistics.fmean(scores), 2) if scores else None,
            "qual_high": max(scores) if scores else None,
            "qual_low": min(scores) if scores else None,
            "matches": matches,
            "ranking": tm_data.get("ranking"),
            "skills": tm_data.get("skills", []),
            "event": tm_data.get("event"),
            "previous_rankings": re_data.get("previous_rankings", []),
            "awards": re_data.get("awards", []),
            "skills_rank": re_data.get("skills_rank"),
            # `team_profiles` has no dedicated organisation/location columns
            # (plan §8's DDL only defines bio/robot_name/etc.) — kept in the
            # flexible cached_stats JSONB rather than silently discarded.
            "team_name": team_row.get("teamName") or team_row.get("team_name") or team_row.get("name"),
            # TM's documented fields are `school`, `city`/`state`/`country`
            # (which "will likely be combined into `location`").
            "organisation": team_row.get("organization") or team_row.get("organisation") or team_row.get("school"),
            "location": _team_location(team_row),
            "country": team_row.get("country") or _country_from_location(team_row.get("location")),
            "division_id": tm_data.get("division_id"),
        }

        return {
            "team_number": str(team_number),
            "pit_location": None,
            "bio": re_data.get("bio"),
            "robot_name": re_data.get("robot_name") or team_row.get("robotName"),
            "cached_stats": cached_stats,
            "fetched_at": time.time(),
        }

    async def _upsert_team_profile(self, profile: dict[str, Any]) -> None:
        async with self._session_factory() as session:
            stmt = pg_insert(TeamProfile).values(
                team_number=profile["team_number"],
                bio=profile.get("bio"),
                robot_name=profile.get("robot_name"),
                cached_stats=profile.get("cached_stats"),
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[TeamProfile.team_number],
                set_={
                    "bio": stmt.excluded.bio,
                    "robot_name": stmt.excluded.robot_name,
                    "cached_stats": stmt.excluded.cached_stats,
                },
            )
            await session.execute(stmt)
            await session.commit()

    async def _publish_team_profile_updated(self, team_number: str) -> None:
        event = EventBusMessage(
            entity_id="scraper",
            entity_tags=[],
            type="team_profile_updated",
            timestamp=time.time(),
            payload={"team_number": str(team_number)},
        )
        try:
            await self._redis.publish(EVENTS_CHANNEL, event.model_dump_json())
        except Exception:
            logger.exception("Failed to publish team_profile_updated for %s", team_number)
