"""get_recipe tool: verbatim markdown, read off RECIPES_DIR.

Never lets the LLM rewrite ingredients or steps. If scale_to_servings is
given, only a scale multiplier is returned — rewriting quantities in the
ingredient list needs quantity parsing, which is Phase 4 scope.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def get_recipe(conn: sqlite3.Connection, recipe_id: int, scale_to_servings: int | None = None) -> dict:
    row = conn.execute(
        "SELECT path, title, servings FROM recipes WHERE id = ?", (recipe_id,)
    ).fetchone()
    if row is None:
        return {"error": f"no recipe with id {recipe_id}"}

    path = Path(row["path"])
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"error": f"could not read recipe file (mount issue?): {e}"}

    result = {"id": recipe_id, "title": row["title"], "path": row["path"], "servings": row["servings"], "markdown": text}
    if scale_to_servings and row["servings"]:
        result["scale_multiplier"] = round(scale_to_servings / row["servings"], 2)
        result["scale_note"] = "multiplier only — ingredient quantities are not auto-rewritten (Phase 4)"
    return result
