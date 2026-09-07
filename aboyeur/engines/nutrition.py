"""BMR/TDEE math + USDA macro lookup + portion scaling.

Ported from the POC's backend/macro_engine.py (task.md §2 salvage list). The
math is unchanged; the USDA cache moved from a JSON file to the `usda_cache`
SQLite table so it lives in the one DB file instead of a second artifact.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

import httpx

from aboyeur.config import Config

USDA_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"

# USDA FoodData Central nutrient IDs (per 100 g).
_N_KCAL, _N_PROTEIN, _N_CARB, _N_FAT = 1008, 1003, 1005, 1004

_ACTIVITY = {
    "sedentary": 1.2, "light": 1.375, "moderate": 1.55,
    "active": 1.725, "very_active": 1.9,
}

# Default per-meal macro split (protein/carb/fat by calories).
_SPLIT = {"protein": 0.30, "carb": 0.40, "fat": 0.30}
_KCAL_PER_G = {"protein": 4.0, "carb": 4.0, "fat": 9.0}

# Weight-goal calorie adjustment applied on top of maintenance (TDEE or
# explicit daily_kcal, both treated as maintenance).
#
# With a goal weight set, the deficit/surplus is computed: a safe weekly rate
# picked from how far the member is from goal, converted to kcal/day via
# ~7700 kcal per kg of body weight. Losing tapers as the goal nears (0.75 →
# 0.5 → 0.25 kg/week); gaining stays a lean-bulk rate (0.25–0.35 kg/week).
#
# Without a goal weight, fixed fallbacks: "lose" = −750/day — middle of the
# standard 500–1000 kcal/day deficit for 1–2 lb/week (e.g. 2440 maintenance
# → 1690 target, inside the textbook 1500–1900 band); "gain" = +400/day.
_GOAL_ADJUST = {"lose": -750.0, "maintain": 0.0, "gain": 400.0}
_KCAL_PER_KG = 7700.0


def goal_adjust_kcal(member: dict) -> float:
    """Signed kcal/day adjustment for the member's weight goal."""
    goal = (member.get("goal") or "maintain").strip().lower()
    if goal not in ("lose", "gain"):
        return 0.0
    weight = member.get("weight_kg")
    goal_weight = member.get("goal_weight_kg")
    if weight and goal_weight:
        remaining = abs(float(weight) - float(goal_weight))
        if goal == "lose":
            rate = 0.25 if remaining < 2 else 0.5 if remaining <= 5 else 0.75
            return -_KCAL_PER_KG * rate / 7.0
        rate = 0.25 if remaining <= 5 else 0.35
        return _KCAL_PER_KG * rate / 7.0
    return _GOAL_ADJUST[goal]

# Never plan below these, whatever the deficit math says — standard minimum
# intakes (1200 women / 1500 men); unknown sex gets the conservative floor.
_MIN_KCAL_BY_SEX = {"f": 1200.0, "m": 1500.0}
_MIN_KCAL_UNKNOWN = 1500.0

# Rough grams-per-ingredient-per-serving assumption when no better signal exists.
_DEFAULT_GRAMS_PER_INGREDIENT = 80.0


# ---------------------------------------------------------------------------
# BMR / TDEE / targets
# ---------------------------------------------------------------------------

def bmr_mifflin(sex: str, age: int, height_cm: float, weight_kg: float) -> float:
    """Mifflin-St Jeor BMR (kcal/day)."""
    base = 10.0 * weight_kg + 6.25 * height_cm - 5.0 * age
    return base + (5.0 if (sex or "").lower().startswith("m") else -161.0)


def tdee(bmr: float, activity: str) -> float:
    """Total daily energy expenditure = BMR x activity factor."""
    return bmr * _ACTIVITY.get((activity or "moderate").lower(), 1.55)


def per_meal_target(daily_kcal: float, meals_per_day: int) -> dict:
    """Per-meal {kcal, protein_g, carb_g, fat_g} using the 30/40/30 split."""
    meals = max(1, int(meals_per_day or 1))
    kcal = float(daily_kcal) / meals
    return {
        "kcal": round(kcal, 1),
        "protein_g": round(_SPLIT["protein"] * kcal / _KCAL_PER_G["protein"], 1),
        "carb_g": round(_SPLIT["carb"] * kcal / _KCAL_PER_G["carb"], 1),
        "fat_g": round(_SPLIT["fat"] * kcal / _KCAL_PER_G["fat"], 1),
    }


