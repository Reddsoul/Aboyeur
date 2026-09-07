# Aboyeur frontend

React + TypeScript + Vite SPA over `backend/main.py`. Built to `dist/` and
served same-origin with the API (one Caddy — see `../DEPLOYMENT.md`), so the
auth cookie and every relative `api.ts` path work with no CORS config.

## Local dev

```bash
# terminal 1 — backend
cd ..
uv sync
uv run uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload \
  --reload-dir aboyeur --reload-dir backend

# terminal 2 — frontend
npm install
npm run dev        # http://localhost:5173
```

Vite proxies `/auth`, `/profile`, `/api` and `/chat` to `localhost:8000`
(`vite.config.ts`), so the browser only ever sees one origin.

Accounts are invite-only — create one with `uv run aboyeur create-user
you@example.com` before signing in.

## Scripts

| Command | What it does |
|---|---|
| `npm run dev` | Vite dev server + HMR on :5173 |
| `npm run build` | `tsc -b` typecheck, then a production build into `dist/` |
| `npm run preview` | Serve the built `dist/` locally |
| `npm run lint` | oxlint |

Smoke tests (`e2e-smoke.mjs`, `e2e-onboarding.mjs`) drive a real browser via
Playwright — edit the base URL at the top of each before running
`node e2e-smoke.mjs`.

## Environment

| Var | Default | Purpose |
|---|---|---|
| `VITE_API_BASE` | `''` (same origin) | Prefix for every API call. Leave empty for local dev and for the same-origin deploy. |

Pointing `VITE_API_BASE` at another origin is not enough on its own to host
the frontend elsewhere: the session cookie is `SameSite=Lax`, so a
cross-origin deploy also needs bearer-token auth and CORS on the backend (see
`../FRONTEND_PLAN.md`).

## Brand

The mark (a penguin in a toque) lives in `public/`: `logo.png` (full figure),
`logo-mark.png` (head crop — below ~44px the whole bird turns to mush), plus
`favicon.png` and `apple-touch-icon.png`. `src/components/Logo.tsx` picks the
right file for the requested size and sits it on an opaque white plate, since
the art is black line work whose white areas are part of the drawing.

Regenerate them from `design/logo/aboyeur-mark-source.jpeg` if the source
changes — the derived files are checked in, there's no build step for them.

## Screens

- `/login` — invite-only sign-in.
- `/onboarding` — first-run only: builds your first client, then a short tour.
  `?tour=1` replays just the tour.
- `/setup` — first-run LLM connection (endpoint, model, key).
- `/app/clients` — the roster: search, add, archive, per-client summary.
- `/app/clients/:id` — one client's workspace: **Profile** (household,
  allergies, targets), **Plans** (their menus and kits), **Details** (contact,
  notes, archive/delete).
- `/app/chat` — the agent, streamed over SSE from `POST /chat`, scoped to the
  client selected in the header.
- `/app/library` — recipe catalog browse/search + favorites (account-level).
- `/app/settings` — LLM settings (account-level).

### Clients and scope

An account is a practitioner; the people they plan for are clients. The header
switcher decides who chat, the profile editor and every plan apply to —
`ClientContext` holds it and persists it per browser. Nothing is shared
between clients: the server keys profile/pantry/menus/kits on `client_id` and
hands out a separate chat agent per client, so switching clients starts a
clean conversation rather than carrying one plan into the next.

### LLM settings

Each account brings its own OpenAI-compatible endpoint. The key is posted to
the backend, encrypted at rest (Fernet), and never sent back to the browser —
the API only ever returns `{base_url, model, configured}`. The backend makes
the actual model call, which is what lets a private endpoint on your own
tailnet work from a browser that can't reach it.
