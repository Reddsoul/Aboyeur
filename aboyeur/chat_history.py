"""Conversations and their messages — one client, many chats.

A client accumulates several conversations over time (this week's plan, a cut
block, a one-off question), so starting a new one must never mean discarding
the last. Sessions hang off a client; messages hang off a session.

Two jobs, same as before, now per conversation:

- the UI lists a client's conversations and reads one back
- a rebuilt Agent is seeded from the conversation being continued

Isolation is unchanged and one hop longer: a session is reachable only through
its client, and a client only through its account (see aboyeur/clients.py).
"""

from __future__ import annotations

import sqlite3
import time

# How much of a conversation is replayed into a rebuilt agent. Long enough to
# carry a planning session across a restart, short enough that the system
# prompt plus history doesn't crowd out the turn being answered.
AGENT_CONTEXT_TURNS = 20

# Fallback shown before the first user message names the conversation.
UNTITLED = "New conversation"

ROLES = ("user", "assistant")


class UnknownSession(Exception):
    """No such conversation, or it belongs to another client."""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _touch(conn: sqlite3.Connection, session_id: int) -> None:
    conn.execute("UPDATE chat_sessions SET updated_at = ? WHERE id = ?", (_now(), session_id))


def _title_from(message: str) -> str:
    """Name a conversation after its opening line — the same shorthand a
    person would use scanning a list of chats."""
    text = " ".join(message.split())
    if len(text) <= 48:
        return text or UNTITLED
    return text[:47].rstrip(" ,.;:") + "…"


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def create_session(conn: sqlite3.Connection, client_id: int, title: str | None = None) -> dict:
    now = _now()
    cur = conn.execute(
        "INSERT INTO chat_sessions (client_id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (client_id, (title or "").strip() or None, now, now),
    )
    conn.commit()
    return get_session(conn, client_id, cur.lastrowid)


def get_session(conn: sqlite3.Connection, client_id: int, session_id: int) -> dict:
    """Gate: a session id is only valid for the client that owns it, so a
    guessed id from another client (or another account) is never readable."""
    row = conn.execute(
        "SELECT id, client_id, title, created_at, updated_at FROM chat_sessions "
        "WHERE id = ? AND client_id = ?",
        (session_id, client_id),
    ).fetchone()
    if row is None:
        raise UnknownSession(f"no conversation {session_id}")
    return _session_dict(conn, row)


def _session_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    stats = conn.execute(
        "SELECT COUNT(*) n, MAX(created_at) last_at FROM chat_messages WHERE session_id = ?",
        (row["id"],),
    ).fetchone()
    preview = conn.execute(
        "SELECT content FROM chat_messages WHERE session_id = ? ORDER BY id DESC LIMIT 1",
        (row["id"],),
    ).fetchone()
    return {
        "id": row["id"],
        "client_id": row["client_id"],
        "title": row["title"] or UNTITLED,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "message_count": stats["n"],
        "last_message_at": stats["last_at"],
        "preview": " ".join((preview["content"] if preview else "").split())[:120],
    }


def list_sessions(conn: sqlite3.Connection, client_id: int) -> list[dict]:
    """Most recently active first — how a list of chats is read."""
    rows = conn.execute(
        "SELECT id, client_id, title, created_at, updated_at FROM chat_sessions "
        "WHERE client_id = ? ORDER BY updated_at DESC, id DESC",
        (client_id,),
    ).fetchall()
    return [_session_dict(conn, r) for r in rows]


def latest_session_id(conn: sqlite3.Connection, client_id: int) -> int | None:
    row = conn.execute(
        "SELECT id FROM chat_sessions WHERE client_id = ? ORDER BY updated_at DESC, id DESC LIMIT 1",
        (client_id,),
    ).fetchone()
    return row["id"] if row is not None else None