# Generic daily kcal fallback when neither full anthropometrics (for BMR) nor
# an explicit daily_kcal exist — midpoint of a plain-language recommended
# range (women ~1600-2000, men ~2100-2500), keyed by sex. "u" (unknown/unspecified
# sex) has no such range to split, so it takes the overall midpoint of both.
_DEFAULT_KCAL_BY_SEX = {"f": 1800.0, "m": 2300.0}
_DEFAULT_KCAL_UNKNOWN = 2000.0


def maintenance_kcal_for_member(member: dict) -> float:
    """Resolve a household member's maintenance kcal (no goal adjustment).

    Full anthropometric data -> BMR x activity (TDEE). Else the supplied
    daily_kcal. Else a sex-aware generic default — never a guessed age or
    other invented anthropometric to force the BMR path.
    """
    if all(member.get(k) is not None for k in ("sex", "age", "height_cm", "weight_kg")):
        b = bmr_mifflin(member["sex"], member["age"], member["height_cm"], member["weight_kg"])
        return tdee(b, member.get("activity", "moderate"))
    if member.get("daily_kcal") is not None:
        return float(member["daily_kcal"])
    sex = (member.get("sex") or "").strip().lower()[:1]
    return _DEFAULT_KCAL_BY_SEX.get(sex, _DEFAULT_KCAL_UNKNOWN)


def daily_kcal_for_member(member: dict) -> float:
    """Maintenance kcal adjusted for the member's weight goal, floored at the
    standard minimum intake for their sex."""
    maintenance = maintenance_kcal_for_member(member)
    adjusted = maintenance + goal_adjust_kcal(member)
    sex = (member.get("sex") or "").strip().lower()[:1]
    floor = _MIN_KCAL_BY_SEX.get(sex, _MIN_KCAL_UNKNOWN)
    return max(adjusted, min(floor, maintenance))


def household_targets(profile: dict) -> dict:
    """Per-person per-meal targets for the whole household.

    Returns {member_id: {label, daily_kcal, source, per_meal:{...}}}.
    """
    household_meals = profile.get("meals_per_day", 3)
    out = {}
    for m in profile.get("household", []):
        maintenance = maintenance_kcal_for_member(m)
        daily = daily_kcal_for_member(m)
        has_anthro = all(m.get(k) is not None for k in ("sex", "age", "height_cm", "weight_kg"))
        meals = m.get("meals_per_day", household_meals)
        out[m["id"]] = {
            "label": m.get("label", m["id"]),
            "goal": (m.get("goal") or "maintain").strip().lower(),
            "goal_weight_kg": m.get("goal_weight_kg"),
            "goal_adjust_kcal": round(goal_adjust_kcal(m)),
            "maintenance_kcal": round(maintenance),
            "daily_kcal": round(daily),
            "source": "bmr" if has_anthro else "fallback",
            "per_meal": per_meal_target(daily, meals),
            "daily_macros": per_meal_target(daily, 1),
        }
    return out


# ---------------------------------------------------------------------------
# Macro strategies (Macro Kit)
# ---------------------------------------------------------------------------

# Additive to the menu path, not a replacement for it: per_meal_target /
# household_targets keep their 30/40/30 split and everything reading them
# (plan_menu, swap_menu_item, the optimizer) is untouched. A strategy only
# applies where something asks for one — currently build_macro_kit.
MACRO_STRATEGIES = ("balanced", "endurance", "cut")

# g protein per kg bodyweight. Endurance drops to the lower figure when the
# higher one would eat too much of the calorie budget to leave room for the
# carbohydrate an endurance athlete actually trains on.
_ENDURANCE_PROTEIN_G_PER_KG = 1.8
_ENDURANCE_PROTEIN_G_PER_KG_LOW = 1.6
_ENDURANCE_PROTEIN_KCAL_CEILING = 0.30
_ENDURANCE_CARB_KCAL_SHARE = 0.55
_ENDURANCE_FAT_KCAL_FLOOR = 0.20
_CUT_PROTEIN_G_PER_KG = 2.2
_CUT_FAT_KCAL_SHARE = 0.25
# Fallback protein shares when no bodyweight is known — nothing here ever
# guesses a weight to force the g/kg path (same rule as the BMR fallback).
_ENDURANCE_PROTEIN_KCAL_SHARE = 0.25
_CUT_PROTEIN_KCAL_SHARE = 0.35


