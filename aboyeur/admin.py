"""Operator console data layer — the admin flag, the event log, and the
read-only queries behind /admin.

Split out of backend/main.py for the same reason clients.py and
chat_history.py are: main.py is the HTTP surface, this is the SQL. Every
function here takes a connection and does no auth of its own — the
`require_admin` dependency in backend/main.py is the only gate, and it runs
before any of this.

A note on time. Every timestamp this app writes is
`strftime('%Y-%m-%dT%H:%M:%SZ')` — ISO8601 with a `T` and a trailing `Z`.
SQLite's own `datetime('now','-7 days')` renders `YYYY-MM-DD HH:MM:SS`
instead, which sorts *before* every stored timestamp of the same day because
a space is below `T` in ASCII. Comparing the two silently returns the wrong
rows rather than erroring, so cutoffs are computed in Python here and passed
as parameters. Don't reach for `datetime('now', ...)` in this file.
"""

from __future__ import annotations

import json
import sqlite3
import time

# Anything longer is a traceback nobody reads in a table cell; the full text
# is still in logs/uvicorn.log for the rare case it matters.
_ERROR_DETAIL_MAX = 4000

FEEDBACK_STATUSES = ("new", "triaged", "in_progress", "resolved", "wontfix")
_CLOSED_STATUSES = ("resolved", "wontfix")


class AdminError(Exception):
    """A bad request against admin data — unknown user, bad status value."""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def since(days: int) -> str:
    """The ISO cutoff `days` ago, in the same shape every stored ts uses."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days * 86_400))


# ---------------------------------------------------------------------------
# The admin flag
# ---------------------------------------------------------------------------


def is_admin(conn: sqlite3.Connection, user_id: int) -> bool:
    row = conn.execute("SELECT is_admin FROM users WHERE id = ?", (user_id,)).fetchone()
    return bool(row is not None and row["is_admin"])


def set_admin(conn: sqlite3.Connection, email: str, admin: bool = True) -> int:
    """Grant or revoke operator access. Returns the affected user's id."""
    email = email.strip().lower()
    row = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if row is None:
        raise AdminError(f"no account for {email}")
    conn.execute("UPDATE users SET is_admin = ? WHERE id = ?", (1 if admin else 0, row["id"]))
    conn.commit()
    return row["id"]


# ---------------------------------------------------------------------------
# Event log
# ---------------------------------------------------------------------------


def record_event(
    conn: sqlite3.Connection,
    kind: str,
    *,
    user_id: int | None = None,
    client_id: int | None = None,
    session_id: int | None = None,
    model: str | None = None,
    latency_ms: int | None = None,
    tool_calls: int | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    ok: bool = True,
    error_type: str | None = None,
    error_detail: str | None = None,
    meta: dict | None = None,
) -> int:
    """One INSERT, no transaction wrapper — see the plan's note on SQLite
    write contention. Callers on the chat path must call this *after* the
    stream closes, never inside it."""
    cur = conn.execute(
        "INSERT INTO events (ts, kind, user_id, client_id, session_id, model, "
        "latency_ms, tool_calls, tokens_in, tokens_out, ok, error_type, "
        "error_detail, meta_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            _now(), kind, user_id, client_id, session_id, model,
            latency_ms, tool_calls, tokens_in, tokens_out,
            1 if ok else 0, error_type,
            (error_detail or "")[:_ERROR_DETAIL_MAX] or None,
            json.dumps(meta) if meta else None,
        ),
    )
    conn.commit()
    return cur.lastrowid


def purge_events(conn: sqlite3.Connection, keep_days: int = 90) -> int:
    """Retention. Returns how many rows went."""
    cur = conn.execute("DELETE FROM events WHERE ts < ?", (since(keep_days),))
    conn.commit()
    return cur.rowcount


def _event_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "ts": row["ts"],
        "kind": row["kind"],
        "user_id": row["user_id"],
        "email": row["email"] if "email" in row.keys() else None,
        "client_id": row["client_id"],
        "session_id": row["session_id"],
        "model": row["model"],
        "latency_ms": row["latency_ms"],
        "tool_calls": row["tool_calls"],
        "tokens_in": row["tokens_in"],
        "tokens_out": row["tokens_out"],
        "ok": bool(row["ok"]),
        "error_type": row["error_type"],
        "error_detail": row["error_detail"],
    }


