"""Schema migrations, run in order: single-household -> multi-tenant (one
account owns a household), then multi-tenant -> practitioner (an account owns
many clients, and each client owns the household data).

Idempotent — every step checks the current shape before touching it, so it's
safe on a fresh DB (nothing to do, db.init_db() already created the final
shape) or on either older one. Run by hand: `uv run python -m aboyeur.migrate`.

The steps are forward-only: two of them (rebuilding a table, encrypting rows
in place) can't be undone by running code. So migrate() refuses to do that
kind of work without first writing a snapshot it can be restored from.

A schema version table is added to track the migration state from the outside.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from aboyeur.crypto import encrypt_field, is_encrypted_field

# The existing single household's data folds under this account. Change
# before running if a different owner is wanted.
SEED_USER_EMAIL = "thepartycaravan@gmail.com"

# Stamped into schema_version once migrate() completes. Bump it in the same
# commit as any new step, so a database says from the outside which shape it
# is in — `sqlite3 aboyeur.db "SELECT * FROM schema_version"` — without
# inferring it from PRAGMA table_info.
SCHEMA_VERSION = 2


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


def _add_nutrition_servings_est(conn: sqlite3.Connection) -> None:
    """nutrition.servings_est — the derived serving count behind a
    per-serving estimate for a recipe with no stated servings (see
    engines/nutrition.py derive_servings). Nullable, nothing to backfill."""
    if not _table_exists(conn, "nutrition"):
        return
    if "servings_est" not in _column_names(conn, "nutrition"):
        conn.execute("ALTER TABLE nutrition ADD COLUMN servings_est INTEGER")
        conn.commit()


def _add_nutrition_override_columns(conn: sqlite3.Connection) -> None:
    """Manual macro correction (feature-spec-2026-09-26.md #1). Plain ADD
    COLUMNs, defaulted off, nothing to backfill."""
    if not _table_exists(conn, "nutrition"):
        return
    existing = _column_names(conn, "nutrition")
    if "override_json" not in existing:
        conn.execute("ALTER TABLE nutrition ADD COLUMN override_json TEXT")
    if "is_override" not in existing:
        conn.execute("ALTER TABLE nutrition ADD COLUMN is_override INTEGER NOT NULL DEFAULT 0")
    conn.commit()


def _add_recipe_equipment_and_meal_prep_columns(conn: sqlite3.Connection) -> None:
    """equipment_json/equipment_classified_at (#5) and meal_prep/meal_prep_at
    (#2) on recipes. NULL/0 defaults — aboyeur.classify_equipment and
    aboyeur.classify_meal_prep backfill the real values."""
    if not _table_exists(conn, "recipes"):
        return
    existing = _column_names(conn, "recipes")
    stmts = {
        "equipment_json": "ALTER TABLE recipes ADD COLUMN equipment_json TEXT",
        "equipment_classified_at": "ALTER TABLE recipes ADD COLUMN equipment_classified_at TEXT",
        "meal_prep": "ALTER TABLE recipes ADD COLUMN meal_prep INTEGER NOT NULL DEFAULT 0",
        "meal_prep_at": "ALTER TABLE recipes ADD COLUMN meal_prep_at TEXT",
    }
    for col, sql in stmts.items():
        if col not in existing:
            conn.execute(sql)
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


def _add_is_admin_column(conn: sqlite3.Connection) -> None:
    """Operator flag for the /admin console. Defaults to 0 for everyone —
    an existing deploy comes out of this migration with no admin at all,
    which is the safe direction. Grant one with `aboyeur set-admin <email>`."""
    if not _table_exists(conn, "users") or "is_admin" in _column_names(conn, "users"):
        return
    conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
    conn.commit()


# SaaS columns on users. Every one is nullable or defaulted so an existing
# beta account comes out of the migration as a `free` BYOK user with no
# Stripe objects — exactly the state it was in, just named.
_USER_SAAS_COLUMNS = {
    "billing_status": "TEXT NOT NULL DEFAULT 'free'",   # free | active | past_due | canceled | comp
    "plan": "TEXT NOT NULL DEFAULT 'free'",             # free | pro
    "country": "TEXT",                                  # ISO-3166 alpha-2, user-editable
    "stripe_customer_id": "TEXT",                       # UNIQUE via index below (ADD COLUMN can't)
    "stripe_subscription_id": "TEXT",
    "paid_months": "INTEGER NOT NULL DEFAULT 0",        # consecutive paid months (loyalty tenure)
    "loyalty_tier": "TEXT",                             # ladder step id; NULL = base price
    "llm_mode": "TEXT NOT NULL DEFAULT 'byok'",         # byok | hosted
    "llm_provider_id": "INTEGER REFERENCES llm_providers(id)",  # NULL = platform default
    "credit_cents": "INTEGER NOT NULL DEFAULT 0",       # hosted-LLM wallet
    "email_verified_at": "TEXT",
    "tos_version": "TEXT",
    "tos_accepted_at": "TEXT",
    "deleted_at": "TEXT",                               # soft delete; `aboyeur purge` hard-deletes after 30d
    "low_balance_emailed_at": "TEXT",                   # throttle for the < 100¢ email
    "session_version": "INTEGER NOT NULL DEFAULT 0",  # revocation counter, see aboyeur.auth
}

_CHAT_MESSAGE_SAAS_COLUMNS = {
    "sensitivity": "TEXT",        # NULL (unclassified) | none | sensitive | unknown
    "sensitivity_json": "TEXT",   # classifier categories only — never content
    "menu_id": "INTEGER",         # menu built/delivered in this turn (PDF export button)
}


def _add_saas_columns(conn: sqlite3.Connection) -> None:
    """Billing / hosted-LLM / privacy columns (see _USER_SAAS_COLUMNS). Plain
    ADD COLUMNs, one per missing column, so a partially-applied earlier run
    picks up where it stopped."""
    if _table_exists(conn, "users"):
        existing = _column_names(conn, "users")
        for column, decl in _USER_SAAS_COLUMNS.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE users ADD COLUMN {column} {decl}")
        if "email_verified_at" not in existing:
            # Accounts that predate verification were provisioned by the
            # operator by hand (aboyeur create-user) — the operator knew the
            # address. Treat them as verified rather than locking every beta
            # tester out of chat on the day this ships.
            conn.execute("UPDATE users SET email_verified_at = created_at "
                         "WHERE email_verified_at IS NULL")
        # UNIQUE can't ride on ADD COLUMN; a unique index is the same promise.
        # SQLite treats NULLs as distinct, so unlinked accounts don't collide.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_stripe_customer "
            "ON users(stripe_customer_id)"
        )
    if _table_exists(conn, "chat_messages"):
        existing = _column_names(conn, "chat_messages")
        for column, decl in _CHAT_MESSAGE_SAAS_COLUMNS.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE chat_messages ADD COLUMN {column} {decl}")
    conn.commit()


# Placeholders — both editable in the admin console. Seeded only when the
# table is empty so an operator's edits are never overwritten on restart.
_LOYALTY_SEED = (("m3", 3, 10), ("m6", 6, 15), ("m12", 12, 25))
_PRICE_SEED = (("pro", "month", "*", 1000), ("pro", "year", "*", 8000))


def _seed_billing_tables(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "loyalty_ladder") or not _table_exists(conn, "plan_prices"):
        return
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if conn.execute("SELECT COUNT(*) FROM loyalty_ladder").fetchone()[0] == 0:
        conn.executemany(
            "INSERT INTO loyalty_ladder (tier, min_paid_months, percent_off) VALUES (?, ?, ?)",
            _LOYALTY_SEED,
        )
    if conn.execute("SELECT COUNT(*) FROM plan_prices").fetchone()[0] == 0:
        conn.executemany(
            "INSERT INTO plan_prices (plan, interval, country, amount_cents, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [(*row, now) for row in _PRICE_SEED],
        )
    conn.commit()


def _encrypt_pii_rows(conn: sqlite3.Connection) -> None:
    """Encrypt existing clients.name/email/phone/notes and profile.data_json
    under DATA_KEY. Idempotent by the `enc:` prefix: already-encrypted rows
    are skipped, so a re-run (or an interrupted first run) is safe."""
    # Snapshot before destructive work (in-place row rewrite)
    _snapshot_before(conn, str(Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent / "snapshot-pre-migrate.sqlite3"))
    
    if _table_exists(conn, "clients"):
        for row in conn.execute("SELECT id, name, email, phone, notes FROM clients").fetchall():
            if all(v is None or is_encrypted_field(v)
                    for v in (row["name"], row["email"], row["phone"], row["notes"])):
                continue
            conn.execute(
                "UPDATE clients SET name = ?, email = ?, phone = ?, notes = ? WHERE id = ?",
                (encrypt_field(row["name"]), encrypt_field(row["email"]),
                 encrypt_field(row["phone"]), encrypt_field(row["notes"]), row["id"]),
            )
    if _table_exists(conn, "profile") and "client_id" in _column_names(conn, "profile"):
        for row in conn.execute("SELECT client_id, data_json FROM profile").fetchall():
            if is_encrypted_field(row["data_json"]):
                continue
            conn.execute(
                "UPDATE profile SET data_json = ? WHERE client_id = ?",
                (encrypt_field(row["data_json"]), row["client_id"]),
            )
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
            (user_id, encrypt_field(_client_name_for(conn, user_id)), now, now),
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


def _snapshot_before(conn: sqlite3.Connection, snapshot_path: str) -> None:
    """Write a VACUUM INTO snapshot as a precondition for destructive work,
    before the snapshot path. If the write fails, the migration must not run.
    This is a read on the source DB (respects single writer) and the write goes to a
    different file, so it's safe against a live connection.

    A no-op when the snapshot already exists: two destructive steps in the
    same migrate() run (or a second, idempotent migrate() call that finds
    nothing left to do) both call this with the same path, and VACUUM INTO
    refuses to overwrite an existing file. The first snapshot taken is
    already "before any destructive work" — that's the one worth keeping.
    """
    dest = Path(snapshot_path)
    if dest.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Use a read-only connection to avoid taking the write lock.
    # The source DB is the one we're migrated; we snap it before we change it.
    src_uri = f"file:{conn.execute('PRAGMA database_list').fetchone()[2]}?mode=ro"
    try:
        sqlite3.connect(src_uri).execute(f"VACUUM INTO '{dest}';").close()
    except Exception as e:
        raise RuntimeError(f"Cannot snapshot database to {dest}: {e}") from e


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

    # Snapshot before destructive work (rebuilds chat_messages)
    _snapshot_before(conn, str(Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent / "snapshot-pre-migrate.sqlite3"))

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
    _add_is_admin_column(conn)
    _add_recipe_classification_columns(conn)
    _add_nutrition_servings_est(conn)
    _add_nutrition_override_columns(conn)
    _add_recipe_equipment_and_meal_prep_columns(conn)
    _migrate_chat_sessions(conn)
    # After _migrate_chat_sessions: it rebuilds chat_messages, and the
    # sensitivity columns must land on the rebuilt table, not the old one.
    _add_saas_columns(conn)
    _seed_billing_tables(conn)
    _encrypt_pii_rows(conn)
    # Create schema version table to track migration state from outside
    if not _table_exists(conn, "schema_version"):
        conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
    conn.execute("DELETE FROM schema_version WHERE version = ?", (SCHEMA_VERSION,))
    conn.commit()
    #  not in db.py's static SCHEMA — see the comment above the
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
    # Set the schema version after all migrations are complete
    conn.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
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