def _balance_to_kcal(grams: dict, daily_kcal: float) -> dict:
    """Force protein/carb/fat grams to sum to daily_kcal by Atwater (4/4/9).

    Overshoot is taken out of carb first, then fat, then protein — carb is
    the least load-bearing of the three for every strategy here (protein is
    set from bodyweight, fat has a health floor). An undershoot goes back
    into carb. Nothing is allowed to go negative.
    """
    out = {k: max(0.0, float(v)) for k, v in grams.items()}
    order = ("carb_g", "fat_g", "protein_g")
    for _ in range(len(order)):
        total = sum(out[k] * _KCAL_PER_G[k[:-2]] for k in out)
        delta = total - float(daily_kcal)
        if abs(delta) < 0.5:
            break
        if delta > 0:
            for key in order:
                if out[key] <= 0:
                    continue
                cut = min(out[key], delta / _KCAL_PER_G[key[:-2]])
                out[key] -= cut
                delta -= cut * _KCAL_PER_G[key[:-2]]
                if delta < 0.5:
                    break
        else:
            out["carb_g"] += -delta / _KCAL_PER_G["carb"]
            break
    return out


def resolve_macro_targets(member: dict, daily_kcal: float, strategy: str) -> dict:
    """Daily {kcal, protein_g, carb_g, fat_g} for one macro strategy.

    "balanced"  -> 30/40/30 of calories (the same split per_meal_target uses,
                   so a balanced kit and the menu path agree).
    "endurance" -> protein 1.8 g/kg bodyweight (1.6 g/kg if that would take
                   more than 30% of calories; 25% of calories when no
                   weight_kg is known), carbohydrate 55% of calories, fat the
                   remainder with a 20%-of-calories floor.
    "cut"       -> protein 2.2 g/kg (35% of calories with no weight_kg), fat
                   25% of calories, carbohydrate the remainder.

    Grams are non-negative and always sum by Atwater to daily_kcal — when the
    fixed terms overshoot, carb gives way first, then fat, then protein.
    Also returns `strategy` and a plain-language `basis` so a kit can state
    where its numbers came from instead of presenting them as given.
    """
    kcal = max(0.0, float(daily_kcal or 0))
    strategy = (strategy or "balanced").strip().lower()
    if strategy not in MACRO_STRATEGIES:
        strategy = "balanced"
    weight = member.get("weight_kg") if member else None
    weight = float(weight) if weight else None

    if strategy == "endurance":
        if weight:
            per_kg = _ENDURANCE_PROTEIN_G_PER_KG
            if per_kg * weight * _KCAL_PER_G["protein"] > _ENDURANCE_PROTEIN_KCAL_CEILING * kcal:
                per_kg = _ENDURANCE_PROTEIN_G_PER_KG_LOW
            protein = per_kg * weight
            basis = f"protein {per_kg} g/kg bodyweight, carbohydrate 55% of calories"
        else:
            protein = _ENDURANCE_PROTEIN_KCAL_SHARE * kcal / _KCAL_PER_G["protein"]
            basis = "protein 25% of calories (no bodyweight on file), carbohydrate 55% of calories"
        carb = _ENDURANCE_CARB_KCAL_SHARE * kcal / _KCAL_PER_G["carb"]
        remainder = kcal - protein * _KCAL_PER_G["protein"] - carb * _KCAL_PER_G["carb"]
        fat = max(remainder, _ENDURANCE_FAT_KCAL_FLOOR * kcal) / _KCAL_PER_G["fat"]
    elif strategy == "cut":
        if weight:
            protein = _CUT_PROTEIN_G_PER_KG * weight
            basis = f"protein {_CUT_PROTEIN_G_PER_KG} g/kg bodyweight, fat 25% of calories"
        else:
            protein = _CUT_PROTEIN_KCAL_SHARE * kcal / _KCAL_PER_G["protein"]
            basis = "protein 35% of calories (no bodyweight on file), fat 25% of calories"
        fat = _CUT_FAT_KCAL_SHARE * kcal / _KCAL_PER_G["fat"]
        carb = max(
            0.0,
            kcal - protein * _KCAL_PER_G["protein"] - fat * _KCAL_PER_G["fat"],
        ) / _KCAL_PER_G["carb"]
    else:
        protein = _SPLIT["protein"] * kcal / _KCAL_PER_G["protein"]
        carb = _SPLIT["carb"] * kcal / _KCAL_PER_G["carb"]
        fat = _SPLIT["fat"] * kcal / _KCAL_PER_G["fat"]
        basis = "30/40/30 split of calories (protein/carbohydrate/fat)"

    balanced = _balance_to_kcal(
        {"protein_g": protein, "carb_g": carb, "fat_g": fat}, kcal
    )
    return {
        "strategy": strategy,
        "basis": basis,
        "kcal": round(kcal, 1),
        "protein_g": round(balanced["protein_g"], 1),
        "carb_g": round(balanced["carb_g"], 1),
        "fat_g": round(balanced["fat_g"], 1),
    }


