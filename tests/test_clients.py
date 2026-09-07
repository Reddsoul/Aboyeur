"""Clients: the ownership gate, per-client isolation, and the migration that
folds an old single-household account into its first client."""

from __future__ import annotations

import json
import sqlite3

import pytest

from aboyeur import auth, clients
from aboyeur.tools.profile import get_profile, update_profile


@pytest.fixture()
def other_user(db_conn: sqlite3.Connection) -> int:
    return auth.get_or_create_user(db_conn, "rival-practitioner@example.com")


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def test_create_and_list(db_conn: sqlite3.Connection, user_id: int):
    a = clients.create_client(db_conn, user_id, "Ada Lovelace", email="ada@example.com")
    b = clients.create_client(db_conn, user_id, "Bo Jackson")
    assert a["status"] == "active" and a["email"] == "ada@example.com"
    assert [c["name"] for c in clients.list_clients(db_conn, user_id)] == ["Ada Lovelace", "Bo Jackson"]
    assert b["id"] != a["id"]


def test_blank_name_rejected(db_conn: sqlite3.Connection, user_id: int):
    with pytest.raises(clients.ClientError):
        clients.create_client(db_conn, user_id, "   ")


def test_update_is_partial(db_conn: sqlite3.Connection, user_id: int):
    c = clients.create_client(db_conn, user_id, "Ada", email="ada@example.com", notes="marathon")
    updated = clients.update_client(db_conn, user_id, c["id"], {"name": "Ada L."})
    assert updated["name"] == "Ada L."
    assert updated["email"] == "ada@example.com"  # untouched keys survive
    assert updated["notes"] == "marathon"


def test_archive_hides_from_the_default_listing(db_conn: sqlite3.Connection, user_id: int):
    c = clients.create_client(db_conn, user_id, "Ada")
    clients.create_client(db_conn, user_id, "Bo")
    clients.archive_client(db_conn, user_id, c["id"])
    assert [x["name"] for x in clients.list_clients(db_conn, user_id)] == ["Bo"]
    assert len(clients.list_clients(db_conn, user_id, include_archived=True)) == 2


def test_delete_cascades_to_their_data(db_conn: sqlite3.Connection, user_id: int):
    c = clients.create_client(db_conn, user_id, "Ada")
    update_profile(db_conn, c["id"], {"add_allergies": ["peanut"]})
    assert db_conn.execute("SELECT COUNT(*) n FROM profile WHERE client_id = ?",
                           (c["id"],)).fetchone()["n"] == 1
    clients.delete_client(db_conn, user_id, c["id"])
    assert db_conn.execute("SELECT COUNT(*) n FROM profile WHERE client_id = ?",
                           (c["id"],)).fetchone()["n"] == 0


# ---------------------------------------------------------------------------
# The ownership gate — the whole isolation story rests on this
# ---------------------------------------------------------------------------

def test_resolve_client_rejects_another_accounts_client(
    db_conn: sqlite3.Connection, user_id: int, other_user: int
):
    theirs = clients.create_client(db_conn, other_user, "Their Client")
    with pytest.raises(clients.UnknownClient):
        clients.resolve_client(db_conn, user_id, theirs["id"])
    with pytest.raises(clients.UnknownClient):
        clients.get_client(db_conn, user_id, theirs["id"])


def test_resolve_client_rejects_an_id_that_does_not_exist(
    db_conn: sqlite3.Connection, user_id: int
):
    with pytest.raises(clients.UnknownClient):
        clients.resolve_client(db_conn, user_id, 999_999)


def test_update_and_delete_also_gate_on_ownership(
    db_conn: sqlite3.Connection, user_id: int, other_user: int
):
    theirs = clients.create_client(db_conn, other_user, "Their Client")
    with pytest.raises(clients.UnknownClient):
        clients.update_client(db_conn, user_id, theirs["id"], {"name": "hijacked"})
    with pytest.raises(clients.UnknownClient):
        clients.delete_client(db_conn, user_id, theirs["id"])
    assert clients.get_client(db_conn, other_user, theirs["id"])["name"] == "Their Client"


def test_list_never_shows_another_accounts_clients(
    db_conn: sqlite3.Connection, user_id: int, other_user: int
):
    clients.create_client(db_conn, user_id, "Mine")
    clients.create_client(db_conn, other_user, "Theirs")
    assert [c["name"] for c in clients.list_clients(db_conn, user_id)] == ["Mine"]
    assert [c["name"] for c in clients.list_clients(db_conn, other_user)] == ["Theirs"]


def test_default_client_is_created_once_and_reused(db_conn: sqlite3.Connection, user_id: int):
    first = clients.default_client_id(db_conn, user_id)
    assert clients.default_client_id(db_conn, user_id) == first
    assert clients.get_client(db_conn, user_id, first)["name"] == "My household"
    # ...and once a real client exists, the oldest one stays the default.
    clients.create_client(db_conn, user_id, "Later Client")
    assert clients.default_client_id(db_conn, user_id) == first


