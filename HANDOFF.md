# Aboyeur — handoff

What Aboyeur is: a conversational nutrition/menu-planning app over a
personal 75k-recipe cookbook library, with a native-tool-calling LLM
agent (local model via Tailscale) doing recipe search, menu planning, and
shopping lists. This doc is the state of the "turn it into a real,
multi-tenant, investor-demoable product" effort — what's done, what's not,
and the non-obvious things that'll save you time.

## Status

- [x] **Multi-tenant backend** — real accounts, auth, every household table
      (profile/pantry/menus/favorites) scoped per user. Tested with real
      isolation checks, not just reasoning.
- [x] **Real React frontend** — login, 9-step onboarding, recipe catalog,
      streaming chat. Driven through an actual headless browser against the
      real backend, not just built and assumed working.
- [x] **189 backend tests passing** (`uv run pytest tests/ -q` — needs
      `uv sync --extra dev` first for `pytest` itself).
- [ ] **Deployed anywhere.** Everything above runs locally only. Full
      runbook is `DEPLOYMENT.md` — needs Jose's cloud/domain/Tailscale
      access to execute, so it's written but not run.

## Run it locally, right now

Both dev servers are already up on this machine as of this writing:

```bash
# Backend (FastAPI + the real agent) — already running via:
cd /Volumes/General/Coding/Python/Aboyeur
uv run uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload --reload-dir aboyeur --reload-dir backend
# -> http://localhost:8000

# Frontend (Vite dev server, proxies /api /auth /profile /chat to :8000):
cd frontend && npm run dev
# -> http://localhost:5173
```

Log in at `localhost:5173/login` with:

| account | password | notes |
|---|---|---|
| `investor1@example.com` | `testpass123` | test account created this session — **delete or repassword before anything real**, see below |
| `thepartycaravan@gmail.com` | *(none set)* | this is Jose's real household (the original single-profile data, migrated). No password — only reachable via the CLI (`uv run aboyeur chat`), not the web login, until someone runs `aboyeur create-user` for it. |

Create more accounts: `uv run aboyeur create-user <email>` (prompts for a
password, hidden input). Invite-only by design — there's no signup page.

## The one thing that will save you the most time