# ---------------------------------------------------------------------------
# USDA lookup (cached in SQLite, best-effort)
# ---------------------------------------------------------------------------

# Corpus ingredient lines are recipe prose, not food names — "stiffly beaten
# whites of the eggs", "one-half cupful of syrup", "salt and ground pepper to
# taste". USDA's search matches poorly against those, which is the main reason
# nutrition estimates come back empty. Strip quantities, measures, prep
# descriptors, and filler down to the food noun before querying.
_MEASURE_WORDS = {
    "cup", "cups", "cupful", "cupfuls", "tablespoon", "tablespoons", "teaspoon",
    "teaspoons", "spoonful", "spoonfuls", "pound", "pounds", "lb", "lbs", "ounce",
    "ounces", "oz", "gram", "grams", "g", "kg", "quart", "quarts", "pint", "pints",
    "gallon", "gallons", "liter", "liters", "slice", "slices", "piece", "pieces",
    "package", "packages", "packaged", "can", "cans", "dozen", "bunch", "sprig",
    "sprigs", "clove", "cloves", "stick", "sticks", "strip", "strips", "dash", "pinch",
}
_DESCRIPTOR_WORDS = {
    "chopped", "diced", "minced", "sliced", "grated", "shredded", "beaten",
    "stiffly", "melted", "softened", "cold", "hot", "warm", "boiling", "cooked",
    "uncooked", "raw", "washed", "seeded", "stoned", "pitted", "peeled", "cut",
    "finely", "coarsely", "thinly", "roughly", "lightly", "well", "large", "small",
    "medium", "nice", "fat", "lean", "stiff", "risen", "increased", "removed",
    "greased", "necessary", "optional", "taste", "ground",
}
_FILLER_WORDS = {
    "a", "an", "the", "of", "and", "to", "with", "for", "into", "in", "about",
    "if", "any", "kind", "some", "few", "little", "one", "two", "three", "four",
    "five", "six", "seven", "eight", "nine", "ten", "twelve", "half", "third",
    "fourth", "fourths", "quarter", "quarters", "whole", "more", "or", "so",
    "one-half", "one-third", "one-fourth", "two-thirds", "three-fourths",
}
_STOP_WORDS = _MEASURE_WORDS | _DESCRIPTOR_WORDS | _FILLER_WORDS
_WORD_RE = re.compile(r"[a-z']+")


def clean_ingredient_name(name: str) -> str:
    """Reduce an ingredient line to the food noun USDA can actually match.

    "stiffly beaten whites of the eggs" -> "whites eggs";
    "one-half cupful of syrup" -> "syrup". Returns "" when nothing survives
    (e.g. "kitchen twine" reduces fine but pure-measure lines don't).
    """
    s = (name or "").lower()
    s = re.sub(r"\([^)]*\)", " ", s)  # drop parentheticals
    s = s.split(" or ")[0]  # "chicken broth or water" -> first alternative
    tokens = [t for t in _WORD_RE.findall(s) if t not in _STOP_WORDS]
    return " ".join(tokens)


# ---------------------------------------------------------------------------
# LLM ingredient parsing: raw line -> (canonical food, estimated grams)
# ---------------------------------------------------------------------------

