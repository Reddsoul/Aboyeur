# Implementation prompt — "Macro Kit" feature

Paste everything below the line into a fresh Opus session at the repo root
(`/Volumes/General/Coding/Python/Aboyeur`).

---

You are extending **Aboyeur** (conversational nutritionist / menu planner over a
75k-recipe cookbook library; native-tool-calling LLM agent on a local model).
Read `HANDOFF.md` and the memory index first. This task adds a **new
deliverable**, the **Macro Kit**, and it is Phase 1 only — no frontend.

## What a Macro Kit is

A *mix-and-match grocery template*, modeled on the Built With Science
"30-Day Filling Diet" (that PDF is in the repo root — read it for the shape,
but see "Do not" below). The diet is not a menu of recipes: it's food
**columns** with **portion rules** — Protein / Volume-veg / Fat (pick 1 per
meal) / Sauce — that you assemble into one bowl per meal and stay on macros
without counting.

Aboyeur's version generalizes that: given a member's macro target + training
profile, produce a small roster of whole foods per column (with per-serving
macros), an assembly rule (grams per column per meal), 2–3 generated example
bowls, the daily macro math, and a shopping list. It uses whole foods
resolved through the **existing USDA pipeline** — not the recipe corpus,
which is 1800s–1920s prose and useless for "200 g chicken + 200 g rice."

The user's driving example: *"I'm a runner and I want a combo of groceries I
can mix and match and hit my macros."*

## Locked decisions (do not relitigate)

1. **Component library is hand-curated**, ~80 whole foods, in a new data
   module. Budgeted at a day or two of curation — seed from the BWS food
   list (~40) + an endurance-athlete carb set (~20: rice, potato, sweet
   potato, oats, pasta, quinoa, tortilla, bagel, banana, dates, honey, …) +
   common veg (~15). Grow later.
2. **Raw / as-purchased weights throughout.** Component macros come from the
   raw USDA entry and gram bands are raw weights (weigh dry rice/pasta/oats,
   raw chicken). `lookup_ingredient_macros` already prefers the raw form —
   lean into that, no cooked-weight override, no special-casing.
3. **`build_macro_kit` is a separate tool from `plan_menu`.** Two tools, two
   purposes. Do **not** unify the engines, the tables, or the optimizer. A
   kit is not a menu.
4. **Do not index the BWS PDF** into `nutrition_reference` — that corpus is
   public-domain 1800s–1920s texts and the prompt flags its hits as "dated,
   attribute the source." A 2024 copyrighted PDF breaks both. Use it only as
   a curation seed and as golden-test fixtures.

## Reuse, don't reinvent

- **Component macros:** `engines/nutrition.py` → `lookup_ingredient_macros(conn,
  config, query)` + `_validated_macros`. Cached in `usda_cache`. Whole-food
  queries resolve at high confidence — the recipe-corpus confidence problem
  doesn't apply here. Honor the `status` field: `success` / `failed` /
  `skipped` (no API key). Degrade like `tools/nutrition.py` does — flag low
  confidence, never crash. `USDA_API_KEY` may be unset in some envs.
- **Allergy filtering:** `engines/allergens.py` → `expand_all()`. Filter
  components by matching expanded allergens against the component name +
  `usda_query`.
- **Shopping list:** `engines/shopping.py` → `categorize_ingredient()` and the
  `build_shopping_list()` section/shape. Match its output shape.
- **Macro-fit scoring:** `engines/optimizer.py` → `macro_fit()` shape for the
  example-bowl fit score.
- **Persistence + scoping pattern:** copy `tools/menu.py`'s `_save_menu` /
  `_load_menu` / `_update_menu` **exactly**, including the `WHERE ... AND
  user_id = ?` filter and the `menu_id IS None → latest for this user`
  branch. HANDOFF documents two real cross-tenant leaks that were exactly
  the "omit an id, get the most recent row, no owner filter" shape — the
  kit's load path must not reintroduce it. There is a test for this (below).

## Constraints / gotchas

- **No new dependencies.** No scipy. numpy is available transitively but the
  portion solver is closed-form (below) — you shouldn't need it.
- **Live DB** is `~/.local/share/aboyeur/aboyeur.db` (via `.env` `DB_PATH`),
  not the repo's stale `data/aboyeur.db`.