def list_events(
    conn: sqlite3.Connection,
    *,
    kind: str | None = None,
    errors_only: bool = False,
    user_id: int | None = None,
    days: int | None = None,
    limit: int = 200,
) -> list[dict]:
    where, params = ["1=1"], []
    if kind:
        where.append("e.kind = ?")
        params.append(kind)
    if errors_only:
        # "Something went wrong" is either an explicit error row or a turn
        # that failed — the Activity screen's error filter must catch both.
        where.append("(e.ok = 0 OR e.kind = 'error')")
    if user_id is not None:
        where.append("e.user_id = ?")
        params.append(user_id)
    if days is not None:
        where.append("e.ts >= ?")
        params.append(since(days))
    params.append(max(1, min(limit, 1000)))
    rows = conn.execute(
        "SELECT e.*, u.email FROM events e LEFT JOIN users u ON u.id = e.user_id "
        f"WHERE {' AND '.join(where)} ORDER BY e.ts DESC, e.id DESC LIMIT ?",
        params,
    ).fetchall()
    return [_event_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Overview — the Dashboard's single call
# ---------------------------------------------------------------------------


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return (row[0] or 0) if row is not None else 0


def _percentile(values: list[int], pct: float) -> int | None:
    """Nearest-rank, on an already-small sample. Good enough for a homelab
    console and free of a numpy dependency."""
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(pct / 100 * len(ordered) + 0.5) - 1))
    return ordered[idx]


def _daily_series(conn: sqlite3.Connection, days: int = 30) -> list[dict]:
    """One row per day for the last `days`, zero-filled. Zero-filling in
    Python rather than SQL keeps quiet days visible as gaps in the chart
    instead of collapsing the x-axis onto only the busy ones."""
    cutoff = since(days)
    messages = {
        r["d"]: r["n"]
        for r in conn.execute(
            "SELECT substr(created_at,1,10) AS d, COUNT(*) AS n FROM chat_messages "
            "WHERE created_at >= ? GROUP BY d",
            (cutoff,),
        )
    }
    turns = {
        r["d"]: (r["n"], r["bad"])
        for r in conn.execute(
            "SELECT substr(ts,1,10) AS d, COUNT(*) AS n, "
            "SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS bad FROM events "
            "WHERE kind = 'chat_turn' AND ts >= ? GROUP BY d",
            (cutoff,),
        )
    }
    out = []
    for offset in range(days - 1, -1, -1):
        day = time.strftime("%Y-%m-%d", time.gmtime(time.time() - offset * 86_400))
        turn_count, errors = turns.get(day, (0, 0))
        out.append({
            "day": day,
            "messages": messages.get(day, 0),
            "turns": turn_count,
            "errors": errors or 0,
        })
    return out