_PARSE_SYSTEM_PROMPT = (
    "You parse recipe ingredient lines. For each input line return the canonical "
    "food name — the food itself, no quantities, measures, or prep words, but KEEP "
    "meaningful distinctions ('egg yolk' is not 'egg', 'heavy cream' is not 'milk') — "
    "and the total grams that line represents, estimated from its quantity "
    "('6 eggs' ≈ 300, '2 tablespoons butter' ≈ 28, 'salt to taste' ≈ 5). "
    "Reply with ONLY a JSON array, same length and order as the input: "
    '[{"food": "eggs", "grams": 300}, ...]. No prose, no code fences.'
)
_MAX_GRAMS = 2000.0


def _llm_parse_lines(config: Config, lines: list[str]) -> list[dict] | None:
    """One batched LLM call parsing every line. None on any failure — the
    caller falls back to the regex cleaner + default grams."""
    from openai import OpenAI  # local import: keep engine importable without config

    try:
        client = OpenAI(base_url=config.llm_base_url, api_key="unused",
                        timeout=60, max_retries=1)
        resp = client.chat.completions.create(
            model=config.llm_model,
            messages=[
                {"role": "system", "content": _PARSE_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(lines)},
            ],
            temperature=0,
        )
        content = (resp.choices[0].message.content or "").strip()
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
        parsed = json.loads(content)
        if not isinstance(parsed, list) or len(parsed) != len(lines):
            return None
        out = []
        for item in parsed:
            food = str(item.get("food", "")).strip().lower()
            grams = float(item.get("grams", 0))
            if not food or grams <= 0:
                return None
            out.append({"food": food, "grams": min(grams, _MAX_GRAMS)})
        return out
    except Exception:
        return None


def parse_ingredients(
    conn: sqlite3.Connection, config: Config, lines: list[str]
) -> list[dict]:
    """[{food, grams, source}] per raw ingredient line.

    Cache-first (ingredient_parse table), then one batched LLM call for the
    misses, then a regex-cleaner fallback (source="heuristic", default grams)
    for anything the LLM couldn't handle. Heuristic results are never cached,
    so the LLM gets retried next time.
    """
    keys = [(line or "").strip().lower() for line in lines]
    out: list[dict | None] = [None] * len(lines)

    for i, key in enumerate(keys):
        if not key:
            out[i] = {"food": "", "grams": 0.0, "source": "empty"}
            continue
        row = conn.execute(
            "SELECT food, grams FROM ingredient_parse WHERE raw = ?", (key,)
        ).fetchone()
        if row is not None:
            out[i] = {"food": row["food"], "grams": row["grams"], "source": "cache"}

    miss_idx = [i for i, v in enumerate(out) if v is None]
    if miss_idx:
        llm_results = _llm_parse_lines(config, [lines[i] for i in miss_idx])
        if llm_results is not None:
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            for i, parsed in zip(miss_idx, llm_results):
                out[i] = {**parsed, "source": "llm"}
                conn.execute(
                    "INSERT INTO ingredient_parse (raw, food, grams, parsed_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(raw) DO UPDATE SET food=excluded.food, grams=excluded.grams, "
                    "parsed_at=excluded.parsed_at",
                    (keys[i], parsed["food"], parsed["grams"], now),
                )
            conn.commit()
        else:
            for i in miss_idx:
                food = clean_ingredient_name(lines[i]) or keys[i]
                out[i] = {"food": food, "grams": _DEFAULT_GRAMS_PER_INGREDIENT,
                          "source": "heuristic"}
    return out

# Per-100g sanity gate on USDA search hits. Search with no dataType filter
# returns branded products first, whose labels are frequently garbage per
# 100 g (observed in the wild: salt at 1330 g carbs/100 g, turmeric at 125 g
# carbs with 0 kcal, "onion" matching onion powder). A food passes only if
# its macro grams can physically fit in 100 g and its stated energy agrees
# with the Atwater energy of its own macros (4/4/9).
_MACRO_SUM_MAX = 105.0  # p+c+f grams per 100 g food; small headroom for rounding
_ATWATER_TOLERANCE = 0.30

# Foods that are macro-zero by definition — USDA search matches them badly
# ("water" ranks water spinach and crackers above bottled water), and a
# lookup can only get them wrong.
_ZERO_MACRO_FOODS = {"water", "ice", "salt", "kosher salt", "sea salt"}


