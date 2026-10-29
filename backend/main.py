"""FastAPI app factory + lifespan (plan §B.10 / §C.2).

Per §C.2, this is the ONLY file permitted to import and instantiate
`LeaderElection`, `AutomationEngine`, `TimerManager`, `Scraper`, `Predictor`,
and the `Loader`, and to wire `on_promoted`/`on_demoted` callbacks.

Wave 2b added `TimerManager` (`backend.modules.timer.manager`) to that
wiring alongside the `LeaderElection` Wave 1 already built. Wave 3a
(Automation Engine) adds the `Loader` (`backend.loader`) and
`AutomationEngine` (`backend.modules.automation.engine`) the same way:
instantiated here, started/stopped from `on_promoted`/`on_demoted`, exposed
on `app.state` only while this node is the leader. Wave 3b adds the
auth/RBAC/integrations/overlays/settings routers and the `/ws/*` WebSocket
routes (and the `ConnectionManager`'s Redis fan-out, which runs on every
node regardless of leadership — see comment below). Wave 4b wires in
`Scraper` (`backend.modules.scraper.scraper`) and `Predictor`
(`backend.modules.predictor.predictor`) the same way.

Run with (per Appendix B.1 — exactly 1 worker per pod):

    uvicorn backend.main:app --host 0.0.0.0 --port 8000 --workers 1
"""
from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import json
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.db import async_session_factory, check_db_connection, engine, get_db
from backend.core.redis import check_redis_connection, redis_client
from backend.core.request_context import RequestContextMiddleware
from backend.core.security import validate_prompter_token
from backend.core.settings import settings
from backend.loader import Loader
from backend.modules.automation.engine import AutomationEngine
from backend.modules.leader import LeaderElection
from backend.modules.notifications.service import NotificationService
from backend.modules.predictor.predictor import Predictor
from backend.modules.scraper.scraper import Scraper
from backend.modules.timer.manager import TimerManager
from backend.routers.audit import router as audit_router
from backend.routers.auth import router as auth_router
from backend.routers.button_pages import router as button_pages_router
from backend.routers.automations import router as automations_router
from backend.routers.debug import router as debug_router
from backend.routers.integrations import router as integrations_router
from backend.routers.notifications import router as notifications_router
from backend.routers.overlays import router as overlays_router
from backend.routers.public_pages import router as public_pages_router
from backend.routers.rooms import router as rooms_router
from backend.routers.scripts import router as scripts_router
from backend.routers.settings import router as settings_router
from backend.routers.static_pages import router as static_pages_router
from backend.routers.status import router as status_router
from backend.routers.team_clips import router as team_clips_router
from backend.routers.teams import router as teams_router
from backend.routers.timers import get_timer_or_404
from backend.routers.timers import router as timers_router
from backend.routers.ws import manager as ws_manager
from backend.routers.ws import router as ws_router
from backend.schemas.health import HealthResponse, ReadyResponse


def _configure_logging() -> None:
    """Configure the root logger (Bug: every `logger.info(...)` call in the
    app — leader promotion/demotion, "WS closed, reconnecting", integration
    status transitions, etc. — was previously silently discarded because
    nothing ever called `logging.basicConfig()`/set a level on the root
    logger, so Python's default root logger level (WARNING) swallowed them.
    Only `.warning`/`.exception` calls were ever visible in `docker logs`.

    Configurable via `LOG_LEVEL` (default INFO) so this can be turned down/up
    per-environment without a code change. This is deliberately a one-time
    startup call, not a logging framework — a plain `basicConfig` with a
    timestamp/level/logger-name format is all the app needs.
    """
    level_name = settings.LOG_LEVEL.upper()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        level = logging.INFO
        level_name = "INFO"
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    logging.getLogger(__name__).info("Logging configured at level %s", level_name)


_configure_logging()

_STATIC_DIR = Path(__file__).parent / "static"

# Wave 4a's built React/Vite SPA (`frontend/`), baked into this location by
# the Dockerfile's `frontend-builder` stage (see `Dockerfile`). Deliberately
# absent in a bare `backend/` checkout (e.g. running `uvicorn backend.main:app`
# straight from a venv during backend-only development) — every use below is
# guarded on `_FRONTEND_DIST.is_dir()` so the app still boots and serves the
# API-only surface in that case, matching this file's existing "never crash
# on a missing optional dependency" posture (Appendix A.10).
_FRONTEND_DIST = _STATIC_DIR / "frontend"

