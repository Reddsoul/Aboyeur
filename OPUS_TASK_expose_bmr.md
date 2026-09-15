# Task: expose raw BMR so Aboyeur can answer "what's my BMR" itself

Small, single-file-ish fix in Aboyeur (personal recipe/nutrition chat app,
FastAPI backend, local LLM via OpenAI-compatible tool-calling). Full test
suite (`pytest`) must stay green; add tests for the fix.

## Bug

**Symptom (user report):** user asks Aboyeur what their BMR is. Model can't
answer without asking the user to hand over the raw numbers and do the
Mifflin-St Jeor math themselves — even though BMR is a required intermediate
step the backend already computes on the way to TDEE.

**Root cause.** [aboyeur/engines/nutrition.py](aboyeur/engines/nutrition.py):
`maintenance_kcal_for_member()` (lines 125-138) calls `bmr_mifflin()` (line
79) internally when full anthropometrics are present, then immediately feeds
the result into `tdee()` and returns only the TDEE value — the intermediate
BMR number is computed and thrown away. `household_targets()` (lines
151-174) builds each member's `targets` entry from
`maintenance_kcal_for_member()`/`daily_kcal_for_member()` and exposes
`maintenance_kcal` (= TDEE) and `daily_kcal` (= TDEE ± goal adjustment), but
never the raw BMR. [aboyeur/tools/profile.py](aboyeur/tools/profile.py)
`get_profile()` (lines 67-87) returns that `targets` dict verbatim to the
model — so the model has literally no field to read BMR from, only TDEE.
`aboyeur/prompts.py` has no instruction about BMR at all (grep for it turns
up nothing outside the age-guessing rule at line 116).

## Fix required

1. **Surface the BMR value instead of discarding it.** In
   [aboyeur/engines/nutrition.py](aboyeur/engines/nutrition.py), give
   `maintenance_kcal_for_member()` (or a small new helper next to it, e.g.
   `bmr_for_member(member: dict) -> float | None`) a way to return the raw
   BMR alongside/independently of the TDEE — whichever shape is less
   invasive to the existing call sites. Simplest: add `bmr_for_member()`
   that does the same `has_anthro` check as
   `maintenance_kcal_for_member()`/`household_targets()` (lines 132,
   161) and returns `bmr_mifflin(...)` when full anthropometrics exist, else
   `None` — never a guessed/fallback BMR (matches the existing house rule at
   prompts.py line 114-118: no invented anthropometrics to force a BMR-based
   number).
2. In `household_targets()` (nutrition.py lines 151-174), add a
   `bmr_kcal` field to each member's target entry: the rounded BMR when
   `has_anthro` is true, `None` when the member falls back to the generic
   default. Put it next to `maintenance_kcal` so the two are easy to read
   together (BMR = resting cost, `maintenance_kcal` = BMR x activity = TDEE).
3. Update the `get_profile` tool schema description in
   [aboyeur/tools/schemas.py](aboyeur/tools/schemas.py) (~lines 77-82) to
   mention `bmr_kcal` explicitly so the model knows the field exists and
   what it means, e.g.: "...computed per-member daily/per-meal calorie and
   macro targets (bmr_kcal = resting BMR, maintenance_kcal = BMR x activity
   = TDEE, both null until the member has full anthropometrics)...".
4. Add a short rule to [aboyeur/prompts.py](aboyeur/prompts.py) (near the
   existing calorie/target guidance around lines 57-59 or the anthropometrics
   rule at 114-118) telling the model: when asked for BMR, TDEE, or
   maintenance calories, read `bmr_kcal`/`maintenance_kcal` straight from
   the member's `targets` entry (call `get_profile` first if not already
   current) and answer directly — never ask the user to supply numbers or
   do the arithmetic themselves, and never compute or guess a BMR yourself
   outside what the tool returns. If `bmr_kcal` is `null` (member missing
   sex/age/height_cm/weight_kg), say which of those is missing and ask for
   it, same as the existing "genuinely blocked" clarifying-question rule.

## Tests to add

In `tests/test_engines.py`, alongside the existing
`test_household_targets_include_goal_and_maintenance` /
`test_household_targets_labels_source_correctly` (lines 122-148+):
- A member with full anthropometrics gets a `bmr_kcal` in `household_targets()`
  equal to `bmr_mifflin(...)` for those same inputs (round-for-round match),
  and it's strictly less than `maintenance_kcal` for any activity level above
  sedentary's floor (since `maintenance_kcal = bmr_kcal * activity_factor`,
  activity_factor >= 1.2).
- A member missing any one of sex/age/height_cm/weight_kg gets `bmr_kcal`
  `None` (or absent, pick one — match how `source: "fallback"` already
  signals this) while still getting a fallback `maintenance_kcal`/`daily_kcal`.
- `tests/test_tools.py`: `get_profile()`'s returned `targets[member_id]`
  carries `bmr_kcal` through unchanged from `household_targets()`.

## Constraints / house rules to respect

- Don't touch `bmr_mifflin()`'s formula or `tdee()`'s activity table — this
  is purely exposing an already-computed number, not changing any math.
- Keep the "never guess anthropometrics to force a BMR" rule intact —
  `bmr_kcal` must be `None`/absent under exactly the same condition
  `source` is already `"fallback"`, not a best-effort estimate.
- `per_meal`/`daily_macros` (30/40/30 split) and `strategy_daily_macros`
  (Macro Kit) are unaffected — this only adds a field, doesn't change any
  existing one's meaning or value.

## Deliverable

Working code + passing `pytest` (existing suite + new tests above). Confirm
in the PR/commit description that asking Aboyeur "what's my BMR" (with a
household member's full stats already saved) now gets a direct numeric
answer with no follow-up question.
