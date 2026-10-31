# TMTools (QEComp) — Source of Truth

**Last verified against the codebase:** 2026-10-30 (`main` @ `c832fb6` + `feat/capture-backend`, Alembic head `b9c1d2e3f4a5`)

This is the single authoritative description of what is in this repository: architecture, decisions, data model, API surface, RBAC, frontend, deployment and tests. It replaces `tm_update_plan.md` (the original v3.0 rewrite plan). Where the old plan and the code disagreed, **the code wins** and this document records the code.

> **Keeping this file correct:** any change to behaviour, routes, schema, permissions, settings, env vars, integrations, UI pages, deployment or tests must update this file in the same change (also required by `CLAUDE.md`). If you find a mismatch, fix this file.

> **Naming:** the product is **TMTools** (user-visible branding, commit `9274acd`). Internal identifiers keep the original codename **QEComp** (`qecomp:*` Redis keys, `qecomp-*` Authentik groups, `qecomp` k8s namespace/DB/user, `qec_` API key prefix). Do not rename these without a migration.

---

## Table of contents

1. [What this is](#1-what-this-is)
2. [Principles and hard rules](#2-principles-and-hard-rules)
3. [Repository layout](#3-repository-layout)
4. [Runtime architecture](#4-runtime-architecture)
5. [Configuration (env vars and system settings)](#5-configuration)
6. [Data model (Postgres)](#6-data-model-postgres)
7. [Redis keyspace and event bus](#7-redis-keyspace-and-event-bus)
8. [Integrations](#8-integrations)
9. [Automation and scripting engine](#9-automation-and-scripting-engine)
10. [Feature modules](#10-feature-modules)
11. [HTTP API](#11-http-api)
12. [WebSockets](#12-websockets)
13. [Standalone and public pages](#13-standalone-and-public-pages)
14. [Auth, sessions and RBAC](#14-auth-sessions-and-rbac)
15. [Frontend (admin SPA)](#15-frontend-admin-spa)
16. [Backend module boundaries](#16-backend-module-boundaries)
17. [Frontend rules](#17-frontend-rules)
18. [Testing](#18-testing)
19. [Build, CI and deployment](#19-build-ci-and-deployment)
20. [Local development and tooling](#20-local-development-and-tooling)
21. [Design system](#21-design-system)
22. [Known gaps, legacy leftovers and discrepancies](#22-known-gaps-legacy-leftovers-and-discrepancies)
23. [Change history](#23-change-history)

---

## 1. What this is

A VEX competition-control system. It connects to one or more VEX Tournament Manager (TM) field-set servers and, driven by match events, controls Spotify, ATEM, ZerOS, OBS, WLED, generic OSC and PTZ cameras through a YAML/Jinja2 automation engine. It also provides:

- an EMCEE **teleprompter** (server-synced timer, cues, match preview);
- **OBS overlays** with Robot 360 videos and team spotlight;
- **team profiles** (TM + VEX Events scraper, AI "high potential" flag, video/clip pipeline);
- **rooms** with public displays, QR codes and **Web Push** team/staff notifications;
- **desk ↔ iPad comms** (stations, messages, screens, timers, agenda);
- **button pages** (Stream-Deck-style action buttons);
- a **public team-facing website** (`/site`) with leaderboards and match recordings;
- an in-browser **clip recorder**.

Everything operational is managed from the admin UI. Only initial deployment (k8s manifests, secrets/env vars) is outside the UI. The only exception credential is the break-glass `admin_local` account (password from env).

It is a full rewrite of the original single-instance Flask/JSON-file tool into a clustered **FastAPI + Postgres + Redis** backend with a **React + Vite** admin SPA. Licence: **AGPL-3.0**.

## 2. Principles and hard rules

1. **UI-only management.** No operator should edit files or run commands to configure the system.
2. **Postgres is the source of truth.** Redis holds cache, sessions, leader lock, pub/sub and timer runtime state. The app degrades (does not halt) if Redis is down: no leader election (the node runs everything as leader), no WS fan-out, no schedule/team caching; the Dashboard shows a "Redis unavailable" banner.
3. **Single-leader execution.** Only the elected leader runs integration clients, the automation engine, timers, scraper, predictor and notification service. Passive nodes serve the API/SPA and proxy leader-only calls.
4. **Exactly one uvicorn worker per pod** (`--workers 1`); multiple workers would each contend for the leader lock.
5. **No blocking calls in the event loop.** See [§16.3](#163-async-discipline).
6. **The predicted score is never exposed** to any user-facing surface; only the boolean `high_potential` flag is.
7. **No secrets in the audit log.** Settings values whose keys contain `secret`, `token`, `password` or `access_key` are redacted before auditing.
8. **Credentials at rest:** every `secret: true` integration config field is Fernet-encrypted (`ENCRYPTION_KEY`) before storage in Postgres JSONB.
9. **Cross-module communication goes through the event bus** (`qecomp:events`) or the Loader public API — never direct imports across boundaries ([§16](#16-backend-module-boundaries)).
10. **WebSocket reconnect:** all WS clients show "Reconnecting…" and back off exponentially (1s → 30s cap).
11. **Admin UI** targets desktop and iPad landscape (≥768px); the teleprompter and station pages are iPad-optimised. Phones are only required for the public/notify/site pages.

## 3. Repository layout

```
.
├── backend/                     FastAPI app (package `backend`, run as backend.main:app)
│   ├── main.py                  App factory + lifespan; ONLY place that wires leader/managers
│   ├── loader.py                Integration registry, live instances, hot-reload, debug log
│   ├── core/                    db, redis, settings, security, sessions, dependencies (RBAC),
│   │                            audit, ntfy, request_context, service_validation, exceptions
│   ├── models/                  SQLAlchemy ORM (one file per area)
│   ├── schemas/                 Pydantic v2 request/response + frozen EventBusMessage
│   ├── routers/                 One APIRouter per resource group (+ ws.py)
│   ├── modules/
│   │   ├── leader.py            Redis lock leader election
│   │   ├── automation/engine.py YAML + Jinja2 engine
│   │   ├── timer/manager.py     Timer-Teleprompter manager
│   │   ├── scraper/scraper.py   TM + VEX Events API scraper
│   │   ├── predictor/           OPR regression + high-potential flag
│   │   ├── media/               ffmpeg processor, S3 helpers, image helpers
│   │   ├── notifications/       logic (pure), service (leader loop), delivery (Web Push)
│   │   ├── comms/               station access/PIN, presence, timer, next-match
│   │   ├── site/leaderboards.py Public-site leaderboards/highlights (pure)
│   │   └── integrations/        base.py + one folder per domain (see §8)
│   ├── static/                  overlay.html, prompter.html, room.html, station.html,
│   │                            notify.html, site.html, leaderboard.html, sw.js
│   │                            (+ built SPA baked in at static/frontend/ by the Dockerfile)
│   ├── tests/                   pytest suite (see §18)
│   ├── requirements.txt, pytest.ini
├── frontend/                    React + Vite admin SPA, vitest unit tests, Playwright e2e
├── alembic/ + alembic.ini       Migrations (repo root, NOT under backend/)
├── k8s/                         deployment, service, ingress (Traefik IngressRoute), configmap, mumble
├── docs/                        API.md (generated), AUTHENTIK_SETUP.md, COMMS.md, TESTING.md,
│                                modules/ (vendor API docs: PyATEMMax, VEX TM API)
├── local-testing/               Throwaway Traefik + Authelia stack for real-OIDC local login
├── tools/                       Operational scripts (see §20)
├── .github/workflows/           tests.yml, docker-publish.yml
├── Dockerfile, compose.yml, .env.example, LICENSE, README.md
├── source-of-truth.md           THIS FILE
├── CLAUDE.md                    Instructions for Claude Code (requires keeping this file updated)
└── modules/, storage/           LEGACY pre-rewrite leftovers (see §22)
```

## 4. Runtime architecture

### 4.1 Process and lifespan (`backend/main.py`)

- `create_app()` refuses to start if `SECRET_KEY` is unset. App title `TMTools`, version `1.0.0`. Middleware: `RequestContextMiddleware`. Logging is configured from `LOG_LEVEL` (default `INFO`).
- `lifespan` checks Postgres/Redis (warns, never crashes), starts the WS `ConnectionManager` Redis fan-out on **every** node, builds `TimerManager`, `Loader`, `AutomationEngine`, `Scraper`, `Predictor`, `NotificationService`, then starts `LeaderElection`.
- `on_promoted`: `loader.load_all()` → `timer_manager.start()` → `automation_engine.start()` → `scraper.start()` → `predictor.start()` → `notification_service.start()`; then publish them on `app.state`. `on_demoted` reverses this and clears `app.state`.
- Shutdown tears down all leader-only services **before** releasing the lock (split-brain protection).
- Probes: `GET /healthz` (liveness, no dependency checks), `GET /readyz` (200 if Postgres is up even when Redis is down — status `degraded`; 503 only when Postgres is down).
- The SPA is served by a catch-all registered **last** (`/{full_path:path}` with path-traversal containment), only if `backend/static/frontend/` exists. `/assets` is mounted statically.

### 4.2 High availability

- **Leader lock:** Redis key `qecomp:leader:lock`, TTL 30s, renewed every 10s by a dedicated task using atomic Lua check-and-renew/check-and-release scripts. Value is `<pod_ip>:<port>` so passive nodes can reach the leader. Backoff on Redis failure caps at 5 minutes. If renewal fails, the node tears down leader services first (split-brain protection) and fires ntfy.
- **Passive command forwarding:** integration service/OAuth/test calls on a passive node are HTTP-proxied to the leader address via `httpx.AsyncClient` (`routers/integrations.py::_proxy_to_leader`). Timer and automation routers return 503 on a node that is not the leader.
- **Config changes** are persisted to Postgres then published on `qecomp:config_change`; the leader hot-reloads the affected component (integrations by the Loader, automations/scripts/timers by their managers). No restarts.
- **Replicas:** `k8s/deployment.yaml` runs **3 replicas** with required pod anti-affinity (one per node). The original plan said 2; the manifest is authoritative.
- **Redis:** the app uses a single `REDIS_URL`; Sentinel/HAProxy failover is infrastructure's job (no Sentinel client in the app).

### 4.3 Data flow

```
TM server ──WS/REST──▶ vex_tm integration ──▶ qecomp:events (Redis pub/sub)
                                             │
      ┌───────────────┬────────────┬─────────┼───────────┬────────────────┐
  Automation      TimerManager  Scraper   Predictor  NotificationService  ws.ConnectionManager
  engine          (matchStopped) (fieldMatch-  (fieldMatch-  (schedule ticks,    (fan-out to
  (triggers →      → state       Assigned →    Assigned →    fieldMatchAssigned)  browsers/iPads/
  call_service)                  profiles)     flag)                              OBS)
```

## 5. Configuration

### 5.1 Environment variables (`backend/core/settings.py`, `.env.example`)

| Variable | Default | Notes |
|---|---|---|
| `POSTGRES_DSN` | `postgresql+asyncpg://qecomp:qecomp@localhost:5433/qecomp` | asyncpg DSN |
| `REDIS_URL` | `redis://localhost:6380/0` | single endpoint |
| `OIDC_ISSUER_URL`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` | empty | Authentik (or any OIDC; Authelia locally) |
| `OIDC_GROUPS_CLAIM` | `groups` | claim carrying group names |
| `ADMIN_LOCAL_PASSWORD` | empty | `/admin_login` disabled while unset |
| `SECRET_KEY` | empty | **required**; signs session cookies and prompter HMAC tokens |
| `ENCRYPTION_KEY` | empty | Fernet key; required to store/read integration secrets |
| `S3_ENDPOINT_URL`, `S3_BUCKET`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` | none | bootstrap fallback; Settings page overrides |
| `S3_REGION` | `us-east-1` | |
| `S3_PUBLIC_ENDPOINT_URL` | none | browser-reachable endpoint used only to sign presigned URLs (needed when `S3_ENDPOINT_URL` is internal, e.g. `http://minio:9000`) |
| `APP_PORT` | `8000` | |
| `POD_IP` | none | k8s downward API; falls back to local IP detection |
| `VAPID_PRIVATE_KEY`, `VAPID_PUBLIC_KEY` | empty | Web Push off while either is unset; room displays still work |
| `VAPID_SUBJECT` | `mailto:admin@example.com` | VAPID `sub` |
| `PUBLIC_BASE_URL` | empty | base for room QR codes; falls back to request base URL |
| `LOG_LEVEL` | `INFO` | |
| `CLOUDFLARE_TUNNEL_TOKEN` | — | used only by `compose.yml`'s `cloudflared` service; never commit a real value |

`.env.example` header and one comment still say "QEComp"/"later wave" — cosmetic, see §22.

### 5.2 System settings (`system_settings` table, edited on the Settings page)

| Key | Contents |
|---|---|
| `s3` | endpoint, public endpoint, bucket, access key, secret key, region |
| `robot_events_api` | `{token}` for the **VEX Events API** (events.vex.com, the successor to the RobotEvents API; base `https://events.vex.com/api/v2`). The key keeps its old name |
| `predictor` | `{high_potential_threshold_pct}` (default 15) |
| `chroma_key_defaults` | `{colour:"#00B140", similarity:0.1, blend:0.05}` |
| `notifications` | ntfy (`server_url`, `topic`, `enabled`) **and** team-alert settings: `team_alerts_enabled`, `lead_matches_x` (3), `lead_minutes_y` (15), `scores_enabled`, `scores_excluded_rounds` (`["F"]`), `late_minutes` (5, 0=off), `agenda_announcements_enabled`, `agenda_lead_minutes` (10), `timezone` (IANA, default UTC) |
| `prompter_index_token` | static token guarding `/prompter` (seeded randomly by migration) |
| `site` | public-site config: next competition, sponsors, map, title, etc. (structured config edited via the Settings page `SiteConfigEditor`) |

Defaults for the first four are seeded by the initial migration; `notifications` and `prompter_index_token` by later migrations.

## 6. Data model (Postgres)

All tables are defined in `backend/models/` and created by Alembic (`alembic/versions/`). **29 tables.**

| Area | Table | Key columns |
|---|---|---|
| Integrations | `integration_instances` | `id` UUID, `entity_id` UNIQUE (`<domain>.<name>`), `domain`, `display_name`, `config` JSONB (secrets Fernet-encrypted), `enabled`, `status`, `tags` TEXT[] (GIN) |
| | `zeros_presets` | `integration_id` FK, `preset_number`, `preset_name`, `description`; UNIQUE (integration, number) and (integration, name) |
| Timers | `timer_instances` | `entity_id`, `display_name`, `field_set_id`, `field_id`, `tags`, `enabled`, `duration_s`, `token_nonce` |
| | `prompter_cues` | `timer_entity_id`, `content` (Markdown), `type` (`script`/`note`/`runsheet`), `sort_order`, `is_active`, `created_by` |
| Overlays | `overlay_instances` | `entity_id`, `display_name`, `field_set_id`, `tags`, `enabled` |
| Automation | `automation_folders` | `name`, `parent_id` (arbitrary nesting) |
| | `automations` | `folder_id`, `alias`, `enabled`, `trigger_yaml`, `condition_yaml`, `action_yaml`, `last_triggered_at` |
| | `scripts` | `name` UNIQUE, `description`, `action_yaml` |
| | `automation_runs` | `automation_id`, `triggered_at`, `trigger_event` JSONB, `status`, `failed_action_index`, `error`, `action_results` JSONB |
| Teams | `team_profiles` | `team_number` PK, `pit_location`, `bio`, `robot_name`, `raw_video_s3_key`, `video_360_s3_key`, `video_processing_status` (`NONE/PROCESSING/DONE/FAILED`), `video_processing_error`, `cached_stats` JSONB, `extra_notes`, `website_url`, `logo_s3_key`, `inspection_status` (`NOT_STARTED/IN_PROGRESS/PASSED`, checklist #43) |
| | `team_clips` | PK (`team_number`,`kind`), `raw_s3_key`, `processed_s3_key`, `status`, `error` |
| Rooms/notifications | `rooms` | `slug` PK, `name`, `stream_url` |
| | `room_teams` | (`room_slug`,`team_number`) |
| | `push_subscriptions` | `endpoint` UNIQUE, `p256dh`, `auth`, `kind` (team/staff), `room_slug`, `user_subject`, `categories` JSONB, `user_agent`, `failure_count`, `last_success_at` |
| | `push_subscription_teams` | (`subscription_id`,`team_number`) |
| | `notification_log` | `key` PK — dedupe claims so failover can't resend an alert |
| Comms | `stations` | `slug` PK, `name`, `role` (`emcee`/`floor_manager`/`camera`), `access_key`, `pin_hash` (scrypt), `key_version` |
| | `comms_presets` | `label`, `message`, `flash`, `role`, `position` |
| | `agenda_items` | `scope` (`day`/`finals`), `start_time`/`end_time` ("HH:MM"), `title`, `details`, `position`, `announce_lead_minutes` |
| | `station_messages` | `station_slug`, `text`, `acknowledged` (replies from iPads) |
| Buttons | `button_pages` | `name` UNIQUE, `position`, `columns` |
| | `buttons` | `page_id`, `label`, `color`, `position`, `action_yaml`, `icon`, `confirm` |
| Capture | `capture_recordings` | `id` UUID, `match_key` UNIQUE, `match_type`, `match_number`, `teams` TEXT[] (GIN), `alliances` JSONB, `status` (`RECORDING/STOPPED/READY/ERROR`), `object_key`, `started_at`, `stopped_at`, `duration_s`, `size_bytes`, `error`. Starting a match whose key already exists resets that row (drops the earlier take and its moments) |
| | `capture_moments` | `recording_id` FK (cascade), `number` (UNIQUE per recording, mark order), `length_s`, `status` (`REQUESTED/CUT/READY/ERROR`), `end_offset_ms` (agent recording clock), `ready_ms` (mark → playable, measured by the agent), `object_key`, `error` |
| Site | `match_recordings` | `match_key` PK, `url`, `title` |
| Access | `role_permissions` | PK (`authentik_group`,`permission`) |
| | `api_keys` | `name`, `key_hash` (SHA-256), `permissions` TEXT[], `created_by`, `last_used_at`, `revoked` |
| System | `system_settings` | `key` PK, `value` JSONB, `updated_by` |
| | `audit_log` | `user_id`, `action`, `resource_type`, `resource_id`, `changes` JSONB, `ip_address` INET; retained forever, no UI deletion |

Migrations: single linear head `c1d2e3f4a5b6` (CI fails on multiple heads). Seed migrations: default role permissions, notifications settings, prompter index token, plus permission additions per feature (rooms, buttons, comms). The k8s `migrate` init container and compose `migrate` service run `alembic upgrade head`.

## 7. Redis keyspace and event bus

| Key / channel | Type | TTL | Purpose |
|---|---|---|---|
| `qecomp:leader:lock` | string | 30s | leader address `<ip>:<port>` |
| `qecomp:session:<id>` | hash | 24h | OIDC/admin session (user, groups, permissions) |
| `qecomp:integration:<entity_id>:status` | string | none | CONNECTED / DEGRADED / DISCONNECTED |
| `qecomp:integration:<entity_id>:state` | hash | none | integration state for telemetry |
| `qecomp:integration:<entity_id>:debug_log` | list | 7d (refreshed on append) | last events per instance (debug view) |
| `qecomp:tm:<entity_id>:token` | string | token expiry | TM OAuth bearer token |
| `qecomp:tm:<entity_id>:schedule` (pattern scanned by notifications) | string | configurable | cached division schedule |
| `qecomp:timer:<entity_id>:state` | hash | none | `start_ts`, `duration_s`, `running` — timer ticks recompute from this, so a new leader resumes mid-countdown |
| `qecomp:team:<number>:profile` | string | 24h | cached team profile |
| `qecomp:capture:agent` | string (JSON) | 45s (agent heartbeat refreshes) | capture agent presence: `agent_id`, `version`, `state`, `detail`, `settings`, `last_seen`. Absent = agent not connected |
| `qecomp:events` | pub/sub | — | **all** internal events |
| `qecomp:config_change` | pub/sub | — | reload notifications |

### 7.1 Canonical event message (`backend/schemas/events.py`, frozen)

```json
{ "entity_id": "vex_tm.division_1", "entity_tags": ["fs1"], "type": "matchStarted",
  "timestamp": 1730000000.0, "payload": { } }
```

`extra="forbid"`; every publisher must use `EventBusMessage`.

### 7.2 Event types

| `type` | Source | Notable payload |
|---|---|---|
| `fieldMatchAssigned` | vex_tm (payload enriched from cached schedule) | `fieldID`, `matchNum`, `round`, `redTeams[]`, `blueTeams[]`, `divisionId` (+ raw `match` tuple) |
| `fieldActivated`, `matchStarted`, `matchStopped`, `audienceDisplayChanged` | vex_tm (TM WS passthrough; `matchStarted` is enriched like `fieldMatchAssigned` with `matchNum`, `round`, `redTeams`, `blueTeams` so recordings can be tagged) | `fieldID`, `matchNum`, `round`, …; `audienceDisplayChanged` carries `display` (e.g. `IN_MATCH`, `RANKINGS`; the TM docs show no `fieldID` for it) |
| `timer_started`, `timer_milestone`, `timer_finished`, `timer_stopped` | TimerManager | `field_set_id`, `field_id`, `remaining`, `elapsed`, `duration_s` |
| `team_profile_updated` | Scraper | `team_number` |
| `match_prediction` | Predictor | `high_potential` (+ internal predicted scores — never forwarded to UIs) |
| `config_change` | routers | `resource_type`, `entity_id`, `action` |
| `integration_status` | Loader/integrations | `status` |
| `overlay_show_team` | engine `spotlight` action / overlays router | team to spotlight |
| `capture_command` | `capture` service / routes | payload is the command forwarded verbatim to the agent's `/ws/agent` socket (`start_recording`, `stop_recording`, `mark`); never sent to other sockets |
| `replay_command` | `modules/capture/replay.py` | `entity_id` = replay player id; payload (`play`/`pause`/`resume`/`restart`/`rate`/`seek`/`stop`) forwarded verbatim to that player's `/ws/replay/{id}` sockets only |
| `capture_recording`, `capture_moment`, `capture_status` | capture service / `/ws/agent` | recording/moment summaries (id, status, match key…) and agent connected state, for the analysis deck |
| comms events (`message`, `screen`, `timer`, `notice`, …) | comms router, notifications delivery | entity `COMMS_ENTITY_ID`; consumed by `/ws/station`, `/ws/room` |

`manual_trigger` is the synthetic event the engine builds for manual/test runs.

## 8. Integrations

One folder per domain under `backend/modules/integrations/<domain>/` with `manifest.yaml` (config schema), `services.yaml` (service schema, used for save/call-time validation and audit redaction), `integration.py`. The Loader scans the directory at startup; adding a folder adds a domain with no engine change. Instances (credentials, IPs, tags) live in Postgres only.

**`Integration` interface (frozen, `base.py`):** `entity_id`, `config` (decrypted), `tags`; `async setup()` (raise on fatal), `async teardown()` (never raises, ≤5s), `async call_service(service, data) -> dict` (`ValueError` for unknown), `async get_state() -> dict` (never raises, returns cached state). A failing `setup()` marks the instance `DEGRADED` and retries with backoff; the loader never crashes. Integrations publish only to `qecomp:events` and never import each other.

| Domain | Name | Config fields | Services | Service permission |
|---|---|---|---|---|
| `vex_tm` | VEX Tournament Manager | `base_url`, `client_id`, `client_secret`*, `api_key`*, `field_set_id`, `poll_interval_seconds` (60), `auth_url` (override) | `start_match`, `end_early`, `abort`, `reset`, `queue_next_match`, `queue_prev_match`, `set_audience_display(display)`, `queue_skills(skills_id)` | `tm:control` |
| `spotify` | Spotify (PKCE browser auth; `requires_oauth`) | `client_id`, `client_secret`*, `device_name` | `play(context_uri?)`, `play_playlist_track(playlist_uri, track_number?)`, `play_track(track_uri, start_time_s?)`, `pause`, `next`, `previous`, `set_volume(volume)` | `audio:control` |
| `atem` | Blackmagic ATEM | `ip`, `field_to_input` (map) | `switch_input(input_index)`, `set_preview(input_index)` | `video:control` |
| `obs` | OBS Studio (obs-websocket v5, OBS 28+) | `host`, `port`, `password`* | `switch_scene(scene_name)`, `trigger_hotkey(hotkey_name)` | `video:control` |
| `zeros` | ZerOS lighting (OSC/UDP) | `ip`, `port` | `set_preset(preset_id \| preset_name)` | `vfx:control` |
| `osc` | Generic OSC sender | `ip`, `port` | `send(address, args?)`, `send_bundle(messages)` | `vfx:control` |
| `wled` | WLED LED strips (JSON API) | `host`, `port` | `set_power(on)`, `set_brightness`, `set_color`, `set_preset`, `set_effect(effect, speed?, intensity?)`, `flash(color?, count?, interval_ms?)` | `vfx:control` |
| `ptz` | PTZ camera (VISCA over IP, UDP 52381) | `host`, `port`, `camera_address` (1–7) | `recall_preset`, `store_preset`, `move(direction, speed?)`, `stop`, `home`, `zoom(direction, speed?)` | `video:control` |
| `artnet` | Art-Net (DMX over IP) for MagicQ virtual sliders via a Node 1 Universe | `ip` (or broadcast), `port` (6454), `universe` (0–32767), `refresh_hz` (4) | `set_channels(start, values)`, `set_slider(channel, level \| percent)`, `fade(channel, level \| percent, duration_ms)`, `blackout` | `vfx:control` |
| `capture` | Capture agent control (no config; DEGRADED while no agent is connected) | none | `start_recording(match \| round, instance, match_num, red_teams, blue_teams)`, `stop_recording` (no-op when idle), `mark_moment(seconds)` | `capture:control` |

`*` = `secret: true`, Fernet-encrypted at rest. Service calls fail closed (403) for a domain with no entry in `_DOMAIN_PERMISSIONS` (`routers/integrations.py`) — add one when adding a domain. BirdDog X1 compatibility for `ptz` is unverified; WLED/OSC/PTZ were added in issue #7.

### 8.1 VEX TM (`vex_tm`)

- One instance = one TM **field set**; it manages all fields in the set (events carry `fieldID`). Create several instances for several field sets/servers.
- `setup()`: fetch an OAuth2 token from `auth_url` (default `https://auth.vextm.dwabtech.com/oauth2/token`), cache in Redis; open a WebSocket to the field-set endpoint with HMAC-signed headers; start the receive loop (events → `qecomp:events`) and the schedule poller (`/api/divisions`, `/api/matches/<div>`, `/api/rankings/<div>/QUAL`, `/api/skills`) into Redis.
- `fieldMatchAssigned` from TM carries only a match tuple; the integration joins it against the cached schedule to add team numbers. After each `matchStopped` it re-fetches the schedule a few times so scores appear within seconds.
- Fixes carried forward from the legacy client: token in Redis (not a file); `await asyncio.sleep`; HMAC signature includes query string; `Host` header includes port only if non-standard; API key whitespace stripped; capped exponential backoff on auth failure; `websockets` ≥11 handshake exception handling.
- Skills matches publish bus events but do not drive overlay/prompter; elimination matches behave like quals (automations can branch on `payload.round`).
- Programme (V5 4-team / IQ 2-team) is auto-detected from match data. VEX U is not supported.

### 8.2 Spotify

PKCE flow runs in the browser (frontend `SpotifyAuthButton`, callback at `/integrations/spotify/callback`); the frontend POSTs tokens to `POST /api/v1/integrations/<entity_id>/oauth_token`; backend encrypts them, loads them into the client and refreshes ahead of expiry in a background task. A live library browser (`GET …/spotify/library`) powers playlist/track pickers in the automation builder. 429s honour `Retry-After` once. Playback telemetry is polled in the background (≤ every 5s), never inside `get_state()`.

### 8.3 ATEM, ZerOS, OBS, WLED, OSC, PTZ

- **ATEM:** `PyATEMMax` is synchronous — all calls via `asyncio.to_thread`; its receive loop runs in a thread started in `setup()` and joined in `teardown()`; cross-thread signalling uses `loop.call_soon_threadsafe`.
- **ZerOS:** `python-osc`; fire-and-forget sends via `asyncio.to_thread`. Presets (name ↔ number) are managed in the UI (`/api/v1/zeros/presets`) and offered as a dropdown in the builder; the Integrations page also has a fire-preset button.
- **Capture (#42/#45):** thin front for `modules/capture/service.py`; unlike the other integrations it imports that module because it needs the recording tables. A watcher compares the agent presence key every 5 s and reports CONNECTED/DEGRADED. Services fail with `IntegrationError` (503 over REST) when no agent is connected. Intended automation: `matchStarted` → `capture.start_recording` with `match: "{{ trigger.payload.match }}"`, `red_teams`/`blue_teams` from the enriched payload; `matchStopped` → `capture.stop_recording`.
- **Art-Net (#39):** non-blocking asyncio UDP (no thread). Holds one 512-channel universe buffer; services mutate it and a keep-alive loop re-sends it `refresh_hz` times a second (nodes drop output when frames stop). Fades run as per-channel tasks at ~30 Hz; a manual set on a channel cancels its fade; `blackout` zeroes everything. Sequence runs 1–255. Tests: `tests/test_integrations/test_artnet_unit.py` (real UDP listener).
- **OBS:** `obs-websocket-py` 1.x (async-native). Scene list is fetched on `setup()`, cached in Redis, refreshable from the UI. Disconnects set `DEGRADED` and trigger reconnect.

## 9. Automation and scripting engine

`backend/modules/automation/engine.py` — frozen API: `start()`, `stop()`, `reload()`, `trigger(automation_id, context={})`.

- **Storage:** automations (grouped in nested folders) and scripts are rows in Postgres; YAML is the storage/evaluation format. The UI offers a form builder (trigger picker, condition builder, drag-and-drop action chain) and a raw YAML editor with a **Validate** button (YAML + Jinja2 syntax, executes nothing — `POST /api/v1/automations/validate`).
- **Reload:** on `config_change` for `automation`, `automation_folder`, `script`, `timer_instance`.
- **Triggers** (`platform`): `state` (optional `entity_id`, `event_type`, `tag`, `match_name` fnmatch against `payload.matchName`) and `timer_milestone` (`entity_id` or `tag`, `remaining: "HH:MM:SS"`). Manual runs use `manual`. `POST …/trigger` runs regardless of trigger conditions.
- **Conditions:** Jinja2 boolean templates.
- **Actions:** `service` (target by `target: <entity_id>` or `target_tag` with optional `target_filter`), `script` (named reusable script), `delay` (hands the rest of the chain to a background task — never blocks the engine), `condition` (inline; false halts the chain), `repeat` (N times or `while`, hard cap 1000 iterations), `spotlight` (publishes `overlay_show_team` to an overlay), `http` (outbound http/https request; scheme checked, private-address guard via `ipaddress`).
- **Tag targeting:** `target_tag` fires on **all** matching entities concurrently (`asyncio.gather`); `target_filter` is a per-candidate Jinja boolean with `trigger` and `target` in scope, e.g. `{{ trigger.entity.tags | intersect(target.tags) | length > 0 }}` — this is how one automation serves every field via convention-named entities (`timer.<fs>_<field>` tagged `fieldCountdown`+`fs<id>`, `spotify.<fs>_<field>` tagged `fieldAudio`+`fs<id>`).
- **Error handling:** a failing action is retried once, then the chain continues; every attempted action's outcome is recorded in `automation_runs.action_results` (first failure also in `failed_action_index`/`error`). A failure after retry fires ntfy.
- **Jinja2 context:** `trigger` (`.entity_id`, `.payload`, `.entity.tags`, `.entity.field`), `states(entity_id, attr?)`, `is_state(entity_id, value)`, `entities_with_tag(tag)`, `now()` (UTC). Filter `intersect`. Environment is a `SandboxedEnvironment`.
- **Execution history:** per-automation (`GET /api/v1/automations/{id}/runs`, needs `settings:edit`).
- **Boundaries:** resolves instances only via `loader.get_instance()` / `get_instances_by_tag()`; never imports integration classes.

Example:

```yaml
alias: "Field Countdown — Spotify at T-10s"
folder: "Timers"
trigger:
  - platform: timer_milestone
    tag: fieldCountdown
    remaining: "00:00:10"
action:
  - service: spotify.play_playlist_track
    target_tag: fieldAudio
    target_filter: "{{ trigger.entity.tags | intersect(target.tags) | length > 0 }}"
    data:
      playlist_uri: "{{ states(trigger.entity_id, 'match_playlist') }}"
```

Pre-results graphic (#40) — TM event types are passed through unchanged, so no code is needed; create this in the Automations page:

```yaml
alias: "Results graphic"
trigger:
  - platform: state
    event_type: audienceDisplayChanged
condition:
  - "{{ trigger.payload.display == 'RESULTS' }}"
action:
  - service: obs.switch_scene
    target: obs.main
    data:
      scene_name: "Results graphic"
```

A second automation on the next display change switches back. The exact `display` value (`RESULTS`) has **not** been confirmed against a live TM (the TM API docs only show `IN_MATCH`/`RANKINGS`); check the live event in Debug → Live Event Bus and adjust the condition if needed.

### 9.1 Entity IDs and tags

Every integration, timer and overlay instance has a system-wide unique `entity_id` of the form `<domain>.<name>` (`vex_tm.division_1`, `atem.main_switcher`, `timer.fs1_field1`, `overlay.main_stream`) and zero or more ad-hoc string tags (edited inline on each entity form; no tag management page). Tags are `TEXT[]` with GIN indexes.

## 10. Feature modules

### 10.1 Timer-Teleprompter (`modules/timer/manager.py`)

- Each timer instance binds a TM `field_set_id` + `field_id` and a `duration_s`. API: `start_countdown`, `stop_timer`, `reset_timer`, `get_state`.
- States: countdown to zero → **Match Running** (count-up elapsed) → ends on `matchStopped` for the bound field → "Match Complete". State lives in Redis; ticks recompute from `start_ts` (1s tick). Emits `timer_started`, `timer_milestone`, `timer_finished`, `timer_stopped`.
- Only the EMCEE starts the countdown (`prompter:control`, via the prompter page or API); field operators cannot from Match Control.
- **Cues:** Markdown cues (script/note/runsheet) stored in Postgres, streamed over WS; reconnecting iPads receive active cues (current + next). Pre-built runsheet and live cue push are both managed on the Timers page (`CuesPanel`).
- **Access:** `/prompter/<entity_id>?token=…` — token is `HMAC-SHA256(entity_id, SECRET_KEY-derived secret + per-instance nonce)`; no OIDC. `PUT /api/v1/timers/<id>` with `regenerate_token: true` rotates the nonce and invalidates old links. Validated at page load and again at WS upgrade (403 on failure). `/prompter` (index of timers) is guarded by the `prompter_index_token` setting.
- Teleprompter shows: server-synced countdown (identical across iPads), cue, next cue, upcoming-match card (teams, robot names, rank, awards) and the ⚡ high-potential flag.

### 10.2 Scraper and predictor

- **Scraper** (`fieldMatchAssigned` → ensure profiles exist): pulls TM data (teams, QUAL rankings, skills, matches, event) using its own `httpx` client with the bearer token from `qecomp:tm:<entity_id>:token`, and VEX Events API data (events.vex.com; bio, robot name, previous seasons, world/UK skills) using the token from Settings. Writes `team_profiles` (`cached_stats` JSONB), caches 24h, emits `team_profile_updated`. Teams are also created from the schedule (Match Control).
- **Predictor** (`fieldMatchAssigned` → `match_prediction`): local OPR least-squares (numpy) over scored matches; flags "high potential" if predicted combined alliance score is above the configured top-X% threshold; falls back to Robot Events data before in-event scores exist. Only the flag is user-visible.

### 10.3 Media pipeline

- Operator uploads a green-screen `.mp4`/`.mov` (Teams page); live WebGL chroma-key preview with colour picker / similarity / blend (`ChromaKeyPreview`).
- Backend uploads via **S3 multipart**, then runs ffmpeg (asyncio subprocess): downscale to ≤1920×1080 with pad, `colorkey=<hex>:<sim>:<blend>`, `format=yuva420p`, `libvpx-vp9`, `-pix_fmt yuva420p -auto-alt-ref 0` → transparent WebM. Raw is kept permanently so **Re-process** works with new sliders. No file size limit.
- S3 keys: `teams/{team}/video/raw.{ext}`, `teams/{team}/video/processed.webm`. Presigned URLs (15 min) via `S3_PUBLIC_ENDPOINT_URL`/settings public endpoint. Failures set `FAILED` + `video_processing_error` and fire ntfy.
- `team_clips` adds additional clip kinds per team (including the in-browser recorder output), processed through the same pipeline; `images.py` handles logo/site images.

### 10.4 Overlays

- Overlay instance = `entity_id` + `field_set_id`; URL `/overlay/<entity_id>` (public, no auth, designed for 1920×1080 OBS browser sources, transparent background).
- On `fieldMatchAssigned` the WS pushes `upcoming_match`; the page fetches presigned video URLs (`/api/v1/public/overlay/teams`), pre-buffers, and renders: **V5** 2×2 with red/blue alliance borders, **IQ** 2×1 no alliance colours. 500ms fade in/out; empty/transparent when nothing queued. Text label per cell is a configurable template (default `{{ team.name }}\n{{ team.number }}`). Robot name display is opt-in.
- **Spotlight:** operators (and `spotlight` automation/button actions) can force a single team onto an overlay (`POST /api/v1/overlays/<id>/show-team`, event `overlay_show_team`).
- The Overlays admin page has a preview of the next queued match without touching live OBS.

### 10.5 Rooms and notifications (`modules/notifications/`)

- **Rooms** group teams; each has a public display `/room/<slug>` (optional livestream embed, team list, QR code, live alerts) and a QR-linked `/notify?room=<slug>` page where phones pick teams (from that room) for alerts.
- A **leader-only** service reads cached TM schedules and sends: arrival alerts (x matches **and**/or y minutes away), late warnings (`late_minutes`), score alerts (skipping excluded rounds, e.g. finals), and agenda announcements (`announce_lead_minutes`/`agenda_lead_minutes`), de-duplicated via `notification_log`.
- **Delivery:** Web Push (VAPID, `pywebpush`) to phones and staff; room displays via WS. Per-subscriber categories; failing subscriptions are pruned via `failure_count`.
- **Broadcasts:** `notifications:broadcast` can message chosen rooms and/or phones. **Staff alerts:** anyone with `notifications:receive` (bell in the navbar, `StaffPushToggle`) gets operator alerts (integration degraded, failover, etc.) and broadcasts.
- iOS requires the page be added to the Home Screen for push. The reverse proxy must route `/room/`, `/notify`, `/notify.webmanifest`, `/sw.js` to the backend (`frontend/nginx.conf`).
- **ntfy** (self-hosted) is a separate operator channel (settings key `notifications`): integration DEGRADED, leader failover, automation action failure after retry, video processing failure.

### 10.6 Comms (desk ↔ iPad) (`modules/comms/`, `routers/comms.py`; see `docs/COMMS.md`)

- **Stations** (one per iPad) have a role (`emcee`, `floor_manager`, `camera`), page `/station/<slug>`. Stations can be **secured**: an `access_key` in the link (rotate to revoke) plus an optional PIN (scrypt-hashed; unlock yields a 12h token). Presence is tracked (green dot / last seen).
- Desk operators send **messages** (everyone / role / station; **Flash** until acknowledged), **presets**, switch the iPads' **screen**, run shared **timers** (countdown green→amber→red→flashing overtime, or count-up; pause/resume/±1 min), and manage **day/finals agendas** (drag reorder, bulk import `HH:MM-HH:MM Title | details`). iPads can type replies to the desk (inbox; staff push on arrival). Emcee stations also show the next-match countdown with upcoming teams/stats (earliest unscored match from the schedule cache).
- Voice is plain **Mumble** (`k8s/mumble.yaml`, 64738 tcp+udp, not behind HTTP ingress); TMTools does not proxy voice. The Mumble manifest is untested in a real cluster.

### 10.7 Button pages

Stream-Deck-style pages of buttons (Button Pages admin page, `buttons:*`). Each button has label, colour, icon, optional **confirm**, and an `action_yaml` built with the visual `ActionBuilder` (same action vocabulary as automations incl. `spotlight`/`http`). Pages and buttons are drag-and-drop orderable. Pressing is `POST …/buttons/{id}/press` (needs `buttons:press`) and works with API keys, so a real Stream Deck can use the HTTP webhook plugin (`Authorization: Bearer qec_…`).

### 10.8 Public site (`/site`)

Unauthenticated, display-safe team-facing website: overview, event/schedule, teams and team pages, rotating leaderboards and season highlights (`modules/site/leaderboards.py`: UK/world skills, programme labels VIQRC/V5RC), match recordings, livestreams, dashboard highlights. Configured via the `site` setting and the match-recordings admin (`MatchRecordingsCard`). Data comes from the schedule cache and `team_profiles.cached_stats`. Team pages include a `checklist` block (inspection, 360 robot video, team video; `modules/site/checklist.py`, shared with the admin API; a video counts when its processed clip is `DONE`, the bot video also counts a finished legacy 360 upload).

### 10.9 Stream Deck / API keys

API keys (`Settings → API Keys`): 32-byte `secrets.token_urlsafe`, prefixed `qec_`, SHA-256 hashed in `api_keys.key_hash`, shown once at creation, scoped to a permission set, revocable. Sent as `Authorization: Bearer qec_…`; usable on any `require_permission` route, e.g. `POST /api/v1/automations/<id>/trigger` or `POST /api/v1/integrations/<entity_id>/service/<service>`.

## 11. HTTP API

Generated reference: [`docs/API.md`](docs/API.md) and the live `/docs` (Swagger) / `/redoc` / `/openapi.json`. All `/api/v1/*` routes need a session cookie or API key plus the permission shown unless marked **public**. This table is derived from the router decorators.

**Health/status/auth**

| Method | Path | Permission |
|---|---|---|
| GET | `/healthz`, `/readyz` | none |
| GET | `/api/v1/status` | session (cluster, leader, integration health) |
| GET | `/auth/login`, `/auth/callback` | none (OIDC) |
| POST | `/auth/logout` | session |
| GET/POST | `/admin_login` | none (local break-glass) |
| GET | `/api/v1/auth/whoami` | none (returns `authenticated`, `subject`, `is_admin_local`, `permissions`) |

**Integrations / Spotify / ZerOS**

| Method | Path | Permission |
|---|---|---|
| GET | `/api/v1/integrations/schemas` | session |
| GET | `/api/v1/integrations` | `integrations:read` |
| POST | `/api/v1/integrations` | `integrations:edit` |
| PUT/DELETE | `/api/v1/integrations/{entity_id}` | `integrations:edit` |
| POST | `/api/v1/integrations/{entity_id}/service/{service}` | per-domain (`tm:control`, `audio:control`, `video:control`, `vfx:control`); leader-proxied |
| POST | `/api/v1/integrations/{entity_id}/oauth_token` | `integrations:edit` |
| GET | `/api/v1/integrations/{entity_id}/state` | `integrations:read` |
| POST | `/api/v1/integrations/{entity_id}/test` | `integrations:read` |
| GET | `/api/v1/integrations/{entity_id}/spotify/library` | `integrations:read` |
| GET | `/api/v1/zeros/presets` | `integrations:read` |
| POST/PUT/DELETE | `/api/v1/zeros/presets[/{preset_id}]` | `integrations:edit` |
| GET | `/api/v1/debug/integrations/{entity_id}/log` | `settings:edit` (per-instance raw event log, last 100) |

**Automations / scripts**

| Method | Path | Permission |
|---|---|---|
| GET | `/api/v1/automations`, `/api/v1/automations/folders` | `automations:read` |
| POST | `/api/v1/automations`, `/api/v1/automations/folders`, `/api/v1/automations/validate` | `automations:edit` |
| PUT/DELETE | `/api/v1/automations/{automation_id}`, `/api/v1/automations/folders/{folder_id}` | `automations:edit` |
| POST | `/api/v1/automations/{automation_id}/trigger` | `automations:trigger` |
| GET | `/api/v1/automations/{automation_id}/runs` | `settings:edit` |
| GET | `/api/v1/scripts` | `automations:read` |
| POST/PUT/DELETE | `/api/v1/scripts[/{script_id}]` | `automations:edit` |

**Timers / prompter**

| Method | Path | Permission |
|---|---|---|
| GET | `/api/v1/timers` | `timers:read` |
| POST/PUT/DELETE | `/api/v1/timers[/{entity_id}]` | `timers:edit` |
| POST | `/api/v1/timers/{entity_id}/start`, `/stop`, `/reset` | `prompter:control` |
| GET | `/api/v1/timers/{entity_id}/cues` | `prompter:view` |
| POST/PUT/DELETE | `/api/v1/timers/{entity_id}/cues[/{cue_id}]` | `prompter:edit` |

**Overlays**

| Method | Path | Permission |
|---|---|---|
| GET | `/api/v1/overlays`, `/api/v1/overlays/{entity_id}/preview` | `overlays:read` |
| POST/PUT/DELETE | `/api/v1/overlays[/{entity_id}]` | `overlays:edit` |
| POST | `/api/v1/overlays/{entity_id}/show-team` | `overlays:edit` |
| GET | `/api/v1/public/overlay/teams` | **public** (presigned team videos for overlay pages) |

**Teams / clips / site admin**

| Method | Path | Permission |
|---|---|---|
| GET | `/api/v1/teams`, `/api/v1/teams/checklist` (per-team inspection / 360 bot video / team video, #43), `/api/v1/teams/{number}`, `/api/v1/teams/batch/videos`, `/api/v1/teams/{number}/video/status`, `/api/v1/teams/{number}/clips` | `teams:read` |
| PUT | `/api/v1/teams/{number}` (pit location, notes, website, `inspection_status`) | `teams:edit` |
| POST/DELETE | `/api/v1/teams/{number}/logo` | `teams:edit` |
| POST | `/api/v1/teams/{number}/video`, `/api/v1/teams/{number}/clips/{kind}` | `video:upload` |
| POST | `/api/v1/site/images` | `settings:edit` |
| GET | `/api/v1/match-recordings` | `settings:read` |
| PUT/DELETE, POST `/import` | `/api/v1/match-recordings[/…]` | `settings:edit` |

**Settings / access**

| Method | Path | Permission |
|---|---|---|
| GET | `/api/v1/settings` | `settings:read` |
| PUT | `/api/v1/settings` | `settings:edit` |
| GET/POST/DELETE | `/api/v1/api-keys[/{key_id}]` | `settings:edit` |
| GET/PUT | `/api/v1/users/roles` | `settings:edit` |
| POST | `/api/v1/users/{user_id}/invalidate-session` | `settings:edit` (force logout) |
| GET | `/api/v1/audit` | `settings:read` |

**Rooms / notifications**

| Method | Path | Permission |
|---|---|---|
| GET | `/api/v1/rooms`, `/api/v1/rooms/{slug}` | `rooms:read` |
| POST/PUT/DELETE | `/api/v1/rooms[/{slug}]` | `rooms:manage` |
| GET | `/api/v1/public/rooms/{slug}`, `/api/v1/public/push/vapid-key` | **public** |
| POST | `/api/v1/public/push/subscribe`, `/status`, `/unsubscribe` | **public** |
| POST | `/api/v1/notifications/staff/subscribe`, `/unsubscribe` | `notifications:receive` |
| POST | `/api/v1/notifications/broadcast` | `notifications:broadcast` |
| GET | `/api/v1/capture/status` | `analysis:view` — agent presence + the recording in progress |
| GET | `/api/v1/capture/recordings[/{id}]` | `analysis:view` — filters `team`, `match_type`, `match_key`, `limit`; includes moments |
| POST | `/api/v1/capture/recordings/start`, `/recordings/stop` | `capture:control` — same as the service calls (503 if no agent) |
| POST | `/api/v1/capture/moments` | `capture:control` — mark the last `length_s` (0–30) seconds; 409 if nothing is recording |
| DELETE | `/api/v1/capture/recordings?match_type=` | `capture:control` — wipe recordings and their stored files (between events) |
| GET | `/api/v1/capture/replay/{id}` | `analysis:view` — current replay player state (`playing`, `paused`, `rate`, `url`, `label`) |
| POST | `/api/v1/capture/replay/{id}/play` | `capture:control` — "send to program": body `{kind: recording\|moment, id, rate (default 0.5), loop}`; stores state in `qecomp:replay:{id}:state` and publishes `replay_command`; 409 if the video is not uploaded. The deck then calls `obs.switch_scene` separately |
| POST | `/api/v1/capture/replay/{id}/control` | `capture:control` — `pause`/`resume`/`restart`/`rate`/`seek`/`stop`; 409 when nothing is playing, 422 on bad values |
| GET | `/api/v1/capture/{recordings\|moments}/{id}/url` | `analysis:view` — presigned GET URL (409 until uploaded) |
| POST | `/api/v1/capture/uploads` | `capture:upload` (agent key) — presigned single-PUT URL for `match` or `moment`, signed against the public endpoint |
| POST | `/api/v1/capture/uploads/{id}/complete` | `capture:upload` — verifies the object exists, marks READY, publishes the event |
| GET | `/api/v1/public/recordings/{id}/video` | **public** — 302 to a presigned GET of a READY match video (linked from the public site's match list; an imported link wins) |
| POST | `/api/v1/notifications/team-alert` | `notifications:broadcast` — alert specific teams (`pit_admin`/`lost_and_found`/`custom`); modal notice in each room containing them + push to subscribers following them (ignores category opt-outs); returns `{rooms, pushed, unreached_teams}`; audited as `team_alert` |
| POST | `/api/v1/notifications/test`; GET `/api/v1/notifications/stats` | `notifications:manage` |

**Comms** (`comms:read` = list/inbox view; `comms:manage` = stations, presets, agenda CRUD, links/PIN; `comms:send` = send/screen/timer/ack/clear inbox)

| Path | Notes |
|---|---|
| `/api/v1/comms/stations` (+ `/{slug}`, `/{slug}/link`, `/{slug}/rotate-link`, `/{slug}/pin`) | station CRUD and secure link/PIN |
| `/api/v1/comms/presets`, `/api/v1/comms/agenda` (+ `/order`, `/import`, `/{id}`) | presets and agenda |
| POST `/api/v1/comms/send`, `/screen`, `/timer` | push to stations |
| GET `/api/v1/comms/inbox`; POST `/inbox/{message_id}/ack`; DELETE `/inbox` | replies from stations |
| **public:** POST `/api/v1/public/stations/{slug}/unlock`; GET `…/{slug}`, `…/{slug}/agenda`, `…/{slug}/next-match`; POST `…/{slug}/messages` | iPad station pages (slug/access-key/PIN-token credentialed) |

**Button pages** (`buttons:read` list; `buttons:manage` create/update/delete/reorder; `buttons:press` press)

`/api/v1/button-pages` (+ `/order`, `/{page_id}`, `/{page_id}/buttons`, `/{page_id}/buttons/order`, `/buttons/{button_id}`, `/buttons/{button_id}/press`).

**Public site (no auth)**

`GET /api/v1/public/site/overview | leaderboards | event | teams | teams/{number} | livestreams`.

## 12. WebSockets

| Path | Auth | Purpose |
|---|---|---|
| `/ws/events` | session cookie (any authenticated user; page-level permission gating is in the SPA) | live event stream for Dashboard, Match Control, Audit, debug |
| `/ws/prompter/{entity_id}` | HMAC token (`?token=`) | `timer_state`, `cues`, upcoming match + ⚡ flag; accepts `start_countdown` only from principals with `prompter:control` |
| `/ws/overlay/{entity_id}` | none | `upcoming_match`, `show_team`, transitions |
| `/ws/replay/{id}` | none (like the overlay; id must match `[A-Za-z0-9._-]{1,100}`) | replay player page: server sends the stored state on connect, then `replay_command` payloads; page sends only keepalives |
| `/ws/agent` | scoped `qec_` key holding `capture:upload` (`Authorization: Bearer` or `?key=`) | capture agent link: server sends `start_recording`/`stop_recording`/`mark`; agent sends `hello`/`status` (heartbeat, refreshes presence), `recording_stopped`, `moment_cut`, `error` |
| `/ws/room/{slug}` | none (slug) | alerts/notices for the room display |
| `/ws/station/{slug}` | slug (+ access key / PIN token if secured) | comms: `message`, `screen`, `timer`, `notice` |

`ConnectionManager` (in `routers/ws.py`): `connect`, `disconnect`, `broadcast(entity_id, msg)`, `broadcast_all(msg)`; `connect(..., receive_all=False)` keeps a socket out of the `broadcast_all` fan-out (the agent socket, so it only gets commands addressed to it); one Redis pub/sub subscriber per node fans `qecomp:events` out to local sockets.

## 13. Standalone and public pages

Vanilla HTML+JS served by FastAPI (no build step, native WebSocket with backoff): `overlay.html`, `replay.html` (OBS replay player), `prompter.html` (entity id/token injected via JSON-encoded substitution to avoid script injection), `room.html`, `station.html`, `notify.html`, `site.html`, `leaderboard.html`, `sw.js` (service worker for push).

| URL | Auth |
|---|---|
| `/overlay/{entity_id}` | none |
| `/prompter` | `?token=<prompter_index_token>` |
| `/prompter/{entity_id}` | `?token=<HMAC>` |
| `/room/{slug}`, `/room/{slug}/qr.svg`, `/notify`, `/notify.webmanifest`, `/sw.js` | none |
| `/station/{slug}` | slug (+ key/PIN) |
| `/site`, `/site/leaderboard` | none |
| `/admin_login` | local admin form (username fixed to `admin_local`) |

These are registered before the SPA catch-all.

## 14. Auth, sessions and RBAC

- **OIDC login** (`/auth/login` → `/auth/callback`): validates the ID token (python-jose + discovery), reads groups from `OIDC_GROUPS_CLAIM`, resolves permissions via `role_permissions`, stores a Redis session (24h) and sets a signed (`itsdangerous`) session cookie. Groups are evaluated **at login only**; changes apply on next login. Admins can force logout (`invalidate-session`).
- **`admin_local`:** break-glass account at `/admin_login`, password = `ADMIN_LOCAL_PASSWORD`, always all permissions, not configurable in the UI.
- **Principal resolution** (`core/dependencies.py`): `Authorization: Bearer qec_…` → API key (hash lookup, scoped permissions) else session cookie. The sentinel permission `"*"` grants everything (used for `admin_local` and `qecomp-admin`'s single seeded row). `require_permission(p)` is the FastAPI dependency; integration service calls check a per-domain permission inline.
- **Unauthenticated browsers** are redirected to OIDC by the SPA.

### 14.1 Permissions

| Permission | Grants |
|---|---|
| `integrations:read` / `:edit` | view / create-update-delete integration instances, ZerOS presets, state, test |
| `automations:read` / `:edit` / `:trigger` | view / modify automations+scripts / manual trigger |
| `timers:read` / `:edit` | view / modify timer instances |
| `prompter:view` / `:control` / `:edit` | view cues / start-stop-reset countdown / edit cues and runsheet |
| `overlays:read` / `:edit` | view / modify overlays and spotlight |
| `teams:read` / `:edit` | view / edit profiles, logo |
| `video:upload` | upload team videos and clips |
| `tm:control`, `audio:control`, `video:control`, `vfx:control` | call services on `vex_tm`; `spotify`; `atem`/`obs`/`ptz`; `zeros`/`osc`/`wled`/`artnet` |
| `rooms:read` / `:manage` | view / modify rooms |
| `notifications:receive` / `:broadcast` / `:manage` | staff push / broadcast / test + stats |
| `buttons:read` / `:press` / `:manage` | view / press / modify button pages |
| `comms:read` / `:send` / `:manage` | view / send+ack / configure stations, presets, agenda |
| `capture:control` / `capture:upload` / `analysis:view` | start-stop-mark recordings, wipe, call `capture.*` services / the agent's upload key (+ `/ws/agent`) / view recordings, moments and agent status (analysis deck) |
| `settings:read` / `:edit` | view settings, audit, recordings / edit settings, API keys, roles, site images, debug views, automation run history |

### 14.2 Seeded Authentik group mappings (editable in Users & Roles)

| Group | Permissions (cumulative across migrations) |
|---|---|
| `qecomp-admin` | `*` |
| `qecomp-operator` | `integrations:read`, `automations:read`, `automations:trigger`, `timers:read`, `teams:read`, `teams:edit`, `video:upload`, `vfx:control`, `video:control`, `audio:control`, `tm:control`, `overlays:read`, `prompter:edit`, `rooms:read`, `rooms:manage`, `notifications:receive`, `notifications:broadcast`, `buttons:read`, `buttons:press`, `buttons:manage`, `comms:read`, `comms:send`, `comms:manage`, `capture:control`, `analysis:view` |
| `qecomp-lighting` | `vfx:control`, `integrations:read` |
| `qecomp-video` | `video:control`, `video:upload`, `overlays:read`, `overlays:edit`, `buttons:read`, `buttons:press`, `capture:control`, `analysis:view` |
| `qecomp-emcee` | `prompter:view`, `prompter:control` |
| `qecomp-staff` | `rooms:read`, `teams:read`, `notifications:receive`, `comms:read`, `analysis:view` |
| `qecomp-viewer` | `teams:read`, `integrations:read`, `automations:read`, `rooms:read`, `buttons:read`, `comms:read` |

Authentik setup: [`docs/AUTHENTIK_SETUP.md`](docs/AUTHENTIK_SETUP.md) (OAuth2/OIDC provider; scopes `openid profile email` plus a group-exposing scope; redirect URI `<app_url>/auth/callback`; set `OIDC_GROUPS_CLAIM` to match). Local real-OIDC testing uses Authelia via `local-testing/`.

## 15. Frontend (admin SPA)

**Stack:** React 18, Vite 5, TypeScript (strict), React Router v6 (`createBrowserRouter`), Zustand (stores), TanStack Query v5 (all REST), TailwindCSS v3 with vmd1.dev tokens, `lucide-react` icons, `react-markdown`, `js-yaml`. No component library, no Axios (native `fetch` via `lib/http.ts`). Lint: oxlint. Unit tests: vitest + Testing Library (Node 22 needed). E2E: Playwright.

**Layout:** `src/api/` (one TanStack-Query module per backend resource, plus `spotifyOAuth.ts`), `src/stores/` (`auth`, `ui`, `ws`), `src/hooks/` (`useWebSocket`, `usePermission`, `useDebounce`, `useCacheSync`), `src/components/{ui,layout,integrations,automations,buttons,comms,overlays,teams,timers,site,ws}`, `src/pages/`, `src/lib/` (`actionBuilder`, `siteConfig`, `recorder`, `push`, `http`), `src/types/api.ts`, `src/router.tsx`.

**Routes** (`src/router.tsx`; navbar in `components/layout/Navbar.tsx`):

| Path | Page | Notes |
|---|---|---|
| `/admin_login` | AdminLogin | public |
| `/integrations/spotify/callback` | SpotifyCallback | PKCE return |
| `/` | Dashboard | integration health + telemetry (Spotify now playing, ATEM input, ZerOS last preset, TM fields), last 50 events, cluster/leader status, Redis-unavailable banner |
| `/match-control` | MatchControl | per-TM-instance live field cards (state, match, teams, recent events) and manual controls: Queue Next/Prev/Skills, Start, End Early, Abort, Reset, audience display. **Field Monitor was merged into this page**; `/fields` redirects here |
| `/integrations` | Integrations | CRUD from manifest schemas, status chips, tags, test connection, Spotify auth button, ZerOS presets + fire, telemetry cards |
| `/automations` | Automations | folder tree (create/rename/delete), enable toggle, form builder + YAML editor, validate, test run, run history |
| `/scripts` | Scripts | same editors |
| `/timers` | Timers | instance CRUD, duration, copyable prompter link, regenerate token, cues/runsheet |
| `/overlays` | Overlays | CRUD, copyable URL, next-match preview, spotlight |
| `/teams`, `/teams/:number` | Teams, TeamDetail | search/filter; stats, notes, pit location, inspection checklist (status select, 360/team-video marks; list page shows progress + incomplete-only filter), logo, video upload + WebGL keying preview + re-process, clips, bio |
| `/analysis` | Analysis | Analysis desk (#38/#41), gated on `analysis:view` (navbar entry too); controls need `capture:control`. Match navigation (arrows, go-to-number) over `GET /capture/recordings`, follows the live recording. **Match tab:** agent/recording state, Mark last 3/5/10 s (buttons + number-key bindings, stored per browser), moment list with upload state, per-moment and full-match "Send to program" (`/capture/replay/{id}/play`, then `obs.switch_scene` on the chosen OBS instance), pause/resume/restart, speed 0.25/0.5/0.75/1×, Back to program (stop + switch scene), Flash LED (`wled.flash`). **Stats tab:** each team's cached stats for the selected match; never shows predictions. Setup (replay id, OBS/WLED instance, scenes, keys) lives in `localStorage` (`lib/analysis.ts`). Polls every 2–3 s (`api/capture.ts`) |
| `/record` | Recorder | in-browser clip recorder |
| `/rooms` | Rooms | rooms, team allocation, QR/links, broadcast |
| `/comms` | Comms | stations, presets, send, screens, timers, agenda, inbox |
| `/buttons` | ButtonPages | visual action builder, drag-and-drop, icons, confirm, press |
| `/audit` | AuditLog | filters (user, action, resource, date), live append via WS |
| `/settings` | Settings | S3, VEX Events API token, predictor threshold, chroma defaults, notifications/ntfy + team alerts, prompter index token, API keys, site config + images, match recordings |
| `/users` | Users | group → permission mapping, force logout |
| `/debug/integration-log`, `/debug/event-bus` | Debug views | `settings:edit`; visible in the navbar to those users |

(Automation execution history lives inside the Automations page, not a separate route.)

## 16. Backend module boundaries

Dependencies flow **downward**: `core` → `models` → `schemas` → `loader`/`modules` → `routers` → `main.py`.

| Module | May import | Must not import |
|---|---|---|
| `core/` | stdlib/3rd-party only (documented exception: `dependencies.py` imports `models.settings` for RBAC) | `models` (else), `schemas`, `routers`, `loader`, `modules` |
| `models/` | `core` (Base) | `schemas`, `routers`, `loader`, `modules` |
| `schemas/` | stdlib + pydantic only | anything in `backend/` |
| `loader.py` | `core`, `models`; integrations loaded via `importlib` | `routers`, automation, timer, scraper, media, predictor |
| `modules/integrations/<d>/` | `core`, 3rd-party libs | `loader`, `routers`, `models`, `schemas`, other domains, other modules |
| `modules/leader.py` | `core` (Redis; also `core.ntfy`) | everything else |
| `modules/automation/` | `core`, `models`, `loader`, `schemas.events` | `routers`, integration modules, timer, scraper, media |
| `modules/timer/` | `core`, `models`, `loader` (lazy, best-effort) | `routers`, automation, scraper, media, integrations |
| `modules/scraper/` | `core`, `models`, `schemas.events` | `routers`, automation, timer, media, predictor |
| `modules/predictor/` | `core`, `models`, `schemas.events` | `routers`, loader, scraper, automation, timer, media |
| `modules/media/*` | `core`, stdlib (+ aioboto3 for s3) | loader, routers, models, other modules |
| `routers/` | `core`, `models`, `schemas`, `loader`, module public APIs | other routers (share via `core/dependencies.py`), business logic (routers are thin) |
| `routers/ws.py` | `core`, `loader`, `models` | integrations, automation, timer, scraper (reach TimerManager via `app.state`) |
| `main.py` | everything — **only** file that instantiates `LeaderElection`, `AutomationEngine`, `TimerManager`, `Scraper`, `Predictor`, `Loader`, `NotificationService` | business logic |

Cross-cutting: no circular imports; no in-process singleton state (state lives in Redis/Postgres); integrations never call each other (coordination is the engine's job); the engine never imports integration classes; the event bus is the only broadcast mechanism; routers are stateless and never spawn background tasks.

### 16.3 Async discipline

Everything runs in one asyncio loop; a blocking call stalls the whole server.

| Banned | Use instead |
|---|---|
| `time.sleep` | `await asyncio.sleep` |
| `requests.*` | one shared `httpx.AsyncClient` per integration (created in `setup`, closed in `teardown`) |
| blocking socket I/O | async sockets or `asyncio.to_thread` |
| `open().read()` | `aiofiles` |
| `subprocess.run` | `asyncio.create_subprocess_exec` |
| sync ORM calls | `await session.execute(...)` (SQLAlchemy async) |
| `while True` without an await | always await something |
| `threading.Event.wait()` | `asyncio.Event` |

`teardown()` must set a shutdown flag, cancel and gather its tasks, close connections, never raise, and finish within 5s.

## 17. Frontend rules

- **Two data channels, never entangled.** REST data → TanStack Query (`useQuery`); push data → Zustand `ws` store via the single `dispatch(event)` entry point. Never write WS data into the Query cache (`setQueryData`), never write Query results into the WS store, never merge both sources into one object in a component. The **only** allowed interaction is cache invalidation, implemented once in `hooks/useCacheSync.ts` (mounted at the app root).
- Ownership: integration config / automation YAML / team profile / timer config / audit history / user roles → Query; integration status, automation last-run, live matches, timer ticks, event feed, live audit entries → `ws` store; current user and permissions → `auth` store (set at login).
- `useWebSocket(url)` is side-effect-only (returns nothing): opens the socket, dispatches parsed JSON to the store, sets/clears the "Reconnecting…" banner via the `ui` store, backs off 1s→30s, closes on unmount.
- No raw `fetch` in pages — all API access via hooks in `src/api/`. Components fetch their own data (props are IDs/primitives); pages compose layout and must not import other pages. `stores/` imports nothing from the app; `api/` knows nothing about UI.
- TypeScript `strict: true`; `any` only with a justifying comment.

## 18. Testing

- **Backend:** `cd backend && pytest --cov=backend --cov-report=term-missing`; CI enforces `--cov-fail-under=80`. Stack: pytest, pytest-asyncio (`asyncio_mode=auto`), httpx `AsyncClient`, pytest-mock, fakeredis, factory_boy, pytest-cov, moto[s3]. Needs real Postgres (5433) and Redis (6380); media tests need MinIO and a local `ffmpeg`. Layout under `backend/tests/`: `test_automation/`, `test_comms/`, `test_core/` (auth, events schema, health, leader, rbac, security, settings), `test_integrations/` (unit per domain + vex_tm e2e with `mock_tm_server.py`), `test_media/`, `test_notifications/`, `test_predictor/`, `test_public_site/`, `test_routers/`, `test_scraper/`, plus loader, lifespan, prompter page, timer and WS tests.
- **Frontend:** `npm run lint`, `npx tsc --noEmit`, `npm test` (vitest), `npm run build`; Playwright e2e under `frontend/e2e/` (`auth`, `buttons`, `comms`, `overlay`, `recorder`, `site` specs; seeded by `tools/e2e_seed.py`). See [`docs/TESTING.md`](docs/TESTING.md).
- **CI** (`.github/workflows/tests.yml`): backend job (Postgres+Redis services, MinIO, `alembic upgrade head`, single-head check, pytest with coverage) and frontend job (Node 22: `npm ci`, type-check, unit tests, build), plus an e2e job.

## 19. Build, CI and deployment

- **Dockerfile** (multi-stage; CI builds each target): `frontend-builder` (node:20) → `frontend` (nginx-unprivileged serving the built SPA on 8080, `frontend/nginx.conf`) and `backend` (python:3.12-slim + `ffmpeg`, `libvpx-dev`; copies `backend/`, `alembic/`, `alembic.ini`, bakes the SPA into `backend/static/frontend/`; non-root uid/gid 1000; `uvicorn backend.main:app --host 0.0.0.0 --port 8000 --workers 1`).
- **compose.yml** (local full stack): `cloudflared` (tunnel), `app`, `migrate` (alembic upgrade head), `bucket-init` (creates `S3_BUCKET` in MinIO), `postgres:16`, `redis:7`, `minio`. Host ports: app 8000, Postgres 5433, Redis 6380, MinIO 9000/9001. `app` has a fixed IP on the external `cf` network (10.65.1.20).
- **docker-publish.yml:** native amd64 + arm64 builds per component, merged into multi-arch manifests on ghcr.io tagged `:backend`, `:frontend`, `<sha>-<component>`, `<version>-<component>`; cosign-signed; runs nightly, on `main`, on `v*.*.*` tags and (build only, no push) on PRs.
- **k8s/** (namespace `qecomp`): `deployment.yaml` (3 replicas, RollingUpdate, required anti-affinity, `migrate` init container, non-root, `/healthz` liveness, `/readyz` readiness, `POD_IP` from downward API, secrets from `qecomp-secrets`, env from `qecomp-config`; resources 250m/256Mi → 1 CPU/512Mi), `service.yaml` (ClusterIP 80→8000), `ingress.yaml` (Traefik `IngressRoute`, host `vex.kmnet.uk`, TLS secret `wildcard-vmd1-tls`), `configmap.yaml` (`OIDC_GROUPS_CLAIM`, `S3_BUCKET`, `S3_REGION`, `S3_ENDPOINT_URL=http://qecomp-minio:9000`, `APP_PORT`), `mumble.yaml` (voice server; password secret must be changed).
- **Deployment is manual** `kubectl apply`; Postgres and Redis are external/managed (DSN and `REDIS_URL` come from secrets). Required secrets: `POSTGRES_DSN`, `REDIS_URL`, `OIDC_ISSUER_URL`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `ADMIN_LOCAL_PASSWORD`, `SECRET_KEY`, `ENCRYPTION_KEY`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`; optional `VAPID_PRIVATE_KEY`, `VAPID_PUBLIC_KEY`.

## 20. Local development and tooling

```bash
docker compose up -d postgres redis minio       # infra only
cd backend && python3 -m venv .venv-backend && source .venv-backend/bin/activate
pip install -r requirements.txt && alembic upgrade head   # run from repo root where alembic.ini lives
uvicorn backend.main:app --host 0.0.0.0 --port 8000
cd frontend && npm install && npm run dev       # http://localhost:5173
```

Or `docker compose up -d` for everything (migrations and bucket creation automated). On bare MinIO you must create `S3_BUCKET` yourself. Log in at `/admin_login` as `admin_local` with `ADMIN_LOCAL_PASSWORD`; use `local-testing/` for a real OIDC (Authelia behind Traefik) login. `.claude/launch.json` defines the `frontend` dev server and a `qecomp-docker` preview URL.

`tools/`: `capture_agent/` (the ffmpeg capture agent, #45: `agent.py`, `requirements.txt`, `README.md`; runs on the AV laptop, talks only to the server via `/ws/agent` + presigned uploads, local-first recording to Matroska → `+faststart` MP4, clip cut ~1.5 s after a mark, persisted upload queue with separate clip/match workers and backoff, flags `--reencode/--no-reencode` and `--video-bitrate`; tested by `backend/tests/test_capture/test_agent_tool.py` against a `lavfi` source), `add_user.py` (legacy), `e2e_seed.py` (seeds data for Playwright), `generate_vapid_keys.py` (Web Push keys), `get_field_sets.py` (list TM field sets), `get_spotify_devices.py`, `migrate_remove_legacy.py`.

Contributions go through pull requests, not direct commits to `main`.

## 21. Design system

The UI matches vmd1.dev: Inter + JetBrains Mono; black background; glass surfaces (`rgba(8,8,8,0.43)` with `backdrop-filter: blur(10px) saturate(155%)`); borders `rgba(255,255,255,0.08)`; success `#86efac`, danger `#fca5a5`. Tokens are CSS variables in `frontend/src/index.css` (`--color-surface`, `--color-border`, `--color-text*`, `--shadow-card*`, `--shadow-glass*`, `--shadow-chip*`) and mirrored as `vmd.*` colours / `vmdCard*` shadows in `frontend/tailwind.config.js`. Component classes: `.vmd-card`, `.vmd-navbar` (sticky glass), `.navbar-anim-link` (icon + sliding label on hover/active), `.vmd-chip`, `.vmd-code-block` (YAML editor). New UI must use these rather than introducing new styles.

## 22. Known gaps, legacy leftovers and discrepancies

Honest list of things that do not match the original plan or each other. Fix and remove entries as they are resolved.

- **Legacy tree still present:** `modules/` (`match_scheduler.py` + READMEs), `storage/` (README, `userInfo/*/Me.txt`) and `tools/add_user.py` are pre-rewrite artefacts. `.gitignore` still references `storage/fields`, `schedule.json` etc. Nothing in `backend/` imports them (not exhaustively verified); candidates for deletion.
- **Image/tag mismatch:** `k8s/deployment.yaml` pulls `ghcr.io/vmd1/vex-tm-tools-priv:latest`, but CI publishes `:backend` / `:frontend` tags under the repository path, and README badges point at `qerobotics/vex-tm-tools`. Reconcile before relying on either.
- **Python version drift:** Dockerfile uses 3.12; CI and the README badge say 3.13.
- **Stale text:** `.env.example` header says "QEComp"/"later wave"; `docs/API.md` is a generated snapshot that may lag the routers; the README "Node 20" badge differs from CI's Node 22. Source-code comments throughout still cite the old plan's section numbers (e.g. "plan §5.8", "Appendix C.2") — those refer to the removed `tm_update_plan.md`; the equivalent content is in this file.
- **Plan items not in the shipped design:** Redis Sentinel client config (infra handles it), a standalone Field Monitor page (merged into Match Control), a top-level `tests/` dir (tests are in `backend/tests/`), `k8s` Postgres/Redis manifests (external), `userManager.py`/JSON storage (removed), `docs/API.md` hand-written (generated).
- **Unverified:** the **VEX Events API** scraper has not been tested against the live service (issue #29; the TM integration *has* been tested against a live TM server); WLED/OSC/PTZ on real hardware and BirdDog X1 VISCA support (the tracking issue #20 was closed as not planned, so this is untracked); Mumble manifest in a real cluster; push-to-talk on iPad; exact behaviour of every WS message type was summarised from `routers/ws.py`, not exhaustively tested.
- **Design decision still open:** whether `/ws/events` should enforce per-permission filtering (currently any authenticated session; gating is client-side).

### 22.1 Planned work (GitHub issues)

> **Planned deployment change (not yet implemented, #46):** the server is moving from k3s to a portable plain-Docker stack (runs on a Mac, a Raspberry Pi or another laptop), with Garage (S3-compatible) replacing MinIO. The k8s/MinIO descriptions elsewhere in this file stay accurate for the current code until that lands. The replay chain only assumes an S3 API with presigned URLs (the existing helper in `backend/modules/media/s3.py` already uses path-style addressing and `S3_PUBLIC_ENDPOINT_URL`).

Not implemented yet. Decisions recorded here are agreed; implementation details marked *proposal* are not.

| Issue | Planned work | Decisions so far |
|---|---|---|
| #13 | **Overlay rework** | VRC pre-match 2×2 (red left / blue right, **square** transparent robot videos, large VS low in the frame, match-number banner on top, flag badge in each cell's outer top corner); IQ pre-match: 2 teams, same panel size, light blue (same as VRC blue), no VS; single-team spotlight: square robot 360 directly above a red label bar (number, name, flag at right end), keyed `team` clip on the right. Text dark gold `#b8860b` (including the match number). All overlays fully transparent (OBS browser source, 1920×1080); no crown in the overlay (background comes from OBS). Long names run under the robot square. Needs per-team flag/country. Mockup HTML is attached to the issue. Open: sponsor logo, deadline |
| #40 | Pre-results graphics | **No new code**: one automation triggered by the `audienceDisplayChanged` event with `display == RESULTS`, calling `obs.switch_scene` (optionally `atem.switch_input`); example documented in §9; the `RESULTS` value still needs confirming on a live TM |
| #45 | **Local ffmpeg capture agent** (on the AV laptop, outside the server deployment) | **Implemented:** server side (`modules/capture/`, `routers/capture.py`, `/ws/agent`, `capture` integration, upload API; see §8, §11, §12) and the agent (`tools/capture_agent/`, see §20). Main capture method: reads the webcam feed the ATEM provides to the laptop, encodes H.264 by default (1 s keyframes; `reencode` on/off and `video_bitrate` are agent settings, off = stream copy, clips always re-encoded), records locally first, uploads with presigned PUT URLs, cuts clips on request. Layout `{matchType}/{matchNumber}/match.mp4` + `segments/moment-N.mp4`; cleared between events. Sizing for 245 IQ matches: ~41 GB at 15 Mbps, ~190 GB as-is at ~70 Mbps. Priorities: live recording is never interrupted, replay clip cut/upload is next and jumps ahead of match-file uploads; target clip ready within ~10 s of the mark. Not involved in playback; may run on a different machine from OBS. Open: packaging |
| #46 | Deployment move | k3s to a portable Docker Compose stack (Mac, Raspberry Pi or another laptop); Garage replaces MinIO (single node, first-run layout/bucket/key init); Mumble becomes a Compose service; multi-arch (amd64 + arm64) images; re-check single-instance leader election. Replay chain does not block on it |
| #42 | Match recording + per-team tagging | **Server side implemented** (recordings tagged with match key and teams, per-team listing on `GET /capture/recordings?team=`, READY videos linked from the public site's match list). Needs the #45 agent to produce footage; team profile surfacing is in the analysis desk match list only (a per-team list on the profile page is pending) |
| #38 | Instant replay | Finals only; marks key moments from the analysis deck (last 3s / 5s / 10s buttons, keyboard-mappable; exact timestamp stamped on the agent's recording clock) and cuts clips from the match recording, uploaded to `{matchType}/{matchNumber}/segments/moment-N`; LED-flash button; moments saved to team profiles; blank slate each match. Depends on #45 and #42 |
| #41 | Analysis/host desk | Finals only; stats screen (2 teams IQ / 4 teams VRC per page, arrows or match-number entry) plus a match screen (live program feed and mark buttons during the match; after the match, moment list and full video, send to program = the server tells a replay player page (an OBS browser source served by the web server, streaming from the object store via presigned GET URLs) to play it, and the deck switches OBS to that scene via `obs.switch_scene`; a video must be uploaded before it can be played; pause, speed 0.5× default / 0.25 / 0.75 / 1×). Never shows predicted scores. Depends on #38, #45, #42 |
| #29 | Validate VEX Events API against the live service | Open; also refresh `backend/tests/mock_tm_server.py` |
| #23, #21, #26 | Real-device verification | Recorder 60fps, push on iOS/Chrome/Edge, iPads + Mumble |

Build order for the replay chain: #45 → #42 → #38 → #41. Not planned: Shearly light mapping (covered by MagicQ via #39), UWB location tracking, a self-hosted livestream, live polls/trivia, NDI.

## 23. Change history

- **v3.0 plan (2026-07-22):** original `tm_update_plan.md`, FastAPI + Postgres + Redis rewrite with HA, integrations, automations, timers, teams, overlays.
- **Rewrite delivered** in waves (core → loader/integrations → engine/timers → routers/WS → frontend → scraper/predictor/media), then QA-pass fixes (leader failover, save-time validation, automation reporting, RobotEvents API migration, hot-reload, S3 settings, security audit fixes).
- **TMTools rebrand** of user-visible text; AGPL-3.0 licence; separate `:backend`/`:frontend` images; Match Control absorbs Field Monitor.
- **Feature additions (issue/PR):** notifications & rooms (#5), WLED/OSC/PTZ (#7), desk↔iPad comms (#8), public site (#10), button pages (#11), Robot 360 overlays + clip recorder (#1/#12), late warnings & agenda announcements (#22), robot-name opt-in & spotlight (#24), visual button builder (#25), comms secure links/PIN/timers/presence/agenda tools (#27), site extras — leaderboards, recordings, logos, bus tracker (#28), Playwright e2e suite (#30).
- **2026-10-31:** Capture agent `tools/capture_agent/` (#45). `ConnectionManager.start()` now waits for its Redis subscription before returning (events published right after startup were lost); lifespan shutdown clears `app.state.leader`.
- **2026-10-31:** Analysis desk SPA page (`/analysis`, `api/capture.ts`, `lib/analysis.ts`, navbar entry gated on `analysis:view`).
- **2026-10-30:** Replay player backend (#41): `modules/capture/replay.py`, `static/replay.html` served at `GET /replay/{id}` (OBS browser source, transparent, `?label=1` caption), `/ws/replay/{id}`, `/api/v1/capture/replay/*`, `replay_command` event.
- **2026-10-30:** Capture backend (#42/#45 server side): `capture_recordings`/`capture_moments` tables, `modules/capture/`, `/api/v1/capture/*`, `/ws/agent`, `capture` integration, `capture:control`/`capture:upload`/`analysis:view` permissions, `matchStarted` enrichment.
- **2026-10-30:** Pit admin / lost & found per-team alerts (#44): `POST /notifications/team-alert`, `delivery.alert_teams`, "Alert teams" card on Rooms and Team detail pages.
- **2026-10-30:** `tm_update_plan.md` consolidated into this file (`source-of-truth.md`) and verified against the code. Same day: added planned work (§22.1) from the QE Comp 26 planning deck — overlay rework (#13), replay chain (#45/#42/#38/#41), Art-Net (#39), checklist (#43), pit alerts (#44) — and recorded that the scraper uses the VEX Events API (events.vex.com).
- **2026-10-30:** planned-work notes updated: capture agent `reencode`/`video_bitrate` settings, clip-priority rules, IQ storage sizing, recordings cleared between events, and the server described as a portable Docker stack (#45, #46).
- **2026-10-30:** Art-Net integration (`artnet`, #39) implemented.
- **2026-10-30:** Team checklist (#43) implemented: `team_profiles.inspection_status`, `GET /api/v1/teams/checklist`, admin Teams/TeamDetail UI, public team page card.
