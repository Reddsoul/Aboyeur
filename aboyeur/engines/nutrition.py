"""BMR/TDEE math + USDA macro lookup + portion scaling.

Ported from the POC's backend/macro_engine.py (task.md §2 salvage list). The
math is unchanged; the USDA cache moved from a JSON file to the `usda_cache`
SQLite table so it lives in the one DB file instead of a second artifact.
"""

from __future__ import annotations

import json
import sqlite3
import time

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


def daily_kcal_for_member(member: dict) -> float:
    """Resolve a household member's daily kcal target.

    Full anthropometric data -> BMR x activity (TDEE). Else the supplied daily_kcal.
    """
    if all(member.get(k) is not None for k in ("sex", "age", "height_cm", "weight_kg")):
        b = bmr_mifflin(member["sex"], member["age"], member["height_cm"], member["weight_kg"])
        return tdee(b, member.get("activity", "moderate"))
    return float(member.get("daily_kcal") or 2000)


def household_targets(profile: dict) -> dict:
    """Per-person per-meal targets for the whole household.

    Returns {member_id: {label, daily_kcal, source, per_meal:{...}}}.
    """
    meals = profile.get("meals_per_day", 3)
    out = {}
    for m in profile.get("household", []):
        daily = daily_kcal_for_member(m)
        has_anthro = all(m.get(k) is not None for k in ("sex", "age", "height_cm", "weight_kg"))
        out[m["id"]] = {
            "label": m.get("label", m["id"]),
            "daily_kcal": round(daily),
            "source": "bmr" if has_anthro else "fallback",
            "per_meal": per_meal_target(daily, meals),
        }
    return out


# ---------------------------------------------------------------------------
# USDA lookup (cached in SQLite, best-effort)
# ---------------------------------------------------------------------------

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

    if not config.usda_api_key:
        result = {"kcal": 0, "protein_g": 0, "carb_g": 0, "fat_g": 0, "status": "skipped"}
    else:
        result = {"kcal": 0, "protein_g": 0, "carb_g": 0, "fat_g": 0, "status": "failed"}
        try:
            resp = httpx.get(
                USDA_URL,
                params={"query": key, "pageSize": 1, "api_key": config.usda_api_key},
                timeout=15,
            )
            resp.raise_for_status()
            foods = resp.json().get("foods") or []
            if foods:
                nutrients = {n.get("nutrientId"): n.get("value", 0)
                             for n in foods[0].get("foodNutrients", [])}
                result = {
                    "kcal": float(nutrients.get(_N_KCAL, 0) or 0),
                    "protein_g": float(nutrients.get(_N_PROTEIN, 0) or 0),
                    "carb_g": float(nutrients.get(_N_CARB, 0) or 0),
                    "fat_g": float(nutrients.get(_N_FAT, 0) or 0),
                    "status": "success",
                }
        except (httpx.HTTPError, ValueError, KeyError):
            result["status"] = "failed"

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
    totals = {"kcal": 0.0, "protein_g": 0.0, "carb_g": 0.0, "fat_g": 0.0}
    statuses = []
    for ing in ingredients or []:
        per100 = lookup_ingredient_macros(conn, config, ing)
        statuses.append(per100["status"])
        factor = grams_per_ingredient / 100.0
        totals["kcal"] += per100["kcal"] * factor
        totals["protein_g"] += per100["protein_g"] * factor
        totals["carb_g"] += per100["carb_g"] * factor
        totals["fat_g"] += per100["fat_g"] * factor

    if not statuses:
        status = "skipped"
    elif "success" in statuses:
        status = "success"
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
    }


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
