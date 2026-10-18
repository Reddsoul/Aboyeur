"""estimate_nutrition tool: per-serving macros for recipes, USDA-backed, cached lazily."""

from __future__ import annotations

import json
import logging
import sqlite3
import time

from aboyeur.config import Config
from aboyeur.engines import nutrition as nutrition_engine

logger = logging.getLogger("aboyeur")


def estimate_nutrition(
    conn: sqlite3.Connection, config: Config, recipe_ids: list[int],
    *, write_frontmatter: bool = True, raise_on_rate_limit: bool = False,
) -> dict[int, dict]:
    """Per-serving kcal/macros per recipe. Cached in the `nutrition` table;
    computed and stored on first request. Low-confidence estimates (no USDA
    key, or lookups failed) are flagged rather than silently trusted.

    write_frontmatter / raise_on_rate_limit: batch-job knobs (see
    aboyeur/estimate_nutrition_batch.py); the agent path uses the defaults.
    """
    out: dict[int, dict] = {}
    for rid in recipe_ids:
        effective = nutrition_engine.get_effective_macros(conn, rid)
        if effective is not None:
            out[rid] = effective
            continue

        rrow = conn.execute(
            "SELECT ingredients_json, servings, path, serving_est_g FROM recipes WHERE id = ?",
            (rid,),
        ).fetchone()
        if rrow is None:
            out[rid] = {"error": "recipe not found"}
            continue

        ingredients = json.loads(rrow["ingredients_json"])
        # Stated servings win; otherwise derived from total grams and the
        # course's plate size (engines/nutrition.py derive_servings), else 4.
        macros = nutrition_engine.estimate_recipe_macros(
            conn, config, ingredients, rrow["servings"], serving_est_g=rrow["serving_est_g"],
            raise_on_rate_limit=raise_on_rate_limit,
        )
        confidence = "medium" if macros["usda_status"] == "success" else "low"
        servings_est = macros["servings_used"] if macros["servings_source"] == "estimated" else None

        # Same reasoning as the usda_cache table (engines/nutrition.py): a
        # "low" confidence estimate is a statement about missing/failed
        # lookups, not about the recipe. Persisting it would freeze a
        # recipe at 0 kcal forever even after ingredient lookups start
        # succeeding (e.g. once a USDA key is added).
        if confidence != "low":
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            conn.execute(
                """INSERT INTO nutrition (recipe_id, kcal, protein_g, carbs_g, fat_g, fiber_g, per_serving, confidence, estimated_at, servings_est)
                   VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                   ON CONFLICT(recipe_id) DO UPDATE SET kcal=excluded.kcal, protein_g=excluded.protein_g,
                   carbs_g=excluded.carbs_g, fat_g=excluded.fat_g, fiber_g=excluded.fiber_g,
                   confidence=excluded.confidence, estimated_at=excluded.estimated_at,
                   servings_est=excluded.servings_est""",
                (rid, macros["calories"], macros["protein_g"], macros["carb_g"], macros["fat_g"],
                 macros.get("fiber_g", 0), confidence, now, servings_est),
            )
            conn.commit()
            # Persist into the recipe .md too, so the estimate survives DB
            # rebuilds — the indexer reads the nutrition block back into the
            # table. Best-effort: an unmounted NAS never fails the tool call.
            if write_frontmatter and not nutrition_engine.write_nutrition_frontmatter(
                rrow["path"], macros, confidence, now
            ):
                logger.warning("couldn't write nutrition frontmatter to %s", rrow["path"])
        out[rid] = {
            "kcal": macros["calories"], "protein_g": macros["protein_g"],
            "carb_g": macros["carb_g"], "fat_g": macros["fat_g"],
            "fiber_g": macros.get("fiber_g", 0), "confidence": confidence,
            "servings_est": servings_est,
        }
    return out


def set_recipe_macro_override(
    conn: sqlite3.Connection, recipe_id: int, macros: dict | None, clear: bool = False,
) -> dict:
    """Manually correct one recipe's per-serving macros (feature-spec-2026-09-26.md
    #1) — global to the recipe, seen by every client that plans with it, the
    same "the calc was just wrong" scope as kit.py's component-level
    macros_override. `macros`: {kcal, protein_g, carb_g, fat_g, fiber_g?} —
    fields you omit are treated as 0, not "leave the calculated value". Set
    `clear=True` to drop the override and go back to the calculated estimate
    (the calculated row underneath was never touched, so nothing recomputes).

    Every call site that reads macros (menu scoring, the day solver, the PDF)
    goes through estimate_nutrition -> engines.nutrition.get_effective_macros,
    so this is the one place that needs to know an override exists.
    """
    row = conn.execute("SELECT id FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    if row is None:
        return {"error": f"no recipe with id {recipe_id}"}

    if clear:
        conn.execute(
            "UPDATE nutrition SET override_json = NULL, is_override = 0 WHERE recipe_id = ?",
            (recipe_id,),
        )
        conn.commit()
        return {"recipe_id": recipe_id, "is_override": False}

    validated = nutrition_engine.validated_recipe_macros(macros or {})
    if validated is None:
        return {
            "error": (
                "macro override rejected — kcal must roughly agree with "
                "4*protein + 4*carb + 9*fat, and every value must be >= 0"
            )
        }

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    override_json = json.dumps(validated)
    conn.execute(
        """INSERT INTO nutrition (recipe_id, per_serving, confidence, override_json, is_override)
           VALUES (?, 1, 'override', ?, 1)
           ON CONFLICT(recipe_id) DO UPDATE SET override_json = excluded.override_json,
           is_override = 1""",
        (recipe_id, override_json),
    )
    conn.commit()
    return {"recipe_id": recipe_id, "is_override": True, "macros": validated}
