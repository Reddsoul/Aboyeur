"""get_shopping_list tool."""

from __future__ import annotations

import json
import sqlite3

from aboyeur.engines import shopping as shopping_engine
from aboyeur.tools.profile import get_profile


def get_shopping_list(conn: sqlite3.Connection, menu_id: int | None = None) -> dict:
    """Consolidated grocery list for a menu, grouped by store section.

    menu_id defaults to the most recently created menu if omitted.
    Pantry items are marked have=True, not removed — the LLM should still
    show what's needed vs. already on hand.
    """
    if menu_id is not None:
        row = conn.execute("SELECT days_json FROM menus WHERE id = ?", (menu_id,)).fetchone()
    else:
        row = conn.execute("SELECT days_json FROM menus ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return {"error": f"no menu with id {menu_id}" if menu_id is not None else "no menu exists yet — call plan_menu first"}

    plan = json.loads(row["days_json"])
    ids = [d["recipe_id"] for d in plan.get("days", [])]
    if not ids:
        return {"sections": {}, "total_items": 0, "to_buy": 0}

    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT ingredients_json FROM recipes WHERE id IN ({placeholders})", ids,
    ).fetchall()
    picks = [{"ingredients": json.loads(r["ingredients_json"])} for r in rows]

    pantry_rows = conn.execute("SELECT item FROM pantry").fetchall()
    pantry = {r["item"] for r in pantry_rows}

    profile = get_profile(conn)
    dislikes = profile.get("dislikes", [])

    return shopping_engine.build_shopping_list(picks, pantry, dislikes)