- **Don't touch `backend/` dead code** (`models.py`, `menu_engine.py`,
  `macro_engine.py`, `preference_engine.py`, `fusion_engine.py`,
  `cookbook_mcp.py`, `rag.py`) — Jose wants it left alone. Live routes are
  in `backend/main.py` around the `/chat` section; its own comments mark the
  boundary. You likely don't need to touch `backend/` at all for Phase 1.
- **Tool schema descriptions target a mid-size local model** — be explicit
  and directive, like the existing entries in `tools/schemas.py`.
- **`_tool_result_text` in `agent.py`:** the kit result is structured JSON,
  so the default `json.dumps` path is correct — do **not** add a
  verbatim-markdown branch (those exist only for `get_recipe` /
  `get_menu_recipes`).
- Match `tests/conftest.py`'s fixture setup for the test DB / `Config`.

---

## Work breakdown

### 1. `aboyeur/data/__init__.py` + `aboyeur/data/components.py` (new)

```python
COMPONENTS: list[dict] = [
    {
        "name": "chicken breast",          # canonical, lowercase, singular
        "role": "protein",                 # see roles below
        "usda_query": "chicken breast raw",
        "default_serving_g": 150,
        "min_g": 80,
        "max_g": 300,
        "diet_ok": ["omnivore"],           # which diet modes allow this food
        "tags": ["lean", "meal-prep"],
        "macros_override": None,            # optional per-100g {kcal,protein_g,carb_g,fat_g};
                                           # use only where USDA is wrong, still run it
                                           # through the _validated_macros sanity shape
    },
    # ...
]
```

Roles: `protein`, `dairy_protein`, `carb_base`, `veg_volume`, `fruit`, `fat`,
`sauce`.

`diet_ok` values: `omnivore`, `pescatarian`, `vegetarian`, `vegan`. Tag each
food with every diet under which it's allowed (tuna → `["omnivore",
"pescatarian"]`; Greek yogurt → `+ "vegetarian"`; tofu → all four). A
component is included if the profile's effective diet (from `diet_modes`,
default `omnivore`) is in `diet_ok`.

Seed the roster from the BWS food list + the carb set + veg set described
above. `veg_volume` and `sauce` entries can carry `macros_override` of near-
zero where a USDA lookup is pointless (leafy greens, hot sauce) — same spirit
as `_ZERO_MACRO_FOODS`.

Add a `tests/` check that every `usda_query` is non-empty and every role is
valid, so a malformed row fails loudly.

### 2. `aboyeur/engines/nutrition.py` — macro strategy

Add:

```python
def resolve_macro_targets(member: dict, daily_kcal: float, strategy: str) -> dict:
    """Daily {protein_g, carb_g, fat_g} for a macro strategy.

    "balanced"  -> 30/40/30 of kcal (matches per_meal_target's split).
    "endurance" -> protein 1.8 g/kg bodyweight (1.6 g/kg if that overshoots;
                   25% of kcal if no weight_kg), carb 55% of kcal,
                   fat = remainder, floored at 20% of kcal.
    "cut"       -> protein 2.2 g/kg (35% of kcal if no weight_kg),
                   fat 25% of kcal, carb = remainder (floor 0).
    Grams must be non-negative and sum by Atwater (4/4/9) to ~daily_kcal;
    if fixed terms overshoot, scale carb down first, then fat, then protein.
    """
