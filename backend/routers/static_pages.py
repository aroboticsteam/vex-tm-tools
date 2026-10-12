"""Standalone HTML page routes that are not part of the React SPA and not
under `/api/v1` (plan Appendix B.3 "Overlay and Teleprompter Pages" /
Appendix B.4 "Teleprompter Index Page").

Mirrors `backend/main.py`'s existing `GET /prompter/{entity_id}` route
(`serve_prompter`) in every particular this module's docstrings call out:
static HTML read once at import time (never a blocking per-request file
read, per Appendix C.5), `entity_id`/token values JSON-encoded before
substitution into the page's `<script>` block (never a raw string
interpolation, so a value containing quotes can't break out of the JS
string literal and inject script), and an `HTMLResponse` return.

Per plan §C.2 this router carries no business logic beyond the page-render
courtesy checks the plan explicitly calls for (entity existence, token
validity) — everything else is delegated to the corresponding `/ws/*`
WebSocket endpoints, which independently re-validate at upgrade time.
"""
from __future__ import annotations

import html as html_module
import json
import secrets
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.dependencies import get_db
from backend.core.security import generate_prompter_token
from backend.models.overlay import OverlayInstance
from backend.models.settings import SystemSetting
from backend.models.timer import TimerInstance

router = APIRouter(tags=["static-pages"])

_STATIC_DIR = Path(__file__).parent.parent / "static"

# Read once at import time (mirroring `backend/main.py`'s `_PROMPTER_HTML`
# pattern) — per Appendix C.5's no-blocking-I/O-in-request-handlers rule,
# neither route below may do a synchronous file read per request. This is a
# static asset baked into the image; a process restart is required (and
# already expected) to pick up a change to it.
_OVERLAY_HTML = (_STATIC_DIR / "overlay.html").read_text(encoding="utf-8")

#: `system_settings.key` gating the teleprompter index page (Appendix B.4).
_PROMPTER_INDEX_SETTINGS_KEY = "prompter_index_token"


# ── GET /overlay/{entity_id} (plan Appendix B.3) ─────────────────────────


@router.get("/overlay/{entity_id}", response_class=HTMLResponse, tags=["overlay"])
async def serve_overlay(
    entity_id: str, session: Annotated[AsyncSession, Depends(get_db)]
) -> HTMLResponse:
    """Serves the standalone OBS overlay page (plan Appendix B.3), with
    `entity_id` injected into a `<script>` block, mirroring
    `backend.main.serve_prompter`'s templating mechanism exactly.

    Per Appendix A.1/A.15/B.3, this page (and its WebSocket,
    `/ws/overlay/{entity_id}`) has NO auth — "security by obscurity of the
    entity_id" is the accepted model, since the URL is only ever pasted into
    an OBS Browser Source, never exposed to end users. The only check made
    here is that the `entity_id` actually names a live `OverlayInstance`, so
    a typo'd/deleted overlay URL 404s immediately on page load rather than
    rendering a page whose WebSocket then fails silently.
    """
    result = await session.execute(
        select(OverlayInstance).where(OverlayInstance.entity_id == entity_id)
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Overlay instance not found")

    # JSON-encode before substitution (not a plain string replace of raw
    # `entity_id`) so a value containing quotes or other JS-special
    # characters can't break out of the `entityId: "__ENTITY_ID__"` string
    # literal and inject script into the page — same reasoning as
    # `backend.main.serve_prompter`.
    html_out = _OVERLAY_HTML.replace('"__ENTITY_ID__"', json.dumps(entity_id))
    return HTMLResponse(content=html_out)


# ── GET /prompter (plan Appendix B.4) ────────────────────────────────────


def _index_row_html(instance: TimerInstance) -> str:
    token = generate_prompter_token(instance.entity_id, instance.token_nonce)
    link = f"/prompter/{instance.entity_id}?token={token}"
    name = html_module.escape(instance.display_name)
    return (
        "<li>"
        f'<a href="{html_module.escape(link)}">{name}</a>'
        f' <span class="entity-id">({html_module.escape(instance.entity_id)})</span>'
        "</li>"
    )


_INDEX_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>TMTools Teleprompter Index</title>
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", Roboto, sans-serif;
    background: #111;
    color: #eee;
    margin: 0;
    padding: 2rem;
  }}
  h1 {{ font-size: 1.25rem; font-weight: 600; }}
  ul {{ list-style: none; padding: 0; }}
  li {{ padding: 0.5rem 0; border-bottom: 1px solid #333; }}
  a {{ color: #6cf; text-decoration: none; font-size: 1.05rem; }}
  a:hover {{ text-decoration: underline; }}
  .entity-id {{ color: #888; font-size: 0.85rem; }}
  .empty {{ color: #888; }}
</style>
</head>
<body>
<h1>Teleprompter Index</h1>
<ul>
{rows}
</ul>
</body>
</html>
"""


@router.get("/prompter", response_class=HTMLResponse, tags=["prompter"])
async def serve_prompter_index(
    session: Annotated[AsyncSession, Depends(get_db)], token: str = ""
) -> HTMLResponse:
    """Serves the teleprompter index page (plan Appendix B.4): a read-only
    listing of every active `TimerInstance`, each linking to its own
    `/prompter/{entity_id}?token=<per-instance HMAC token>` page.

    Per Appendix B.4, this bare index (unlike the per-instance teleprompter
    pages) is gated by a single shared `?token=<value>` query param checked
    against the `prompter_index_token` system_settings row, rather than by
    any per-entity HMAC — it has no single entity_id of its own to derive
    one from. The per-instance links it renders still each carry their own
    real `generate_prompter_token` value (the same one
    `backend.main.serve_prompter` / `/ws/prompter/{entity_id}` validate),
    so possessing the index token does not itself grant access to any
    given instance's teleprompter beyond what the rendered links already
    encode.

    Returns 403 if the setting row is missing (not yet seeded/configured)
    or the supplied token doesn't match, using a constant-time comparison
    (`secrets.compare_digest`) to avoid leaking timing information about
    the correct value — matching `validate_prompter_token`'s approach for
    the per-instance case.
    """
    setting = await session.get(SystemSetting, _PROMPTER_INDEX_SETTINGS_KEY)
    expected = None
    if setting is not None:
        expected = setting.value.get("token") if isinstance(setting.value, dict) else None

    if not expected or not token or not secrets.compare_digest(expected, token):
        raise HTTPException(status_code=403, detail="Invalid or missing prompter index token")

    result = await session.execute(
        select(TimerInstance)
        .where(TimerInstance.enabled.is_(True))
        .order_by(TimerInstance.display_name)
    )
    instances = list(result.scalars().all())

    if instances:
        rows = "\n".join(_index_row_html(instance) for instance in instances)
    else:
        rows = '<li class="empty">No active timer instances.</li>'

    return HTMLResponse(content=_INDEX_PAGE_TEMPLATE.format(rows=rows))
