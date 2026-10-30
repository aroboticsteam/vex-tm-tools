# CLAUDE.md

## Keep `source-of-truth.md` up to date

[`source-of-truth.md`](source-of-truth.md) is the single authoritative description of this codebase (architecture, data model, API, RBAC, integrations, frontend, deployment, tests).

**Every time you make a change to the codebase, update `source-of-truth.md` in the same change.** This includes, but is not limited to: routes and permissions, DB models and Alembic migrations, system settings keys, env vars, integrations and their services, automation triggers/actions, event types, WebSocket endpoints, frontend pages/routes, module boundaries, tests/CI, Docker/k8s config, and known gaps. Also bump the "Last verified" line and add a line to the change history. If you notice the file disagrees with the code, fix the file.
