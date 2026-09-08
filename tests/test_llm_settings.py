"""Per-user LLM settings: encryption at rest, the save/get contract, and the
backend's effective-config + unconfigured-chat behavior."""

from __future__ import annotations

import os
import sqlite3

import pytest

from aboyeur import crypto, llm_settings


@pytest.fixture(autouse=True)
def _fresh_fernet():
    """crypto._fernet is lru_cached — tests that swap keys must not leak the
    cached instance into the next test."""
    crypto._fernet.cache_clear()
    yield
    crypto._fernet.cache_clear()


# ---------------------------------------------------------------------------
# crypto
# ---------------------------------------------------------------------------

def test_encrypt_round_trip(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("LLM_SETTINGS_KEY", Fernet.generate_key().decode())
    token = crypto.encrypt("sk-secret-123")
    assert token != "sk-secret-123"
    assert crypto.decrypt(token) == "sk-secret-123"


def test_key_derived_from_jwt_secret_when_unset(monkeypatch):
    monkeypatch.delenv("LLM_SETTINGS_KEY", raising=False)
    token = crypto.encrypt("sk-derived")
    crypto._fernet.cache_clear()  # a fresh process would rebuild it the same way
    assert crypto.decrypt(token) == "sk-derived"


def test_rotated_key_cannot_decrypt(monkeypatch):
    from cryptography.fernet import Fernet, InvalidToken
    monkeypatch.setenv("LLM_SETTINGS_KEY", Fernet.generate_key().decode())
    token = crypto.encrypt("sk-old")
    crypto._fernet.cache_clear()
    monkeypatch.setenv("LLM_SETTINGS_KEY", Fernet.generate_key().decode())
    with pytest.raises(InvalidToken):
        crypto.decrypt(token)


def test_bad_configured_key_raises_clear_error(monkeypatch):
    monkeypatch.setenv("LLM_SETTINGS_KEY", "not-a-fernet-key")
    with pytest.raises(RuntimeError, match="LLM_SETTINGS_KEY"):
        crypto.encrypt("x")


# ---------------------------------------------------------------------------
# llm_settings store
# ---------------------------------------------------------------------------

def test_unconfigured_user(db_conn: sqlite3.Connection, user_id: int):
    assert llm_settings.get_llm_settings(db_conn, user_id) is None
    public = llm_settings.get_llm_settings_public(db_conn, user_id)
    assert public == {"base_url": "", "model": "", "configured": False}


def test_save_then_get_returns_plaintext_key(db_conn: sqlite3.Connection, user_id: int):
    llm_settings.save_llm_settings(db_conn, user_id, "https://api.example.com/v1/", "gpt-x", "sk-abc")
    stored = llm_settings.get_llm_settings(db_conn, user_id)
    assert stored == {"base_url": "https://api.example.com/v1", "model": "gpt-x",
                      "api_key": "sk-abc"}  # trailing slash normalized off
    row = db_conn.execute("SELECT api_key_encrypted FROM llm_settings").fetchone()
    assert "sk-abc" not in row["api_key_encrypted"]  # at rest it's ciphertext


def test_public_shape_never_carries_the_key(db_conn: sqlite3.Connection, user_id: int):
    llm_settings.save_llm_settings(db_conn, user_id, "https://api.example.com/v1", "gpt-x", "sk-abc")
    public = llm_settings.get_llm_settings_public(db_conn, user_id)
    assert public == {"base_url": "https://api.example.com/v1", "model": "gpt-x",
                      "configured": True}
    assert "sk-abc" not in str(public)


def test_blank_key_on_edit_keeps_the_stored_one(db_conn: sqlite3.Connection, user_id: int):
    llm_settings.save_llm_settings(db_conn, user_id, "https://a.example/v1", "old-model", "sk-keep")
    llm_settings.save_llm_settings(db_conn, user_id, "https://b.example/v1", "new-model", "")
    stored = llm_settings.get_llm_settings(db_conn, user_id)
    assert stored["base_url"] == "https://b.example/v1"
    assert stored["model"] == "new-model"
    assert stored["api_key"] == "sk-keep"


def test_first_save_without_a_key_is_allowed(db_conn: sqlite3.Connection, user_id: int):
    """Keyless self-hosted endpoints are real — store the placeholder the
    OpenAI client needs rather than rejecting the save."""
    llm_settings.save_llm_settings(db_conn, user_id, "http://100.70.6.1:4000/v1", "hard", None)
    assert llm_settings.get_llm_settings(db_conn, user_id)["api_key"] == "unused"


def test_missing_base_url_or_model_rejected(db_conn: sqlite3.Connection, user_id: int):
    with pytest.raises(ValueError):
        llm_settings.save_llm_settings(db_conn, user_id, "", "gpt-x", "sk-abc")
    with pytest.raises(ValueError):
        llm_settings.save_llm_settings(db_conn, user_id, "https://a.example/v1", "  ", "sk-abc")


def test_undecryptable_row_reads_as_unconfigured(db_conn: sqlite3.Connection,
                                                 user_id: int, monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("LLM_SETTINGS_KEY", Fernet.generate_key().decode())
    llm_settings.save_llm_settings(db_conn, user_id, "https://a.example/v1", "m", "sk-abc")
    crypto._fernet.cache_clear()
    monkeypatch.setenv("LLM_SETTINGS_KEY", Fernet.generate_key().decode())
    # The row is still there (so the UI keeps showing base_url/model), but the
    # server treats the user as unconfigured rather than crashing on decrypt.
    assert llm_settings.get_llm_settings(db_conn, user_id) is None
    assert llm_settings.get_llm_settings_public(db_conn, user_id)["configured"] is True


def test_delete(db_conn: sqlite3.Connection, user_id: int):
    llm_settings.save_llm_settings(db_conn, user_id, "https://a.example/v1", "m", "sk-abc")
    llm_settings.delete_llm_settings(db_conn, user_id)
    assert llm_settings.get_llm_settings(db_conn, user_id) is None


# ---------------------------------------------------------------------------
# backend/main.py — effective config + the unconfigured /chat gate
#
# backend.main builds its DB connection at import time, so DB_PATH is pointed
# at a throwaway file before the import and the module is imported exactly
# once, module-scoped, for this file.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def backend_main(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("backend-main") / "aboyeur.db"
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
def backend_user(backend_main) -> int:
    from aboyeur import auth
    conn = backend_main._profile_conn()
    try:
        uid = auth.get_or_create_user(conn, "llm-settings-test@example.com")
        llm_settings.delete_llm_settings(conn, uid)
    finally:
        conn.close()
    backend_main._drop_session_agent(uid)
    return uid


def test_effective_config_none_for_unconfigured_user(backend_main, backend_user):
    assert backend_main._effective_config(backend_user) is None


def test_effective_config_falls_back_to_env_for_the_seed_user(backend_main):
    config = backend_main._effective_config(backend_main._SEED_USER_ID)
    assert config is backend_main._aboyeur_config  # the OpenWebUI shim's path


def test_effective_config_applies_the_users_override(backend_main, backend_user):
    conn = backend_main._profile_conn()
    try:
        llm_settings.save_llm_settings(conn, backend_user, "https://mine.example/v1",
                                       "my-model", "sk-mine")
    finally:
        conn.close()
    config = backend_main._effective_config(backend_user)
    assert config.llm_base_url == "https://mine.example/v1"
    assert config.llm_model == "my-model"
    assert config.llm_api_key == "sk-mine"
    # Everything else still comes from the server's own config.
    assert config.db_path == backend_main._aboyeur_config.db_path


def test_chat_nudges_an_unconfigured_user_instead_of_500ing(backend_main, backend_user):
    from fastapi.testclient import TestClient
    from aboyeur import auth

    client = TestClient(backend_main.app)
    client.cookies.set(backend_main._SESSION_COOKIE, auth.issue_token(backend_user))
    res = client.post("/chat", json={"message": "plan my week"})
    assert res.status_code == 200
    body = res.text
    assert backend_main.NEEDS_LLM_SETUP_MESSAGE in body
    assert body.rstrip().endswith("data: [DONE]")


# ---------------------------------------------------------------------------
# The HTTP-level client gate. Same guarantee as tests/test_clients.py, but
# through the routes, where a practitioner could actually try it.
# ---------------------------------------------------------------------------

@pytest.fixture()
def api(backend_main):
    from fastapi.testclient import TestClient
    return TestClient(backend_main.app)


def _as(backend_main, api, email: str):
    """Log the TestClient in as `email`, creating the account if needed."""
    from aboyeur import auth
    conn = backend_main._profile_conn()
    try:
        user_id = auth.get_or_create_user(conn, email)
    finally:
        conn.close()
    api.cookies.set(backend_main._SESSION_COOKIE, auth.issue_token(user_id))
    return user_id


def test_clients_crud_over_http(backend_main, api):
    _as(backend_main, api, "crud-practitioner@example.com")

    created = api.post("/api/clients", json={"name": "Ada Lovelace", "email": "ada@example.com"})
    assert created.status_code == 200
    client_id = created.json()["id"]

    listed = api.get("/api/clients").json()["clients"]
    assert [c["name"] for c in listed] == ["Ada Lovelace"]
    assert listed[0]["menu_count"] == 0 and listed[0]["has_profile"] is False

    renamed = api.patch(f"/api/clients/{client_id}", json={"name": "Ada L."})
    assert renamed.json()["name"] == "Ada L."
    assert renamed.json()["email"] == "ada@example.com"  # partial patch

    assert api.delete(f"/api/clients/{client_id}").json()["status"] == "archived"
    assert api.get("/api/clients").json()["clients"] == []
    assert len(api.get("/api/clients?include_archived=true").json()["clients"]) == 1


def test_routes_404_on_another_practitioners_client(backend_main, api):
    _as(backend_main, api, "owner-practitioner@example.com")
    mine = api.post("/api/clients", json={"name": "Mine"}).json()["id"]

    _as(backend_main, api, "rival-practitioner@example.com")
    assert api.get(f"/api/clients/{mine}").status_code == 404
    assert api.get(f"/api/clients/{mine}/profile").status_code == 404
    assert api.get(f"/api/clients/{mine}/plans").status_code == 404
    assert api.patch(f"/api/clients/{mine}", json={"name": "hijacked"}).status_code == 404
    assert api.delete(f"/api/clients/{mine}").status_code == 404
    assert api.post("/chat", json={"message": "hi", "client_id": mine}).status_code == 404

    # ...and the rival's own listing never mentions it.
    assert "Mine" not in [c["name"] for c in api.get("/api/clients").json()["clients"]]


def test_profile_writes_land_on_one_client_only(backend_main, api):
    _as(backend_main, api, "two-clients@example.com")
    a = api.post("/api/clients", json={"name": "Ada"}).json()["id"]
    b = api.post("/api/clients", json={"name": "Bo"}).json()["id"]

    api.post(f"/api/clients/{a}/profile", json={
        "household": [{"id": "", "label": "Ada", "sex": "f", "age": "34",
                       "height_cm": "168", "weight_kg": "61"}],
        "allergies": ["peanut"], "dislikes": [], "likes": [],
        "cuisine_affinity": [], "diet_modes": [], "meals_per_day": 3, "units": "metric",
    })

    assert api.get(f"/api/clients/{a}/profile").json()["allergies"] == ["peanut"]
    other = api.get(f"/api/clients/{b}/profile").json()
    assert other["allergies"] == [] and other["household"] == []


def test_each_client_gets_its_own_agent_session(backend_main, api):
    """Switching clients must not reuse the previous client's Agent — that
    cache is where a conversation (and its plan) would otherwise leak."""
    user_id = _as(backend_main, api, "agent-cache@example.com")
    a = api.post("/api/clients", json={"name": "Ada"}).json()["id"]
    b = api.post("/api/clients", json={"name": "Bo"}).json()["id"]

    conn = backend_main._profile_conn()
    try:
        llm_settings.save_llm_settings(conn, user_id, "http://unused.example/v1", "m", "sk-x")
    finally:
        conn.close()

    session_a = api.post(f"/api/clients/{a}/sessions").json()["id"]
    session_b = api.post(f"/api/clients/{b}/sessions").json()["id"]

    agent_a = backend_main._get_session_agent(user_id, a, session_a)
    agent_b = backend_main._get_session_agent(user_id, b, session_b)
    assert agent_a is not agent_b
    assert agent_a.client_id == a and agent_b.client_id == b
    assert backend_main._get_session_agent(user_id, a, session_a) is agent_a  # cached

    # A second conversation with the SAME client is a different agent — that's
    # what makes "new conversation" actually start fresh.
    second = api.post(f"/api/clients/{a}/sessions").json()["id"]
    assert backend_main._get_session_agent(user_id, a, second) is not agent_a

    # Editing one client's profile invalidates that client's agents only.
    backend_main._drop_session_agent(user_id, a)
    assert backend_main._get_session_agent(user_id, a, session_a) is not agent_a
    assert backend_main._get_session_agent(user_id, b, session_b) is agent_b

    # An LLM settings change invalidates every agent for the account.
    backend_main._drop_session_agent(user_id)
    assert backend_main._get_session_agent(user_id, b, session_b) is not agent_b


def test_chat_without_a_client_id_falls_back_to_the_default_client(backend_main, api):
    user_id = _as(backend_main, api, "default-client@example.com")
    res = api.post("/chat", json={"message": "hello"})
    assert res.status_code == 200  # not a 404: the account gets a default client
    conn = backend_main._profile_conn()
    try:
        from aboyeur import clients as clients_mod
        assert len(clients_mod.list_clients(conn, user_id)) == 1
    finally:
        conn.close()


def test_failed_llm_turn_is_not_left_in_the_transcript(backend_main, api):
    """When the endpoint fails outright, the user sees the error notice in the
    stream but neither it nor the orphaned question is persisted — reopening
    the chat doesn't replay a stale error."""
    from aboyeur import chat_history

    user_id = _as(backend_main, api, "failed-turn@example.com")
    conn = backend_main._profile_conn()
    try:
        client_id = _profile_default_client(backend_main, conn, user_id)
        session_id = chat_history.create_session(conn, client_id)["id"]
        chat_history.append_message(conn, session_id, "user", "earlier answered turn")
        chat_history.append_message(conn, session_id, "assistant", "earlier reply")
    finally:
        conn.close()

    class _BoomAgent:
        def send(self, _message):
            raise RuntimeError("endpoint down")
            yield  # pragma: no cover

    backend_main._record_message(session_id, "user", "plan my week")
    chunks = list(backend_main._agent_stream(_BoomAgent(), "plan my week", record_for=session_id))
    assert any("internal error" in c for c in chunks)  # user still told

    conn = backend_main._profile_conn()
    try:
        roles = [m["role"] for m in chat_history.list_messages(conn, session_id)]
        contents = [m["content"] for m in chat_history.list_messages(conn, session_id)]
    finally:
        conn.close()
    assert roles == ["user", "assistant"]  # the earlier turn only
    assert "internal error" not in "".join(contents)
    assert "plan my week" not in "".join(contents)


def _profile_default_client(backend_main, conn, user_id: int) -> int:
    from aboyeur import clients as clients_mod
    existing = clients_mod.list_clients(conn, user_id)
    if existing:
        return existing[0]["id"]
    return clients_mod.create_client(conn, user_id, "Default")["id"]


# ---------------------------------------------------------------------------
# Chat history, per-client favorites, and the account (vs client) split
# ---------------------------------------------------------------------------

def test_chat_transcript_is_recorded_and_scoped_per_client(backend_main, api):
    from aboyeur import chat_history

    _as(backend_main, api, "transcript@example.com")
    a = api.post("/api/clients", json={"name": "Ada"}).json()["id"]
    b = api.post("/api/clients", json={"name": "Bo"}).json()["id"]

    # /profile is answered without the LLM, so it exercises recording without
    # needing a model on the other end.
    api.post("/chat", json={"message": "/profile", "client_id": a})

    body = api.get(f"/api/clients/{a}/messages").json()
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    assert body["messages"][0]["content"] == "/profile"
    assert api.get(f"/api/clients/{b}/messages").json()["messages"] == []

    conn = backend_main._profile_conn()
    try:
        # ...and it's what a rebuilt agent would be seeded with.
        assert [m["role"] for m in chat_history.agent_history(conn, body["session_id"])] == [
            "user", "assistant",
        ]
    finally:
        conn.close()

    assert api.delete(f"/api/clients/{a}/messages").json()["cleared"] == 2
    assert api.get(f"/api/clients/{a}/messages").json()["messages"] == []


def test_transcripts_are_not_readable_across_accounts(backend_main, api):
    _as(backend_main, api, "transcript-owner@example.com")
    mine = api.post("/api/clients", json={"name": "Mine"}).json()["id"]
    api.post("/chat", json={"message": "/profile", "client_id": mine})

    _as(backend_main, api, "transcript-rival@example.com")
    assert api.get(f"/api/clients/{mine}/messages").status_code == 404
    assert api.delete(f"/api/clients/{mine}/messages").status_code == 404


def test_favorites_are_per_client(backend_main, api):
    _as(backend_main, api, "favorites@example.com")
    a = api.post("/api/clients", json={"name": "Ada"}).json()["id"]
    b = api.post("/api/clients", json={"name": "Bo"}).json()["id"]

    conn = backend_main._profile_conn()
    try:
        # This backend fixture runs against a throwaway DB with no corpus
        # indexed, so favoriting needs a recipe row to point at.
        conn.execute(
            "INSERT OR IGNORE INTO recipes (id, path, title, book, cuisine, servings, "
            "ingredients_json, tags_json, mtime, indexed_at) VALUES "
            "(1, '/tmp/fixture.md', 'Fixture Stew', 'Fixtures', 'test', 2, '[]', '[]', 0, ?)",
            ("2026-01-01T00:00:00Z",),
        )
        conn.commit()
    finally:
        conn.close()

    def starred(client_id: int) -> bool:
        # The grid endpoint, not the detail one: detail reads the recipe file
        # off disk, and this fixture row has no file behind it.
        results = api.get(f"/api/recipes?client_id={client_id}").json()["results"]
        return next(r["favorite"] for r in results if r["id"] == 1)

    assert api.post(f"/api/recipes/1/favorite?client_id={a}").json()["favorite"] is True
    assert starred(a) is True
    assert starred(b) is False  # the same recipe, unstarred for the other client

    assert api.delete(f"/api/recipes/1/favorite?client_id={a}").json()["favorite"] is False
    assert starred(a) is False


def test_account_is_the_practitioner_not_a_client(backend_main, api):
    _as(backend_main, api, "account-owner@example.com")

    account = api.get("/api/account").json()
    assert account["email"] == "account-owner@example.com"
    # Reading the account gives the owner their own client record — "Me".
    assert account["self_client"]["name"] == "Me"

    updated = api.patch("/api/account", json={"name": "Jose", "practice": "Aboyeur Nutrition"}).json()
    assert updated["name"] == "Jose" and updated["practice"] == "Aboyeur Nutrition"
    # ...and that rename touched the account, not the client record.
    assert api.get(f"/api/clients/{account['self_client']['id']}").json()["name"] == "Me"


def test_account_email_must_stay_unique(backend_main, api):
    _as(backend_main, api, "first-owner@example.com")
    _as(backend_main, api, "second-owner@example.com")
    assert api.patch("/api/account", json={"email": "first-owner@example.com"}).status_code == 409


def test_password_change_requires_the_current_one(backend_main, api):
    from aboyeur import auth

    user_id = _as(backend_main, api, "pw-change@example.com")
    conn = backend_main._profile_conn()
    try:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                     (auth.hash_password("original-pass"), user_id))
        conn.commit()
    finally:
        conn.close()

    assert api.post("/api/account/password", json={
        "current_password": "wrong", "new_password": "brand-new-pass"}).status_code == 403
    assert api.post("/api/account/password", json={
        "current_password": "original-pass", "new_password": "short"}).status_code == 400
    assert api.post("/api/account/password", json={
        "current_password": "original-pass", "new_password": "brand-new-pass"}).status_code == 200

    conn = backend_main._profile_conn()
    try:
        assert auth.verify_login(conn, "pw-change@example.com", "brand-new-pass") == user_id
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Many conversations per client — a new chat must not cost you the old one
# ---------------------------------------------------------------------------

def test_new_conversation_keeps_the_previous_one(backend_main, api):
    _as(backend_main, api, "conversations@example.com")
    client_id = api.post("/api/clients", json={"name": "Ada"}).json()["id"]

    api.post("/chat", json={"message": "/profile", "client_id": client_id})
    first = api.get(f"/api/clients/{client_id}/messages").json()["session_id"]

    second = api.post(f"/api/clients/{client_id}/sessions").json()["id"]
    assert second != first
    api.post("/chat", json={"message": "/profile", "client_id": client_id, "session_id": second})

    # Both survive, each with its own messages.
    sessions = api.get(f"/api/clients/{client_id}/sessions").json()["sessions"]
    assert {s["id"] for s in sessions} == {first, second}
    assert all(s["message_count"] == 2 for s in sessions)
    assert len(api.get(f"/api/clients/{client_id}/messages?session_id={first}").json()["messages"]) == 2

    # Deleting one leaves the other alone.
    api.delete(f"/api/clients/{client_id}/sessions/{second}")
    remaining = api.get(f"/api/clients/{client_id}/sessions").json()["sessions"]
    assert [s["id"] for s in remaining] == [first]


def test_a_conversation_is_titled_by_its_first_message(backend_main, api):
    _as(backend_main, api, "titles@example.com")
    client_id = api.post("/api/clients", json={"name": "Ada"}).json()["id"]

    fresh = api.post(f"/api/clients/{client_id}/sessions").json()
    assert fresh["title"] == "New conversation"

    api.post("/chat", json={"message": "/profile please", "client_id": client_id,
                            "session_id": fresh["id"]})
    titled = api.get(f"/api/clients/{client_id}/sessions").json()["sessions"][0]
    assert titled["title"] == "/profile please"
    assert titled["preview"]  # the last message, for the list

    renamed = api.patch(f"/api/clients/{client_id}/sessions/{fresh['id']}",
                        json={"title": "Cut block week 3"}).json()
    assert renamed["title"] == "Cut block week 3"


def test_messages_default_to_the_most_recent_conversation(backend_main, api):
    _as(backend_main, api, "latest-conversation@example.com")
    client_id = api.post("/api/clients", json={"name": "Ada"}).json()["id"]

    api.post("/chat", json={"message": "/profile", "client_id": client_id})
    newest = api.post(f"/api/clients/{client_id}/sessions").json()["id"]
    api.post("/chat", json={"message": "/profile", "client_id": client_id, "session_id": newest})

    # No session_id: the chat screen opens on the one last used.
    assert api.get(f"/api/clients/{client_id}/messages").json()["session_id"] == newest


def test_a_conversation_belongs_to_exactly_one_client(backend_main, api):
    _as(backend_main, api, "session-scope@example.com")
    a = api.post("/api/clients", json={"name": "Ada"}).json()["id"]
    b = api.post("/api/clients", json={"name": "Bo"}).json()["id"]
    a_session = api.post(f"/api/clients/{a}/sessions").json()["id"]

    # Ada's conversation is not reachable through Bo, even on the same account.
    assert api.get(f"/api/clients/{b}/messages?session_id={a_session}").status_code == 404
    assert api.delete(f"/api/clients/{b}/sessions/{a_session}").status_code == 404
    assert api.post("/chat", json={"message": "hi", "client_id": b,
                                   "session_id": a_session}).status_code == 404

    # ...nor through another account at all.
    _as(backend_main, api, "session-rival@example.com")
    assert api.get(f"/api/clients/{a}/sessions").status_code == 404


def test_old_per_client_transcripts_fold_into_one_conversation(tmp_path):
    """The first round keyed messages by client — one endless thread. Those
    rows must land in a conversation, not vanish."""
    import sqlite3
    from aboyeur.migrate import _migrate_chat_sessions

    conn = sqlite3.connect(tmp_path / "old.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE clients (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
                              name TEXT NOT NULL, email TEXT, phone TEXT, notes TEXT,
                              status TEXT NOT NULL DEFAULT 'active', is_self INTEGER NOT NULL DEFAULT 0,
                              created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE chat_messages (id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL,
                                    role TEXT NOT NULL, content TEXT NOT NULL,
                                    created_at TEXT NOT NULL);
        INSERT INTO clients VALUES (1, 1, 'Ada', NULL, NULL, NULL, 'active', 0, 'x', 'x');
        INSERT INTO clients VALUES (2, 1, 'Bo', NULL, NULL, NULL, 'active', 0, 'x', 'x');
        INSERT INTO chat_messages VALUES (1, 1, 'user', 'plan my week', '2026-09-01T00:00:00Z');
        INSERT INTO chat_messages VALUES (2, 1, 'assistant', 'here you go', '2026-09-01T00:00:01Z');
        INSERT INTO chat_messages VALUES (3, 2, 'user', 'and mine', '2026-09-02T00:00:00Z');
        """
    )
    conn.commit()

    _migrate_chat_sessions(conn)

    sessions = {r["client_id"]: r["id"] for r in conn.execute("SELECT id, client_id FROM chat_sessions")}
    assert len(sessions) == 2  # one per client that had messages
    assert conn.execute("SELECT COUNT(*) n FROM chat_messages WHERE session_id = ?",
                        (sessions[1],)).fetchone()["n"] == 2
    assert conn.execute("SELECT COUNT(*) n FROM chat_messages WHERE session_id = ?",
                        (sessions[2],)).fetchone()["n"] == 1
    assert "session_id" in {c["name"] for c in conn.execute("PRAGMA table_info(chat_messages)")}


def test_favorites_section_finds_stars_outside_the_first_page(backend_main, api):
    """The Library's Favorites view used to fetch a page of the catalog and
    keep the starred rows. Over a real corpus a star is almost never on the
    first page, so favorites looked like they weren't saving."""
    _as(backend_main, api, "favorites-page@example.com")
    client_id = api.post("/api/clients", json={"name": "Ada"}).json()["id"]

    conn = backend_main._profile_conn()
    try:
        for i in range(1, 61):
            conn.execute(
                "INSERT OR IGNORE INTO recipes (id, path, title, book, cuisine, servings, "
                "ingredients_json, tags_json, mtime, indexed_at) VALUES "
                "(?, ?, ?, 'Fixtures', 'test', 2, '[]', '[]', 0, '2026-01-01T00:00:00Z')",
                (i, f"/tmp/fixture-{i}.md", f"Recipe {i:03d}"),
            )
        conn.commit()
    finally:
        conn.close()

    # Star one that sorts well past a default page.
    api.post(f"/api/recipes/58/favorite?client_id={client_id}")

    page = api.get(f"/api/recipes?client_id={client_id}&limit=24").json()
    assert 58 not in [r["id"] for r in page["results"]]  # not on page one

    favorites = api.get(f"/api/recipes?client_id={client_id}&favorites_only=true").json()
    assert [r["id"] for r in favorites["results"]] == [58]
    assert favorites["total"] == 1
    assert favorites["results"][0]["favorite"] is True


def test_favorites_only_is_empty_for_a_client_with_no_stars(backend_main, api):
    _as(backend_main, api, "favorites-empty@example.com")
    client_id = api.post("/api/clients", json={"name": "Ada"}).json()["id"]
    body = api.get(f"/api/recipes?client_id={client_id}&favorites_only=true").json()
    assert body["results"] == [] and body["total"] == 0
