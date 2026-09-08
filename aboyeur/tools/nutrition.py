"""estimate_nutrition tool: per-serving macros for recipes, USDA-backed, cached lazily."""

from __future__ import annotations

import json
import logging
import sqlite3
import time

from aboyeur.config import Config
from aboyeur.engines import nutrition as nutrition_engine

logger = logging.getLogger("aboyeur")


def estimate_nutrition(conn: sqlite3.Connection, config: Config, recipe_ids: list[int]) -> dict[int, dict]:
    """Per-serving kcal/macros per recipe. Cached in the `nutrition` table;
    computed and stored on first request. Low-confidence estimates (no USDA
    key, or lookups failed) are flagged rather than silently trusted.
    """
    out: dict[int, dict] = {}
    for rid in recipe_ids:
        row = conn.execute(
            "SELECT kcal, protein_g, carbs_g, fat_g, fiber_g, confidence "
            "FROM nutrition WHERE recipe_id = ?",
            (rid,),
        ).fetchone()
        if row is not None:
            out[rid] = {
                "kcal": row["kcal"], "protein_g": row["protein_g"],
                "carb_g": row["carbs_g"], "fat_g": row["fat_g"],
                "fiber_g": row["fiber_g"], "confidence": row["confidence"],
            }
            continue

        rrow = conn.execute(
            "SELECT ingredients_json, servings, path FROM recipes WHERE id = ?", (rid,)
        ).fetchone()
        if rrow is None:
            out[rid] = {"error": "recipe not found"}
            continue

        ingredients = json.loads(rrow["ingredients_json"])
        servings = rrow["servings"] or 4
        macros = nutrition_engine.estimate_recipe_macros(conn, config, ingredients, servings)
        confidence = "medium" if macros["usda_status"] == "success" else "low"

        # Same reasoning as the usda_cache table (engines/nutrition.py): a
        # "low" confidence estimate is a statement about missing/failed
        # lookups, not about the recipe. Persisting it would freeze a
        # recipe at 0 kcal forever even after ingredient lookups start
        # succeeding (e.g. once a USDA key is added).
        if confidence != "low":
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            conn.execute(
                """INSERT INTO nutrition (recipe_id, kcal, protein_g, carbs_g, fat_g, fiber_g, per_serving, confidence, estimated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                   ON CONFLICT(recipe_id) DO UPDATE SET kcal=excluded.kcal, protein_g=excluded.protein_g,
                   carbs_g=excluded.carbs_g, fat_g=excluded.fat_g, fiber_g=excluded.fiber_g,
                   confidence=excluded.confidence, estimated_at=excluded.estimated_at""",
                (rid, macros["calories"], macros["protein_g"], macros["carb_g"], macros["fat_g"],
                 macros.get("fiber_g", 0), confidence, now),
            )
            conn.commit()
            # Persist into the recipe .md too, so the estimate survives DB
            # rebuilds — the indexer reads the nutrition block back into the
            # table. Best-effort: an unmounted NAS never fails the tool call.
            if not nutrition_engine.write_nutrition_frontmatter(
                rrow["path"], macros, confidence, now
            ):
                logger.warning("couldn't write nutrition frontmatter to %s", rrow["path"])
        out[rid] = {
            "kcal": macros["calories"], "protein_g": macros["protein_g"],
            "carb_g": macros["carb_g"], "fat_g": macros["fat_g"],
            "fiber_g": macros.get("fiber_g", 0), "confidence": confidence,
        }
    return out
