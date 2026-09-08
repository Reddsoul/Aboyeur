"""Schema migrations, run in order: single-household -> multi-tenant (one
account owns a household), then multi-tenant -> practitioner (an account owns
many clients, and each client owns the household data).

Idempotent — every step checks the current shape before touching it, so it's
safe on a fresh DB (nothing to do, db.init_db() already created the final
shape) or on either older one. Run by hand: `uv run python -m aboyeur.migrate`.
"""

from __future__ import annotations

import json
import sqlite3
import time

# The existing single household's data folds under this account. Change
# before running if a different owner is wanted.
SEED_USER_EMAIL = "thepartycaravan@gmail.com"


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _is_pre_multitenant(conn: sqlite3.Connection, table: str) -> bool:
    """True only for the original single-household shape.

    Guarding on `"user_id" not in columns` is wrong now that a third shape
    exists: a client-keyed table has no user_id either, so that test read
    "needs migrating" and cheerfully rebuilt an already-migrated table back
    into the user_id shape, dropping its rows. The real marker is the absence
    of BOTH scoping columns.
    """
    columns = _column_names(conn, table)
    return not ({"user_id", "client_id"} & columns)


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
    if not _table_exists(conn, "profile") or not _is_pre_multitenant(conn, "profile"):
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
    if not _table_exists(conn, "pantry") or not _is_pre_multitenant(conn, "pantry"):
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
    if not _table_exists(conn, "menus") or not _is_pre_multitenant(conn, "menus"):
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


def _add_recipe_classification_columns(conn: sqlite3.Connection) -> None:
    """course / serving_est_g / meal_complete / classified_at on recipes —
    filled by aboyeur/classify_recipes.py, read by the menu path. Plain ADD
    COLUMNs with defaults; the classifier backfills the real values."""
    if not _table_exists(conn, "recipes"):
        return
    existing = _column_names(conn, "recipes")
    stmts = {
        "course": "ALTER TABLE recipes ADD COLUMN course TEXT NOT NULL DEFAULT 'unknown'",
        "serving_est_g": "ALTER TABLE recipes ADD COLUMN serving_est_g REAL",
        "meal_complete": "ALTER TABLE recipes ADD COLUMN meal_complete INTEGER NOT NULL DEFAULT 0",
        "classified_at": "ALTER TABLE recipes ADD COLUMN classified_at TEXT",
    }
    for col, sql in stmts.items():
        if col not in existing:
            conn.execute(sql)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_recipes_course ON recipes(course)")
    conn.commit()


def _add_account_columns(conn: sqlite3.Connection) -> None:
    """The account screen needs somewhere to put the practitioner's own name.
    Plain ADD COLUMNs: nullable, no default, nothing to backfill."""
    if not _table_exists(conn, "users"):
        return
    existing = _column_names(conn, "users")
    for column in ("name", "practice"):
        if column not in existing:
            conn.execute(f"ALTER TABLE users ADD COLUMN {column} TEXT")

    _ensure_clients_is_self(conn)
    conn.commit()


def _ensure_clients_is_self(conn: sqlite3.Connection) -> None:
    """clients.is_self marks the practitioner's own record. On an account that
    predates the flag it's the client the household was folded into — the
    oldest one. Callable mid-transaction: the fold itself writes this column,
    so it has to exist before that runs."""
    if not _table_exists(conn, "clients") or "is_self" in _column_names(conn, "clients"):
        return
    conn.execute("ALTER TABLE clients ADD COLUMN is_self INTEGER NOT NULL DEFAULT 0")
    conn.execute(
        "UPDATE clients SET is_self = 1 WHERE id IN "
        "(SELECT MIN(id) FROM clients GROUP BY user_id)"
    )


def _client_name_for(conn: sqlite3.Connection, user_id: int) -> str:
    """Name the auto-created client after the household's primary member, so
    an existing user opens the new UI and sees a person. With no household to
    read, it's the account owner's own client — "Me"."""
    row = conn.execute(
        "SELECT data_json FROM profile_old WHERE user_id = ?", (user_id,)
    ).fetchone()
    if row is not None:
        try:
            household = json.loads(row["data_json"]).get("household") or []
            label = (household[0].get("label") or "").strip() if household else ""
            if label:
                return label
        except (ValueError, AttributeError, IndexError, TypeError):
            pass  # a malformed profile is not a reason to fail the migration
    return "Me"


