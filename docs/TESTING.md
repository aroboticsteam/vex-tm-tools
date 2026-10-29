# Testing

| Layer | Where | Run |
|---|---|---|
| Backend (pytest, real Postgres/Redis/MinIO/ffmpeg) | `backend/tests` | `pytest backend` (env vars as in `.github/workflows/tests.yml`) |
| Frontend unit/component (Vitest + Testing Library) | `frontend/src/**/*.test.ts(x)` | `cd frontend && npm test` |
| End to end (Playwright, real browser + backend) | `frontend/e2e` | see below |

CI runs all three (`backend-tests`, `frontend-build`, `e2e`).

## End-to-end tests

They drive a real backend that serves the built SPA, with a synthetic 60 fps camera for the recorder.
Covered: sign-in, button pages (visual builder, confirm, feedback, drag-and-drop), comms desk ↔ station iPad
(messages, presence, timers and colour cues, secured link + PIN, agenda DnD/import), recorder, the public site and
its admin tools (config form, map upload, recordings, team logo/website, leaderboard rotation), overlay spotlight.

```bash
# 1. backend env (same variables as CI) and a migrated database
alembic upgrade head
python tools/e2e_seed.py                      # fixture teams + a cached TM schedule (python tools/e2e_seed.py --cleanup to remove)

# 2. build the SPA into the backend and start it
(cd frontend && npm run build) && rm -rf backend/static/frontend && cp -R frontend/dist backend/static/frontend
python -m uvicorn backend.main:app --port 8765

# 3. run
cd frontend && npx playwright install chromium   # once
E2E_BASE_URL=http://localhost:8765 npm run e2e
```

Notes: use a Redis database of your own (e.g. `redis://localhost:6380/5`) if another stack shares the instance, otherwise
your server won't become the cluster leader and button presses are proxied elsewhere. `E2E_CHANNEL=chrome` uses an installed
Chrome instead of downloading Chromium. Tests create their own data (names start with `e2e`/`E2E`) and clean up after themselves.
