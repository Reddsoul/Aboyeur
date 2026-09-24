"""Demographic nutrition engine — BMR/TDEE math + USDA macro lookup + scaling.

Pure math + a single outbound USDA call path (httpx). No LLM, no FastAPI imports,
so the ingest image can call into it at index time to pre-compute recipe macros.
"""
import json
import logging
import os
from pathlib import Path
from typing import Optional

import httpx

USDA_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"

_log = logging.getLogger(__name__)
_warned_no_key = False


def usda_api_key() -> str:
    """The USDA FoodData Central key, read at call time.

    Deliberately not a module-level constant: backend/main.py imports this
    module before `aboyeur/__init__` runs load_dotenv(), so an import-time
    os.getenv resolves to "" under `uvicorn backend.main:app` even when .env
    has the key. Reading lazily means the env is populated by the time the
    first lookup happens.
    """
    return (os.environ.get("USDA_API_KEY") or "").strip()

# USDA FoodData Central nutrient IDs (per 100 g).
_N_KCAL, _N_PROTEIN, _N_CARB, _N_FAT = 1008, 1003, 1005, 1004

# Activity multipliers for TDEE.
_ACTIVITY = {
    "sedentary": 1.2, "light": 1.375, "moderate": 1.55,
    "active": 1.725, "very_active": 1.9,
}

# Default per-meal macro split (protein/carb/fat by calories).
_SPLIT = {"protein": 0.30, "carb": 0.40, "fat": 0.30}
_KCAL_PER_G = {"protein": 4.0, "carb": 4.0, "fat": 9.0}

# Rough grams-per-ingredient-per-serving assumption when no better signal exists.
_DEFAULT_GRAMS_PER_INGREDIENT = 80.0

_DATA_DIR = Path(os.getenv("DB_PATH", "data/aboyeur.db")).resolve().parent
_CACHE_PATH = _DATA_DIR / "usda_cache.json"
_cache: Optional[dict] = None


# ---------------------------------------------------------------------------
# BMR / TDEE / targets
# ---------------------------------------------------------------------------

def bmr_mifflin(sex: str, age: int, height_cm: float, weight_kg: float) -> float:
    """Mifflin-St Jeor BMR (kcal/day)."""
    base = 10.0 * weight_kg + 6.25 * height_cm - 5.0 * age
    return base + (5.0 if (sex or "").lower().startswith("m") else -161.0)


def tdee(bmr: float, activity: str) -> float:
    """Total daily energy expenditure = BMR × activity factor."""
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

    Full anthropometric data -> BMR×activity (TDEE). Else the supplied daily_kcal.
    `member` is a dict from user_profile.json.
    """
    if all(member.get(k) is not None for k in ("sex", "age", "height_cm", "weight_kg")):
        b = bmr_mifflin(member["sex"], member["age"], member["height_cm"], member["weight_kg"])
        return tdee(b, member.get("activity", "moderate"))
    return float(member.get("daily_kcal") or 2000)


def household_targets(profile: dict) -> dict:
    """Per-person per-meal targets for the whole household.

    Returns {member_id: {label, daily_kcal, source, per_meal:{...}}}.
    """
    meals = profile.get("meals_per_day", 1)
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
# USDA lookup (cached, best-effort)
# ---------------------------------------------------------------------------

def _load_cache() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(_CACHE_PATH.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            _cache = {}
    return _cache


def _save_cache():
    if _cache is None:
        return
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(_cache, indent=0))
    except OSError:
        pass


def lookup_ingredient_macros(name: str) -> dict:
    """Per-100g macros for one ingredient via USDA. Cached on disk.

    Returns {kcal, protein_g, carb_g, fat_g, status}.
    status: success | failed | skipped (no API key).
    """
    key = (name or "").strip().lower()
    if not key:
        return {"kcal": 0, "protein_g": 0, "carb_g": 0, "fat_g": 0, "status": "failed"}

    cache = _load_cache()
    if key in cache:
        return cache[key]

    api_key = usda_api_key()
    if not api_key:
        global _warned_no_key
        if not _warned_no_key:
            _warned_no_key = True
            _log.warning(
                "USDA_API_KEY is unset — ingredient macro lookups will return "
                "status=skipped with zero macros. Set it in .env (or the "
                "deployment env) and restart; get a free key at "
                "https://fdc.nal.usda.gov/api-key-signup"
            )
        return {"kcal": 0, "protein_g": 0, "carb_g": 0, "fat_g": 0, "status": "skipped"}

    result = {"kcal": 0, "protein_g": 0, "carb_g": 0, "fat_g": 0, "status": "failed"}
    try:
        resp = httpx.get(
            USDA_URL,
            params={"query": key, "pageSize": 1, "api_key": api_key},
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

    cache[key] = result
    _save_cache()
    return result


def estimate_recipe_macros(ingredients: list[str], servings: int = 4,
                           grams_per_ingredient: float = _DEFAULT_GRAMS_PER_INGREDIENT) -> dict:
    """Best-effort per-serving macros for a recipe from its ingredient names.

    Returns {calories, protein_g, carb_g, fat_g, usda_status}. Macros are 0 when
    nothing resolves (usda_status failed/skipped) so callers can flag it.
    """
    servings = max(1, int(servings or 1))
    totals = {"kcal": 0.0, "protein_g": 0.0, "carb_g": 0.0, "fat_g": 0.0}
    statuses = []
    for ing in ingredients or []:
        per100 = lookup_ingredient_macros(ing)
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


def flag_off_target(actual_kcal: float, target_kcal: float, tolerance: float = 0.15) -> Optional[str]:
    """Return 'under'/'over'/None if actual misses target by > tolerance."""
    if not target_kcal:
        return None
    delta = (actual_kcal - target_kcal) / target_kcal
    if delta < -tolerance:
        return "under"
    if delta > tolerance:
        return "over"
    return None
