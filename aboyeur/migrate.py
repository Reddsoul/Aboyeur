"""One-time migration: single-household schema -> multi-tenant schema.

Idempotent — checks each table's current shape before touching it, so it's
safe to run against a fresh DB (nothing to do, db.init_db() already created
the new shape) or an existing one (rebuilds profile/pantry/menus under a
seed user). Run by hand once: `uv run python -m aboyeur.migrate`.
"""

from __future__ import annotations

import sqlite3
import time

# The existing single household's data folds under this account. Change
# before running if a different owner is wanted.
SEED_USER_EMAIL = "thepartycaravan@gmail.com"


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _ensure_seed_user(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT id FROM users WHERE email = ?", (SEED_USER_EMAIL,)).fetchone()
    if row is not None:
        return row["id"]
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cur = conn.execute(
        "INSERT INTO users (email, password_hash, created_at) VALUES (?, NULL, ?)",
        (SEED_USER_EMAIL, now),
    )
    conn.commit()
    return cur.lastrowid


def _migrate_profile(conn: sqlite3.Connection, seed_user_id: int) -> None:
    if not _table_exists(conn, "profile") or "user_id" in _column_names(conn, "profile"):
        return
    conn.execute("ALTER TABLE profile RENAME TO profile_old")
    conn.execute(
        "CREATE TABLE profile (user_id INTEGER PRIMARY KEY REFERENCES users(id) "
        "ON DELETE CASCADE, data_json TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    old = conn.execute("SELECT data_json, updated_at FROM profile_old WHERE id = 1").fetchone()
    if old is not None:
        conn.execute(
            "INSERT INTO profile (user_id, data_json, updated_at) VALUES (?, ?, ?)",
            (seed_user_id, old["data_json"], old["updated_at"]),
        )
    conn.execute("DROP TABLE profile_old")
    conn.commit()


def _migrate_pantry(conn: sqlite3.Connection, seed_user_id: int) -> None:
    if not _table_exists(conn, "pantry") or "user_id" in _column_names(conn, "pantry"):
        return
    conn.execute("ALTER TABLE pantry RENAME TO pantry_old")
    conn.execute(
        "CREATE TABLE pantry (user_id INTEGER NOT NULL REFERENCES users(id) "
        "ON DELETE CASCADE, item TEXT NOT NULL, qty TEXT, added_at TEXT NOT NULL, "
        "expires_at TEXT, PRIMARY KEY (user_id, item))"
    )
    for row in conn.execute("SELECT item, qty, added_at, expires_at FROM pantry_old"):
        conn.execute(
            "INSERT INTO pantry (user_id, item, qty, added_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (seed_user_id, row["item"], row["qty"], row["added_at"], row["expires_at"]),
        )
    conn.execute("DROP TABLE pantry_old")
    conn.commit()


def _migrate_menus(conn: sqlite3.Connection, seed_user_id: int) -> None:
    if not _table_exists(conn, "menus") or "user_id" in _column_names(conn, "menus"):
        return
    conn.execute("ALTER TABLE menus RENAME TO menus_old")
    conn.execute(
        "CREATE TABLE menus (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL "
        "REFERENCES users(id) ON DELETE CASCADE, week_of TEXT NOT NULL, "
        "days_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft', "
        "created_at TEXT NOT NULL)"
    )
    for row in conn.execute("SELECT id, week_of, days_json, status, created_at FROM menus_old"):
        conn.execute(
            "INSERT INTO menus (id, user_id, week_of, days_json, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (row["id"], seed_user_id, row["week_of"], row["days_json"],
             row["status"], row["created_at"]),
        )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_menus_user ON menus(user_id)")
    conn.execute("DROP TABLE menus_old")
    conn.commit()


def migrate(conn: sqlite3.Connection) -> int:
    """Run every table migration. Returns the seed user's id."""
    if not _table_exists(conn, "users"):
        raise RuntimeError("users table missing — call aboyeur.db.init_db(conn) first")
    seed_user_id = _ensure_seed_user(conn)
    _migrate_profile(conn, seed_user_id)
    _migrate_pantry(conn, seed_user_id)
    _migrate_menus(conn, seed_user_id)
    # Deliberately not in db.py's static SCHEMA — see the comment above the
    # menus table there. Safe to run every time: menus.user_id is guaranteed
    # to exist by this point, whether the table was just rebuilt above or
    # was already fresh.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_menus_user ON menus(user_id)")
    # kits is a post-multi-tenant table — it never exists in the old shape,
    # so its index would be safe in db.py's SCHEMA too. It lives here anyway
    # so every user-scoping index is created in one place.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_kits_user ON kits(user_id)")
    conn.commit()
    return seed_user_id


def main() -> None:
    from aboyeur.config import Config
    from aboyeur.db import connect, init_db

    config = Config.load()
    conn = connect(config.db_path)
    init_db(conn)
    seed_user_id = migrate(conn)
    print(f"migrated {config.db_path} — seed user: {SEED_USER_EMAIL} (id {seed_user_id})")


if __name__ == "__main__":
    main()
