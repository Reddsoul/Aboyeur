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

    agent_a = backend_main._get_session_agent(user_id, a)
    agent_b = backend_main._get_session_agent(user_id, b)
    assert agent_a is not agent_b
    assert agent_a.client_id == a and agent_b.client_id == b
    assert backend_main._get_session_agent(user_id, a) is agent_a  # cached per client

    # Editing one client's profile invalidates only that client's agent.
    backend_main._drop_session_agent(user_id, a)
    assert backend_main._get_session_agent(user_id, a) is not agent_a
    assert backend_main._get_session_agent(user_id, b) is agent_b

    # An LLM settings change invalidates every client's agent for the account.
    backend_main._drop_session_agent(user_id)
    assert backend_main._get_session_agent(user_id, b) is not agent_b


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
