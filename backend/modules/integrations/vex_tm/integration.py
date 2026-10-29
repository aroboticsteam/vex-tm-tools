"""Async VEX TM integration client (plan §5.3 / §3.7 / §C.7.3).

Ports the legacy synchronous `modules/tm_manager/{api_client,connector,
schedule_fetcher}.py` to async, applying every fix from plan §3.7:

- OAuth2 bearer token cached in Redis (``qecomp:tm:<entity_id>:token``),
  never a flat file.
- All waits are ``await asyncio.sleep(...)`` — never ``time.sleep()``.
- HMAC ``StringToSign`` includes the full URI path *and* query string.
- ``Host`` header (and the value used in the signature) omits the port for
  standard ports (80/443) and includes it otherwise.
- ``api_key`` is stripped of surrounding whitespace at config-load time.
- Schedule data is cached in Redis (``qecomp:tm:<entity_id>:schedule``),
  never written to the local filesystem.
- Exponential backoff (capped at 5 minutes) on repeated auth/API failures.
- Uses ``websockets.exceptions.InvalidHandshake`` (``InvalidStatusCode`` was
  removed in ``websockets`` >= 11) and ``additional_headers=`` (``websockets``
  >= 11/13's replacement for the old ``extra_headers=`` kwarg).

Per §C.7.3: the persistent ``httpx.AsyncClient`` is created in ``setup()``
and closed in ``teardown()``; the WebSocket receive loop and the schedule
poller both run as ``asyncio.Task``s created in ``setup()`` and cancelled in
``teardown()`` — never ``threading.Timer``.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlparse

import httpx
import websockets

from backend.core.exceptions import IntegrationError
from backend.modules.integrations.base import STATUS_KEY_TMPL, Integration
from backend.schemas.events import EventBusMessage

logger = logging.getLogger(__name__)

DEFAULT_AUTH_URL = "https://auth.vextm.dwabtech.com/oauth2/token"
EVENTS_CHANNEL = "qecomp:events"
# Teams found in the polled schedule, for the scraper to create profiles for
# (kept off `qecomp:events` so it doesn't show up in the live feed/automations).
TEAMS_DISCOVERED_CHANNEL = "qecomp:tm_teams_discovered"
TOKEN_KEY_TMPL = "qecomp:tm:{entity_id}:token"
SCHEDULE_KEY_TMPL = "qecomp:tm:{entity_id}:schedule"

MAX_BACKOFF_SECONDS = 300  # 5 minutes, per plan §3.7

# Scores show up in `/api/matches/{division}` a little after `matchStopped`,
# so re-fetch at these offsets (seconds) instead of waiting for the next
# poll — the notification service reads the cached schedule for score alerts.
POST_MATCH_REFRESH_DELAYS = (10, 20, 30)
# TM's docs ask clients not to poll resources more than ~once a minute.
DEFAULT_POLL_INTERVAL_SECONDS = 60


def _host_header(hostname: str, scheme: str, port: int | None) -> str:
    """Only include the port when it's non-standard for the scheme (§3.7 fix)."""
    if port is None:
        return hostname
    if (scheme == "https" and port == 443) or (scheme == "http" and port == 80):
        return hostname
    return f"{hostname}:{port}"


def _string_to_sign(http_verb: str, uri_path_and_query: str, token: str, host: str, date: str) -> str:
    """VEX TM API HMAC StringToSign (§3.7 fix: includes the query string)."""
    return (
        f"{http_verb.upper()}\n"
        f"{uri_path_and_query}\n"
        f"token:{token}\n"
        f"host:{host}\n"
        f"x-tm-date:{date}\n"
    )


