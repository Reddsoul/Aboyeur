"""Recipe favoriting — the Library UI's star toggle.

Not an LLM tool (not in tools/schemas.py, never touches a chat turn) —
called directly by backend/main.py's REST routes for the catalog UI.
"""

from __future__ import annotations

import sqlite3
import time


def add_favorite(conn: sqlite3.Connection, user_id: int, recipe_id: int) -> None:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    conn.execute(
        "INSERT INTO favorites (user_id, recipe_id, created_at) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id, recipe_id) DO NOTHING",
        (user_id, recipe_id, now),
    )
    conn.commit()


def remove_favorite(conn: sqlite3.Connection, user_id: int, recipe_id: int) -> None:
    conn.execute(
        "DELETE FROM favorites WHERE user_id = ? AND recipe_id = ?", (user_id, recipe_id)
    )
    conn.commit()


def list_favorite_ids(conn: sqlite3.Connection, user_id: int) -> set[int]:
    rows = conn.execute(
        "SELECT recipe_id FROM favorites WHERE user_id = ?", (user_id,)
    ).fetchall()
    return {r["recipe_id"] for r in rows}
