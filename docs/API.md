# QEComp Backend API

This document is generated from the running backend's own `GET /openapi.json`
(FastAPI's auto-generated schema), not hand-written from the plan's §11
prose — the plan's API summary was written before implementation and does
not exactly match the shipped routes. For the full interactive schema
(request/response bodies, enums, examples), run the backend and open
`/docs` (Swagger UI) or `/redoc`, or fetch `/openapi.json` directly.

All `/api/v1/*` routes require an authenticated session (OIDC via Authentik,
or the local emergency admin login at `/admin_login` — plan §3.9) plus the
RBAC permission the specific route requires; see `backend/core/dependencies.py`
and plan §13 for the permission list. `/healthz`, `/readyz`, `/admin_login`,
`/auth/*`, and the teleprompter/overlay pages are the only endpoints
reachable without a prior session.

## Health & Status

| Method | Path | Description |
|---|---|---|
| GET | `/healthz` | Liveness probe. Always 200 while the process is alive — no dependency checks. |
| GET | `/readyz` | Readiness probe. Checks Postgres + Redis. Returns `{status, db, redis}`; `status` is `"ok"` only when both are reachable, `"degraded"` if Redis alone is down (graceful degradation per Appendix A.10), and the HTTP status itself is 503 only when Postgres is unreachable (Postgres is the required source of truth). |

## Auth (`/auth`, `/admin_login`, `/api/v1/auth`)

| Method | Path | Description |
|---|---|---|
| GET | `/auth/login` | Starts the Authentik OIDC login redirect. |
| GET | `/auth/callback` | OIDC callback — exchanges the code, establishes the session. |
| POST | `/auth/logout` | Ends the current session. |
| GET | `/admin_login` | HTML login form for the local emergency admin account (plan §3.9), bypasses OIDC entirely. |
| POST | `/admin_login` | Submits `username`/`password` form fields for the local emergency admin; `admin_local` + `ADMIN_LOCAL_PASSWORD` env var. |
| GET | `/api/v1/auth/whoami` | Returns the current session's user id, display name, and effective permissions. |

## Integrations (`/api/v1/integrations`)

Manages dynamically-loaded integration instances (`vex_tm`, `spotify`,
`atem`, `zeros`, `obs`, `wled`, `osc`, `ptz` — plan §5.3-§5.7) via the `Loader`/`INTEGRATION_REGISTRY`.

| Method | Path | Description |
|---|---|---|
| GET | `/api/v1/integrations` | List all configured integration instances and their live connection status. |
| POST | `/api/v1/integrations` | Create a new integration instance (validates against the domain's `manifest.yaml` `config_schema`). |
| GET | `/api/v1/integrations/schemas` | List every registered integration domain's manifest (available domains + their config schema), for the Integrations UI's "add instance" form. |
| PUT | `/api/v1/integrations/{entity_id}` | Update an instance's config; hot-reloads it via the `Loader`. |
| DELETE | `/api/v1/integrations/{entity_id}` | Remove an instance and tear down its live client. |
| GET | `/api/v1/integrations/{entity_id}/state` | Fetch the instance's current `get_state()` telemetry. |
| POST | `/api/v1/integrations/{entity_id}/service/{service}` | Invoke one of the instance's `call_service()` actions (e.g. `start_match`, `play`, `set_scene`). |
| POST | `/api/v1/integrations/{entity_id}/oauth_token` | Completes Spotify's browser-based PKCE flow (plan §3.5) by submitting the obtained token. |
| GET | `/api/v1/integrations/{entity_id}/spotify/library` | Lists the Spotify instance's playlists/tracks for the automation action picker. |

## Automations & Scripts (`/api/v1/automations`, `/api/v1/scripts`)

YAML + Jinja2 automation engine (plan §5.8/§10).

| Method | Path | Description |
|---|---|---|
| GET | `/api/v1/automations` | List automations (optionally filtered by folder). |
| POST | `/api/v1/automations` | Create an automation from a form-built or raw-YAML definition. |
| PUT | `/api/v1/automations/{automation_id}` | Update an automation. |
| DELETE | `/api/v1/automations/{automation_id}` | Delete an automation. |
| POST | `/api/v1/automations/validate` | Dry-run validate a YAML automation definition without saving it. |
| POST | `/api/v1/automations/{automation_id}/trigger` | Manually fire an automation (bypassing its normal trigger condition). |
| GET | `/api/v1/automations/{automation_id}/runs` | Execution history for one automation (plan Appendix A.11's debug view). |
| GET | `/api/v1/automations/folders` | List automation folders. |
| POST | `/api/v1/automations/folders` | Create an automation folder. |
| GET | `/api/v1/scripts` | List reusable scripts (callable from automations). |
| POST | `/api/v1/scripts` | Create a reusable script. |
| PUT | `/api/v1/scripts/{script_id}` | Update a script. |
| DELETE | `/api/v1/scripts/{script_id}` | Delete a script. |

## Timers (`/api/v1/timers`)

Timer-Teleprompter instances (plan §5.10/§7).

| Method | Path | Description |
|---|---|---|
| GET | `/api/v1/timers` | List timer instances. |
| POST | `/api/v1/timers` | Create a timer instance. |
| PUT | `/api/v1/timers/{entity_id}` | Update a timer instance's config. |
| DELETE | `/api/v1/timers/{entity_id}` | Delete a timer instance. |
| POST | `/api/v1/timers/{entity_id}/start` | Start the countdown. |
| POST | `/api/v1/timers/{entity_id}/stop` | Stop/pause the countdown. |
| POST | `/api/v1/timers/{entity_id}/reset` | Reset the countdown to its configured duration. |
| GET | `/api/v1/timers/{entity_id}/cues` | List the timer's runsheet cues. |
| POST | `/api/v1/timers/{entity_id}/cues` | Add a cue. |
| PUT | `/api/v1/timers/{entity_id}/cues/{cue_id}` | Update a cue. |
| DELETE | `/api/v1/timers/{entity_id}/cues/{cue_id}` | Delete a cue. |

See also `GET /prompter/{entity_id}` under Prompter below, and the `/ws/*`
WebSocket channels for the live countdown/cue stream.

## Overlays (`/api/v1/overlays`)

OBS browser-source overlay instances (plan §5.14).

| Method | Path | Description |
|---|---|---|
| GET | `/api/v1/overlays` | List overlay instances. |
| POST | `/api/v1/overlays` | Create an overlay instance. |
| PUT | `/api/v1/overlays/{entity_id}` | Update an overlay instance. |
| DELETE | `/api/v1/overlays/{entity_id}` | Delete an overlay instance. |
| GET | `/api/v1/overlays/{entity_id}/preview` | Renders the same layout the OBS browser source shows, for the admin UI's live preview. |

## Teams (`/api/v1/teams`)

Team profiles + green-screen video upload pipeline (plan §5.11/§5.13).

| Method | Path | Description |
|---|---|---|
| GET | `/api/v1/teams` | List team profiles. |
| GET | `/api/v1/teams/{number}` | Get one team's profile (bio, stats, OPR, video status). |
| PUT | `/api/v1/teams/{number}` | Update a team's editable fields (pit location, bio overrides, etc). |
| POST | `/api/v1/teams/{number}/video` | Upload a raw video for the green-screen keying pipeline; returns `202` immediately, processing runs in the background. |
| GET | `/api/v1/teams/{number}/video/status` | Poll the background FFmpeg/S3 pipeline's status (`PROCESSING`/`DONE`/`FAILED`). |
| GET | `/api/v1/teams/batch/videos` | Batch-resolve processed video S3 URLs for a comma-separated list of team numbers (used by the overlay page). |

## Settings, API Keys, Roles & ZerOS Presets (`/api/v1`)

| Method | Path | Description |
|---|---|---|
| GET | `/api/v1/settings` | List system settings (Robot Events API token, high-potential threshold, etc). |
| PUT | `/api/v1/settings` | Update system settings. |
| GET | `/api/v1/api-keys` | List API keys (Stream Deck webhook auth, plan §5.15/Appendix B.7). |
| POST | `/api/v1/api-keys` | Create an API key. |
| DELETE | `/api/v1/api-keys/{key_id}` | Revoke an API key. |
| GET | `/api/v1/users/roles` | List RBAC group -> permission mappings. |
| PUT | `/api/v1/users/roles` | Replace RBAC group -> permission mappings. |
| GET | `/api/v1/zeros/presets` | List ZerOS lighting presets. |
| POST | `/api/v1/zeros/presets` | Create a ZerOS preset. |
| PUT | `/api/v1/zeros/presets/{preset_id}` | Update a ZerOS preset. |
| DELETE | `/api/v1/zeros/presets/{preset_id}` | Delete a ZerOS preset. |

## Rooms (`/api/v1/rooms`)

| Method | Path | Description |
|---|---|---|
| GET | `/api/v1/rooms` | List rooms with their allocated teams (`rooms:read`). |
| GET | `/api/v1/rooms/{slug}` | One room (`rooms:read`). |
| POST | `/api/v1/rooms` | Create a room: `slug` (`[a-z0-9-]`), `name`, optional `stream_url` (YouTube links are normalised to `/embed/`), `teams[]` (`rooms:manage`). 409 if the slug exists. |
| PUT | `/api/v1/rooms/{slug}` | Replace name, stream URL and team list (`rooms:manage`). |
| DELETE | `/api/v1/rooms/{slug}` | Delete a room (`rooms:manage`); subscriptions made from it keep working but lose their room. |

## Notifications (`/api/v1`)

Team-facing endpoints are **public** (no login) — they are used from phones that scanned a room's QR code. They are rate-limited per client IP, and the push endpoint a client registers must belong to a known browser push service (Google/FCM, Mozilla, Apple, Microsoft).

| Method | Path | Description |
|---|---|---|
| GET | `/api/v1/public/rooms/{slug}` | Public room info: name, stream URL, team list (used by the room and subscribe pages). |
| GET | `/api/v1/public/push/vapid-key` | VAPID public key for `pushManager.subscribe`; 503 if push is not configured. |
| POST | `/api/v1/public/push/subscribe` | Follow teams: `endpoint`, `keys{p256dh,auth}`, `room`, `teams[]`. Teams must belong to the room. Re-posting the same endpoint replaces its team list. |
| POST | `/api/v1/public/push/status` | `{endpoint}` → `{subscribed, room, teams}`. |
| POST | `/api/v1/public/push/unsubscribe` | `{endpoint}` → 204. |
| POST | `/api/v1/notifications/staff/subscribe` | Register this browser for staff alerts (`notifications:receive`); optional `categories` (`system`, `broadcast`). |
| POST | `/api/v1/notifications/staff/unsubscribe` | Remove a staff subscription (`notifications:receive`). |
| POST | `/api/v1/notifications/broadcast` | `{title, message, rooms[]\|null, push, modal}` — to room displays and/or phones; `rooms: null` = every room (`notifications:broadcast`). |
| POST | `/api/v1/notifications/test` | Push a test message to all staff subscriptions (`notifications:manage`). |
| GET | `/api/v1/notifications/stats` | Subscription counts, per room (`notifications:manage`). |

Alert behaviour (x matches / y minutes ahead, scores, excluded rounds) is configured in the `notifications` system setting (Settings → Team alerts).

## Public pages

| Method | Path | Description |
|---|---|---|
| GET | `/room/{slug}` | Room display: livestream embed, team list, QR code, and live alerts over `/ws/room/{slug}`. 404 for an unknown room. |
| GET | `/room/{slug}/qr.svg` | QR code linking to `/notify?room={slug}` (uses `PUBLIC_BASE_URL`, else the request host). |
| GET | `/notify?room={slug}` | Phone page: pick teams from the room and turn on notifications. |
| GET | `/sw.js`, `/notify.webmanifest` | Service worker and PWA manifest (iOS only delivers push to a web app added to the Home Screen). |

## Prompter

| Method | Path | Description |
|---|---|---|
| GET | `/prompter/{entity_id}?token=...` | Serves the standalone teleprompter HTML page (plan Appendix B.3/B.4). The HMAC `token` query param (plan Appendix B.8) is validated server-side before the page renders; an invalid/stale token 403s here in addition to the WebSocket-upgrade-time check at `/ws/prompter/{entity_id}`. |

## WebSocket Channels

Not represented in `/openapi.json` (FastAPI does not include WebSocket
routes in the OpenAPI schema) — documented here from `backend/routers/ws.py`
directly.

| Path | Auth | Description |
|---|---|---|
| `/ws/events` | session | Raw `qecomp:events` bus fan-out (plan Appendix A.11's "Live Event Bus" debug view). |
| `/ws/prompter/{entity_id}?token=...` | HMAC token query param (plan Appendix B.8) | Live countdown/cue/`high_potential` stream for one timer-teleprompter instance. The token is validated at WebSocket-upgrade time (constant-time comparison); an invalid/expired token closes the connection. Only the boolean `high_potential` flag from the AI predictor is ever sent — never the underlying predicted scores (see `backend/modules/predictor/predictor.py`'s module docstring). |
| `/ws/room/{slug}` | none (the room slug is the only credential, like overlay URLs) | Room display feed: only that room's `notice` messages (`{type, id, title, message, modal, duration, timestamp}`). Closes with 4003 for an unknown room. |
| `/ws/overlay/{entity_id}` | none (plan Appendix A.1: "security by obscurity of the entity_id") | Streams `upcoming_match`-shaped messages for the overlay instance's bound field set. |

## Notes on generation

Regenerate this file after any router change with:

```bash
curl -s http://localhost:8000/openapi.json | python3 -c "
import json, sys
d = json.load(sys.stdin)
for path, methods in sorted(d['paths'].items()):
    for method, op in methods.items():
        print(method.upper(), path, op.get('summary'))
"
```

or open `/docs` for the full interactive Swagger UI.
