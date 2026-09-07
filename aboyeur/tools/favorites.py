"""Recipe favoriting — the Library UI's star toggle, per client.

Not an LLM tool (not in tools/schemas.py, never touches a chat turn) —
called directly by backend/main.py's REST routes for the catalog UI.
"""

from __future__ import annotations

import sqlite3
import time


def add_favorite(conn: sqlite3.Connection, client_id: int, recipe_id: int) -> None:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    conn.execute(
        "INSERT INTO favorites (client_id, recipe_id, created_at) VALUES (?, ?, ?) "
        "ON CONFLICT(client_id, recipe_id) DO NOTHING",
        (client_id, recipe_id, now),
    )
    conn.commit()


def remove_favorite(conn: sqlite3.Connection, client_id: int, recipe_id: int) -> None:
    conn.execute(
        "DELETE FROM favorites WHERE client_id = ? AND recipe_id = ?", (client_id, recipe_id)
    )
    conn.commit()


def list_favorite_ids(conn: sqlite3.Connection, client_id: int) -> set[int]:
    rows = conn.execute(
        "SELECT recipe_id FROM favorites WHERE client_id = ?", (client_id,)
    ).fetchall()
    return {r["recipe_id"] for r in rows}