def _self_client_ids(conn: sqlite3.Connection) -> dict[int, int]:
    """One client per user that owns any per-person rows, keyed by user id.

    Reads from the *_old tables, so it must run while they still exist.
    """
    user_ids: set[int] = set()
    for table in ("profile_old", "pantry_old", "menus_old", "kits_old", "favorites_old"):
        if _table_exists(conn, table):
            user_ids |= {r["user_id"] for r in conn.execute(f"SELECT DISTINCT user_id FROM {table}")}

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    out: dict[int, int] = {}
    for user_id in sorted(user_ids):
        existing = conn.execute(
            "SELECT id FROM clients WHERE user_id = ? ORDER BY id LIMIT 1", (user_id,)
        ).fetchone()
        if existing is not None:
            out[user_id] = existing["id"]
            continue
        # The folded household is the practitioner's own client (is_self).
        cur = conn.execute(
            "INSERT INTO clients (user_id, name, status, is_self, created_at, updated_at) "
            "VALUES (?, ?, 'active', 1, ?, ?)",
            (user_id, _client_name_for(conn, user_id), now, now),
        )
        out[user_id] = cur.lastrowid
    return out


# Each entry: table -> (columns carried over, CREATE for the new shape).
_CLIENT_SCOPED = {
    "profile": (
        ("data_json", "updated_at"),
        "CREATE TABLE profile (client_id INTEGER PRIMARY KEY REFERENCES clients(id) "
        "ON DELETE CASCADE, data_json TEXT NOT NULL, updated_at TEXT NOT NULL)",
    ),
    "pantry": (
        ("item", "qty", "added_at", "expires_at"),
        "CREATE TABLE pantry (client_id INTEGER NOT NULL REFERENCES clients(id) "
        "ON DELETE CASCADE, item TEXT NOT NULL, qty TEXT, added_at TEXT NOT NULL, "
        "expires_at TEXT, PRIMARY KEY (client_id, item))",
    ),
    "menus": (
        ("id", "week_of", "days_json", "status", "created_at"),
        "CREATE TABLE menus (id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL "
        "REFERENCES clients(id) ON DELETE CASCADE, week_of TEXT NOT NULL, "
        "days_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft', "
        "created_at TEXT NOT NULL)",
    ),
    "kits": (
        ("id", "data_json", "created_at"),
        "CREATE TABLE kits (id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL "
        "REFERENCES clients(id) ON DELETE CASCADE, data_json TEXT NOT NULL, "
        "created_at TEXT NOT NULL)",
    ),
    # A star says "this recipe suits this person" — a per-client judgement,
    # so favorites move with the rest rather than staying on the account.
    "favorites": (
        ("recipe_id", "created_at"),
        "CREATE TABLE favorites (client_id INTEGER NOT NULL REFERENCES clients(id) "
        "ON DELETE CASCADE, recipe_id INTEGER NOT NULL REFERENCES recipes(id) "
        "ON DELETE CASCADE, created_at TEXT NOT NULL, PRIMARY KEY (client_id, recipe_id))",
    ),
}