def overview(conn: sqlite3.Connection) -> dict:
    d7, d30, d1 = since(7), since(30), since(1)

    latencies = [
        r[0] for r in conn.execute(
            "SELECT latency_ms FROM events WHERE kind = 'chat_turn' AND ok = 1 "
            "AND latency_ms IS NOT NULL AND ts >= ?",
            (d7,),
        )
    ]
    turns_7d = _scalar(conn, "SELECT COUNT(*) FROM events WHERE kind = 'chat_turn' AND ts >= ?", (d7,))
    errors_7d = _scalar(
        conn,
        "SELECT COUNT(*) FROM events WHERE (ok = 0 OR kind = 'error') AND ts >= ?",
        (d7,),
    )

    # "Active" = said something. A login without a message is a visit, not use.
    active_sql = (
        "SELECT COUNT(DISTINCT c.user_id) FROM chat_messages m "
        "JOIN chat_sessions s ON s.id = m.session_id "
        "JOIN clients c ON c.id = s.client_id "
        "WHERE m.created_at >= ?"
    )

    return {
        "users_total": _scalar(conn, "SELECT COUNT(*) FROM users"),
        "users_active_7d": _scalar(conn, active_sql, (d7,)),
        "users_active_30d": _scalar(conn, active_sql, (d30,)),
        "clients_total": _scalar(conn, "SELECT COUNT(*) FROM clients WHERE status = 'active'"),
        "sessions_total": _scalar(conn, "SELECT COUNT(*) FROM chat_sessions"),
        "messages_24h": _scalar(conn, "SELECT COUNT(*) FROM chat_messages WHERE created_at >= ?", (d1,)),
        "messages_7d": _scalar(conn, "SELECT COUNT(*) FROM chat_messages WHERE created_at >= ?", (d7,)),
        "messages_30d": _scalar(conn, "SELECT COUNT(*) FROM chat_messages WHERE created_at >= ?", (d30,)),
        "turns_7d": turns_7d,
        "errors_7d": errors_7d,
        # Rate against turns, not against every event — a turn is the thing
        # that can fail. No turns yet means no rate, not a suspicious 0%.
        "error_rate_7d": round(errors_7d / turns_7d, 4) if turns_7d else None,
        "latency_p50_ms": _percentile(latencies, 50),
        "latency_p95_ms": _percentile(latencies, 95),
        "menus_7d": _scalar(conn, "SELECT COUNT(*) FROM menus WHERE created_at >= ?", (d7,)),
        "feedback_open": _scalar(
            conn,
            f"SELECT COUNT(*) FROM feedback WHERE status NOT IN ({','.join('?' * len(_CLOSED_STATUSES))})",
            _CLOSED_STATUSES,
        ),
        "thumbs_up_30d": _scalar(
            conn, "SELECT COUNT(*) FROM feedback WHERE rating > 0 AND created_at >= ?", (d30,)
        ),
        "thumbs_down_30d": _scalar(
            conn, "SELECT COUNT(*) FROM feedback WHERE rating < 0 AND created_at >= ?", (d30,)
        ),
        "daily": _daily_series(conn, 30),
    }


# ---------------------------------------------------------------------------
# Users / sessions / transcripts
# ---------------------------------------------------------------------------

# A user's messages are two joins away (user -> clients -> sessions ->
# messages); every per-user count below walks that same path.
_USER_ROWS = """
SELECT
    u.id, u.email, u.name, u.practice, u.is_admin, u.created_at,
    (SELECT COUNT(*) FROM clients c WHERE c.user_id = u.id) AS client_count,
    (SELECT COUNT(*) FROM chat_sessions s
        JOIN clients c ON c.id = s.client_id WHERE c.user_id = u.id) AS session_count,
    (SELECT COUNT(*) FROM chat_messages m
        JOIN chat_sessions s ON s.id = m.session_id
        JOIN clients c ON c.id = s.client_id WHERE c.user_id = u.id) AS message_count,
    (SELECT MAX(m.created_at) FROM chat_messages m
        JOIN chat_sessions s ON s.id = m.session_id
        JOIN clients c ON c.id = s.client_id WHERE c.user_id = u.id) AS last_active,
    (SELECT COUNT(*) FROM events e
        WHERE e.user_id = u.id AND (e.ok = 0 OR e.kind = 'error') AND e.ts >= ?) AS errors_7d
FROM users u
"""


def _user_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "email": row["email"],
        "name": row["name"],
        "practice": row["practice"],
        "is_admin": bool(row["is_admin"]),
        "created_at": row["created_at"],
        "client_count": row["client_count"],
        "session_count": row["session_count"],
        "message_count": row["message_count"],
        "last_active": row["last_active"],
        "errors_7d": row["errors_7d"],
    }


def list_users(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        _USER_ROWS + " ORDER BY last_active IS NULL, last_active DESC, u.id",
        (since(7),),
    ).fetchall()
    return [_user_dict(r) for r in rows]


def get_user(conn: sqlite3.Connection, user_id: int) -> dict:
    row = conn.execute(_USER_ROWS + " WHERE u.id = ?", (since(7), user_id)).fetchone()
    if row is None:
        raise AdminError("no such user")
    user = _user_dict(row)
    user["clients"] = [
        {
            "id": c["id"],
            "name": c["name"],
            "status": c["status"],
            "is_self": bool(c["is_self"]),
            "created_at": c["created_at"],
            "session_count": c["session_count"],
        }
        for c in conn.execute(
            "SELECT c.*, (SELECT COUNT(*) FROM chat_sessions s WHERE s.client_id = c.id) "
            "AS session_count FROM clients c WHERE c.user_id = ? ORDER BY c.id",
            (user_id,),
        )
    ]
    user["sessions"] = list_sessions_for_user(conn, user_id)
    return user


