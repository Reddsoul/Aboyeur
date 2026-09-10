"""Admin console: the is_admin gate, the event log, and the feedback loop.

The gate is the point of this file. The console's code ships in every user's
bundle and the plan accepts that, which makes `require_admin` the only thing
standing between a beta tester and every other tester's transcripts — so it
gets tested through the routes, where someone could actually try it.
"""

from __future__ import annotations

import os

import pytest


# Same trick as tests/test_llm_settings.py: backend.main reads DB_PATH at
# import time and builds its connection then, so the env var has to be set
# before the import and the module imported exactly once per file.
@pytest.fixture(scope="module")
def backend_main(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("backend-admin") / "aboyeur.db"
    previous = os.environ.get("DB_PATH")
    os.environ["DB_PATH"] = str(db_path)
    try:
        import backend.main as main
    finally:
        if previous is None:
            os.environ.pop("DB_PATH", None)
        else:
            os.environ["DB_PATH"] = previous
    return main


@pytest.fixture()
def api(backend_main):
    from fastapi.testclient import TestClient
    return TestClient(backend_main.app)


def _as(backend_main, api, email: str, admin: bool = False) -> int:
    from aboyeur import admin as admin_mod, auth

    conn = backend_main._profile_conn()
    try:
        user_id = auth.get_or_create_user(conn, email)
        admin_mod.set_admin(conn, email, admin)
    finally:
        conn.close()
    api.cookies.set(backend_main._SESSION_COOKIE, auth.issue_token(user_id))
    return user_id


ADMIN_GETS = [
    "/api/admin/overview",
    "/api/admin/users",
    "/api/admin/users/1",
    "/api/admin/sessions/1",
    "/api/admin/events",
    "/api/admin/logs/tail",
    "/api/admin/feedback",
]


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ADMIN_GETS)
def test_admin_routes_403_a_normal_account(backend_main, api, path):
    _as(backend_main, api, "civilian@example.com", admin=False)
    assert api.get(path).status_code == 403


@pytest.mark.parametrize("path", ADMIN_GETS)
def test_admin_routes_401_without_a_session(backend_main, api, path):
    api.cookies.clear()
    assert api.get(path).status_code == 401


def test_admin_feedback_patch_is_gated_too(backend_main, api):
    """The one admin route that writes. A 403 here matters more than on the
    reads — triage state is shared, not per-caller."""
    _as(backend_main, api, "civilian2@example.com", admin=False)
    assert api.patch("/api/admin/feedback/1", json={"status": "resolved"}).status_code == 403


def test_admin_sees_the_console(backend_main, api):
    _as(backend_main, api, "operator@example.com", admin=True)
    res = api.get("/api/admin/overview")
    assert res.status_code == 200
    body = res.json()
    assert body["users_total"] >= 1
    assert len(body["daily"]) == 30


def test_auth_me_reports_the_flag(backend_main, api):
    _as(backend_main, api, "civilian3@example.com", admin=False)
    assert api.get("/auth/me").json()["is_admin"] is False
    _as(backend_main, api, "operator2@example.com", admin=True)
    assert api.get("/auth/me").json()["is_admin"] is True


def test_revoking_admin_closes_the_door(backend_main, api):
    from aboyeur import admin as admin_mod

    _as(backend_main, api, "demoted@example.com", admin=True)
    assert api.get("/api/admin/users").status_code == 200

    conn = backend_main._profile_conn()
    try:
        admin_mod.set_admin(conn, "demoted@example.com", False)
    finally:
        conn.close()
    # Same cookie, same session — the flag is read per request, not baked
    # into the token, so a revoke takes effect without waiting for logout.
    assert api.get("/api/admin/users").status_code == 403


# ---------------------------------------------------------------------------
# Feedback ownership
# ---------------------------------------------------------------------------


def _conversation(backend_main, user_id: int, name: str) -> tuple[int, int]:
    """A client with one user + one assistant message. Returns
    (session_id, assistant_message_id)."""
    from aboyeur import chat_history, clients

    conn = backend_main._profile_conn()
    try:
        client = clients.create_client(conn, user_id, name, None, None, None)
        session = chat_history.create_session(conn, client["id"], None)
        chat_history.append_message(conn, session["id"], "user", "plan my week")
        assistant_id = chat_history.append_message(
            conn, session["id"], "assistant", "Here is a week of dinners."
        )
    finally:
        conn.close()
    return session["id"], assistant_id


def test_thumb_round_trip(backend_main, api):
    user_id = _as(backend_main, api, "rater@example.com")
    session_id, message_id = _conversation(backend_main, user_id, "Ada")

    res = api.post("/api/feedback", json={
        "session_id": session_id, "message_id": message_id,
        "rating": -1, "body": "portions were way off",
    })
    assert res.status_code == 200
    assert res.json()["rating"] == -1
    assert res.json()["status"] == "new"

    assert api.get(f"/api/sessions/{session_id}/feedback").json()["ratings"] == {
        str(message_id): -1
    }

    # Re-thumbing edits the one row rather than stacking another.
    flipped = api.post("/api/feedback", json={
        "session_id": session_id, "message_id": message_id, "rating": 1,
    })
    assert flipped.json()["id"] == res.json()["id"]
    assert flipped.json()["rating"] == 1

    assert api.delete(f"/api/feedback/message/{message_id}").json()["deleted"] == 1
    assert api.get(f"/api/sessions/{session_id}/feedback").json()["ratings"] == {}


