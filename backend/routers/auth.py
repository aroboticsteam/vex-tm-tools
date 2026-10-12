"""Auth router (plan §11 "Auth" / Appendix A.1 / B.9 / §3.9).

Owns:
  - `GET  /auth/login`    OIDC redirect (Authorization Code + PKCE).
  - `GET  /auth/callback` OIDC callback: exchanges the code, validates the ID
    token, extracts groups from `settings.OIDC_GROUPS_CLAIM`, resolves them
    to permissions via `role_permissions`, and creates a server-side session.
  - `POST /auth/logout`   Clears the session (cookie + Redis key).
  - `GET  /admin_login`   Local emergency admin login page (plan §3.9).
  - `POST /admin_login`   Submits the local admin password.

Per §C.2, this router may import `backend/core/`, `backend/models/`,
`backend/schemas/`, and reach integration/loader code only via `Depends()` —
it does none of that here since OIDC/session logic is self-contained.

Testability (task requirement): real Authentik is unavailable in this
environment, so OIDC discovery + JWKS fetching go through a small
`OIDCClient` class that takes an injectable `httpx.AsyncClient` factory.
Tests construct an `OIDCClient` with a mocked discovery document and a
self-signed test JWT instead of hitting a real issuer.
"""
from __future__ import annotations

import logging
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jose import jwt
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.db import get_db
from backend.core.dependencies import (
    CurrentPrincipal,
    get_current_principal,
    resolve_permissions_for_groups,
)
from backend.core.redis import get_redis
from backend.core.settings import settings
from backend.core.sessions import (
    SESSION_COOKIE_NAME,
    SESSION_TTL_SECONDS,
    create_session,
    delete_session,
    unsign_session_id,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

#: Redis key holding the short-lived OIDC `state` -> PKCE `code_verifier`
#: mapping generated at /auth/login and consumed at /auth/callback.
OIDC_STATE_KEY_TMPL = "qecomp:oidc_state:{state}"
OIDC_STATE_TTL_SECONDS = 600  # 10 minutes — long enough for an IdP round trip.

ADMIN_LOCAL_USER_ID = "admin_local"


# ── OIDC client (mockable per task requirement) ──────────────────────────


class OIDCClient:
    """Thin wrapper around OIDC discovery + JWKS + code exchange.

    `http_client_factory` is injectable so tests can supply a fake
    `httpx.AsyncClient` (or a `respx`/mock transport) instead of hitting a
    real Authentik instance, which isn't available in this environment.
    """

    def __init__(
        self,
        issuer_url: str,
        client_id: str,
        client_secret: str,
        *,
        http_client_factory: Any = None,
    ) -> None:
        self.issuer_url = issuer_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self._http_client_factory = http_client_factory or (lambda: httpx.AsyncClient())
        self._discovery_doc: dict[str, Any] | None = None
        self._jwks: dict[str, Any] | None = None

    async def discover(self) -> dict[str, Any]:
        if self._discovery_doc is not None:
            return self._discovery_doc
        try:
            async with self._http_client_factory() as client:
                resp = await client.get(f"{self.issuer_url}/.well-known/openid-configuration")
                resp.raise_for_status()
                self._discovery_doc = resp.json()
        except httpx.HTTPError as exc:
            # A configured-but-unreachable issuer (DNS failure, connection
            # refused, a transient restart) must surface as a clean 503
            # rather than propagate as an unhandled 500 — this is the one
            # place every OIDC entry point (`login`, `callback`) reaches to
            # talk to the issuer.
            raise HTTPException(
                status_code=503, detail="OIDC provider is currently unreachable"
            ) from exc
        return self._discovery_doc

    async def jwks(self) -> dict[str, Any]:
        if self._jwks is not None:
            return self._jwks
        doc = await self.discover()
        async with self._http_client_factory() as client:
            resp = await client.get(doc["jwks_uri"])
            resp.raise_for_status()
            self._jwks = resp.json()
        return self._jwks

    async def authorization_url(self, *, redirect_uri: str, state: str, code_challenge: str) -> str:
        doc = await self.discover()
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "scope": "openid profile email groups",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{doc['authorization_endpoint']}?{urlencode(params)}"

    async def exchange_code(
        self, *, code: str, redirect_uri: str, code_verifier: str
    ) -> dict[str, Any]:
        doc = await self.discover()
        async with self._http_client_factory() as client:
            resp = await client.post(
                doc["token_endpoint"],
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "code_verifier": code_verifier,
                },
            )
            resp.raise_for_status()
            return resp.json()

    #: The only algorithm Authentik's OIDC provider is configured to sign
    #: with. Deliberately hardcoded rather than trusting the token's own
    #: (unverified) header `alg` field — accepting an attacker-controlled
    #: algorithm there is the classic JWT "algorithm confusion" hole
    #: (CWE-347): it would let a forged token pick, say, HS256 and get
    #: "verified" against a JWKS RSA public key used as an HMAC secret.
    ID_TOKEN_ALGORITHMS = ["RS256"]

    async def validate_id_token(self, id_token: str) -> dict[str, Any]:
        """Validate signature + standard claims of an OIDC ID token via
        `python-jose`, returning its decoded claims."""
        jwks = await self.jwks()
        try:
            header = jwt.get_unverified_header(id_token)
        except Exception as exc:
            raise HTTPException(status_code=401, detail="Invalid ID token header") from exc

        # No silent fallback to "the first JWKS key" when `kid` doesn't
        # match anything: that would let a token signed with an unrelated
        # key still get checked against whatever key happens to be first,
        # rather than failing closed.
        key = next((k for k in jwks.get("keys", []) if k.get("kid") == header.get("kid")), None)
        if key is None:
            raise HTTPException(status_code=401, detail="No matching JWKS key for ID token")

        try:
            claims = jwt.decode(
                id_token,
                key,
                algorithms=self.ID_TOKEN_ALGORITHMS,
                audience=self.client_id,
                issuer=self.issuer_url,
                options={"verify_at_hash": False},
            )
        except Exception as exc:
            raise HTTPException(status_code=401, detail=f"Invalid ID token: {exc}") from exc
        return claims