def _norm_word(w: str) -> str:
    """Light singular/plural normalization so 'onion' matches 'Onions, raw'.

    -es is only stripped after the endings that actually take an -es plural
    (potatoes, dishes, boxes); a plain -e word + s ('vegetables', 'cheeses')
    just loses the s — the old blanket -es rule produced 'vegetabl'."""
    if len(w) > 3 and w.endswith(("oes", "ches", "shes", "sses", "xes", "zes")):
        return w[:-2]
    if len(w) > 2 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def fold_plural(name: str) -> str:
    """Word-by-word singular fold: 'potatoes' -> 'potato', 'green onions' ->
    'green onion'. Used to merge plural/singular spellings of the same food
    in shopping-list aggregation and overlap scoring."""
    return " ".join(_norm_word(w) for w in (name or "").lower().split())


def _validated_macros(food: dict) -> dict | None:
    """Extract per-100g macros from one USDA search hit, or None if the
    entry is internally inconsistent (junk label data)."""
    nutrients = {n.get("nutrientId"): n.get("value", 0)
                 for n in food.get("foodNutrients", [])}
    kcal = float(nutrients.get(_N_KCAL, 0) or 0)
    protein = float(nutrients.get(_N_PROTEIN, 0) or 0)
    carb = float(nutrients.get(_N_CARB, 0) or 0)
    fat = float(nutrients.get(_N_FAT, 0) or 0)
    if min(kcal, protein, carb, fat) < 0 or protein + carb + fat > _MACRO_SUM_MAX:
        return None
    atwater = 4.0 * protein + 4.0 * carb + 9.0 * fat
    if kcal <= 0:
        if atwater <= 0:
            # All-zero: search results sometimes ship Foundation entries with
            # an empty nutrient set ("Oil, canola" at 0 kcal). Genuinely
            # macro-free foods (water, salt) are answered from
            # _ZERO_MACRO_FOODS before USDA is ever queried, so an all-zero
            # hit here is missing data, not information — skip it.
            return None
        # Energy missing but macros present (some Foundation entries omit
        # nutrient 1008) — recompute from the macros instead of summing
        # 0 kcal against real grams.
        kcal = atwater
    elif kcal >= 20 and abs(kcal - atwater) / kcal > _ATWATER_TOLERANCE:
        return None
    return {"kcal": kcal, "protein_g": protein, "carb_g": carb, "fat_g": fat,
            "status": "success"}


def validated_per_100g(macros: dict) -> dict | None:
    """Run curated per-100 g macros through the same gate a USDA hit gets.

    The Macro Kit's component library carries hand-entered macros for foods
    USDA can't answer well (leafy greens, condiments, konjac noodles). Those
    are still data entry, so they go through _validated_macros' physical
    checks — grams must fit in 100 g, stated energy must agree with its own
    Atwater energy — rather than being trusted because a human typed them.
    Returns None on a row that fails, so a typo fails loudly.
    """
    return _validated_macros({"foodNutrients": [
        {"nutrientId": _N_KCAL, "value": float(macros.get("kcal") or 0)},
        {"nutrientId": _N_PROTEIN, "value": float(macros.get("protein_g") or 0)},
        {"nutrientId": _N_CARB, "value": float(macros.get("carb_g") or 0)},
        {"nutrientId": _N_FAT, "value": float(macros.get("fat_g") or 0)},
    ]})


