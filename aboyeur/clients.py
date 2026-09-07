"""Clients — the people a practitioner plans for, and the subject key for
every per-person table (profile, pantry, menus, kits).

The isolation guarantee this module exists to provide: `resolve_client` is
the only way to turn a caller-supplied client id into one the rest of the
code will act on, and it refuses any client the account doesn't own. Below
that gate, tools filter on `client_id` alone — one client's plans are not
merely hidden from another's queries, they're unreachable by them.
"""

from __future__ import annotations

import sqlite3
import time

__all__ = [
    "ClientError",
    "UnknownClient",
    "create_client",
    "list_clients",
    "get_client",
    "update_client",
    "archive_client",
    "delete_client",
    "resolve_client",
    "default_client_id",
    "client_row_to_dict",
]


class ClientError(Exception):
    """Bad client input (blank name, unknown status)."""


class UnknownClient(ClientError):
    """No such client, or it belongs to a different account. Deliberately one
    error for both — telling the two apart leaks whether an id exists."""


_STATUSES = ("active", "archived")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def client_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "email": row["email"],
        "phone": row["phone"],
        "notes": row["notes"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def create_client(
    conn: sqlite3.Connection,
    user_id: int,
    name: str,
    email: str | None = None,
    phone: str | None = None,
    notes: str | None = None,
) -> dict:
    name = (name or "").strip()
    if not name:
        raise ClientError("a client needs a name")
    now = _now()
    cur = conn.execute(
        "INSERT INTO clients (user_id, name, email, phone, notes, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
        (user_id, name, (email or "").strip() or None, (phone or "").strip() or None,
         (notes or "").strip() or None, now, now),
    )
    conn.commit()
    return get_client(conn, user_id, cur.lastrowid)


def list_clients(conn: sqlite3.Connection, user_id: int, include_archived: bool = False) -> list[dict]:
    sql = "SELECT * FROM clients WHERE user_id = ?"
    if not include_archived:
        sql += " AND status = 'active'"
    sql += " ORDER BY status = 'archived', LOWER(name)"
    return [client_row_to_dict(r) for r in conn.execute(sql, (user_id,))]


def get_client(conn: sqlite3.Connection, user_id: int, client_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM clients WHERE id = ? AND user_id = ?", (client_id, user_id)
    ).fetchone()
    if row is None:
        raise UnknownClient(f"no client {client_id}")
    return client_row_to_dict(row)


def resolve_client(conn: sqlite3.Connection, user_id: int, client_id: int | None) -> int:
    """The ownership gate. Returns a client id safe to hand to the tools.

    `None` resolves to the account's default client (see below) so callers
    with no picker of their own — the CLI, the OpenAI-compat shim — still get
    a real subject instead of silently planning for whoever came first.
    Raises UnknownClient for anyone else's client, so a guessed id is a 404
    and never a data leak.
    """
    if client_id is None:
        return default_client_id(conn, user_id)
    row = conn.execute(
        "SELECT id FROM clients WHERE id = ? AND user_id = ?", (int(client_id), user_id)
    ).fetchone()
    if row is None:
        raise UnknownClient(f"no client {client_id}")
    return row["id"]


def default_client_id(conn: sqlite3.Connection, user_id: int) -> int:
    """This account's fallback subject: its oldest active client, created on
    demand if the account has none yet."""
    row = conn.execute(
        "SELECT id FROM clients WHERE user_id = ? AND status = 'active' "
        "ORDER BY id LIMIT 1",
        (user_id,),
    ).fetchone()
    if row is not None:
        return row["id"]
    return create_client(conn, user_id, "My household")["id"]


def update_client(conn: sqlite3.Connection, user_id: int, client_id: int, patch: dict) -> dict:
    """Partial update. Only the keys present in `patch` are touched, so the
    client form can send one field without blanking the rest."""
    get_client(conn, user_id, client_id)  # ownership gate

    sets, values = [], []
    if "name" in patch:
        name = (patch["name"] or "").strip()
        if not name:
            raise ClientError("a client needs a name")
        sets.append("name = ?")
        values.append(name)
    for field in ("email", "phone", "notes"):
        if field in patch:
            value = patch[field]
            sets.append(f"{field} = ?")
            values.append((str(value).strip() or None) if value is not None else None)
    if "status" in patch:
        status = str(patch["status"]).strip().lower()
        if status not in _STATUSES:
            raise ClientError(f"status must be one of {', '.join(_STATUSES)}")
        sets.append("status = ?")
        values.append(status)

    if sets:
        sets.append("updated_at = ?")
        values.extend([_now(), client_id, user_id])
        conn.execute(
            f"UPDATE clients SET {', '.join(sets)} WHERE id = ? AND user_id = ?", values
        )
        conn.commit()
    return get_client(conn, user_id, client_id)


def archive_client(conn: sqlite3.Connection, user_id: int, client_id: int) -> dict:
    """Soft delete — the default for the UI. Their plans and profile survive,
    which is the whole point when someone comes back six months later."""
    return update_client(conn, user_id, client_id, {"status": "archived"})


def delete_client(conn: sqlite3.Connection, user_id: int, client_id: int) -> None:
    """Hard delete, cascading to profile/pantry/menus/kits. Irreversible —
    the API only exposes this behind an explicit flag."""
    get_client(conn, user_id, client_id)
    conn.execute("DELETE FROM clients WHERE id = ? AND user_id = ?", (client_id, user_id))
    conn.commit()