def test_default_client_skips_archived(db_conn: sqlite3.Connection, user_id: int):
    first = clients.create_client(db_conn, user_id, "Ada")["id"]
    second = clients.create_client(db_conn, user_id, "Bo")["id"]
    clients.archive_client(db_conn, user_id, first)
    assert clients.default_client_id(db_conn, user_id) == second


# ---------------------------------------------------------------------------
# One plan must not feed into another
# ---------------------------------------------------------------------------

def test_profiles_do_not_bleed_between_two_clients_of_one_practitioner(
    db_conn: sqlite3.Connection, user_id: int
):
    a = clients.create_client(db_conn, user_id, "Ada")["id"]
    b = clients.create_client(db_conn, user_id, "Bo")["id"]

    update_profile(db_conn, a, {"add_allergies": ["peanut"],
                                "add_member": {"id": "ada", "label": "Ada", "sex": "f",
                                               "age": 34, "height_cm": 168, "weight_kg": 61}})
    update_profile(db_conn, b, {"add_allergies": ["shellfish"],
                                "add_member": {"id": "bo", "label": "Bo", "sex": "m",
                                               "age": 41, "height_cm": 185, "weight_kg": 95}})

    pa, pb = get_profile(db_conn, a), get_profile(db_conn, b)
    assert pa["allergies"] == ["peanut"] and pb["allergies"] == ["shellfish"]
    assert [m["id"] for m in pa["household"]] == ["ada"]
    assert [m["id"] for m in pb["household"]] == ["bo"]
    # Targets are computed per client, so they can't coincide by accident here.
    assert pa["targets"]["ada"]["daily_kcal"] != pb["targets"]["bo"]["daily_kcal"]


def test_plans_are_scoped_to_one_client(db_conn: sqlite3.Connection, user_id: int):
    """A menu row is reachable only through its own client_id — the property
    every plan-reading tool relies on."""
    a = clients.create_client(db_conn, user_id, "Ada")["id"]
    b = clients.create_client(db_conn, user_id, "Bo")["id"]
    db_conn.execute(
        "INSERT INTO menus (client_id, week_of, days_json, status, created_at) "
        "VALUES (?, '2026-09-07', '[]', 'draft', '2026-09-07T00:00:00Z')", (a,),
    )
    db_conn.commit()
    assert db_conn.execute("SELECT COUNT(*) n FROM menus WHERE client_id = ?",
                           (a,)).fetchone()["n"] == 1
    assert db_conn.execute("SELECT COUNT(*) n FROM menus WHERE client_id = ?",
                           (b,)).fetchone()["n"] == 0


def test_deleting_a_client_leaves_the_other_untouched(db_conn: sqlite3.Connection, user_id: int):
    a = clients.create_client(db_conn, user_id, "Ada")["id"]
    b = clients.create_client(db_conn, user_id, "Bo")["id"]
    update_profile(db_conn, a, {"add_allergies": ["peanut"]})
    update_profile(db_conn, b, {"add_allergies": ["shellfish"]})
    clients.delete_client(db_conn, user_id, a)
    assert get_profile(db_conn, b)["allergies"] == ["shellfish"]


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def _multitenant_db(path) -> sqlite3.Connection:
    """A database in the pre-clients shape: per-person tables keyed by user."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE,
                            password_hash TEXT, created_at TEXT NOT NULL);
        CREATE TABLE profile (user_id INTEGER PRIMARY KEY, data_json TEXT NOT NULL,
                              updated_at TEXT NOT NULL);
        CREATE TABLE pantry (user_id INTEGER NOT NULL, item TEXT NOT NULL, qty TEXT,
                             added_at TEXT NOT NULL, expires_at TEXT,
                             PRIMARY KEY (user_id, item));
        CREATE TABLE menus (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
                            week_of TEXT NOT NULL, days_json TEXT NOT NULL,
                            status TEXT NOT NULL DEFAULT 'draft', created_at TEXT NOT NULL);
        CREATE TABLE kits (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
                           data_json TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE clients (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
                              name TEXT NOT NULL, email TEXT, phone TEXT, notes TEXT,
                              status TEXT NOT NULL DEFAULT 'active',
                              created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        """
    )
    return conn