def _sign(api_key: str, string_to_sign: str) -> str:
    return hmac.new(api_key.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _match_tuple_key(match_tuple: dict[str, Any]) -> tuple[Any, ...]:
    """Normalizes a TM `matchTuple` (or the `fieldMatchAssigned` WS event's
    `match` field, which shares the same field names) into a hashable key
    usable to join a live event against the cached `/api/matches/{division}`
    schedule."""
    return (
        _as_int(match_tuple.get("division")),
        _as_int(match_tuple.get("session")),
        str(match_tuple.get("round")),
        _as_int(match_tuple.get("match")),
        _as_int(match_tuple.get("instance")),
    )


class VexTmIntegration(Integration):
    """VEX TM field-set integration: match control, schedule cache, live events."""

    # service name -> function(data) -> websocket command dict
    _SERVICE_COMMANDS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
        "start_match": lambda data: {"cmd": "start"},
        "end_early": lambda data: {"cmd": "endEarly"},
        "abort": lambda data: {"cmd": "abort"},
        "reset": lambda data: {"cmd": "reset"},
        "queue_next_match": lambda data: {"cmd": "queueNextMatch"},
        "queue_prev_match": lambda data: {"cmd": "queuePrevMatch"},
        "set_audience_display": lambda data: {"cmd": "setAudienceDisplay", "display": data["display"]},
        "queue_skills": lambda data: {"cmd": "queueSkills", "skillsID": data["skills_id"]},
    }

    def __init__(self, entity_id: str, config: dict[str, Any], redis: Any, db_pool: Any) -> None:
        super().__init__(entity_id, config, redis, db_pool)
        self._http: httpx.AsyncClient | None = None
        self._auth_http: httpx.AsyncClient | None = None
        self._ws: Any = None
        self._tasks: list[asyncio.Task] = []
        self._shutdown = asyncio.Event()
        self._last_event: dict[str, Any] | None = None
        self._auth_url = self.config.get("auth_url") or DEFAULT_AUTH_URL
        # Match-tuple -> {"divisionId", "matchNum", "round", "redTeams",
        # "blueTeams"} join index, built from the cached `/api/matches/{div}`
        # schedule. `fieldMatchAssigned` events only carry a match tuple
        # (division/round/match/instance/session) — this index lets
        # `_handle_ws_message` resolve it to the two alliances' team numbers
        # before republishing, since that's what every downstream consumer
        # (scraper, ws.py, predictor) actually needs.
        self._match_index: dict[tuple[Any, ...], dict[str, Any]] = {}
        self._refresh_task: asyncio.Task | None = None

    # ── lifecycle ────────────────────────────────────────────────────
    async def setup(self) -> None:
        self.config["api_key"] = (self.config.get("api_key") or "").strip()
        if not self.config.get("base_url"):
            raise IntegrationError(f"vex_tm[{self.entity_id}]: 'base_url' is required")
        if not self.config.get("field_set_id"):
            raise IntegrationError(f"vex_tm[{self.entity_id}]: 'field_set_id' is required")

        self._shutdown = asyncio.Event()
        self._auth_url = self.config.get("auth_url") or DEFAULT_AUTH_URL
        self._http = httpx.AsyncClient(base_url=self.config["base_url"], timeout=10.0)
        self._auth_http = httpx.AsyncClient(timeout=10.0)

        # Fail fast (bounded attempts) so the loader can mark this DEGRADED
        # and retry in the background rather than hanging setup() forever.
        await self._get_token(max_attempts=5)

        self._tasks = [
            asyncio.create_task(self._supervised_receive_loop()),
            asyncio.create_task(self._schedule_poll_loop()),
        ]

    async def teardown(self) -> None:
        try:
            self._shutdown.set()
            if self._refresh_task is not None:
                self._tasks.append(self._refresh_task)
                self._refresh_task = None
            for task in self._tasks:
                task.cancel()
            if self._tasks:
                await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks = []
            if self._ws is not None:
                with contextlib.suppress(Exception):
                    await self._ws.close()
                self._ws = None
            if self._http is not None:
                with contextlib.suppress(Exception):
                    await self._http.aclose()
                self._http = None
            if self._auth_http is not None:
                with contextlib.suppress(Exception):
                    await self._auth_http.aclose()
                self._auth_http = None
        except Exception:
            logger.exception("Error during vex_tm teardown for %s", self.entity_id)

    # ── services ─────────────────────────────────────────────────────
    async def call_service(self, service: str, data: dict[str, Any]) -> dict[str, Any]:
        data = data or {}
        builder = self._SERVICE_COMMANDS.get(service)
        if builder is None:
            raise ValueError(f"Unknown service '{service}' for vex_tm integration")
        if self._ws is None:
            raise IntegrationError(f"vex_tm[{self.entity_id}]: WebSocket is not connected")
        message = builder(data)
        await self._ws.send(json.dumps(message))
        return {"sent": message}

    async def get_state(self) -> dict[str, Any]:
        try:
            status = await self._redis.get(STATUS_KEY_TMPL.format(entity_id=self.entity_id)) or "DISCONNECTED"
        except Exception:
            status = "UNKNOWN"
        return {
            "entity_id": self.entity_id,
            "connected": self._ws is not None,
            "status": status,
            "last_event": self._last_event,
        }

    # ── OAuth2 token (Redis-cached, §3.7) ────────────────────────────
    async def _get_token(self, force: bool = False, max_attempts: int | None = None) -> str:
        key = TOKEN_KEY_TMPL.format(entity_id=self.entity_id)
        if not force:
            cached = await self._redis.get(key)
            if cached:
                return cached
        return await self._fetch_token(max_attempts=max_attempts)

    async def _fetch_token(self, max_attempts: int | None = None) -> str:
        assert self._auth_http is not None
        backoff = 1
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = await self._auth_http.post(
                    self._auth_url,
                    auth=(self.config.get("client_id", ""), self.config.get("client_secret", "")),
                    data={"grant_type": "client_credentials"},
                )
            except httpx.HTTPError as exc:
                logger.warning("vex_tm[%s]: auth request failed: %s", self.entity_id, exc)
                if max_attempts is not None and attempt >= max_attempts:
                    raise IntegrationError(f"vex_tm[{self.entity_id}]: could not reach auth server") from exc
                await asyncio.sleep(min(backoff, MAX_BACKOFF_SECONDS))
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                continue

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", backoff))
                if max_attempts is not None and attempt >= max_attempts:
                    raise IntegrationError(f"vex_tm[{self.entity_id}]: auth rate-limited, giving up")
                await asyncio.sleep(min(retry_after, MAX_BACKOFF_SECONDS))
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                continue

            if resp.status_code >= 500:
                if max_attempts is not None and attempt >= max_attempts:
                    raise IntegrationError(f"vex_tm[{self.entity_id}]: auth server error {resp.status_code}")
                await asyncio.sleep(min(backoff, MAX_BACKOFF_SECONDS))
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                continue

            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise IntegrationError(f"vex_tm[{self.entity_id}]: auth failed: {exc}") from exc

            token_data = resp.json()
            token = token_data["access_token"]
            expires_in = int(token_data.get("expires_in", 3600))
            key = TOKEN_KEY_TMPL.format(entity_id=self.entity_id)
            await self._redis.set(key, token, ex=max(expires_in - 60, 30))
            return token

    # ── WebSocket receive loop ───────────────────────────────────────
    async def _open_ws(self):
        token = await self._get_token()
        parsed = urlparse(self.config["base_url"])
        ws_scheme = "wss" if parsed.scheme == "https" else "ws"
        host = _host_header(parsed.hostname, parsed.scheme, parsed.port)
        uri_path = f"/api/fieldsets/{self.config['field_set_id']}"
        date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
        signature = _sign(self.config["api_key"], _string_to_sign("GET", uri_path, token, host, date))

        headers = {
            "Authorization": f"Bearer {token}",
            "x-tm-date": date,
            "x-tm-signature": signature,
        }
        ws_netloc = f"{parsed.hostname}:{parsed.port}" if parsed.port else parsed.hostname
        ws_url = f"{ws_scheme}://{ws_netloc}{uri_path}"
        return await websockets.connect(ws_url, additional_headers=headers)

    async def _supervised_receive_loop(self) -> None:
        """Runs `_receive_loop` and guarantees it self-heals: `_receive_loop`
        itself already retries on every connect/receive failure it knows
        about, but this is a belt-and-suspenders outer guard so that *any*
        exception which somehow still escapes it (a bug, an unanticipated
        exception type from the websockets/httpx internals, a spurious
        `asyncio.CancelledError` not caused by our own `teardown()`, ...)
        gets logged loudly and the loop is restarted, instead of the task
        quietly dying and leaving this integration stuck DISCONNECTED for
        the rest of the process's life (bug found in QA pass 2026-09-20)."""
        while not self._shutdown.is_set():
            try:
                await self._receive_loop()
            except asyncio.CancelledError:
                if self._shutdown.is_set():
                    raise
                logger.warning(
                    "vex_tm[%s]: receive loop task cancelled unexpectedly (not a shutdown); restarting",
                    self.entity_id,
                )
            except Exception:
                logger.exception(
                    "vex_tm[%s]: receive loop crashed unexpectedly; restarting", self.entity_id
                )
            if not self._shutdown.is_set():
                # `_receive_loop` should never return normally while we're
                # not shutting down — if it did (or just crashed), don't
                # spin a hot loop recreating it.
                await asyncio.sleep(1)

    async def _receive_loop(self) -> None:
        backoff = 1
        while not self._shutdown.is_set():
            try:
                ws = await self._open_ws()
            except asyncio.CancelledError:
                # A real shutdown-triggered cancellation must propagate so
                # the task actually stops; anything else (e.g. a spurious
                # cancellation racing with `_get_token()`/`websockets.connect`
                # internals) must NOT silently kill the whole loop with zero
                # logging — treat it like any other transient connect
                # failure and keep retrying with backoff.
                if self._shutdown.is_set():
                    raise
                logger.warning(
                    "vex_tm[%s]: WS connect cancelled unexpectedly; retrying", self.entity_id
                )
                await asyncio.sleep(min(backoff, 60))
                backoff = min(backoff * 2, 60)
                continue
            except websockets.exceptions.InvalidHandshake as exc:
                # Replacement for the removed InvalidStatusCode (§3.7 fix).
                logger.warning("vex_tm[%s]: WS handshake rejected: %s", self.entity_id, exc)
                await asyncio.sleep(min(backoff, 60))
                backoff = min(backoff * 2, 60)
                continue
            except Exception as exc:
                logger.warning("vex_tm[%s]: WS connect failed: %s", self.entity_id, exc)
                await asyncio.sleep(min(backoff, 60))
                backoff = min(backoff * 2, 60)
                continue

            backoff = 1
            self._ws = ws
            try:
                async for raw in ws:
                    await self._handle_ws_message(raw)
            except websockets.exceptions.ConnectionClosed as exc:
                logger.info("vex_tm[%s]: WS closed (%s), reconnecting", self.entity_id, exc)
            except asyncio.CancelledError:
                if self._shutdown.is_set():
                    raise
                logger.warning(
                    "vex_tm[%s]: WS receive loop cancelled unexpectedly; reconnecting", self.entity_id
                )
            except Exception:
                logger.exception("vex_tm[%s]: WS receive loop error", self.entity_id)
            finally:
                self._ws = None
                with contextlib.suppress(Exception):
                    await ws.close()
            if not self._shutdown.is_set():
                await asyncio.sleep(min(backoff, 5))

    async def _handle_ws_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("vex_tm[%s]: could not decode WS message: %s", self.entity_id, raw)
            return
        event_type = data.get("type")
        if not event_type:
            return

        payload = data
        if event_type == "fieldMatchAssigned":
            payload = await self._enrich_field_match_assigned(data)
        elif event_type == "matchStopped":
            self._schedule_post_match_refresh()

        self._last_event = payload
        message = EventBusMessage(
            entity_id=self.entity_id,
            entity_tags=list(self.tags),
            type=event_type,
            timestamp=time.time(),
            payload=payload,
        )
        try:
            await self._redis.publish(EVENTS_CHANNEL, message.model_dump_json())
        except Exception:
            logger.exception("vex_tm[%s]: failed to publish event to Redis", self.entity_id)

    async def _enrich_field_match_assigned(self, data: dict[str, Any]) -> dict[str, Any]:
        """The real TM `fieldMatchAssigned` event only carries a match tuple
        (``{"division":1,"session":0,"round":"QUAL","match":2,"instance":1}``)
        — never team numbers. Join it against the cached `/api/matches/{div}`
        schedule (fetching/indexing on demand on a cache miss) and return the
        event payload augmented with `divisionId`/`matchNum`/`round`/
        `redTeams`/`blueTeams` so scraper.py, routers/ws.py, and predictor.py
        (which all read those keys straight off the payload) keep working
        against a real TM server."""
        match_tuple = data.get("match")
        if not isinstance(match_tuple, dict) or match_tuple.get("division") is None:
            return data

        key = _match_tuple_key(match_tuple)
        entry = self._match_index.get(key)
        if entry is None:
            division = match_tuple.get("division")
            try:
                await self._fetch_and_index_division(division)
            except Exception:
                logger.exception(
                    "vex_tm[%s]: failed to fetch schedule for division %s to resolve match %s",
                    self.entity_id,
                    division,
                    match_tuple,
                )
            entry = self._match_index.get(key)

        if entry is None:
            logger.warning(
                "vex_tm[%s]: no schedule entry for match %s; forwarding event unenriched",
                self.entity_id,
                match_tuple,
            )
            return data

        return {
            **data,
            "divisionId": entry["divisionId"],
            "matchNum": entry["matchNum"],
            "round": entry["round"],
            "redTeams": entry["redTeams"],
            "blueTeams": entry["blueTeams"],
        }

    def _schedule_post_match_refresh(self) -> None:
        """After a match stops, re-fetch the schedule a few times so its final
        score reaches the cache quickly. A newer stop restarts the sequence."""
        if self._shutdown.is_set():
            return
        if self._refresh_task is not None and not self._refresh_task.done():
            self._refresh_task.cancel()
        self._refresh_task = asyncio.create_task(self._post_match_refresh())

    async def _post_match_refresh(self) -> None:
        for delay in POST_MATCH_REFRESH_DELAYS:
            await asyncio.sleep(delay)
            try:
                await self._fetch_and_cache_schedule()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("vex_tm[%s]: post-match schedule refresh failed", self.entity_id)

    # ── REST GET (signed) ────────────────────────────────────────────
    async def _get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        assert self._http is not None
        parsed = urlparse(self.config["base_url"])
        host = _host_header(parsed.hostname, parsed.scheme, parsed.port)

        request = self._http.build_request("GET", endpoint, params=params)
        raw_path = request.url.raw_path
        uri_path_and_query = raw_path.decode("utf-8") if isinstance(raw_path, (bytes, bytearray)) else str(raw_path)

        token = await self._get_token()
        backoff = 1
        last_exc: Exception | None = None
        for attempt in range(5):
            date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
            signature = _sign(self.config["api_key"], _string_to_sign("GET", uri_path_and_query, token, host, date))
            headers = {
                "Authorization": f"Bearer {token}",
                "x-tm-date": date,
                "x-tm-signature": signature,
            }
            resp = await self._http.get(endpoint, params=params, headers=headers)

            if resp.status_code == 401 and attempt == 0:
                # Token may be stale server-side; force a refresh once.
                token = await self._get_token(force=True)
                continue
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", backoff))
                await asyncio.sleep(min(retry_after, MAX_BACKOFF_SECONDS))
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                continue
            try:
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                await asyncio.sleep(min(backoff, MAX_BACKOFF_SECONDS))
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                continue
        raise IntegrationError(f"vex_tm[{self.entity_id}]: GET {endpoint} failed after retries") from last_exc

    # ── Schedule poller (Redis-cached, §3.7) ─────────────────────────
    async def _schedule_poll_loop(self) -> None:
        interval = int(self.config.get("poll_interval_seconds") or DEFAULT_POLL_INTERVAL_SECONDS)
        while not self._shutdown.is_set():
            try:
                await self._fetch_and_cache_schedule()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("vex_tm[%s]: schedule fetch failed", self.entity_id)
            await asyncio.sleep(interval)

    async def _fetch_and_cache_schedule(self) -> None:
        divisions_data = await self._get("/api/divisions")
        divisions = divisions_data.get("divisions", [])

        # Each division's matches are an independent REST round-trip (its
        # own HMAC signing + network hop) — fetch them concurrently rather
        # than one at a time so poll latency doesn't scale linearly with
        # the number of divisions.
        matches_results = await asyncio.gather(
            *(self._get(f"/api/matches/{division['id']}") for division in divisions)
        )

        schedule: dict[str, Any] = {"divisions": []}
        for division, matches_data in zip(divisions, matches_results):
            matches = matches_data.get("matches", [])
            schedule["divisions"].append(
                {"id": division["id"], "name": division.get("name"), "matches": matches}
            )
            self._index_division_matches(division["id"], matches)

        key = SCHEDULE_KEY_TMPL.format(entity_id=self.entity_id)
        await self._redis.set(key, json.dumps(schedule))

        # Every team that appears in the schedule, so the scraper can create
        # profiles up front instead of only when a match is first queued.
        teams = sorted(
            {t for entry in self._match_index.values() for t in (*entry["redTeams"], *entry["blueTeams"]) if t},
        )
        if teams:
            try:
                await self._redis.publish(
                    TEAMS_DISCOVERED_CHANNEL, json.dumps({"entity_id": self.entity_id, "teams": teams})
                )
            except Exception:
                logger.exception("vex_tm[%s]: failed to publish discovered teams", self.entity_id)

    async def _fetch_and_index_division(self, division_id: Any) -> None:
        """On-demand fetch of a single division's schedule, for the cache-miss
        path in `_enrich_field_match_assigned` (e.g. right at process start,
        before the poll loop's first tick has populated `_match_index`)."""
        matches_data = await self._get(f"/api/matches/{division_id}")
        self._index_division_matches(division_id, matches_data.get("matches", []))

    def _index_division_matches(self, division_id: Any, matches: list[dict[str, Any]]) -> None:
        for m in matches or []:
            try:
                key, entry = self._build_match_index_entry(division_id, m)
            except Exception:
                logger.exception(
                    "vex_tm[%s]: could not index match %r for division %s", self.entity_id, m, division_id
                )
                continue
            self._match_index[key] = entry

    @staticmethod
    def _build_match_index_entry(division_id: Any, m: dict[str, Any]) -> tuple[tuple[Any, ...], dict[str, Any]]:
        """Parses one `/api/matches/{division_id}` entry (per
        `matchInfo.alliances`/`matchInfo.matchTuple`, docs `Match List
        Resource`) into a `(key, entry)` pair for `_match_index`. Per VEX TM
        convention, alliance index 0 is red and index 1 is blue. Falls back
        to a flatter `redTeams`/`blueTeams`/`matchNum` shape for any
        deployment (or test double) that doesn't nest under `matchInfo`."""
        match_info = m.get("matchInfo") or {}
        match_tuple = match_info.get("matchTuple")
        alliances = match_info.get("alliances")

        if match_tuple:
            round_ = match_tuple.get("round")
            match_num = match_tuple.get("match")
            key = _match_tuple_key(match_tuple)
        else:
            round_ = m.get("round")
            match_num = m.get("matchNum") or m.get("match_num") or m.get("number")
            key = _match_tuple_key(
                {
                    "division": division_id,
                    "session": m.get("session", 0),
                    "round": round_,
                    "match": match_num,
                    "instance": m.get("instance", 1),
                }
            )

        if alliances:
            red_teams = [str(t.get("number")) for t in ((alliances[0].get("teams") if len(alliances) > 0 else []) or [])]
            blue_teams = [str(t.get("number")) for t in ((alliances[1].get("teams") if len(alliances) > 1 else []) or [])]
        else:
            red_teams = [str(t) for t in (m.get("redTeams") or [])]
            blue_teams = [str(t) for t in (m.get("blueTeams") or [])]

        entry = {
            "divisionId": _as_int(division_id),
            "matchNum": match_num,
            "round": round_,
            "redTeams": red_teams,
            "blueTeams": blue_teams,
        }
        return key, entry


# The class the loader imports dynamically (see backend/loader.py's
# fallback-scan for any `Integration` subclass if this name is absent).
IntegrationClass = VexTmIntegration
