# Frontend plan — BYO-LLM setup + static-deployable web app

Planning doc for the current frontend push. Companion to `HANDOFF.md`
(overall product state) and `DEPLOYMENT.md` (server runbook).

## Decisions locked

| Question | Decision |
|---|---|
| Where the recipe/menu/nutrition brain runs | **Self-hosted Python backend**, always-on, same operational model as the DGX Spark LLM box. Frontend is a static build that talks to it. No browser port of the engines. |
| Accounts | **Keep multi-tenant** — real logins, per-user server-side data (unchanged from `HANDOFF.md`). |
| LLM endpoint + key | **Entered in the browser** by each user, **stored server-side encrypted** (Fernet). Backend makes the actual LLM call (a browser off the tailnet can't reach a Tailscale LLM address anyway). Env `LLM_*` stays as the fallback for the CLI and the OpenWebUI shim. |
| Screen scope this round | **Settings / first-run LLM setup only.** Menu-planner and Macro-Kit screens are *not* in this round (their APIs partly exist — revisit later). |

## Deployment model (assumption — confirm)

The static build is served **same-origin** with the API, exactly as
`DEPLOYMENT.md` already specifies (one Caddy instance: `/api /auth /chat
/profile` → uvicorn, everything else → `frontend/dist/`). "Drop the files
over" = `npm run build` then rsync `dist/` to that box.

This keeps the current auth as-is: JWT in an httpOnly `SameSite=Lax` cookie,
no CORS, every `api.ts` call a relative path.

**If instead you want the frontend on a managed static host (Netlify /
Cloudflare Pages / Vercel / GitHub Pages) on a different origin than the
API**, that's a separate small follow-up, not this round:
- switch auth from cookie → `Authorization: Bearer <jwt>` in `localStorage`
  (a `SameSite=Lax` cookie is not sent on cross-site XHR)
- add `CORSMiddleware` to `backend/main.py` with an allow-list of origins
- SPA rewrite configs (`_redirects` / `vercel.json` / `404.html` copy)

This round adds `VITE_API_BASE` (default `''`) so the bundle can already be
pointed at another origin — the cookie is then the only remaining blocker.

---

## Work breakdown

### Phase A — Backend: per-user LLM config

**A1. Dependency**
- `pyproject.toml`: add `cryptography>=42` (for Fernet). Note the
  `link-mode = "copy"` caveat already in `[tool.uv]` — a plain `uv sync`
  after this is fine.

**A2. `aboyeur/crypto.py` (new)**
- `encrypt(plaintext: str) -> str` / `decrypt(token: str) -> str`, Fernet.
- Key resolution: `LLM_SETTINGS_KEY` env (a urlsafe-base64 32-byte Fernet
  key) if set; else derive one from `JWT_SECRET`
  (`urlsafe_b64encode(sha256(JWT_SECRET).digest())`) and log a warning the
  same way `auth.py` does for the insecure default. Deriving keeps it
  zero-config for dev; a dedicated key is recommended for the deploy so
  rotating `JWT_SECRET` doesn't orphan every stored LLM key.

**A3. `aboyeur/config.py`**
- Add `llm_api_key: str | None` to the `Config` dataclass.
- `Config.load()` reads `LLM_API_KEY` env (default `None`).

**A4. LLM call sites use the key**
- `aboyeur/llm.py` `make_client`: `api_key=config.llm_api_key or "unused"`.
- `aboyeur/engines/nutrition.py:357` (`_llm_parse_lines`): same change.
- These are the only two spots (`grep` confirmed). Everything downstream
  already threads a `Config` through, so a per-request `Config` override
  reaches ingredient parsing inside `plan_menu` / `estimate_nutrition` /
  `build_macro_kit` for free.

**A5. `aboyeur/db.py` — schema**
- Add, alongside `kits` (also a post-multi-tenant table, so `CREATE TABLE
  IF NOT EXISTS` in the static `SCHEMA` is enough — no `migrate.py` work,
  nothing to fold):
  ```sql
  CREATE TABLE IF NOT EXISTS llm_settings (
      user_id           INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
      base_url          TEXT NOT NULL,
      model             TEXT NOT NULL,
      api_key_encrypted TEXT NOT NULL,
      updated_at        TEXT NOT NULL
  );
  ```

**A6. `aboyeur/llm_settings.py` (new)**
- `get_llm_settings(conn, user_id) -> dict | None` — decrypts, returns
  `{base_url, model, api_key}`; `None` if no row.
- `get_llm_settings_public(conn, user_id) -> dict` — `{base_url, model,
  configured: bool}`, **never** the key.
- `save_llm_settings(conn, user_id, base_url, model, api_key)` — encrypt +
  upsert. If `api_key` is blank/omitted and a row exists, keep the stored
  key (lets the user edit base_url/model without re-pasting the key).

**A7. `backend/main.py`**
- `_effective_config(conn, user_id) -> Config | None`:
  - row present → `dataclasses.replace(_aboyeur_config, llm_base_url=...,
    llm_model=..., llm_api_key=...)`
  - no row, user is `_SEED_USER_ID` → base `_aboyeur_config` (env)
  - no row, real user → `None` (signals "unconfigured")
- `_get_session_agent` / `_reset_session_agent`: build the agent with the
  effective config; if `None`, don't build — `/chat` streams one message:
  *"Set up your AI model in Settings before chatting."* then `[DONE]`.
- `oa_chat_completions` (OpenWebUI shim): unchanged — always seed user,
  always env config.
- New routes (all `Depends(get_current_user)`, own `_profile_conn()`):
  - `GET  /api/llm-settings` → `get_llm_settings_public(...)`
  - `PUT  /api/llm-settings` → body `{base_url, model, api_key?}` →
    `save_llm_settings(...)` → returns the public shape. Also **resets the
    user's session agent** so the next turn picks up the change.
  - `POST /api/llm-settings/test` → body `{base_url, model, api_key?}` (or
    empty to test the stored one) → build a client, `GET {base_url}/models`
    with a short timeout, or a 1-token completion as a fallback probe →
    `{ok: bool, detail: str}`. Never echoes the key.

**A8. Tests — `tests/test_llm_settings.py`**
- Fernet round-trip; key-derivation path.
- `save` then `get` returns the plaintext key; `get_llm_settings_public`
  never does.
- blank-key edit keeps the stored key.
- `_effective_config`: override applied; seed user falls back to env; real
  user with no row → `None`.
- `/chat` for an unconfigured real user streams the setup nudge, not a 500.

### Phase B — Frontend: Settings screen + first-run gate

**B1. `src/api.ts`**
- `API_BASE = import.meta.env.VITE_API_BASE ?? ''`; prefix every path
  (including `streamChat`'s `/chat`).
- `LlmSettings` type `{ base_url: string; model: string; configured: boolean }`.
- `getLlmSettings()`, `saveLlmSettings({base_url, model, api_key?})`,
  `testLlmConnection(body)`.

**B2. `src/routes/Settings.tsx` + `Settings.css` (new)**
- Fields: **Provider base URL** (`https://…/v1`), **Model**, **API key**
  (password input, show/hide, placeholder "•••••••• (leave blank to keep
  current)" when already `configured`).
- **Test connection** button → spinner → green/red result line.
- **Save** → success toast; on first-run, then navigates to `/app/chat`.
- Prefill `base_url` / `model` from `getLlmSettings()`.
- Reuses existing form primitives (`.text-input`, `.btn-primary`,
  `.btn-ghost`) and `tokens.css`.

**B3. LLM-status in `AuthContext` (or a small `useLlmStatus` hook)**
- On login / mount, fetch `getLlmSettings()`, expose `llmConfigured: boolean`
  and a `refreshLlmStatus()`.

**B4. `src/App.tsx` — routing**
- New route `/setup` → `<Settings firstRun />` (no app chrome).
- `/app/settings` → `<Settings />` (inside `AppShell`).
- `RequireAuth` / post-login redirect: `userId && !llmConfigured` → `/setup`.
  Order: **login → onboarding (profile) → `/setup` (LLM) → `/app/chat`**,
  but `/setup` is also reachable/repeatable any time and doesn't block
  onboarding.

**B5. `src/routes/AppShell.tsx`**
- Add a gear icon (next to the profile avatar) → `/app/settings`.

**B6. `src/routes/Chat.tsx`**
- If `!llmConfigured`: swap the composer for a "Set up your AI model to
  start chatting → Settings" CTA.
- Keep the existing mid-stream error fallback; the backend's "configure
  your model" message renders fine as a normal assistant bubble.

### Phase C — Local dev + docs

**C1. `frontend/README.md`** — replace the Vite template boilerplate with the
real workflow:
```bash
# terminal 1 — backend
uv sync
uv run uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload \
  --reload-dir aboyeur --reload-dir backend
# terminal 2 — frontend
cd frontend && npm install && npm run dev   # http://localhost:5173
```
Note `VITE_API_BASE` is empty for local dev (Vite proxy handles it).

**C2. `.env.example`** — add:
```
# Optional: default LLM for the CLI and any user who hasn't set their own
LLM_API_KEY=
# Encrypts per-user stored LLM keys. Generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
LLM_SETTINGS_KEY=
```

**C3. `DEPLOYMENT.md`** — add `LLM_SETTINGS_KEY` to the VM `.env` block and
its generate command; one line noting per-user LLM config now exists and
`LLM_MODEL` env is just the fallback.

**C4. Verify** — `npm run build`; serve `dist/` (or via Caddy); log in as a
fresh test user; hit `/setup`; point at the self-hosted LiteLLM
(`http://100.70.6.1:4000/v1`, real key if the proxy needs one); Test → Save
→ send a chat message end-to-end. Re-run `frontend/e2e-smoke.mjs` after
editing its base URL.

---

## Files touched

**New:** `aboyeur/crypto.py`, `aboyeur/llm_settings.py`,
`tests/test_llm_settings.py`, `frontend/src/routes/Settings.tsx`,
`frontend/src/routes/Settings.css`.

**Changed:** `pyproject.toml`, `aboyeur/config.py`, `aboyeur/llm.py`,
`aboyeur/engines/nutrition.py`, `aboyeur/db.py`, `backend/main.py`,
`frontend/src/api.ts`, `frontend/src/App.tsx`,
`frontend/src/auth/AuthContext.tsx`, `frontend/src/routes/AppShell.tsx`,
`frontend/src/routes/Chat.tsx`, `frontend/README.md`, `.env.example`,
`DEPLOYMENT.md`.

**Untouched:** all `aboyeur/tools/*` and `aboyeur/engines/*` except the one
`nutrition.py` line; the whole `backend/` June-POC dead-code path;
`Onboarding`, `Library` screens.

## Sequencing

1. **A1–A4** (deps, crypto, config, call sites) — no behavior change yet,
   `api_key or "unused"` is a no-op against the current self-hosted LLM.
2. **A5–A6** (table, module) + **A8** tests.
3. **A7** (routes + effective config + chat gate).
4. **B1–B6** frontend.
5. **C** docs + end-to-end verify.

Phases 1–2 are independently shippable and low-risk; the visible change
starts at 3.

## Risks / watch-items

- **Cross-origin cookie** — covered above. If the "hosting site" isn't the
  same origin as the API, the bearer-token follow-up is required before
  login works there.
- **`test` endpoint as SSRF surface** — an authenticated user can make the
  backend GET an arbitrary `base_url`. Acceptable for an invite-only
  self-hosted app; if it ever matters, restrict to `https?://` + block
  link-local/metadata IPs.
- **Key at rest** — Fernet-encrypted, but the decrypt key sits in the same
  `.env` as everything else. This raises the bar (no plaintext keys in a DB
  dump) without pretending to be an HSM. Documented as such.
- **`LLM_SETTINGS_KEY` rotation** orphans stored keys — users re-enter. Note
  it in `DEPLOYMENT.md`.
- **Latency** — `HANDOFF.md` already flags 30–120 s menu/shopping calls; a
  per-user LLM that's slow or down now surfaces as a chat error rather than
  a server-wide config problem, which is an improvement, but the Chat
  screen still has no real progress affordance beyond typing dots.

## Explicitly out of scope this round

- Menu-planner screen (API `/api/menu/plan` exists, unused).
- Macro-Kit screen (needs new REST routes over `build_macro_kit`).
- Profile/targets dashboard.
- Cross-origin static hosting (bearer auth + CORS + rewrite configs).
- Any browser-side port of the engines.