def test_migration_folds_each_household_into_its_own_client(tmp_path):
    from aboyeur.migrate import _migrate_to_clients

    conn = _multitenant_db(tmp_path / "old.db")
    now = "2026-01-01T00:00:00Z"
    for uid, email in ((1, "one@example.com"), (2, "two@example.com")):
        conn.execute("INSERT INTO users (id, email, created_at) VALUES (?, ?, ?)",
                     (uid, email, now))
    conn.execute("INSERT INTO profile VALUES (1, ?, ?)",
                 (json.dumps({"household": [{"id": "jose", "label": "Jose"}],
                              "allergies": ["peanut"]}), now))
    conn.execute("INSERT INTO profile VALUES (2, ?, ?)",
                 (json.dumps({"household": [], "allergies": []}), now))
    conn.execute("INSERT INTO menus (id, user_id, week_of, days_json, created_at) "
                 "VALUES (7, 1, '2026-01-05', '[]', ?)", (now,))
    conn.execute("INSERT INTO kits (id, user_id, data_json, created_at) VALUES (3, 2, '{}', ?)",
                 (now,))
    conn.execute("INSERT INTO pantry VALUES (1, 'rice', '1kg', ?, NULL)", (now,))
    conn.commit()

    _migrate_to_clients(conn)

    rows = {r["user_id"]: r for r in conn.execute("SELECT * FROM clients")}
    assert len(rows) == 2
    assert rows[1]["name"] == "Jose"          # named from the primary member
    assert rows[2]["name"] == "My household"  # ...or the fallback when there is none

    assert conn.execute("SELECT client_id FROM menus WHERE id = 7").fetchone()[0] == rows[1]["id"]
    assert conn.execute("SELECT client_id FROM kits WHERE id = 3").fetchone()[0] == rows[2]["id"]
    assert conn.execute("SELECT client_id FROM pantry").fetchone()[0] == rows[1]["id"]
    assert json.loads(
        conn.execute("SELECT data_json FROM profile WHERE client_id = ?",
                     (rows[1]["id"],)).fetchone()[0]
    )["allergies"] == ["peanut"]


def test_migration_is_idempotent(tmp_path):
    from aboyeur.migrate import _migrate_to_clients

    conn = _multitenant_db(tmp_path / "old.db")
    now = "2026-01-01T00:00:00Z"
    conn.execute("INSERT INTO users (id, email, created_at) VALUES (1, 'a@b.c', ?)", (now,))
    conn.execute("INSERT INTO profile VALUES (1, '{}', ?)", (now,))
    conn.commit()

    _migrate_to_clients(conn)
    first = conn.execute("SELECT COUNT(*) n FROM clients").fetchone()["n"]
    _migrate_to_clients(conn)  # second run must be a no-op, not a second client
    assert conn.execute("SELECT COUNT(*) n FROM clients").fetchone()["n"] == first == 1
    assert conn.execute("SELECT COUNT(*) n FROM profile").fetchone()["n"] == 1


def test_migration_clears_a_stale_old_table_from_an_interrupted_run(tmp_path):
    """An interrupted run can leave `profile_old` behind while `profile` is
    still in the user_id shape. That leftover is stale by definition — the
    rerun must drop it rather than collide on the rename."""
    from aboyeur.migrate import _migrate_to_clients

    conn = _multitenant_db(tmp_path / "old.db")
    now = "2026-01-01T00:00:00Z"
    conn.execute("INSERT INTO users (id, email, created_at) VALUES (1, 'a@b.c', ?)", (now,))
    conn.execute("INSERT INTO profile VALUES (1, '{\"household\": []}', ?)", (now,))
    conn.execute("CREATE TABLE profile_old (client_id INTEGER, data_json TEXT, updated_at TEXT)")
    conn.commit()

    _migrate_to_clients(conn)

    assert conn.execute(
        "SELECT COUNT(*) n FROM sqlite_master WHERE name = 'profile_old'"
    ).fetchone()["n"] == 0
    assert "client_id" in {c["name"] for c in conn.execute("PRAGMA table_info(profile)")}


def test_old_migration_steps_leave_client_keyed_tables_alone(tmp_path):
    """The pre-multi-tenant steps must not fire on an already-client-keyed
    table. Guarding on "no user_id column" made them rebuild it back into the
    user_id shape and drop every row."""
    from aboyeur.migrate import _migrate_menus, _migrate_profile

    conn = sqlite3.connect(tmp_path / "new.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE profile (client_id INTEGER PRIMARY KEY, data_json TEXT NOT NULL,
                              updated_at TEXT NOT NULL);
        CREATE TABLE menus (id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL,
                            week_of TEXT NOT NULL, days_json TEXT NOT NULL,
                            status TEXT NOT NULL DEFAULT 'draft', created_at TEXT NOT NULL);
        INSERT INTO profile VALUES (1, '{"allergies": ["peanut"]}', '2026-01-01T00:00:00Z');
        INSERT INTO menus VALUES (1, 1, '2026-01-05', '[]', 'draft', '2026-01-01T00:00:00Z');
        """
    )
    conn.commit()

    _migrate_profile(conn, seed_user_id=1)
    _migrate_menus(conn, seed_user_id=1)

    assert conn.execute("SELECT COUNT(*) n FROM profile").fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) n FROM menus").fetchone()["n"] == 1
    assert "client_id" in {c["name"] for c in conn.execute("PRAGMA table_info(profile)")}