def test_cannot_rate_someone_elses_turn(backend_main, api):
    owner = _as(backend_main, api, "owner@example.com")
    session_id, message_id = _conversation(backend_main, owner, "Grace")

    # A different account, guessing ids off the end of its own range.
    _as(backend_main, api, "stranger@example.com")
    res = api.post("/api/feedback", json={
        "session_id": session_id, "message_id": message_id, "rating": 1,
    })
    assert res.status_code == 404


def test_a_bad_rating_is_rejected(backend_main, api):
    user_id = _as(backend_main, api, "shouter@example.com")
    session_id, message_id = _conversation(backend_main, user_id, "Alan")
    res = api.post("/api/feedback", json={
        "session_id": session_id, "message_id": message_id, "rating": 5,
    })
    assert res.status_code == 400


def test_triage_flow_and_transcript_deep_link(backend_main, api):
    user_id = _as(backend_main, api, "complainer@example.com")
    session_id, message_id = _conversation(backend_main, user_id, "Edsger")
    api.post("/api/feedback", json={
        "session_id": session_id, "message_id": message_id,
        "rating": -1, "body": "invented a recipe",
    })

    _as(backend_main, api, "triager@example.com", admin=True)
    items = api.get("/api/admin/feedback?status=open").json()["feedback"]
    row = next(f for f in items if f["message_id"] == message_id)
    assert row["email"] == "complainer@example.com"
    assert row["client_name"] == "Edsger"
    # The excerpt is what makes the board readable without opening every turn.
    assert "week of dinners" in row["message_excerpt"]

    patched = api.patch(f"/api/admin/feedback/{row['id']}",
                        json={"status": "resolved", "admin_note": "fixed portion scaling"}).json()
    assert patched["status"] == "resolved"
    assert patched["resolved_at"] is not None
    assert not [f for f in api.get("/api/admin/feedback?status=open").json()["feedback"]
                if f["message_id"] == message_id]

    # Reopening clears the stamp, so resolved_at always means "actually closed".
    reopened = api.patch(f"/api/admin/feedback/{row['id']}", json={"status": "new"}).json()
    assert reopened["resolved_at"] is None

    # The board deep-links here; the thumb must ride along on its own turn.
    transcript = api.get(f"/api/admin/sessions/{session_id}").json()
    rated = next(m for m in transcript["messages"] if m["id"] == message_id)
    assert rated["feedback"]["rating"] == -1
    assert rated["feedback"]["body"] == "invented a recipe"
    assert transcript["email"] == "complainer@example.com"


# ---------------------------------------------------------------------------
# The event log
# ---------------------------------------------------------------------------


def test_error_events_surface_with_their_user(backend_main, api):
    from aboyeur import admin as admin_mod

    user_id = _as(backend_main, api, "unlucky@example.com")
    session_id, _ = _conversation(backend_main, user_id, "Barbara")

    conn = backend_main._profile_conn()
    try:
        admin_mod.record_event(conn, "chat_turn", user_id=user_id, session_id=session_id,
                               latency_ms=3300, tool_calls=2)
        admin_mod.record_event(conn, "chat_turn", user_id=user_id, session_id=session_id,
                               latency_ms=800, ok=False, error_type="APIError",
                               error_detail="Traceback (most recent call last):\nboom")
    finally:
        conn.close()

    _as(backend_main, api, "watcher@example.com", admin=True)
    errors = api.get("/api/admin/events?errors_only=true").json()["events"]
    mine = [e for e in errors if e["session_id"] == session_id]
    assert len(mine) == 1
    # The whole point of the events table over logger.exception: the row knows
    # who it happened to and which conversation died.
    assert mine[0]["email"] == "unlucky@example.com"
    assert mine[0]["error_type"] == "APIError"
    assert "boom" in mine[0]["error_detail"]
    assert mine[0]["ok"] is False

    everything = api.get("/api/admin/events").json()["events"]
    assert len([e for e in everything if e["session_id"] == session_id]) == 2


def test_overview_rate_is_null_before_any_turns(backend_main):
    """A rate of 0% would be a claim; there's nothing to divide by yet."""
    from aboyeur import admin as admin_mod
    from aboyeur.db import connect, init_db
    from aboyeur.migrate import migrate
    import tempfile
    import pathlib

    db = pathlib.Path(tempfile.mkdtemp()) / "empty.db"
    conn = connect(db)
    init_db(conn)
    migrate(conn)
    try:
        assert admin_mod.overview(conn)["error_rate_7d"] is None
        assert admin_mod.overview(conn)["latency_p50_ms"] is None
    finally:
        conn.close()


def test_log_tail_is_bounded(backend_main, api):
    _as(backend_main, api, "tailer@example.com", admin=True)
    res = api.get("/api/admin/logs/tail?lines=10")
    assert res.status_code == 200
    assert len(res.json()["lines"]) <= 10
