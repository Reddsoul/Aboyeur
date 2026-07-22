# Aboyeur — Build Spec

**Aboyeur**: the kitchen expediter who takes orders, shouts them to the right stations, and makes sure every plate goes out right. This project is that, plus a nutritionist and a personal menu maker, for a 60,000-recipe personal cookbook library.

---

## 1. What it is

A conversational LLM harness that:

1. **Understands the request** — "Japanese week", "high-protein dinners, no dairy", "something with the chicken I have".
2. **Picks real recipes** from the user's library (60,140 markdown files, 222 cookbooks). Never invents recipes.
3. **Portions to the household** — scales servings per person using BMR/TDEE targets.
4. **Enforces dietary restrictions** — allergies are hard filters (code, not LLM); dislikes and diet modes (vegetarian, low-carb, etc.) shape selection.
5. **Fills macros and micronutrients** — estimates per-recipe nutrition via USDA FoodData Central; scores the weekly plan for macro fit and micronutrient coverage; swaps recipes to close gaps.
6. **Minimizes the grocery list** — selects the week's recipes to maximize shared ingredients and pantry overlap, then emits a consolidated shopping list grouped by store section.

## 2. Why the POC falls short (lessons learned)

`poc.py` (1,554 lines, single file) works as a demo but has an inverted architecture:

- **Heuristics do the thinking, the LLM narrates.** Intent detection is keyword lists (`_WEEK_PHRASES`), cuisine detection is a hand-maintained word set, dietary detection is regex. Every edge case became another list entry. This does not scale and misfires constantly.
- **The LLM never chooses.** Search results are computed by scoring functions, the week is picked by a greedy loop, then the LLM is handed the result to describe. When the pipeline picks wrong, the LLM can't fix it.
- **Index is a flat JSON file** loaded fully into RAM, rebuilt by re-reading 60k files over SMB. Slow, fragile, no incremental update.
- **No real nutrition.** Macro math exists (`backend/macro_engine.py`) but is never wired into selection. Micronutrients absent entirely.
- **Salvageable parts:** recipe frontmatter parsing, BMR/TDEE math, greedy overlap optimizer concept, shopping-list categorization (`backend/utils.py`), USDA lookup with cache (`backend/macro_engine.py`). Port the logic, not the wiring.

**The fix — invert control:** the LLM is the orchestrator. It receives the user message, the profile, and a set of tools. It decides what to search, what to select, when to ask. Deterministic code does what code is good at: indexing, filtering, arithmetic, optimization. The LLM does what it's good at: understanding intent, judging fit, conversing.

## 3. Architecture

```
User (terminal REPL now; Odysseus/MCP later)
        │
        ▼
┌─────────────────────────────────────────────┐
│  Agent loop (aboyeur/agent.py)              │
│  LLM w/ native tool calling (LiteLLM/Spark) │
│  system prompt = role + profile + rules     │
└──────┬──────────────────────────────────────┘
       │ tool calls (JSON)
       ▼
┌─────────────────────────────────────────────┐
│  Tool layer (aboyeur/tools/)                │
│  search · recipe · profile · pantry ·       │
│  nutrition · menu · shopping                │
└──────┬──────────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────────┐
│  Engines + storage                          │
│  SQLite (recipes + FTS5, pantry, profile,   │
│  menus, USDA cache) · indexer · macro math ·│
│  overlap optimizer · USDA client            │
└─────────────────────────────────────────────┘
```

### Key decisions

- **SQLite + FTS5, not JSON, not ChromaDB (for now).** One file, no services, full-text search over 60k titles+ingredients is instant. Semantic search can be added later as an optional tool backed by Chroma; don't block on it. The docker-compose/Chroma stack from the earlier iteration is out of scope for v1.
- **LLM does native tool calling.** LiteLLM endpoint (`LLM_BASE_URL`, OpenAI-compatible) with `tools=[...]`. Model is local Qwen — keep tools few (≤10), coarse-grained, with descriptions written for a mid-size model. Every tool returns compact JSON, token-budgeted (cap result counts, truncate ingredient lists).
- **Allergy filtering happens in code.** `search_recipes` and `plan_menu` take the profile's allergies and hard-exclude matching recipes before results ever reach the LLM. The LLM handles soft preferences (dislikes, cuisine affinity); it never gets the chance to serve an allergen.
- **Recipes are verbatim.** When showing a recipe, read the markdown file and print it. Never let the LLM rewrite ingredient quantities or steps (except explicit portion scaling, which is arithmetic in code).
- **Menu optimization is a deterministic tool.** LLM calls `plan_menu(candidate_ids, days, targets)`; a greedy/beam optimizer maximizes ingredient overlap + pantry usage + macro fit and returns the plan with scores. The LLM reviews it, can swap items via `swap_menu_item`, and presents it.