def _redirect_uri(request: Request) -> str:
    return str(request.url_for("oidc_callback"))


def _get_oidc_client(request: Request) -> OIDCClient:
    """Returns the `OIDCClient` to use — `app.state.oidc_client` if a test
    (or `main.py`) has injected one, otherwise a default built from
    `settings`."""
    injected = getattr(request.app.state, "oidc_client", None)
    if injected is not None:
        return injected
    return OIDCClient(settings.OIDC_ISSUER_URL, settings.OIDC_CLIENT_ID, settings.OIDC_CLIENT_SECRET)


def _pkce_pair() -> tuple[str, str]:
    """Generate a PKCE `(code_verifier, code_challenge)` pair (S256)."""
    import base64
    import hashlib

    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def _set_session_cookie(response: Response, signed_value: str) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        signed_value,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=True,  # `Secure` is evaluated by the browser against the page's own origin
        # scheme, not the backend's internal transport — the app is only ever exposed over
        # HTTPS (k8s/ingress.yaml's `websecure` entrypoint), so this must always be True.
    )


# ── OIDC login/callback/logout (Appendix A.1) ────────────────────────────


@router.get("/auth/login")
async def oidc_login(request: Request, redis_client: Any = Depends(get_redis)) -> RedirectResponse:
    if not settings.OIDC_ISSUER_URL:
        raise HTTPException(status_code=503, detail="OIDC is not configured (OIDC_ISSUER_URL unset)")

    client = _get_oidc_client(request)
    state = secrets.token_urlsafe(24)
    verifier, challenge = _pkce_pair()

    await redis_client.hset(
        OIDC_STATE_KEY_TMPL.format(state=state),
        mapping={"code_verifier": verifier, "created_at": str(time.time())},
    )
    await redis_client.expire(OIDC_STATE_KEY_TMPL.format(state=state), OIDC_STATE_TTL_SECONDS)

    redirect_uri = _redirect_uri(request)
    url = await client.authorization_url(redirect_uri=redirect_uri, state=state, code_challenge=challenge)
    return RedirectResponse(url)