logger = logging.getLogger(__name__)

# Read once at import time and cached in memory — per Appendix C.5's
# no-blocking-I/O-in-request-handlers rule, `serve_prompter` below must not
# do a synchronous file read on every request. `prompter.html` is a static
# asset baked into the image, so a process restart is required (and already
# expected) to pick up a change to it.
_PROMPTER_HTML = (_STATIC_DIR / "prompter.html").read_text(encoding="utf-8")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup/shutdown orchestration.

    Verifies DB/Redis connectivity (best-effort; never crash per Appendix
    A.10), exposes them on `app.state`, and runs leader election + the
    `TimerManager` / `Loader` / `AutomationEngine` (only the leader
    instantiates/runs these — plan §3.1/§5.1/§5.2). The WS
    `ConnectionManager`'s Redis fan-out runs on every node regardless of
    leadership (see comment below).

    ─────────────────────────────────────────────────────────────────────
    Wave 4b wires `Scraper`/`Predictor` in below, the same way
    `timer_manager`/`loader`/`automation_engine` are: instantiated here,
    started/stopped from `on_promoted`/`on_demoted`, and (leader-only)
    exposed on `app.state`. `app.state.predictor` is additionally what
    `ws_manager.get_predictor` (wired below) resolves against, so
    `/ws/prompter/{entity_id}`'s `high_potential` flag lookup only works
    while this node is the leader — matching the passive-node semantics
    already established for `timer_manager`/`automation_engine`.
    ─────────────────────────────────────────────────────────────────────
    """
    db_ok = await check_db_connection()
    redis_ok = await check_redis_connection()
    if not db_ok:
        logger.warning("Postgres not reachable at startup — /readyz will report degraded")
    if not redis_ok:
        logger.warning(
            "Redis not reachable at startup (Appendix A.10: app degrades gracefully, "
            "does not crash)"
        )

    timer_manager = TimerManager(redis_client)
    loader = Loader(redis_client, async_session_factory)
    automation_engine = AutomationEngine(redis_client, async_session_factory)
    scraper = Scraper(redis_client, async_session_factory)
    predictor = Predictor(redis_client, async_session_factory)
    notification_service = NotificationService(redis_client, async_session_factory)
    # Deliberately NOT set on `app.state` here — only the leader runs these
    # (plan §3.1/§5.1/§5.2). They're set in `on_promoted` and cleared in
    # `on_demoted` below so that `backend.routers.timers._require_timer_manager`
    # / `backend.routers.automations._require_engine`'s 503 ("not the
    # current leader") and `/ws/prompter/{entity_id}`'s equivalent close code
    # are actually reachable on a passive node instead of finding a
    # constructed-but-never-started manager with an empty config cache
    # (which previously surfaced as a misleading 404/empty registry).
    app.state.timer_manager = None
    app.state.automation_engine = None
    app.state.loader = None
    app.state.predictor = None

    # WS ConnectionManager's background Redis pub/sub fan-out runs on every
    # node (both leader and passive pods forward `qecomp:events` messages to
    # their own connected WebSocket clients) — unlike TimerManager/Loader/
    # AutomationEngine, it holds no leader-only state, so it starts/stops
    # with the app itself rather than with promotion/demotion.
    ws_manager.get_timer_manager = lambda: app.state.timer_manager
    ws_manager.get_predictor = lambda: getattr(app.state, "predictor", None)
    await ws_manager.start()

    leader = LeaderElection(redis_client, port=settings.APP_PORT)

    async def on_promoted() -> None:
        # Loader first: AutomationEngine's `service`/`target_tag` actions
        # resolve entities exclusively via `backend.loader.get_instance()`/
        # `get_instances_by_tag()` (plan §C.5 rule 4), so integration
        # instances must be live before the engine starts processing
        # `qecomp:events`.
        await loader.load_all()
        await timer_manager.start()
        await automation_engine.start()
        await scraper.start()
        await predictor.start()
        await notification_service.start()
        app.state.timer_manager = timer_manager
        app.state.automation_engine = automation_engine
        app.state.loader = loader
        app.state.predictor = predictor

    async def on_demoted() -> None:
        app.state.timer_manager = None
        app.state.automation_engine = None
        app.state.loader = None
        app.state.predictor = None
        await notification_service.stop()
        await predictor.stop()
        await scraper.stop()
        await automation_engine.stop()
        await timer_manager.stop()
        await loader.teardown_all()

    leader.on_promoted = on_promoted
    leader.on_demoted = on_demoted
    app.state.leader = leader
    await leader.start()

    yield

    # Shutdown. Split-brain protection (plan §3.1): tear down every
    # leader-only service BEFORE releasing the Redis leader lock.
    # `LeaderElection.stop()` releases the lock unconditionally and does NOT
    # invoke `on_demoted` — that callback only fires from the election
    # loop's renewal-failure/RedisError branches — so calling `leader.stop()`
    # first would let a standby node acquire the lock and start its own
    # TimerManager/AutomationEngine/Loader while this node's tick loops/
    # pub-sub listeners/integration clients are still mid-teardown, causing
    # duplicate `qecomp:events` publishers and dual-command integration
    # conflicts (ATEM/OSC/Spotify).
    app.state.timer_manager = None
    app.state.automation_engine = None
    app.state.loader = None
    app.state.predictor = None
    try:
        await notification_service.stop()
    except Exception:
        logger.exception("Error stopping NotificationService during shutdown")
    try:
        await predictor.stop()
    except Exception:
        logger.exception("Error stopping Predictor during shutdown")
    try:
        await scraper.stop()
    except Exception:
        logger.exception("Error stopping Scraper during shutdown")
    try:
        await automation_engine.stop()
    except Exception:
        logger.exception("Error stopping AutomationEngine during shutdown")
    try:
        await timer_manager.stop()
    except Exception:
        logger.exception("Error stopping TimerManager during shutdown")
    try:
        await loader.teardown_all()
    except Exception:
        logger.exception("Error tearing down Loader instances during shutdown")
    try:
        await leader.stop()
    except Exception:
        logger.exception("Error stopping leader election during shutdown")
    try:
        await ws_manager.stop()
    except Exception:
        logger.exception("Error stopping WS ConnectionManager during shutdown")
    try:
        await redis_client.aclose()
    except Exception:
        logger.exception("Error closing Redis client during shutdown")
    try:
        await engine.dispose()
    except Exception:
        logger.exception("Error disposing DB engine during shutdown")


def create_app() -> FastAPI:
    # Fail loudly at startup rather than silently signing session cookies /
    # prompter HMAC tokens (backend/core/sessions.py, backend/core/security.py)
    # with a known default — same "fail on use, don't silently default"
    # posture as ENCRYPTION_KEY's EncryptionError in backend/core/security.py.
    if not settings.SECRET_KEY:
        raise RuntimeError("SECRET_KEY is not set. Refusing to start.")

    app = FastAPI(
        title="TMTools",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.add_middleware(RequestContextMiddleware)

    @app.get("/healthz", response_model=HealthResponse, tags=["health"])
    async def healthz() -> HealthResponse:
        """Liveness probe. No dependency checks — must always return 200 as
        long as the process is alive."""
        return HealthResponse(status="ok")

    @app.get("/readyz", tags=["health"])
    async def readyz():
        """Readiness probe. Checks DB + Redis connectivity.

        Per Appendix A.10, Redis being down must degrade gracefully rather
        than crash — Postgres remains the source of truth. We report 503 only
        when the *response itself* indicates the instance can't serve
        traffic acceptably; DB-only ("degraded") status is still reported
        with a 200-compatible payload plus an explicit `status` field, but we
        still return 503 to k8s readiness when DB is unreachable (Postgres is
        the source of truth and required), while a Redis-only outage keeps
        the pod marked ready and "degraded".
        """
        db_ok = await check_db_connection()
        redis_ok = await check_redis_connection()

        # "ok" only when everything is reachable; any single outage is
        # reported as "degraded" (DB-down is additionally reflected in the
        # HTTP status code below, since Postgres is the required source of
        # truth while Redis-only outages are a documented graceful-
        # degradation mode per Appendix A.10).
        status = "ok" if (db_ok and redis_ok) else "degraded"

        payload = ReadyResponse(status=status, db=db_ok, redis=redis_ok)
        # Only a dead Postgres makes the pod truly not-ready; Redis-only
        # outages are a documented graceful-degradation mode (Appendix A.10).
        http_status = 200 if db_ok else 503
        return JSONResponse(status_code=http_status, content=payload.model_dump())

    app.include_router(teams_router)
    app.include_router(team_clips_router)
    app.include_router(rooms_router)
    app.include_router(button_pages_router)
    app.include_router(notifications_router)
    app.include_router(auth_router)
    app.include_router(integrations_router)
    app.include_router(overlays_router)
    app.include_router(settings_router)
    app.include_router(ws_router)
    app.include_router(audit_router)
    app.include_router(status_router)
    app.include_router(debug_router)
    # `/overlay/{entity_id}` (standalone HTML, plan Appendix B.3) and
    # `/prompter` (bare teleprompter index, Appendix B.4) — registered here,
    # before the SPA catch-all below, same reasoning as `/prompter/{entity_id}`.
    app.include_router(static_pages_router)
    app.include_router(public_pages_router)

    @app.get("/prompter/{entity_id}", response_class=HTMLResponse, tags=["prompter"])
    async def serve_prompter(
        entity_id: str, token: str = "", session: AsyncSession = Depends(get_db)
    ):
        """Serves the standalone teleprompter page (plan Appendix B.3/B.4),
        with `entity_id`/`token` injected into a `<script>` block per the
        plan's templating instructions.

        Per Appendix B.8, the HMAC token is validated here (in addition to
        Wave 3's WebSocket-upgrade-time validation) purely so an invalid/
        stale link 403s immediately on page load rather than only failing
        silently once the WebSocket tries to connect. This is a page-level
        courtesy check, not a substitute for Wave 3's own validation at
        `/ws/prompter/{entity_id}`. Validation uses the same constant-time
        `validate_prompter_token` helper Wave 3's WS endpoint is expected to
        use, rather than a plain `!=` comparison, to avoid leaking timing
        information about the correct token.
        """
        # Reuses routers/timers.py's lookup (same 404 semantics) instead of
        # duplicating the query here.
        row = await get_timer_or_404(session, entity_id)

        if not validate_prompter_token(row.entity_id, row.token_nonce, token):
            raise HTTPException(status_code=403, detail="Invalid or expired teleprompter token")

        # JSON-encode before substitution (not a plain string replace) so an
        # entity_id/token containing quotes or other JS-special characters
        # can't break out of the `entityId: "__ENTITY_ID__"` string literal
        # and inject script into the page.
        html = _PROMPTER_HTML.replace('"__ENTITY_ID__"', json.dumps(entity_id))
        html = html.replace('"__TOKEN__"', json.dumps(token))
        return HTMLResponse(content=html)

    app.include_router(timers_router)
    app.include_router(automations_router)
    app.include_router(scripts_router)

    # Wave 4a SPA static serving — registered LAST so every API/WS/prompter
    # route above always wins the match first; this only ever catches
    # requests nothing else claimed. No-ops entirely if the image wasn't
    # built with the frontend baked in (see `_FRONTEND_DIST` above), so
    # backend-only dev/test environments are unaffected.
    if _FRONTEND_DIST.is_dir():
        assets_dir = _FRONTEND_DIST / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="spa-assets")

        _spa_index = _FRONTEND_DIST / "index.html"

        @app.get("/{full_path:path}", include_in_schema=False)
        async def serve_spa(full_path: str) -> FileResponse:
            """Client-side-routing fallback for the React SPA.

            Only reachable for GET requests that matched none of the routes
            registered above (this is the last route FastAPI/Starlette
            tries), so deep-linking or refreshing on an SPA route like
            `/dashboard` still serves `index.html` (React Router then
            resolves the path client-side) instead of 404ing.

            `full_path` is attacker-controlled, so we resolve and re-check
            containment under `_FRONTEND_DIST` before serving it (path traversal).
            """
            candidate = (_FRONTEND_DIST / full_path).resolve()
            if (
                full_path
                and candidate.is_file()
                and candidate.is_relative_to(_FRONTEND_DIST.resolve())
            ):
                return FileResponse(candidate)
            return FileResponse(_spa_index)

    return app


app = create_app()
