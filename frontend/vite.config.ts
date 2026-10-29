/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
//
// The backend (FastAPI/uvicorn) runs on http://localhost:8000 in dev
// (plan Appendix B.1). We proxy /api, /auth, /admin_login, /ws, and
// /prompter under the Vite dev server's own origin so the browser never
// needs CORS and cookies set by the backend are same-site. In production
// the built SPA is served by the same origin as the backend (Traefik
// IngressRoute per Appendix A.9), so no proxy/base-URL config is needed
// there either — VITE_API_BASE_URL only ever needs to be set for a dev
// server pointed at a non-default backend port/host.
const backendTarget = process.env.VITE_API_BASE_URL || 'http://localhost:8000'

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    css: false,
    include: ['src/**/*.test.{ts,tsx}'],
  },
  server: {
    // Allows a local Traefik reverse proxy (local-testing/docker-compose.proxy.yml)
    // fronting this dev server at https://vex.localhost to forward requests
    // here — Vite 5+ rejects unrecognized Host headers by default.
    host: true,
    allowedHosts: ['vex.localhost', '.vex.localhost'],
    proxy: {
      '/api': { target: backendTarget, changeOrigin: true },
      '/healthz': { target: backendTarget, changeOrigin: true },
      '/readyz': { target: backendTarget, changeOrigin: true },
      '/auth': { target: backendTarget, changeOrigin: true },
      // GET /admin_login is intentionally NOT proxied: the backend serves
      // its own plain-HTML fallback page at that path (backend/routers/auth.py),
      // but this SPA has its own React route at /admin_login (pages/AdminLogin.tsx)
      // that should render instead when a browser navigates there directly.
      // Only the POST (the actual credential submission) needs to reach the
      // backend, so `bypass` skips the proxy for anything else and lets
      // Vite fall through to the SPA's index.html/client-side routing.
      '/admin_login': {
        target: backendTarget,
        changeOrigin: true,
        bypass: (req) => (req.method === 'POST' ? undefined : req.url),
      },
      '/prompter': { target: backendTarget, changeOrigin: true },
      // Trailing slash matters: this must match only the public OBS
      // browser-source route `/overlay/{entity_id}` (backend/routers/overlays.py),
      // not the admin SPA route `/overlays` (pages/Overlays.tsx) — a bare
      // '/overlay' prefix matches both (Vite's proxy does string-prefix
      // matching), which sends a hard navigation to /overlays into the
      // backend's baked-in production index.html instead of this dev
      // server's own SPA shell, producing a silent blank page.
      '/overlay/': { target: backendTarget, changeOrigin: true },
      // Public room display + phone subscribe pages (backend/routers/public_pages.py).
      // `/room/` has a trailing slash for the same reason as `/overlay/` above
      // (the admin SPA page is `/rooms`).
      '/room/': { target: backendTarget, changeOrigin: true },
      '/notify': { target: backendTarget, changeOrigin: true },
      '/sw.js': { target: backendTarget, changeOrigin: true },
      '/ws': { target: backendTarget, changeOrigin: true, ws: true },
    },
  },
})