```

Do **not** change `per_meal_target`, `household_targets`'s existing
`per_meal` / `daily_macros` outputs, or anything the menu path reads. If you
want strategy visible in `get_profile`, add it as *new* fields
(`macro_strategy`, `strategy_daily_macros`) only when a strategy is set —
additive, non-breaking. The kit engine can also just call
`resolve_macro_targets` directly.

### 3. `aboyeur/engines/kit.py` (new)

**Stage A — roster.** From `COMPONENTS`, drop anything failing diet /
expanded-allergen / explicit-exclude checks. Per column pick `kit_size[role]`
items (defaults: protein 3, dairy_protein 1, carb_base {endurance 4, balanced
3, cut 1}, veg_volume 5, fruit 2, fat 3, sauce 4). Selection is a simple
deterministic shortlist — sort by (USDA confidence desc, then a stable order
for variety), take first N. Not an optimization. Resolve each picked
component's per-100g macros (`macros_override` or `lookup_ingredient_macros`);
compute per-serving macros at `default_serving_g`. Flag components whose
lookup didn't resolve; drop one only if its column would still be non-empty.

**Stage B — portion solve** (closed-form, per meal). Targets `P*, C*, F*,
kcal*` = `resolve_macro_targets(...) / meals_per_day`. Choose one protein `p`,
one carb base `b`, one fat `f`; anchor veg at ~300 g (BWS "fill the bowl"),
fruit optional/0 by default.

1. veg fixed → known small macro contribution.
2. `g_b = (C* − veg_carb) / carb_per_g(b)`  — carb base is the main carb lever.
3. `g_p = (P* − veg_protein − g_b·protein_per_g(b)) / protein_per_g(p)`.
4. incidental fat = veg + carb + protein fat; `g_f = max(0, (F* − incidental) /
   fat_per_g(f))` — if protein/carb already cover fat, drop the fat serving.
5. clamp `g_p, g_b, g_f` to the component bands.
6. recompute total kcal; nudge `g_b` then `g_p` within band to close a kcal
   gap. If a band bound blocks the target, stop and **report the residual**
   (`status`: `ok` / `under` / `over`, same pattern as the menu optimizer's
   under-days — never silently claim a target is hit).
7. fit = `1 − normalized macro error` (reuse `optimizer.macro_fit` shape).

`per_meal_grams` = the solve for the representative combo (first protein ×
first carb × first fat). `example_bowls` = solve the top 3 distinct combos,
rank by fit.

**Stage C — emit** the result dict (shape below).

### 4. `aboyeur/db.py` + `aboyeur/migrate.py` — `kits` table

```sql
CREATE TABLE IF NOT EXISTS kits (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    data_json   TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
```

Add to `SCHEMA` in `db.py`. In `migrate.py`, create it + an
`idx_kits_user` index (follow the same reasoning the file already documents
for why `idx_menus_user` is created in `migrate`, not `db.py`).

### 5. `aboyeur/tools/kit.py` (new) + `tools/schemas.py` + `tools/__init__.py` + `agent.py`

```python
def build_macro_kit(
    conn, config, user_id: int,
    meals_per_day: int | None = None,   # default from profile
    days: int = 7,                      # shopping-list horizon
    emphasis: str | None = None,        # "endurance"|"cut"|"balanced";
                                        # default from profile macro_strategy, else "balanced"
    kit_size: dict | None = None,
    include_foods: list[str] | None = None,
    exclude_foods: list[str] | None = None,
) -> dict
```

Reads profile (targets, `weight_kg`, `diet_modes`, allergies, dislikes),
resolves the macro target via `resolve_macro_targets`, calls `engines/kit.py`,
persists via a **user-scoped** `_save_kit`, returns `{"kit_id": ..., **kit}`.

Register the schema (explicit description: when to use this vs
`plan_menu` — "use this for a mix-and-match grocery set / 'foods to hit my
macros', NOT for specific daily recipes"), wire into `agent.py`'s `_dispatch`
and `tools/__init__.py`.

### 6. `aboyeur/tools/shopping.py` — kit-aware

Extend `get_shopping_list` to accept a kit id. Detection: try `menus` first,
then `kits`, both `user_id`-scoped. For a kit: total grams per component =
`per_meal_grams[role] × days × meals_per_day / (# items in that column)`
(even rotation), grouped by section via `categorize_ingredient`, same output
shape as the menu path. Keep menu behavior byte-identical.

Update the `get_shopping_list` schema description to say it handles a kit id
too.

### 7. `aboyeur/tools/profile.py` + `update_profile` schema

Add `set_macro_strategy` (`"balanced"|"endurance"|"cut"`) — replace semantics.
Store on the profile root (household-wide) for Phase 1.

### 8. `aboyeur/prompts.py`

Add a branch to the system prompt:

- If the user wants a mix-and-match grocery set / "give me foods to hit my
  macros" / "groceries I can combine" rather than specific daily recipes →
  call `build_macro_kit`, **not** `search_recipes` + `plan_menu`.
- When the user states a sport or training ("I run", "marathon training",
  "cyclist", "I lift") → `update_profile` with `activity` and
  `set_macro_strategy` (`endurance` for running/cycling/rowing;
  `cut` if they state a fat-loss goal; `balanced` otherwise) in the same
  turn, then proceed — same auto-update discipline the prompt already
  mandates for stated personal facts.
- Kit delivery is **one step** (a kit is self-contained — do not apply the
  menu's STEP 1/STEP 2 confirm-gate): present the columns with per-serving
  macros, the assembly rule (grams per column per meal), 2–3 example bowls
  with macro breakdowns, and the daily total vs target with its status. Then
  offer the shopping list (`get_shopping_list` with the kit id). One
  estimation-not-medical-advice footer.

## Kit result shape (lock this)

```python
{
    "kit_id": int,
    "emphasis": "endurance",
    "meals_per_day": 3,
    "days": 7,
    "targets": {
        "strategy": "endurance",
        "basis": "protein 1.8 g/kg, carb 55% kcal",
        "daily": {"kcal": 2900, "protein_g": 145, "carb_g": 400, "fat_g": 78},
        "per_meal": {"kcal": 967, "protein_g": 48, "carb_g": 133, "fat_g": 26},
    },
    "columns": {
        "protein": [
            {"name": "chicken breast", "serving_g": 150, "kcal": 165,
             "protein_g": 31, "carb_g": 0, "fat_g": 3.6, "confidence": "medium"},
            # ...
        ],
        "carb_base": [...], "veg_volume": [...], "fat": [...],
        "sauce": [...], "fruit": [...], "dairy_protein": [...],
    },
    "assembly_rule": {
        "text": "Per meal, one bowl: 1 protein ~180 g raw · 1 carb base ~190 g raw · veg ~300 g · 1 fat serving · sauce/seasoning free. Swap any item within its column.",
        "per_meal_grams": {"protein": 180, "carb_base": 190, "veg_volume": 300, "fat": 15, "fruit": 0},
        "tips": [],   # optional BWS satiety strategies, only for emphasis="cut"
    },
    "example_bowls": [
        {"items": [{"name": "chicken breast", "grams": 180},
                   {"name": "white rice", "grams": 190},
                   {"name": "broccoli", "grams": 300},
                   {"name": "olive oil", "grams": 12}],
         "kcal": 955, "protein_g": 49, "carb_g": 130, "fat_g": 25, "fit": 0.94},
        # ...
    ],
    "daily_totals": {"kcal": 2870, "protein_g": 147, "carb_g": 395, "fat_g": 77,
                     "target_kcal": 2900, "status": "ok"},
    "low_confidence_components": ["quinoa"],
    "notes": ["Weights are raw / as-purchased.", "..."],
}
```

## Tests (`tests/test_engines.py`, `tests/test_tools.py`)

- `components.py` well-formed (roles valid, queries non-empty).
- `resolve_macro_targets`: endurance protein ≈ 1.8 g/kg; all three strategies'
  grams sum by Atwater to within a few % of `daily_kcal`; no-`weight_kg`
  fallback path.
- Portion solve: for a synthetic target, the representative bowl lands within
  tolerance on each macro, or reports the blocking bound honestly.
- Kit build end-to-end with a mocked/monkeypatched `lookup_ingredient_macros`
  (don't hit USDA in tests — check how `test_tools.py` handles this today).
- Kit shopping list: quantities scale with `days` and `meals_per_day`,
  sections are assigned.
- **Cross-tenant isolation:** user A's `build_macro_kit`, then user B calls
  `get_shopping_list` / kit load with no id and with A's kit id — must not
  see A's kit. Mirror the menu isolation test.
- Golden: an `emphasis="cut"` kit for a ~2000 kcal / 3-meal target produces
  per-meal bowls in the BWS envelope (~400–520 kcal, 45–72 g protein,
  ≤ ~13 g fat), using the 5 PDF bowls as reference fixtures.

## Definition of done

- `uv run pytest tests/ -q` green (run `uv sync --extra dev` first).
- CLI smoke: `uv run aboyeur chat`, then *"I'm a runner training for a
  half marathon, I'm 70 kg — build me a mix-and-match grocery kit to hit my
  macros"* → agent sets endurance strategy, builds a kit, shows columns +
  assembly rule + example bowls + daily math, offers the shopping list.
- `HANDOFF.md` updated with the new feature, the `kits` table, the new tool,
  and any non-obvious thing the next session needs.
- Component-library curation (the ~80 rows) done to a usable first pass —
  this is the bulk of the manual effort; the rest is plumbing.

## Explicitly out of scope (Phase 2)

Frontend Kit view; per-meal variation instead of one template; weekly
variety rotation; pantry-aware "what can I cook right now from the kit";
micro gap-fill; changing the menu path to use macro strategies.