**`backend/` is two systems wearing one file.** `backend/main.py` has live
routes wired to the real `aboyeur/` engine (`/chat`, `/v1/chat/completions`,
`/profile/*`, and the new `/auth/*` + `/api/*` from this session) sitting
right next to dead code from an abandoned June proof-of-concept
(`backend/models.py`'s SQLAlchemy `Book`/`Recipe`/`PantryItem` tables,
`menu_engine.py`, `macro_engine.py`, `preference_engine.py`, `rag.py`,
`fusion_engine.py`, `cookbook_mcp.py` — never populated, and `models.py`'s
`Recipe` table collides by name with the real one, so anything that
imports it 500s). **Jose asked to leave the dead code untouched** rather
than delete it (maybe it's wanted for something later — ask him, don't
assume it's safe to remove). Point is: if you're extending the backend,
check which category a route/file falls into before building on it —
`backend/main.py`'s own comments mark the boundary pretty clearly around
the `/chat` section.

The real database lives at `~/.local/share/aboyeur/aboyeur.db` (set via
`.env`'s `DB_PATH`), **not** the repo's `data/aboyeur.db` — that's a stale
leftover copy on the NAS mount (slow SMB access is why `DB_PATH` was moved
off it in the first place; see the comment in `aboyeur/config.py`).

## What actually changed this session

**Schema** (`aboyeur/db.py`, new `aboyeur/migrate.py`): added `users` and
`favorites` tables; `profile`/`pantry`/`menus` gained a `user_id` column
(profile was previously a single hardcoded row — `CHECK (id = 1)`).
`aboyeur/migrate.py` is an idempotent one-time migration that folds the
existing single household under a seed user
(`thepartycaravan@gmail.com`) — already run against the real DB, safe to
re-run (no-ops if already migrated).

**Auth** (new `aboyeur/auth.py`): bcrypt + JWT-in-httpOnly-cookie. Started
with `passlib`, swapped to plain `bcrypt` mid-session after discovering
`passlib`'s bcrypt backend is broken against current `bcrypt` releases
(a real, current, widely-hit incompatibility — don't reach for `passlib`
here again).

**Every tool that touches profile/pantry/menus now takes `user_id`**
(`aboyeur/tools/profile.py`, `pantry.py`, `menu.py`, `shopping.py`,
`aboyeur/agent.py`). Two real cross-tenant bugs were found and fixed doing
this — both were "omit an id, get the most recent row" patterns with no
owner filter, which is exactly the shape that silently leaks between
accounts:
- `menu.py`'s `_load_menu(menu_id=None)`
- `shopping.py`'s `get_shopping_list` had its *own* separate copy of the
  same unscoped query

Also removed a module-global (`aboyeur/tools/profile.py`'s old
`_guest_profile`) that relied on the backend serializing every turn behind
one lock to avoid leaking guest sessions across users — fragile the moment
real concurrency exists. Guest profiles are now passed explicitly through
the call chain instead.

**New REST API** (`backend/main.py`): `/auth/login|logout|me`,
`/api/recipes` (search+browse+paginate — `aboyeur/tools/search.py` gained
a `browse_recipes` for this, since the LLM-facing `search_recipes` has a
hard 25-result cap and no empty-query browse mode), `/api/recipes/{id}`,
favorite toggle (new `aboyeur/tools/favorites.py`), `/api/menu/plan`,
`/api/menu/{id}/shopping-list`. `/chat` now keys off the authenticated
user instead of a client-supplied `session_id` (that was never
authenticated — anyone could steer another session by guessing its id).

**A deadlock I introduced and fixed, live**: first pass wrapped a lock
around the *entire* tool-dispatch call, which meant `plan_menu`'s real
network calls (USDA lookups, LLM ingredient parsing) held the lock too —
one slow request froze the whole server for every user. Real fix: stopped
sharing one `sqlite3.Connection` across threads at all — each agent/request
gets its own connection now (`backend/main.py`'s `_get_session_agent` /
`_reset_session_agent`, the CLI's `cmd_chat`). WAL mode is built for
exactly that; no cross-request lock needed once connections aren't shared.

**SSE framing bug** (`backend/main.py`'s `_sse` helper): didn't re-prefix
embedded newlines per the SSE spec, so any multi-line streamed reply
(routine — markdown lists, paragraph breaks) would silently truncate at
the first line break for a spec-compliant client. This was never caught
before because the only prior consumer was the CLI, which talks to the
`Agent` object directly and never parses SSE at all. Fixed on both the
backend (`_sse`) and the new frontend's parser (`src/api.ts`'s
`streamChat`).

**Frontend** (new `frontend/`, React + TypeScript + Vite): ports the
onboarding/library/chat screens from the earlier design-canvas mockup
(`design/aboyeur-app/*.dc.html`) onto the real API. Three things from the
mockup were deliberately **not** carried over because the real data
doesn't support them (they'd have been dishonest UI — filters that always
return nothing):
- Diet-mode / "Quick (≤30 min)" smart filters — no real diet-tag or
  prep-time field exists in the corpus.
- "Allergy-safe" toggle — redundant, since every `/api/recipes` query
  already hard-excludes the caller's stored allergies automatically.
- "New Recipe" creation modal — no backend support exists for writing into
  the corpus; faking persistence would be worse than not having the
  feature.

Two more real bugs the frontend build caught (via an actual headless
Playwright run against the real backend, not just code review):
- Same nested-`<button>`-inside-`<button>` mistake from the original
  design mockup recurred in the real Library's list view — invalid HTML5,
  the browser silently reparents the DOM. Fixed the same way as before
  (outer element becomes a `<div role="button">`).
- `/api/recipes/{id}` only returned what the LLM-facing `get_recipe` tool
  gives (`id`, `title`, `path`, `servings`, `markdown`) — the detail panel
  expected `book`/`cuisine`/`tags`/`ingredients` too (same shape as the
  catalog grid) and crashed on load. Backend route now enriches the
  response with those fields directly from the `recipes` table.

**Also**: `uv sync` silently uninstalled `fastapi`/`uvicorn`/`sqlalchemy`
mid-session (they'd only ever been installed ad hoc, never declared in
`pyproject.toml`) and crashed the running dev server. Fixed by declaring
them properly as dependencies — if you ever see the backend mysteriously
missing packages after a `uv sync`, this is why, and it shouldn't happen
again now that they're declared.

## Known issues / things to watch

- **Chat/menu-planning latency is real and sometimes slow** — observed
  30–120+ seconds for `get_shopping_list` under heavy test load, because
  ingredient parsing/USDA lookups are live network calls against a single
  home LLM server. Not a bug, just a real UX consideration: the frontend
  has no loading-state affordance for this beyond the typing-dots
  indicator, which will look stuck on a slow request. Worth a spinner /
  timeout message if this becomes a demo pain point.
- **The DGX Spark is a single home machine.** If it's off, or home
  internet is down, chat stops working regardless of cloud deployment
  health. `tailscale ping 100.70.6.1` is the fast sanity check.
- **SQLite at POC scale is fine**, but every new backend route should keep
  using the short-lived-connection-per-request pattern (`_profile_conn()`
  in `backend/main.py`) rather than reintroducing a shared connection —
  that's exactly the pattern whose absence caused the deadlock above.
- Two throwaway Playwright smoke scripts are sitting in `frontend/`
  (`e2e-smoke.mjs`, `e2e-onboarding.mjs`) — genuinely useful for
  re-verifying the login→chat→library and onboarding→save flows after
  future changes, kept intentionally rather than deleted. Not part of any
  build step.

## What's next

1. **Deploy it** — `DEPLOYMENT.md` has the full runbook (VM, Tailscale
   join, corpus/DB transfer, systemd, Caddy). Needs Jose to actually drive
   it (cloud account, domain, Tailscale admin access).
2. Before showing anyone real: delete or repassword the
   `investor1@example.com` test account (`DEPLOYMENT.md` has the exact
   command), and generate a fresh `JWT_SECRET` for whatever environment
   ends up production (a dev one already exists in the local `.env` —
   don't reuse it).
3. Nothing in the approved plan is left undone besides deployment itself —
   Phase 3 (integration) was verified live, not just claimed, during this
   session.

## Macro Kits (added this session)

A **Macro Kit** is a second deliverable alongside menus: a mix-and-match
grocery template. Food columns (protein / dairy protein / carb base / volume
veg / fruit / fat / sauce) plus a per-meal gram rule, so someone assembles a
bowl and lands on their macros without following a recipe or counting.
Modelled on the Built With Science "30-Day Filling Diet" (the PDF in the repo
root), generalized to any macro target. Driving request: *"I'm a runner and I
want a combo of groceries I can mix and match and hit my macros."*

Phase 1 is engine + tool + tests. No frontend.

**A kit is not a menu, and nothing is shared between them.** Separate tool
(`build_macro_kit` vs `plan_menu`), separate table (`kits` vs `menus`),
separate engine (`engines/kit.py` vs `engines/optimizer.py`). They reuse only
the USDA lookup, the allergen expander, `optimizer.macro_fit`'s scoring
shape, and the grocery-section categorizer. Don't unify them later without a
reason better than "they look similar" — a menu is recipes on days, a kit is
foods with a rule.

**New/changed files**

- `aboyeur/data/components.py` (new) — the hand-curated library, ~90 whole
  foods with role, USDA query, serving size, a gram band, diet tags, and an
  optional per-100 g `macros_override`. This is the file to grow; everything
  else is plumbing.
- `aboyeur/engines/kit.py` (new) — roster → closed-form portion solve →
  emit. No scipy, no numpy, no optimizer.
- `aboyeur/engines/nutrition.py` — added `resolve_macro_targets(member,
  daily_kcal, strategy)` (balanced / endurance / cut) and
  `validated_per_100g()`. `per_meal_target` / `household_targets` are
  **unchanged** — the menu path reads those and must keep its 30/40/30 split.
- `aboyeur/tools/kit.py` (new) — `build_macro_kit`, plus the shared
  user-scoped `load_kit`.
- `aboyeur/tools/shopping.py` — `get_shopping_list` now also builds a kit's
  list (`kit_id`, or a kit id passed as `menu_id`, or falling back to the
  most recent kit when there's no menu). The menu path is untouched.
- `aboyeur/tools/profile.py` — `set_macro_strategy`; `get_profile` gains a
  per-member `strategy_daily_macros` **only** when a strategy is set.
- `aboyeur/db.py` + `aboyeur/migrate.py` — the `kits` table and
  `idx_kits_user`. `aboyeur chat` runs `init_db` + `migrate` itself, so the
  table appears on next run; nothing to do by hand.
- `aboyeur/prompts.py`, `aboyeur/tools/schemas.py`, `aboyeur/agent.py` — the
  kit branch, the tool schema, the dispatch.

**Things that will save you time**

- **The shared USDA lookup destroys precise queries.** `lookup_ingredient_macros`
  runs `clean_ingredient_name` first, which strips descriptors — so
  `"ground beef 93% lean raw"` is sent to USDA as **`"beef"`**, and
  `"quinoa uncooked"` as `"quinoa"` (which ranks *cooked* quinoa first). That
  is why eleven components carry a `macros_override`: whole eggs (search
  returns the egg-WHITE row, 0 g fat), lean ground beef, sweet potato,
  quinoa, sourdough bread, olives (search matched *bananas* on the shared
  word "ripe", then preferred it for containing "raw"), orange (returns
  orange peel), apple, peanut butter (returns the reduced-fat product),
  whole wheat pasta and bulgur (both collapsed onto other rows). Each
  override's comment names the USDA row it was transcribed from. **If you add
  a component, verify what its query actually resolves to** — every one of
  the above looked fine until it was printed side by side with the real
  value. Overrides are sanity-gated by `validated_per_100g`, and there's a
  test asserting every one of them passes.
- **Gram bands are load-bearing, not decoration.** The solver clamps into the
  band, so a flattering `min_g` turns a low-carb target into a carbohydrate
  overshoot paid for out of protein. A 40 g floor on dry rice produced
  exactly that; 20 g fixed it. When a solve lands under a carb base's
  minimum, both answers (drop it, or the minimum serving) are worked out in
  full and the closer one wins — weighted carb-gives-way-first, the same
  priority `resolve_macro_targets` uses.
- **`status` is calorie-only** (same vocabulary as the menu path's
  `daily_totals`). Calories can land dead on while a macro is badly off, so
  bowls and daily totals also carry **`macros_on_target`**, plus `residual`
  and `blocked_by` (which component's band stopped the solve). The system
  prompt tells the model to surface all of it. Nothing here silently claims
  a target was hit.
- **`load_kit` is public and shared** with `tools/shopping.py` on purpose.
  The two cross-tenant leaks this codebase has already shipped were both
  "omit an id, get the most recent row, no owner filter" — and the second was
  a *second copy* of the same query living in `shopping.py`. There are tests
  for both branches.
- **The kit shopping list rotates columns**, and two columns don't use the
  assembly rule's per-meal grams: `dairy_protein` shares the protein slot but
  at its own serving size (170 g of yogurt is not 170 g of chicken, so the
  protein column's share shrinks to make room), and `sauce` is bought at its
  own serving size every meal. Items carry `meals_covered` instead of the
  menu list's `used_in_recipes`.
- **The BWS PDF is not indexed and must not be.** `nutrition_reference` is
  public-domain 1800s-1920s text and the prompt flags its hits as dated; a
  2024 copyrighted PDF breaks both. It was used as a curation seed, and its
  five bowls are the fixtures in `test_cut_kit_bowls_land_in_the_bws_envelope`.
- Without a `USDA_API_KEY` every component resolves to zero and the kit comes
  back with confidence flags, a note, and one item per column rather than an
  error — same degrade-don't-crash contract `estimate_nutrition` has. Tests
  run in exactly that state unless they take the `component_macros` fixture
  (`tests/conftest.py`), which is why every kit test does.
- **`.gitignore` had `data/` unanchored**, which matched `aboyeur/data/`
  too — the component library, the bulk of the manual work, was invisible to
  git and would have been lost. Changed to `/data/` (the stale root DB copy
  is what it was ever meant to exclude). Watch for this if you add another
  package directory with a common name.
- A kit is built for **one member** — `household[0]` — not a household
  average, because averaging two people's targets produces a bowl that fits
  neither. It says so in its notes when the household has more than one.
- The prompt now tells the model to set `activity` when a sport is
  mentioned. `activity` is an effort level (`sedentary`/`light`/`moderate`/
  `active`/`very_active`); an unrecognized value like `"running"` silently
  falls back to moderate in `tdee()`. The prompt says so explicitly — this
  is pre-existing behavior, not new, but the kit branch makes it easier to
  trip.

**Verified live**, not just tested: `uv run pytest tests/ -q` is green (189),
every component resolves against the real USDA API, and the real local model
drives the whole flow in two turns — `get_profile` → `update_profile`
(activity + `set_macro_strategy: endurance`) → `build_macro_kit` →
`get_shopping_list(kit_id)`, with no `search_recipes` or `plan_menu` in
sight.

**Phase 2 (not done):** frontend Kit view; per-meal variation instead of one
template; weekly variety rotation; pantry-aware "what can I cook right now
from the kit"; micronutrient gap-fill; macro strategies on the menu path.

## Key files

- `aboyeur/db.py`, `aboyeur/migrate.py`, `aboyeur/auth.py` — schema, migration, auth
- `aboyeur/agent.py` — the LLM tool-calling loop
- `aboyeur/tools/*.py` — the actual tool implementations (all now `user_id`-scoped)
- `aboyeur/engines/kit.py`, `aboyeur/data/components.py` — the Macro Kit engine and its food library
- `backend/main.py` — FastAPI app; skim its own comments to tell live routes from dead ones
- `frontend/src/routes/{Login,Onboarding,Library,Chat,AppShell}.tsx` — the real UI
- `frontend/src/api.ts` — the one place every backend call is defined
- `design/aboyeur-app/*.dc.html` — the original mockup the frontend was ported from (UX/copy reference, not live code)
- `DEPLOYMENT.md` — how to actually ship this