## 4. Data

### Recipe corpus
- Location: `/Volumes/General-1/Cookbooks/Recipes/<Book_Name>/<Recipe>.md` (SMB share `atabey/General`; also mounted at `/Volumes/General`, which can go stale — path must be configurable via `RECIPES_DIR` env var and errors must be friendly when the mount is down).
- Format: YAML frontmatter (`title`, `source`, `cuisine`, `ingredients` (normalized names), `tags`) + markdown body (`## Ingredients` with quantities, `## Instructions`, `## Notes` often containing "SERVES N").
- ~60,140 files, 222 book dirs. Skip `00_Dashboard.md` and non-recipe files (no frontmatter → skip, log count).

### SQLite schema (data/aboyeur.db)
- `recipes(id, path, title, book, cuisine, servings, ingredients_json, tags_json, mtime, indexed_at)`
- `recipes_fts` — FTS5 over (title, ingredients, tags, cuisine, book)
- `nutrition(recipe_id, kcal, protein_g, carbs_g, fat_g, fiber_g, micros_json, per_serving, estimated_at)` — filled lazily
- `usda_cache(ingredient, fdc_id, macros_json, micros_json, fetched_at)`
- `pantry(item, qty, added_at, expires_at)`
- `profile` — single-row JSON (household members, allergies, dislikes, likes, cuisine affinity, diet modes, plan settings)
- `menus(id, week_of, days_json, status)` — draft/approved history
- `interactions(ts, session, role, content, intent)` — feedback log for preference learning

### Indexer
- `aboyeur index` CLI: walk corpus, parse frontmatter + SERVES, upsert by `(path, mtime)` — incremental by default, `--rebuild` for full. Progress bar; tolerate unreadable files. Target: full rebuild acceptable if < ~10 min over SMB; incremental runs in seconds.

## 5. Tools (the LLM's API)

Keep to this set. Names/args final; descriptions written in the code for the model.

| Tool | Args | Returns |
|---|---|---|
| `search_recipes` | `query`, optional `cuisine`, `book`, `include_ingredients[]`, `exclude_ingredients[]`, `limit≤25` | compact list: id, title, book, cuisine, first 6 ingredients, pantry-match flag. Allergens pre-excluded. |
| `get_recipe` | `id`, optional `scale_to_servings` | full verbatim markdown; if scaling, quantities recomputed by code |
| `get_profile` | — | household, targets (computed BMR/TDEE per member), restrictions, preferences |
| `update_profile` | structured patch (add/remove allergy, dislike, member, diet mode…) | updated profile |
| `manage_pantry` | `action: list/add/remove/clear`, `items[]` | pantry state |
| `estimate_nutrition` | `recipe_ids[]` | per-serving kcal/macros/key micros per recipe (USDA, cached; flag low-confidence estimates) |
| `plan_menu` | `candidate_ids[]` (≥2× days), `days`, `meals_per_day`, optional `constraints` | optimized plan: per-day recipe + portion multiplier per member, overlap score, macro fit vs targets, micro coverage %, gaps list |
| `swap_menu_item` | `day`, `replacement_id` | re-scored plan |
| `get_shopping_list` | optional `menu_id` | consolidated list grouped by category, pantry items subtracted, quantities merged |

Rules encoded in the system prompt: only reference recipes returned by tools; always check profile before planning; present plans with day-by-day summary + macro fit + what to buy; ask at most one clarifying question and only when genuinely blocked.

## 6. Nutrition engine

- **Targets:** Mifflin-St Jeor BMR × activity factor per household member (port from `backend/macro_engine.py`). Per-meal targets = daily / meals, dinner weighted 35% if 3 meals.
- **Recipe estimation:** map normalized frontmatter ingredients → USDA FDC search (cached in SQLite forever). Parse quantities from the `## Ingredients` body where possible; fall back to standard portions per category with a `confidence: low` flag. Divide by SERVES.
- **Portioning:** per member multiplier = member kcal target / recipe per-serving kcal, rounded to ¼. Cap at 2.0 with a "consider a side" note rather than absurd multipliers.
- **Micros (v1 scope):** track iron, calcium, potassium, vitamin A, vitamin C, vitamin D, B12, folate, zinc, magnesium, sodium (over-limit flag). Weekly coverage = sum across plan vs RDA × 7 per member. `plan_menu` reports coverage % and the top 3 gaps; the optimizer uses coverage as a tiebreaker; the LLM suggests swaps/sides to close gaps.
- Nutrition is **estimation, not medical advice** — say so in output footer once per plan, not every message.

## 7. Menu optimizer

Greedy with restarts (port concept from `pick_week`/`menu_engine.select_recipes`, rewrite clean):