def _migrate_chat_sessions(conn: sqlite3.Connection) -> None:
    """Give the first round's per-client transcripts a conversation to live in.

    chat_messages started out keyed by client_id — one endless thread per
    person, which meant "new chat" could only be spelled "delete the old one".
    Each client's existing messages fold into a single session so nothing is
    lost, and new conversations sit alongside it.
    """
    if not _table_exists(conn, "chat_messages"):
        return
    if "client_id" not in _column_names(conn, "chat_messages"):
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS chat_sessions ("
            "id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL REFERENCES clients(id) "
            "ON DELETE CASCADE, title TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute("ALTER TABLE chat_messages RENAME TO chat_messages_old")
        conn.execute(
            "CREATE TABLE chat_messages (id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL "
            "REFERENCES chat_sessions(id) ON DELETE CASCADE, role TEXT NOT NULL, "
            "content TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for row in conn.execute(
            "SELECT client_id, MIN(created_at) first_at, MAX(created_at) last_at "
            "FROM chat_messages_old GROUP BY client_id"
        ).fetchall():
            cur = conn.execute(
                "INSERT INTO chat_sessions (client_id, title, created_at, updated_at) "
                "VALUES (?, 'Earlier conversation', ?, ?)",
                (row["client_id"], row["first_at"] or now, row["last_at"] or now),
            )
            conn.execute(
                "INSERT INTO chat_messages (id, session_id, role, content, created_at) "
                "SELECT id, ?, role, content, created_at FROM chat_messages_old WHERE client_id = ?",
                (cur.lastrowid, row["client_id"]),
            )
        conn.execute("DROP TABLE chat_messages_old")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _migrate_to_clients(conn: sqlite3.Connection) -> None:
    """Re-key the per-person tables from user_id to client_id, folding each
    account's household into one client of its own.

    Runs inside BEGIN IMMEDIATE. Two processes can absolutely start at once —
    uvicorn's --reload restarts the app on every edit, and each start calls
    migrate() — and without the write lock their renames interleave into a
    half-migrated database (observed: `profile` left in the old shape while
    `profile_old` held the new one). Under the lock the loser waits, then
    finds nothing pending and no-ops.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        _ensure_clients_is_self(conn)
        pending = [
            t for t in _CLIENT_SCOPED
            if _table_exists(conn, t) and "user_id" in _column_names(conn, t)
        ]
        if not pending:
            conn.execute("ROLLBACK")
            return

        for table in pending:
            # A leftover *_old from an interrupted run is stale by definition:
            # the live table still has the old shape, so nothing was carried
            # over from it. Drop it rather than colliding on the rename.
            if _table_exists(conn, f"{table}_old"):
                conn.execute(f"DROP TABLE {table}_old")
            conn.execute(f"ALTER TABLE {table} RENAME TO {table}_old")

        client_ids = _self_client_ids(conn)

        for table in pending:
            columns, create_sql = _CLIENT_SCOPED[table]
            conn.execute(create_sql)
            cols = ", ".join(columns)
            placeholders = ", ".join("?" * (len(columns) + 1))
            for row in conn.execute(f"SELECT user_id, {cols} FROM {table}_old"):
                client_id = client_ids.get(row["user_id"])
                if client_id is None:
                    continue  # orphan row for a user that no longer exists
                conn.execute(
                    f"INSERT INTO {table} (client_id, {cols}) VALUES ({placeholders})",
                    (client_id, *(row[c] for c in columns)),
                )
            conn.execute(f"DROP TABLE {table}_old")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def migrate(conn: sqlite3.Connection) -> int:
    """Run every table migration. Returns the seed user's id."""
    if not _table_exists(conn, "users"):
        raise RuntimeError("users table missing — call aboyeur.db.init_db(conn) first")
    seed_user_id = _ensure_seed_user(conn)
    _migrate_profile(conn, seed_user_id)
    _migrate_pantry(conn, seed_user_id)
    _migrate_menus(conn, seed_user_id)
    _migrate_to_clients(conn)
    _add_account_columns(conn)
    _add_recipe_classification_columns(conn)
    _migrate_chat_sessions(conn)
    # Deliberately not in db.py's static SCHEMA — see the comment above the
    # menus table there. Safe to run every time: the scoping column is
    # guaranteed to exist by this point, whether the table was just rebuilt
    # above or was already fresh.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_menus_client ON menus(client_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_kits_client ON kits(client_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_clients_user ON clients(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_session "
                 "ON chat_messages(session_id, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_sessions_client "
                 "ON chat_sessions(client_id, updated_at)")
    conn.execute("DROP INDEX IF EXISTS idx_chat_messages_client")
    # The pre-client indexes are dead once the tables are re-keyed; SQLite
    # drops them with the old tables, but an interrupted run could leave one.
    conn.execute("DROP INDEX IF EXISTS idx_menus_user")
    conn.execute("DROP INDEX IF EXISTS idx_kits_user")
    conn.commit()
    return seed_user_id


def main() -> None:
    from aboyeur.config import Config
    from aboyeur.db import connect, init_db, schema_lock

    config = Config.load()
    conn = connect(config.db_path)
    with schema_lock(config.db_path):
        init_db(conn)
        seed_user_id = migrate(conn)
    print(f"migrated {config.db_path} — seed user: {SEED_USER_EMAIL} (id {seed_user_id})")


if __name__ == "__main__":
    main()