def resolve_session(conn: sqlite3.Connection, client_id: int, session_id: int | None) -> int:
    """Turn a caller-supplied conversation id into one safe to write to.

    None continues this client's most recent conversation, creating the first
    one if they have none — so a caller with no picker (the CLI, the shim)
    still lands somewhere real rather than nowhere.
    """
    if session_id is None:
        existing = latest_session_id(conn, client_id)
        return existing if existing is not None else create_session(conn, client_id)["id"]
    return get_session(conn, client_id, session_id)["id"]


def rename_session(conn: sqlite3.Connection, client_id: int, session_id: int, title: str) -> dict:
    get_session(conn, client_id, session_id)
    conn.execute(
        "UPDATE chat_sessions SET title = ?, updated_at = ? WHERE id = ?",
        ((title or "").strip() or None, _now(), session_id),
    )
    conn.commit()
    return get_session(conn, client_id, session_id)


def delete_session(conn: sqlite3.Connection, client_id: int, session_id: int) -> None:
    get_session(conn, client_id, session_id)
    conn.execute("DELETE FROM chat_sessions WHERE id = ?", (session_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

def append_message(conn: sqlite3.Connection, session_id: int, role: str, content: str,
                   menu_id: int | None = None) -> int:
    """menu_id: the menu an assistant turn built, swapped or delivered —
    the transcript offers "Export PDF" on that bubble (backend/main.py stamps
    it from the turn's tool results)."""
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    cur = conn.execute(
        "INSERT INTO chat_messages (session_id, role, content, created_at, menu_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (session_id, role, content, _now(), menu_id),
    )
    # The first user message names the conversation, so a list of chats reads
    # as what was asked rather than a column of timestamps.
    if role == "user":
        conn.execute(
            "UPDATE chat_sessions SET title = ? WHERE id = ? AND title IS NULL",
            (_title_from(content), session_id),
        )
    _touch(conn, session_id)
    conn.commit()
    return cur.lastrowid


def list_messages(conn: sqlite3.Connection, session_id: int, limit: int | None = None) -> list[dict]:
    """Oldest first — the order a transcript is read in. `limit` keeps the
    most RECENT n, not the first n."""
    if limit is not None:
        rows = list(reversed(conn.execute(
            "SELECT id, role, content, created_at, menu_id FROM chat_messages "
            "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, int(limit)),
        ).fetchall()))
    else:
        rows = conn.execute(
            "SELECT id, role, content, created_at, menu_id FROM chat_messages "
            "WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
    return [
        {"id": r["id"], "role": r["role"], "content": r["content"],
         "created_at": r["created_at"], "menu_id": r["menu_id"]}
        for r in rows
    ]


def agent_history(conn: sqlite3.Connection, session_id: int) -> list[dict]:
    """The recent transcript in the shape Agent(history=...) wants: plain
    role/content pairs, no tool-call rounds (those aren't preserved across
    turns by design — see aboyeur/agent.py)."""
    return [
        {"role": m["role"], "content": m["content"]}
        for m in list_messages(conn, session_id, limit=AGENT_CONTEXT_TURNS)
        if m["content"].strip()
    ]


def delete_last_message_if_role(conn: sqlite3.Connection, session_id: int, role: str) -> bool:
    """Drop the newest message in a conversation, but only when it has `role`.

    Used to roll back a user turn that produced no answer (the LLM endpoint
    failed outright), so the transcript doesn't keep a question with no reply
    and a retry doesn't stack two user messages in the rebuilt agent's history.
    Returns whether a row was deleted.
    """
    row = conn.execute(
        "SELECT id, role FROM chat_messages WHERE session_id = ? ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    if row is None or row["role"] != role:
        return False
    conn.execute("DELETE FROM chat_messages WHERE id = ?", (row["id"],))
    _touch(conn, session_id)
    conn.commit()
    return True


def clear_messages(conn: sqlite3.Connection, session_id: int) -> int:
    """Empty one conversation without deleting it. Returns how many went."""
    cur = conn.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
    _touch(conn, session_id)
    conn.commit()
    return cur.rowcount
