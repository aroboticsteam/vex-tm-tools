# QEComp backend

FastAPI application implementing the design in
[`../source-of-truth.md`](../source-of-truth.md). Single ASGI app, run with
exactly one Uvicorn worker per pod (source-of-truth §2) — leader election
(`modules/leader.py`) assumes one process per pod, not one lock per worker.

## Layout

| Path | Owns |
|---|---|
| `core/` | DB/Redis clients, settings, Fernet encryption, RBAC (`dependencies.py`), sessions. May only import stdlib/third-party — nothing else in `backend/` imports it upward. |
| `models/` | SQLAlchemy ORM tables |
| `schemas/` | Pydantic request/response models, including the frozen `EventBusMessage` (`schemas/events.py`) every event on `qecomp:events` uses |
| `loader.py` | Scans `modules/integrations/<domain>/` for integration clients, hot-reloads them from Postgres on `config_change` pub/sub |
| `modules/integrations/{vex_tm,spotify,atem,zeros,obs}/` | One client per integration domain, each implementing the `Integration` ABC (`modules/integrations/base.py`) |
| `modules/automation/engine.py` | YAML + Jinja2 (sandboxed) automation engine: trigger matching, tag-based targeting, `service`/`script`/`delay`/`condition`/`repeat` actions |
| `modules/timer/manager.py` | Timer-Teleprompter countdown/milestone state machine |
| `modules/scraper/`, `modules/predictor/`, `modules/media/` | Team-profile scraper, OPR match predictor, FFmpeg green-screen pipeline + S3 upload |
| `routers/` | Thin FastAPI routers — validate input, call a module, return a response; no business logic here |
| `static/` | Standalone (non-SPA) pages: `prompter.html`, `overlay.html` |
| `main.py` | App factory + lifespan: the *only* file that instantiates the leader-only managers (Loader, AutomationEngine, TimerManager, Scraper, Predictor) and wires them to `on_promoted`/`on_demoted` |

Cross-module communication goes through the `qecomp:events` Redis pub/sub
channel or the Loader's public API (`get_instance`, `get_instances_by_tag`,
...) — never direct imports between `modules/integrations/*`,
`modules/automation/`, `modules/timer/`, etc. See plan Appendix C for the
full boundary rules.

## Setup

```bash
python3 -m venv .venv-backend && source .venv-backend/bin/activate
pip install -r requirements.txt

# from the repo root:
docker compose up -d postgres redis minio
alembic upgrade head
```

Required env vars (see `.env.example` at the repo root): `POSTGRES_DSN`,
`REDIS_URL`, `SECRET_KEY`, `ENCRYPTION_KEY`, `ADMIN_LOCAL_PASSWORD`. OIDC
(`OIDC_ISSUER_URL`/`OIDC_CLIENT_ID`/`OIDC_CLIENT_SECRET`) and S3
(`S3_ENDPOINT_URL`/`S3_BUCKET`/`S3_ACCESS_KEY`/`S3_SECRET_KEY`) are only
needed for OIDC login and video upload respectively — everything else
works without them.

```bash
uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

## Tests

```bash
pytest --cov=backend --cov-report=term-missing
```

Most tests need real Postgres/Redis (`docker compose up -d postgres
redis`); the media pipeline tests additionally need MinIO
(`docker compose up -d minio`) and a local `ffmpeg` binary. Tests that
can't reach Postgres skip themselves rather than failing.