def lookup_ingredient_macros(conn: sqlite3.Connection, config: Config, name: str) -> dict:
    """Per-100g macros for one ingredient via USDA. Cached in the usda_cache table.

    Returns {kcal, protein_g, carb_g, fat_g, status}.
    status: success | failed | skipped (no API key).
    """
    key = (name or "").strip().lower()
    if not key:
        return {"kcal": 0, "protein_g": 0, "carb_g": 0, "fat_g": 0, "status": "failed"}

    row = conn.execute("SELECT macros_json FROM usda_cache WHERE ingredient = ?", (key,)).fetchone()
    if row is not None:
        return json.loads(row["macros_json"])

    if key in _ZERO_MACRO_FOODS or clean_ingredient_name(key) in _ZERO_MACRO_FOODS:
        return {"kcal": 0.0, "protein_g": 0.0, "carb_g": 0.0, "fat_g": 0.0,
                "status": "success"}

    if not config.usda_api_key:
        result = {"kcal": 0, "protein_g": 0, "carb_g": 0, "fat_g": 0, "status": "skipped"}
    else:
        result = {"kcal": 0, "protein_g": 0, "carb_g": 0, "fat_g": 0, "status": "failed"}
        # Query the cleaned food noun first ("syrup", not "one-half cupful of
        # syrup"), falling back to the raw line if the cleaned one misses.
        # The cache key stays the raw name. Curated datasets (Foundation /
        # SR Legacy) are tried before the unfiltered pool — branded products
        # only get a look when no curated food matches, and every hit must
        # pass the _validated_macros sanity gate.
        queries = [q for q in dict.fromkeys([clean_ingredient_name(key), key]) if q]
        for data_types in ("Foundation,SR Legacy", None):
            for q in queries:
                params = {"query": q, "pageSize": 10, "api_key": config.usda_api_key}
                if data_types:
                    params["dataType"] = data_types
                try:
                    resp = httpx.get(USDA_URL, params=params, timeout=15)
                    resp.raise_for_status()
                    foods = resp.json().get("foods") or []
                except (httpx.HTTPError, ValueError, KeyError):
                    result["status"] = "failed"
                    continue
                # Among sane hits, prefer one whose description actually
                # names the queried food — plain relevance ranking happily
                # returns "Butter, salted" for the query "salt".
                validated = [(food, _validated_macros(food)) for food in foods]
                validated = [(f, v) for f, v in validated if v is not None]
                if validated:
                    q_words = {_norm_word(w) for w in q.split()}
                    named = []
                    for f, v in validated:
                        d_words = {_norm_word(w) for w in
                                   _WORD_RE.findall((f.get("description") or "").lower())}
                        if q_words & d_words:
                            named.append((v, "raw" in d_words))
                    if named:
                        # Recipe lines state as-purchased (raw) quantities, so
                        # a raw-form entry beats a prepared one — "Tomatoes,
                        # red, raw" (18 kcal) over "Tomatoes, sun-dried" (302).
                        result = next((v for v, raw in named if raw), named[0][0])
                    else:
                        result = validated[0][1]
                    break
            if result["status"] == "success":
                break
            if result["status"] == "success":
                break

    # Only cache real hits. "skipped" (no key) and "failed" (no key yet, rate
    # limit, transient network error, or a query USDA just doesn't match)
    # aren't information about the ingredient — caching them would silently
    # freeze that non-result forever, even after a key is added or the
    # transient problem clears. Retrying costs one HTTP call; a poisoned
    # cache costs it forever.
    if result["status"] == "success":
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        conn.execute(
            "INSERT INTO usda_cache (ingredient, macros_json, fetched_at) VALUES (?, ?, ?) "
            "ON CONFLICT(ingredient) DO UPDATE SET macros_json=excluded.macros_json, "
            "fetched_at=excluded.fetched_at",
            (key, json.dumps(result), now),
        )
        conn.commit()
    return result


def estimate_recipe_macros(
    conn: sqlite3.Connection,
    config: Config,
    ingredients: list[str],
    servings: int = 4,
    grams_per_ingredient: float = _DEFAULT_GRAMS_PER_INGREDIENT,
) -> dict:
    """Best-effort per-serving macros for a recipe from its ingredient names.

    Returns {calories, protein_g, carb_g, fat_g, usda_status}. Macros are 0 when
    nothing resolves (usda_status failed/skipped) so callers can flag confidence.
    """
    servings = max(1, int(servings or 1))
    # LLM parse gives each line a real food name and quantity-aware grams
    # ("6 eggs" -> eggs/300g, "12 eggs" -> eggs/600g). Skipped without a USDA
    # key — nothing downstream would use the parse. USDA cache is keyed by the
    # parsed food, so every "N eggs" variant shares one cache entry.
    if config.usda_api_key and ingredients:
        parsed = parse_ingredients(conn, config, ingredients)
    else:
        parsed = [
            {"food": ing, "grams": grams_per_ingredient, "source": "heuristic"}
            for ing in ingredients or []
        ]

    totals = {"kcal": 0.0, "protein_g": 0.0, "carb_g": 0.0, "fat_g": 0.0}
    statuses = []
    matched_grams = total_grams = 0.0
    for p in parsed:
        if not p["food"]:
            continue
        per100 = lookup_ingredient_macros(conn, config, p["food"])
        statuses.append(per100["status"])
        total_grams += p["grams"]
        if per100["status"] == "success":
            matched_grams += p["grams"]
        factor = p["grams"] / 100.0
        totals["kcal"] += per100["kcal"] * factor
        totals["protein_g"] += per100["protein_g"] * factor
        totals["carb_g"] += per100["carb_g"] * factor
        totals["fat_g"] += per100["fat_g"] * factor

    matched = statuses.count("success")
    total = len(statuses)
    if not statuses:
        status = "skipped"
    elif matched and matched / total >= 0.5:
        # Enough of the recipe resolved to trust the estimate. Unresolved
        # ingredients contributed 0 kcal — extrapolate the matched grams over
        # them so a 10-ingredient dish with 6 hits isn't reported at 60% of
        # its real calories (that systematic undercount is what made "hit the
        # daily target" math unusable downstream).
        status = "success"
        if matched < total and matched_grams > 0:
            scale = total_grams / matched_grams
            totals = {k: v * scale for k, v in totals.items()}
    elif matched:
        status = "partial"  # under half resolved — too thin to trust
    elif all(s == "skipped" for s in statuses):
        status = "skipped"
    else:
        status = "failed"

    return {
        "calories": round(totals["kcal"] / servings, 1),
        "protein_g": round(totals["protein_g"] / servings, 1),
        "carb_g": round(totals["carb_g"] / servings, 1),
        "fat_g": round(totals["fat_g"] / servings, 1),
        "usda_status": status,
        "matched_ingredients": matched,
        "total_ingredients": total,
    }


