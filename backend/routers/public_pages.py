"""Public, unauthenticated pages for the room/notification feature (issue #5):

- `GET /room/{slug}`           room display: livestream embed + live notices
- `GET /notify`                public "follow any teams" page (no QR/room/code needed)
- `GET /sw.js`                 service worker (must live at the root scope)
- `GET /notify.webmanifest`    PWA manifest (iOS only delivers push to installed web apps)

Same conventions as `static_pages.py`: static HTML read once at import,
values JSON-encoded before substitution into the page's `<script>`, and the
only server-side check is that the room exists (so a stale QR 404s). All data
the pages show comes from the public `/api/v1/public/*` endpoints and the
public `/ws/room/{slug}` socket.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.dependencies import get_db
from backend.models.comms import Station
from backend.models.room import Room
from backend.schemas.room import SLUG_PATTERN

router = APIRouter(tags=["public-pages"])

_STATIC_DIR = Path(__file__).parent.parent / "static"
_ROOM_HTML = (_STATIC_DIR / "room.html").read_text(encoding="utf-8")
_NOTIFY_HTML = (_STATIC_DIR / "notify.html").read_text(encoding="utf-8")
_SITE_HTML = (_STATIC_DIR / "site.html").read_text(encoding="utf-8")
_LEADERBOARD_HTML = (_STATIC_DIR / "leaderboard.html").read_text(encoding="utf-8")
_STATION_HTML = (_STATIC_DIR / "station.html").read_text(encoding="utf-8")
_SW_JS = (_STATIC_DIR / "sw.js").read_text(encoding="utf-8")

_SLUG_RE = re.compile(SLUG_PATTERN)


async def _require_room(db: AsyncSession, slug: str) -> Room:
    room = await db.get(Room, slug) if _SLUG_RE.match(slug) else None
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found")
    return room


@router.get("/room/{slug}", response_class=HTMLResponse)
async def serve_room(slug: str, db: Annotated[AsyncSession, Depends(get_db)]) -> HTMLResponse:
    await _require_room(db, slug)
    return HTMLResponse(_ROOM_HTML.replace('"__ROOM_SLUG__"', json.dumps(slug)))


@router.get("/site", response_class=HTMLResponse)
async def serve_site() -> HTMLResponse:
    """Team-facing public website (issue #10): one page, hash-routed, fed by
    `/api/v1/public/site/*`."""
    return HTMLResponse(_SITE_HTML)


@router.get("/site/leaderboard", response_class=HTMLResponse)
async def serve_leaderboard() -> HTMLResponse:
    """Big-screen rotating leaderboards (issue #28): overall / tournament /
    skills / awards per panel interval, IQ <-> VRC per programme interval."""
    return HTMLResponse(_LEADERBOARD_HTML)


@router.get("/station/{slug}", response_class=HTMLResponse)
async def serve_station(slug: str, db: Annotated[AsyncSession, Depends(get_db)]) -> HTMLResponse:
    station = await db.get(Station, slug) if _SLUG_RE.match(slug) else None
    if station is None:
        raise HTTPException(status_code=404, detail="Station not found")
    return HTMLResponse(_STATION_HTML.replace('"__STATION_SLUG__"', json.dumps(slug)))


@router.get("/notify", response_class=HTMLResponse)
async def serve_notify() -> HTMLResponse:
    # The team list is read client-side from `/api/v1/public/site/teams`.
    return HTMLResponse(_NOTIFY_HTML)


@router.get("/sw.js")
async def serve_service_worker() -> Response:
    return Response(
        _SW_JS,
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


@router.get("/notify.webmanifest")
async def notify_manifest() -> JSONResponse:
    return JSONResponse(
        {
            "name": "TMTools Match Alerts",
            "short_name": "Match Alerts",
            "start_url": "/notify",
            "scope": "/",
            "display": "standalone",
            "background_color": "#0f1115",
            "theme_color": "#0f1115",
            "icons": [{"src": "/favicon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}],
        },
        media_type="application/manifest+json",
    )
