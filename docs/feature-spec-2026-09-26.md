# Feature spec — macro overrides, client notes, tolerance, PDF scaling, tools-available

Date: 2026-09-26
Scope: all changes target `aboyeur/*` (the live agent/tool-calling path driven by `backend/main.py`).
`backend/models.py` / `macro_engine.py` / `menu_engine.py` / `fusion_engine.py` are the older parallel
prototype, still routed for a few legacy endpoints — not touched by this spec.

Each section: current state (with file:line), what changes, and open decisions that need a call before
implementation.

---

## 1. Manual macro override on recipes

**Current state**
Macros live in the `nutrition` table (`aboyeur/db.py:119-134`), keyed by `recipe_id`:
`kcal, protein_g, carbs_g, fat_g, fiber_g, micros_json, per_serving, confidence, estimated_at, servings_est`.
Calculated by `estimate_recipe_macros` (`aboyeur/engines/nutrition.py:690`) via LLM ingredient parse +
USDA lookup. No override/manual field exists anywhere on this table today.

There's already a naming convention for this exact concept, just at the *ingredient* level:
`macros_override` in `aboyeur/engines/kit.py:159-181` / `aboyeur/data/components.py` — a curated
per-100g override dict, with `lookup` returning `status: "override"` when used. Reuse that convention.

**Change**
- Add override storage to `nutrition` table: `override_json` (same shape as calculated macros) +
  `is_override` bool, or a sibling `nutrition_override` table if scoping needs to be per-client (see
  open decision below).
- Add a single "effective macros" accessor (`get_effective_macros(recipe_id, ...)`) that returns
  `override_json` if `is_override` else the calculated row. **Every** call site that currently reads
  `nutrition` directly must go through this accessor instead — scoring (`macro_fit`, `optimizer.py:378`),
  `day_solver.py` least-squares solve, and the PDF `_macro_line` (`export_pdf.py:274`) all read macros
  independently today and would each silently keep using stale calculated values if only one is patched.
