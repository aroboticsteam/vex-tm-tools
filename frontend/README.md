# QEComp frontend

React 18 + Vite 5 + TypeScript (strict) admin SPA for QEComp, per
[`source-of-truth.md`](../source-of-truth.md) §15/§17/§21.
Talks to the FastAPI backend in [`../backend/`](../backend/).

## Stack

- **Routing:** React Router v6 (`createBrowserRouter`)
- **Server state:** TanStack Query v5 — all REST calls go through hooks in `src/api/`, never raw `fetch` in a page/component
- **Client/live state:** Zustand — `src/stores/auth.ts`, `ws.ts`, `ui.ts`
- **Realtime:** a custom `useWebSocket` hook (`src/hooks/`) with exponential-backoff reconnect, feeding the `ws` store
- **Styling:** Tailwind CSS with the vmd1.dev design tokens (`src/index.css`, `tailwind.config.js`)

REST-fetched data and WebSocket-pushed data are kept in separate stores by
design (plan §C.8) — a WS event may only trigger a TanStack Query
`invalidateQueries` (via `useCacheSync`, mounted once at the app root),
never write directly into the query cache, and a component never merges
the two into one object.

`/prompter/<entity_id>` and `/overlay/<entity_id>` are deliberately **not**
part of this SPA (plan Appendix B.3) — they're standalone HTML pages
served by the backend (`backend/static/`).

## Development

```bash
npm install
npm run dev
```

The dev server proxies `/api`, `/auth`, `/admin_login` (POST only), `/ws`,
`/prompter`, and `/overlay` to the backend (default
`http://localhost:8000`, override with `VITE_API_BASE_URL`) — see
`vite.config.ts`. Everything else is this SPA, matching the single-origin
design the production deployment uses (an ingress splitting by path
instead of Vite's dev proxy).

To exercise the app behind a reverse proxy at a real-looking hostname
(`https://vex.localhost`) with real OIDC login instead of the emergency
admin form, see [`../local-testing/README.md`](../local-testing/README.md).

## Building

```bash
npm run build   # tsc -b && vite build
```

## Project layout

| Path | Owns |
|---|---|
| `src/api/` | TanStack Query hooks — one file per backend resource |
| `src/stores/` | Zustand stores (no imports from `api/`/`components/`/`pages/`) |
| `src/hooks/` | Reusable hooks (`useWebSocket`, `usePermission`, `useCacheSync`, ...) |
| `src/components/` | Reusable UI, grouped by domain (`ui/`, `layout/`, `integrations/`, `automations/`, `teams/`, ...) |
| `src/pages/` | One file per route — layout composition only, no raw fetches |