# ---------------------------------------------------------------------------
# Persisting estimates into recipe markdown frontmatter
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^(---\s*\n)(.*?\n)(---\s*\n)", re.DOTALL)
_NUTRITION_BLOCK_RE = re.compile(r"^nutrition:[^\n]*\n(?:[ \t]+[^\n]*\n)*", re.MULTILINE)


def write_nutrition_frontmatter(
    path: str, macros: dict, confidence: str, estimated_at: str
) -> bool:
    """Write a `nutrition:` block into a recipe .md's frontmatter so the
    estimate survives DB rebuilds (the indexer reads it back into the
    nutrition table). Replaces any existing block; only the block is touched —
    the rest of the frontmatter is left byte-for-byte alone. Returns False
    (never raises) when the file is missing/unwritable or has no frontmatter.
    """
    block = (
        "nutrition:\n"
        f"  kcal: {macros['calories']}\n"
        f"  protein_g: {macros['protein_g']}\n"
        f"  carbs_g: {macros['carb_g']}\n"
        f"  fat_g: {macros['fat_g']}\n"
        f"  confidence: {confidence}\n"
        f"  estimated_at: {estimated_at}\n"
    )
    try:
        p = Path(path)
        text = p.read_text(encoding="utf-8")
        m = _FRONTMATTER_RE.match(text)
        if not m:
            return False
        open_fence, fm, close_fence = m.group(1), m.group(2), m.group(3)
        fm, n = _NUTRITION_BLOCK_RE.subn(block, fm, count=1)
        if n == 0:
            fm = fm + block
        p.write_text(open_fence + fm + close_fence + text[m.end():], encoding="utf-8")
        return True
    except OSError:
        return False


def scale_recipe(recipe_macros: dict, target_kcal: float,
                  min_mult: float = 0.25, max_mult: float = 4.0) -> dict:
    """Scale a recipe's per-serving macros to hit a target kcal.

    Returns {portion_multiplier, calories, protein_g, carb_g, fat_g}.
    Multiplier clamped to a sane band; 1.0 if base calories unknown.
    """
    base = float(recipe_macros.get("calories") or 0)
    if base <= 0 or not target_kcal:
        mult = 1.0
    else:
        mult = max(min_mult, min(max_mult, float(target_kcal) / base))
    return {
        "portion_multiplier": round(mult, 2),
        "calories": round(base * mult, 1),
        "protein_g": round(float(recipe_macros.get("protein_g") or 0) * mult, 1),
        "carb_g": round(float(recipe_macros.get("carb_g") or 0) * mult, 1),
        "fat_g": round(float(recipe_macros.get("fat_g") or 0) * mult, 1),
    }


def flag_off_target(actual_kcal: float, target_kcal: float, tolerance: float = 0.15) -> str | None:
    """Return 'under'/'over'/None if actual misses target by > tolerance."""
    if not target_kcal:
        return None
    delta = (actual_kcal - target_kcal) / target_kcal
    if delta < -tolerance:
        return "under"
    if delta > tolerance:
        return "over"
    return None