def list_sessions_for_user(conn: sqlite3.Connection, user_id: int, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        "SELECT s.id, s.title, s.created_at, s.updated_at, c.id AS client_id, "
        "c.name AS client_name, "
        "(SELECT COUNT(*) FROM chat_messages m WHERE m.session_id = s.id) AS message_count, "
        "(SELECT MAX(m.created_at) FROM chat_messages m WHERE m.session_id = s.id) AS last_message_at "
        "FROM chat_sessions s JOIN clients c ON c.id = s.client_id "
        "WHERE c.user_id = ? ORDER BY s.updated_at DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "title": r["title"] or "Untitled conversation",
            "client_id": r["client_id"],
            "client_name": r["client_name"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "message_count": r["message_count"],
            "last_message_at": r["last_message_at"],
        }
        for r in rows
    ]


def get_transcript(conn: sqlite3.Connection, session_id: int) -> dict:
    head = conn.execute(
        "SELECT s.id, s.title, s.created_at, s.updated_at, c.id AS client_id, "
        "c.name AS client_name, u.id AS user_id, u.email "
        "FROM chat_sessions s JOIN clients c ON c.id = s.client_id "
        "JOIN users u ON u.id = c.user_id WHERE s.id = ?",
        (session_id,),
    ).fetchone()
    if head is None:
        raise AdminError("no such conversation")

    # Thumbs land on the transcript beside the turn that earned them, so the
    # admin reading a complaint sees the message it was about in place.
    thumbs = {
        r["message_id"]: {"rating": r["rating"], "body": r["body"], "status": r["status"]}
        for r in conn.execute(
            "SELECT message_id, rating, body, status FROM feedback WHERE session_id = ?",
            (session_id,),
        )
    }
    messages = [
        {
            "id": m["id"],
            "role": m["role"],
            "content": m["content"],
            "created_at": m["created_at"],
            "feedback": thumbs.get(m["id"]),
        }
        for m in conn.execute(
            "SELECT id, role, content, created_at FROM chat_messages "
            "WHERE session_id = ? ORDER BY id",
            (session_id,),
        )
    ]
    return {
        "id": head["id"],
        "title": head["title"] or "Untitled conversation",
        "client_id": head["client_id"],
        "client_name": head["client_name"],
        "user_id": head["user_id"],
        "email": head["email"],
        "created_at": head["created_at"],
        "updated_at": head["updated_at"],
        "messages": messages,
    }


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------


def owns_message(conn: sqlite3.Connection, user_id: int, session_id: int, message_id: int) -> bool:
    """A thumb may only be cast on a message in a conversation belonging to a
    client on the caller's own book — the same one-hop ownership check every
    client-scoped route makes, done in one query."""
    row = conn.execute(
        "SELECT 1 FROM chat_messages m JOIN chat_sessions s ON s.id = m.session_id "
        "JOIN clients c ON c.id = s.client_id "
        "WHERE m.id = ? AND m.session_id = ? AND c.user_id = ?",
        (message_id, session_id, user_id),
    ).fetchone()
    return row is not None


def upsert_feedback(
    conn: sqlite3.Connection,
    user_id: int,
    session_id: int,
    message_id: int,
    rating: int,
    body: str | None = None,
) -> dict:
    """Re-thumbing the same message edits the existing row. A row that was
    already triaged resets to 'new' on a change, because the operator's
    verdict was about the old rating/comment, not this one."""
    if rating not in (-1, 1):
        raise AdminError("rating must be -1 or 1")
    now = _now()
    client = conn.execute(
        "SELECT c.id FROM chat_sessions s JOIN clients c ON c.id = s.client_id WHERE s.id = ?",
        (session_id,),
    ).fetchone()
    conn.execute(
        "INSERT INTO feedback (created_at, updated_at, user_id, client_id, session_id, "
        "message_id, rating, body) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(message_id, user_id) DO UPDATE SET "
        "rating = excluded.rating, body = excluded.body, updated_at = excluded.updated_at, "
        "status = 'new', resolved_at = NULL",
        (now, now, user_id, client["id"] if client else None, session_id,
         message_id, rating, (body or "").strip() or None),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM feedback WHERE message_id = ? AND user_id = ?", (message_id, user_id)
    ).fetchone()
    return _feedback_dict(row)


