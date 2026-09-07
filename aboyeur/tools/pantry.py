"""manage_pantry tool."""

from __future__ import annotations

import sqlite3
import time


def manage_pantry(
    conn: sqlite3.Connection,
    client_id: int,
    action: str,
    items: list[dict] | list[str] | None = None,
) -> list[dict]:
    """action: list | add | remove | clear.

    For add, items may be plain names or {"item", "qty", "expires_at"} dicts.
    Always returns the resulting pantry state, scoped to client_id.
    """
    items = items or []
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    if action == "add":
        for it in items:
            if isinstance(it, str):
                name, qty, expires = it, None, None
            else:
                name, qty, expires = it.get("item"), it.get("qty"), it.get("expires_at")
            conn.execute(
                "INSERT INTO pantry (client_id, item, qty, added_at, expires_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(client_id, item) DO UPDATE SET qty=excluded.qty, expires_at=excluded.expires_at",
                (client_id, name.lower().strip(), qty, now, expires),
            )
        conn.commit()
    elif action == "remove":
        for it in items:
            name = it if isinstance(it, str) else it.get("item")
            conn.execute(
                "DELETE FROM pantry WHERE client_id = ? AND item = ?",
                (client_id, name.lower().strip()),
            )
        conn.commit()
    elif action == "clear":
        conn.execute("DELETE FROM pantry WHERE client_id = ?", (client_id,))
        conn.commit()
    elif action != "list":
        raise ValueError(f"unknown pantry action: {action!r}")

    rows = conn.execute(
        "SELECT item, qty, added_at, expires_at FROM pantry WHERE client_id = ? ORDER BY item",
        (client_id,),
    ).fetchall()
    return [dict(r) for r in rows]