- Score(candidate | selected) = w1·ingredient_overlap(meaningful ingredients only — keep the `UNIVERSAL_STAPLES` exclusion idea) + w2·pantry_overlap + w3·macro_fit + w4·micro_gap_fill − w5·same_book_penalty − w6·protein_repeat_penalty.
- Constraints: no dessert as dinner, protein present (unless vegetarian mode), no recipe repeated, respect `constraints` from the LLM (e.g. "2 fish nights").
- Deterministic, fast (<1s for 60-candidate pool), returns scores so the LLM can explain *why* the week hangs together.

## 8. Project layout

```
Aboyeur/
├── aboyeur/
│   ├── __init__.py
│   ├── cli.py            # entrypoints: chat, index, stats
│   ├── agent.py          # LLM loop, tool dispatch, system prompt
│   ├── llm.py            # LiteLLM client, streaming, retry
│   ├── config.py         # env/.env: RECIPES_DIR, LLM_*, USDA_API_KEY, DB_PATH
│   ├── db.py             # SQLite schema + connection
│   ├── indexer.py        # corpus walker/parser
│   ├── tools/            # one module per tool, shared registry + JSON schemas
│   ├── engines/
│   │   ├── nutrition.py  # BMR/TDEE, USDA client, recipe estimation
│   │   ├── optimizer.py  # menu selection
│   │   └── shopping.py   # list building + categorization
│   └── prompts.py
├── tests/                # pytest; fixtures = ~20 real recipe files copied into tests/fixtures
├── data/                 # aboyeur.db (gitignored)
├── poc.py                # frozen reference — do not extend
├── backend/              # old iteration — reference only, delete at end of Phase 3
└── task.md               # this file
```

- Python 3.12+, uv or plain venv. Deps: `openai`, `httpx`, `pyyaml`, `rich` (REPL), `pytest`. No FastAPI/Docker/Chroma in v1.
- Type hints throughout. No file over ~400 lines.

## 9. Build phases

Each phase ends runnable + tested. Do not start N+1 until N's acceptance passes.

**Phase 1 — Foundation: config, DB, indexer.**
`aboyeur index` builds SQLite from the corpus; `aboyeur stats` prints books/recipes/cuisines. Accept: 60k recipes indexed, incremental reindex touches only changed files, FTS query "chicken ginger" returns sane results in <100ms.

**Phase 2 — Tool layer + engines (no LLM).**
All 9 tools callable from Python with real data. Port + clean: macro math, USDA client w/ cache, optimizer, shopping list. Accept: unit tests green; `plan_menu` on a 60-candidate Indian pool returns 7 dinners with overlap score and macro fit; allergy exclusion provably filters (test with "peanut" in profile).

**Phase 3 — Agent loop.**
`aboyeur chat`: streaming REPL (rich), native tool calling, tool-call trace visible with `--debug`. System prompt with profile injection. Accept end-to-end conversations: "plan me a Japanese week" → searches → plans → presents with macros + shopping list; "I'm allergic to shellfish" → profile updated → future results exclude; "show the Tuesday recipe" → verbatim file; "swap Tuesday, I hate eggplant" → swap + dislike recorded.

**Phase 4 — Nutrition depth.**
Quantity parsing from ingredient bodies, micronutrient coverage in plans, per-member portion multipliers in output, gap-closing swap suggestions. Accept: plan output shows per-member portions and weekly micro coverage with top gaps; estimates flagged low-confidence where quantity parsing failed.

**Phase 5 — Polish + integration.**
Preference learning from interaction log (LLM summarizes rejections → profile suggestions, user confirms). Optional: MCP server exposing the same tools for Odysseus (santo-domingo:7860). Shopping list export to markdown/file. Delete `backend/`, update README.

## 10. Non-goals (v1)

- No web UI, no Docker, no ChromaDB, no epub ingestion (corpus is already markdown).
- No multi-week planning, no cooking-mode timers, no image handling.
- No cloud LLM — Spark/LiteLLM endpoint only, but keep the client provider-agnostic (it's already OpenAI-compatible).

## 11. Environment

```
RECIPES_DIR=/Volumes/General-1/Cookbooks/Recipes
LLM_BASE_URL=http://spark-50a1.local:4000/v1   # LiteLLM on DGX Spark (Tailscale fallback: http://100.70.6.1:4000/v1)
LLM_MODEL=hard                                  # hard=Qwen3.6-27B-VL (tool calling)
USDA_API_KEY=...                                # https://fdc.nal.usda.gov/api-key-signup
DB_PATH=./data/aboyeur.db
```

Verify tool-calling support on the `hard` model early in Phase 3; if native tool calls are unreliable, fall back to JSON-mode function calling with a strict schema and retry-on-parse-failure — but try native first.