def delete_feedback(conn: sqlite3.Connection, user_id: int, message_id: int) -> int:
    cur = conn.execute(
        "DELETE FROM feedback WHERE message_id = ? AND user_id = ?", (message_id, user_id)
    )
    conn.commit()
    return cur.rowcount


def my_feedback_for_session(conn: sqlite3.Connection, user_id: int, session_id: int) -> dict:
    """`{message_id: rating}` for the thumbs this user cast in this
    conversation — what the chat screen needs to render its buttons lit."""
    return {
        r["message_id"]: r["rating"]
        for r in conn.execute(
            "SELECT message_id, rating FROM feedback WHERE user_id = ? AND session_id = ?",
            (user_id, session_id),
        )
    }


def _feedback_dict(row: sqlite3.Row) -> dict:
    out = {
        "id": row["id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "user_id": row["user_id"],
        "client_id": row["client_id"],
        "session_id": row["session_id"],
        "message_id": row["message_id"],
        "rating": row["rating"],
        "body": row["body"],
        "status": row["status"],
        "admin_note": row["admin_note"],
        "resolved_at": row["resolved_at"],
    }
    keys = row.keys()
    if "email" in keys:
        out["email"] = row["email"]
    if "client_name" in keys:
        out["client_name"] = row["client_name"]
    if "message_excerpt" in keys:
        out["message_excerpt"] = row["message_excerpt"]
    return out


def list_feedback(conn: sqlite3.Connection, status: str | None = None,
                  limit: int = 200) -> list[dict]:
    where, params = ["1=1"], []
    if status == "open":
        where.append(f"f.status NOT IN ({','.join('?' * len(_CLOSED_STATUSES))})")
        params.extend(_CLOSED_STATUSES)
    elif status:
        where.append("f.status = ?")
        params.append(status)
    params.append(max(1, min(limit, 1000)))
    rows = conn.execute(
        "SELECT f.*, u.email, c.name AS client_name, "
        "substr(m.content, 1, 400) AS message_excerpt "
        "FROM feedback f LEFT JOIN users u ON u.id = f.user_id "
        "LEFT JOIN clients c ON c.id = f.client_id "
        "LEFT JOIN chat_messages m ON m.id = f.message_id "
        f"WHERE {' AND '.join(where)} "
        # Thumb-downs first inside each time slice: a complaint is the row
        # worth reading, a thumb-up is a datapoint for the chart.
        "ORDER BY f.rating ASC, f.created_at DESC LIMIT ?",
        params,
    ).fetchall()
    return [_feedback_dict(r) for r in rows]


def update_feedback(conn: sqlite3.Connection, feedback_id: int,
                    status: str | None = None, admin_note: str | None = None) -> dict:
    row = conn.execute("SELECT * FROM feedback WHERE id = ?", (feedback_id,)).fetchone()
    if row is None:
        raise AdminError("no such feedback")
    sets, params = ["updated_at = ?"], [_now()]
    if status is not None:
        if status not in FEEDBACK_STATUSES:
            raise AdminError(f"status must be one of {', '.join(FEEDBACK_STATUSES)}")
        sets.append("status = ?")
        params.append(status)
        # Closing stamps the time; reopening clears it, so the column always
        # means "when this was actually put to bed".
        sets.append("resolved_at = ?")
        params.append(_now() if status in _CLOSED_STATUSES else None)
    if admin_note is not None:
        sets.append("admin_note = ?")
        params.append(admin_note.strip() or None)
    params.append(feedback_id)
    conn.execute(f"UPDATE feedback SET {', '.join(sets)} WHERE id = ?", params)
    conn.commit()
    return _feedback_dict(
        conn.execute("SELECT * FROM feedback WHERE id = ?", (feedback_id,)).fetchone()
    )