@router.get("/auth/callback", name="oidc_callback")
async def oidc_callback(
    request: Request,
    code: str,
    state: str,
    db: AsyncSession = Depends(get_db),
    redis_client: Any = Depends(get_redis),
) -> RedirectResponse:
    state_key = OIDC_STATE_KEY_TMPL.format(state=state)
    state_data = await redis_client.hgetall(state_key)
    if not state_data:
        raise HTTPException(status_code=400, detail="Invalid or expired OIDC state")
    await redis_client.delete(state_key)
    code_verifier = state_data["code_verifier"]

    client = _get_oidc_client(request)
    redirect_uri = _redirect_uri(request)
    tokens = await client.exchange_code(code=code, redirect_uri=redirect_uri, code_verifier=code_verifier)

    id_token = tokens.get("id_token")
    if not id_token:
        raise HTTPException(status_code=401, detail="OIDC token response missing id_token")
    claims = await client.validate_id_token(id_token)

    user_id = claims.get("sub", "")
    display_name = claims.get("preferred_username") or claims.get("email") or user_id
    groups = claims.get(settings.OIDC_GROUPS_CLAIM, []) or []
    if isinstance(groups, str):
        groups = [groups]

    # Appendix B.9: groups -> permissions resolved once, at login time.
    permissions = await resolve_permissions_for_groups(db, list(groups))

    session_id, signed_cookie = await create_session(
        redis_client,
        user_id=user_id,
        display_name=display_name,
        groups=list(groups),
        permissions=permissions,
    )
    logger.info("OIDC login: user=%s groups=%s session=%s", user_id, groups, session_id)

    response = RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    _set_session_cookie(response, signed_cookie)
    return response


@router.post("/auth/logout")
async def logout(
    request: Request, response: Response, redis_client: Any = Depends(get_redis)
) -> JSONResponse:
    cookie_value = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie_value:
        session_id = unsign_session_id(cookie_value)
        if session_id is not None:
            await delete_session(redis_client, session_id)
    out = JSONResponse({"ok": True})
    out.delete_cookie(SESSION_COOKIE_NAME)
    return out


# ── Local emergency admin (plan §3.9 / Appendix B.9) ─────────────────────

_ADMIN_LOGIN_PAGE = """<!doctype html>
<html><head><title>TMTools Emergency Admin Login</title></head>
<body>
<h1>Emergency Admin Login</h1>
<form method="post" action="/admin_login">
  <label>Username <input type="text" name="username" value="admin_local" readonly></label><br>
  <label>Password <input type="password" name="password"></label><br>
  <button type="submit">Log in</button>
</form>
</body></html>"""


@router.get("/admin_login", response_class=HTMLResponse)
async def admin_login_page() -> HTMLResponse:
    """Serves the local emergency admin login page. Username is always
    `admin_local` and never goes through Authentik (plan §3.9)."""
    return HTMLResponse(content=_ADMIN_LOGIN_PAGE)


@router.post("/admin_login")
async def admin_login_submit(
    username: str = Form(...),
    password: str = Form(...),
    redis_client: Any = Depends(get_redis),
) -> JSONResponse:
    # Break-glass path is disabled entirely (not "defaults to a known
    # password") when ADMIN_LOCAL_PASSWORD is unset — refuse every attempt
    # up front rather than falling through to a compare against "".
    if not settings.ADMIN_LOCAL_PASSWORD:
        raise HTTPException(status_code=503, detail="admin_local login is not configured")
    if username != ADMIN_LOCAL_USER_ID:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    # Constant-time comparison to avoid leaking password-length/prefix
    # timing information.
    if not secrets.compare_digest(password, settings.ADMIN_LOCAL_PASSWORD):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # admin_local always has all permissions, regardless of role_permissions
    # (plan §3.9: "not configurable from UI").
    session_id, signed_cookie = await create_session(
        redis_client,
        user_id=ADMIN_LOCAL_USER_ID,
        display_name="Emergency Admin",
        groups=[],
        permissions=[],
        is_admin_local=True,
    )
    logger.warning("admin_local login (session=%s)", session_id)

    response = JSONResponse({"ok": True})
    _set_session_cookie(response, signed_cookie)
    return response


# ── Whoami (convenience for the frontend auth store) ─────────────────────


@router.get("/api/v1/auth/whoami")
async def whoami(principal: CurrentPrincipal | None = Depends(get_current_principal)) -> dict:
    if principal is None:
        return {"authenticated": False}
    return {
        "authenticated": True,
        "subject": principal.subject,
        "is_admin_local": principal.is_admin_local,
        "permissions": sorted(principal.permissions),
    }