- New tool-call patch (mirrors `update_profile`'s patch-key pattern in `aboyeur/tools/profile.py`) to set
  or clear an override on a recipe.

**Open decision**
Is the override global to the recipe (one correction, seen by every client using that recipe), or scoped
per `(client_id, recipe_id)`? Global is simpler and matches "the calc was just wrong" framing; per-client
is needed if two clients would legitimately disagree (different brand of the same ingredient, different
prep). Recommend **global** unless you expect the second case — flag if wrong.

---

## 2. Client notes → meal-type hints + meal-prep filtering

**Current state**
Two unrelated "notes" already exist and neither is the right place:
- `clients.notes` (`aboyeur/db.py:202-216`) — practitioner-facing freeform text about the client, not diet
  logic.
- `profile` table (`aboyeur/db.py:230-234`) — encrypted JSON blob, shape = `DEFAULT_PROFILE`
  (`aboyeur/tools/profile.py:16-29`): `household, allergies, dislikes, likes, cuisine_affinity,
  diet_modes, meals_per_day, units, variety`. All structured lists — no free-text field.

Bigger gap found during recon: recipes already carry a `course` tag (`breakfast|main|side|dessert|...`,
set by `aboyeur/classify_recipes.py`), but the planner's slot-fill (`_pick_from`, `optimizer.py:619`)
**never checks course against slot number** — `main_pool` mixes `main` + `breakfast` + `unknown` and the
greedy scorer picks by overlap/pantry/macro-fit only. So today a breakfast-classified recipe can be
assigned to a dinner slot. This has to be fixed regardless of the notes feature, or "eggs are breakfast"
has nothing to hook into.

There's no meal-prep signal anywhere — no column, no classifier pass for it.

**Change**
- Add a free-text `notes` field inside the profile JSON (`DEFAULT_PROFILE`), separate from
  `clients.notes`. Parse it with an LLM pass (same pattern as `parse_ingredients` — cache the
  parse, don't re-run it every plan) into structured hints:
  - `meal_type_hints`: `{ingredient_or_dish: [breakfast|lunch|dinner]}`
  - `meal_prep_requested`: bool (or a list of which meals should be batch-cookable)
- Fix slot assignment in `_pick_from` to actually weight/filter by `course` vs slot position (slot 1 of N
  → prefer `course=breakfast`; later slots → deprioritize `breakfast` unless a hint overrides it). This is
  the prerequisite fix — without it, hints have no mechanism to act through.
- Meal-prep: add a `meal_prep: bool` column on `recipes`, populated by a new classifier pass modeled on
  `classify_recipes.py` (batch-cook / leftover-friendly heuristic: large yield, stores well, no
  last-minute-only steps). When `meal_prep_requested` is set, filter/boost the pool the same way
  `is_menu_ineligible` (`optimizer.py:302`) already excludes dessert/drink/component.

**Open decision**
Hints from notes ("eggs are breakfast") vs the existing `course` classifier tag — if they conflict (notes
say eggs are breakfast, classifier already agrees, fine; but what if a user says "steak is breakfast for
me"), does the user hint override the classifier outright, or just add a scoring boost? Recommend **hint
overrides classifier for that client only** (store as a per-client exception, don't mutate the shared
`recipes.course` value) — otherwise one client's quirky preference leaks into every other client's plans.

---

## 3. Macro tolerance ±10g + raw-ingredient fallback

**Current state**
- Per-dish scoring: `macro_fit` (`optimizer.py:378`), 45% calorie / 55% macro closeness.
- Day-level pass/fail: `day_macro_status` (`optimizer.py:411`) using **percentage** tolerances —
  `DAILY_KCAL_TOLERANCE = 0.15`, `DAILY_MACRO_TOLERANCE = 0.25` (`optimizer.py:97,102`). Not grams.
- Portion scaling: `day_solver.py` least-squares solve, clamped `PORTION_MULT_MIN/MAX = 0.75/2.0`
  (`optimizer.py:121-122`).
- When it still can't hit target: `aboyeur/tools/menu.py` widens the recipe pool
  (`_widen_pool`, line 188; `REPAIR_ROUNDS = (60,150,300)`) and re-plans off days (`_repair_failed_days`,
  line 312). After rounds are exhausted, **the plan still ships** with `macro_status: "off"` and
  `gap_notes` surfaced — it never silently fails, but it also never closes the gap by any means other than
  swapping in a different whole recipe.
- No raw-ingredient fallback exists at all — repair only ever pulls more classified cookbook recipes.

**Change**
- Add an absolute-gram check on top of the existing percentage tolerance: day is "off" if any macro is
  more than 10g from target, **even if** it's within the 25% band (percentage tolerance alone is too loose
  at high protein targets — 25% of 160g protein is a 40g slop). Keep the % tolerance as the loose backstop
  for small targets where ±10g would be unreasonably tight (e.g. a 20g fat target).
- New fallback step after `REPAIR_ROUNDS` is exhausted and the gap is still >10g: assemble a corrective
  raw-ingredient add-on line (e.g. "+30g chicken breast") using the existing
  `lookup_ingredient_macros` (`nutrition.py:581`) + fiber, sized to close the specific macro that's short.
  Surface it as an explicit added line item on the day, not folded invisibly into a recipe.
- Same raw-ingredient-macros-+-fiber logic also covers the "USDA lookup partially fails" case: when
  `usda_status` is partial/unknown for an ingredient inside `estimate_recipe_macros`, fall back to that
  ingredient's raw per-100g macros instead of dropping it or guessing.

**Open decision**
±10g — per meal, or per day (summed)? Per-day is far more achievable with whole-recipe repair; per-meal
basically forces the raw-ingredient fallback into constant use. Recommend **per day**, matching where
`day_macro_status` already operates — say so if you meant per meal.

---

## 4. PDF export — apply portion math to ingredients, not just print the multiplier

**Current state**
`aboyeur/export_pdf.py`, fpdf2. `_portion_note` (line 284) prints e.g. *"Portion: eat 0.5x servings of
this dish."* — a label only. `_render_recipes` (lines 463-501) renders the recipe's **original markdown
verbatim** (`pdf.write_html(markdown_to_html(...))`, line 495) — ingredient quantities in the printed list
are never touched. Macro numbers in `_macro_line` (line 274) *are* already scaled correctly — only the
ingredient text is wrong/misleading relative to the macros shown above it.

**Change**
- Recipes already have parsed structured ingredients (`ingredients_json` on the `recipes` table,
  `aboyeur/db.py:16-35`, populated at index time) — use that instead of re-parsing markdown. Multiply each
  ingredient quantity by `portion_multiplier` and render a scaled ingredient block in place of the raw
  markdown ingredient list.
- Keep `_portion_note` but change its wording to state plainly that the list below is already scaled
  (e.g. *"Ingredients below scaled to 0.5x."*) so it doesn't read as a leftover multiplier the user has to
  apply themselves.

**Open decision**
Rounding for scaled quantities that don't divide cleanly (0.5 × "1 egg" = "½ egg", 0.5 × "1 tsp" is fine).
Recommend: round to the nearest sane kitchen unit per ingredient type, and where that's not possible
(discrete items — eggs, cans, whole chicken breasts) round up and note it rather than print a fraction
that isn't cookable.

---

## 5. Client "tools available" + recipe equipment filtering

**Current state**
No equipment field exists anywhere. Closest thing is `technique` on the *legacy* `backend/models.py`
Recipe model — a single enum (`stir_fry|braise|roast|steam|raw|bake`) keyword-matched in
`backend/utils.py:32` (e.g. `"wok"` → `stir_fry`). That's legacy-path only, single-label, and not really
an equipment list. `DEFAULT_PROFILE` has nothing equipment-related. Grep across `aboyeur/` and `backend/`
for `equipment|appliance|tools_available|kitchen_tools` turns up nothing else.

**Change**
- Add `tools_available` to `DEFAULT_PROFILE` (`aboyeur/tools/profile.py`) — a list, e.g.
  `["oven","stovetop","blender","food_processor","slow_cooker","air_fryer","wok",...]`.
- Recipe side: add an `equipment_json` (or `equipment_tags`) column on `recipes`, populated by a new
  classifier pass modeled on `classify_recipes.py` (LLM reads instructions, tags required equipment).
  Ship an MVP first via keyword scan over instructions (wok, sous vide, air fryer, stand mixer, blender,
  food processor, grill, slow cooker/instant pot, dutch oven) since that's a same-day win; move to the
  LLM classifier pass if the keyword list proves too lossy.
- Planner integration: hard-exclude recipes requiring tools not in `tools_available` from `main_pool` /
  `_widen_pool`, same mechanism as `is_menu_ineligible` (`optimizer.py:302`) — not a soft score penalty,
  since a recipe you can't cook shouldn't ever appear regardless of how well it scores otherwise.

**Open decision**
Opt-in list ("only what I checked") vs opt-out ("assume common tools, flag only what's missing"). Opt-in
is stricter but means every existing client profile defaults to *zero* tools and every recipe gets
excluded until they fill it in. Recommend **opt-out**: default `tools_available` to a "common kitchen"
set (stovetop, oven, basic pots/pans, mixing bowls) and let the client uncheck things they don't have —
much less onboarding friction, and matches how `diet_modes`/`allergies` already work (empty list =
no restriction).

---

## Suggested build order

1. Course→slot mapping fix (prerequisite for #2, and probably overdue on its own).
2. Macro override (#1) — self-contained, single accessor function, no planner coupling.
3. Tools-available (#5) — hard-exclude filter is a small, isolated change once #1's slot-mapping fix
   pattern exists as a template.
4. Client notes → meal-type hints + meal-prep (#2) — depends on #1's fix.
5. PDF ingredient scaling (#4) — self-contained, no planner changes.
6. Macro tolerance + raw-ingredient fallback (#3) — most invasive (new solver step), do last once the
   pool-filtering changes above have settled so the fallback isn't fighting a moving target.
