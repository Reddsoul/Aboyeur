"""The four hardening changes from the pre-launch audit, tested where an
attacker would actually push on them: the URL policy for BYOK endpoints
(SSRF), the legacy-route kill switch, session revocation, and — via
test_account.py — hashed email tokens.
"""

from __future__ import annotations

import os

import pytest

from aboyeur import urlguard


# ---------------------------------------------------------------------------
# urlguard
# ---------------------------------------------------------------------------


@pytest.fixture()
def strict(monkeypatch):
    """The hosted deploy's policy: COOKIE_SECURE on, no private override."""
    monkeypatch.setenv("COOKIE_SECURE", "true")
    monkeypatch.delenv("ALLOW_PRIVATE_LLM_URLS", raising=False)


@pytest.mark.parametrize("url", [
    "http://litestream:9090/metrics",          # sibling container, plain http
    "https://backend:8000/api/admin/users",    # no dot -> not a public hostname
    "https://169.254.169.254/latest/meta-data",  # cloud metadata, link-local
    "https://127.0.0.1/v1",
    "https://[::1]/v1",
    "https://10.0.0.5/v1",
    "https://192.168.1.10:4000/v1",
    "https://100.70.6.1:4000/v1",              # CGNAT / Tailscale
    "https://user:pw@api.openai.com/v1",       # embedded credentials
    "ftp://api.openai.com/v1",
    "api.openai.com/v1",                       # no scheme
    "https://llm.internal/v1",
    "https://box.local/v1",
])
def test_strict_policy_rejects(strict, url):
    with pytest.raises(urlguard.UnsafeUrlError):
        urlguard.check_llm_url(url, resolve=False)


@pytest.mark.parametrize("url", [
    "https://api.openai.com/v1",
    "https://api.anthropic.com/v1/",
    "https://openrouter.ai/api/v1",
    "https://8.8.8.8/v1",                      # public literal IP is fine
])
def test_strict_policy_allows_public_https(strict, url):
    assert urlguard.check_llm_url(url, resolve=False) == url.rstrip("/")


def test_strict_policy_resolves_dns_to_public(strict, monkeypatch):
    import socket

    def fake_getaddrinfo(host, port, proto=0):
        ip = {"evil.example.com": "10.1.2.3", "good.example.com": "93.184.216.34"}[host]
        return [(socket.AF_INET, socket.SOCK_STREAM, proto, "", (ip, port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(urlguard.UnsafeUrlError):
        urlguard.check_llm_url("https://evil.example.com/v1")
    assert urlguard.check_llm_url("https://good.example.com/v1")


def test_dev_policy_allows_private(monkeypatch):
    monkeypatch.setenv("COOKIE_SECURE", "false")
    assert urlguard.check_llm_url("http://100.70.6.1:4000/v1", resolve=False)
    assert urlguard.check_llm_url("http://localhost:11434/v1", resolve=False)


def test_explicit_override_allows_private(monkeypatch):
    monkeypatch.setenv("COOKIE_SECURE", "true")
    monkeypatch.setenv("ALLOW_PRIVATE_LLM_URLS", "true")
    assert urlguard.check_llm_url("http://litellm:4000/v1", resolve=False)


@pytest.mark.parametrize("url", ["", "not a url", "https://", "mailto:x@y.z"])
def test_garbage_rejected_under_any_policy(monkeypatch, url):
    monkeypatch.setenv("COOKIE_SECURE", "false")
    with pytest.raises(urlguard.UnsafeUrlError):
        urlguard.check_llm_url(url, resolve=False)


def test_save_llm_settings_applies_policy(db_conn, user_id, monkeypatch):
    from aboyeur import llm_settings

    monkeypatch.setenv("COOKIE_SECURE", "true")
    with pytest.raises(ValueError):
        llm_settings.save_llm_settings(db_conn, user_id, "http://backend:8000", "m", "k")
    assert llm_settings.get_llm_settings(db_conn, user_id) is None
    llm_settings.save_llm_settings(db_conn, user_id, "https://api.openai.com/v1", "m", "k")


# ---------------------------------------------------------------------------
# Routes: legacy kill switch + session revocation
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def backend_main(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("backend-sec") / "aboyeur.db"
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


def _login(backend_main, api, email: str) -> int:
    from aboyeur import auth

    conn = backend_main._profile_conn()
    try:
        user_id = auth.get_or_create_user(conn, email)
        token = auth.issue_session(conn, user_id)
    finally:
        conn.close()
    api.cookies.set(backend_main._SESSION_COOKIE, token)
    return user_id


@pytest.mark.parametrize("method,path", [
    ("DELETE", "/books/anything"),
    ("GET", "/books"),
    ("POST", "/pantry/add"),
    ("POST", "/menu/generate"),
    ("GET", "/preferences"),
    ("GET", "/v1/models"),
    ("POST", "/v1/chat/completions"),
    ("GET", "/stats"),
])
def test_legacy_routes_are_404_by_default(backend_main, api, method, path):
    assert backend_main._LEGACY_ENABLED is False
    r = api.request(method, path, json={})
    assert r.status_code == 404


def test_api_routes_unaffected_by_legacy_block(backend_main, api):
    # /api/recipes shares a word with the blocked /recipes/correct — prefix
    # matching must not swallow it.
    _login(backend_main, api, "legacy-check@example.com")
    assert api.get("/api/recipes?limit=1").status_code != 404
    assert api.get("/health").status_code == 200


def test_old_cookie_dies_after_logout_all(backend_main, api):
    _login(backend_main, api, "revoke@example.com")
    assert api.get("/auth/me").status_code == 200
    stolen = api.cookies.get(backend_main._SESSION_COOKIE)

    assert api.post("/auth/logout-all").status_code == 200

    api.cookies.set(backend_main._SESSION_COOKIE, stolen)
    r = api.get("/auth/me")
    assert r.status_code == 401
    assert "revoked" in r.json()["detail"]


def test_password_change_revokes_other_sessions_but_keeps_this_one(backend_main, api):
    from aboyeur import auth

    uid = _login(backend_main, api, "pwchange@example.com")
    conn = backend_main._profile_conn()
    try:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                     (auth.hash_password("old-password-123"), uid))
        conn.commit()
        other_device = auth.issue_session(conn, uid)
    finally:
        conn.close()

    r = api.post("/api/account/password",
                 json={"current_password": "old-password-123", "new_password": "new-password-456"})
    assert r.status_code == 200, r.text
    # The responding browser got a fresh cookie and keeps working.
    assert api.get("/auth/me").status_code == 200

    # The jar now also holds the Set-Cookie'd fresh token (domain-scoped);
    # clear it so only the other device's cookie is presented.
    api.cookies.clear()
    api.cookies.set(backend_main._SESSION_COOKIE, other_device)
    assert api.get("/auth/me").status_code == 401


def test_reset_password_revokes_sessions(backend_main, api):
    from aboyeur import account, auth

    uid = _login(backend_main, api, "reset-revoke@example.com")
    assert api.get("/auth/me").status_code == 200
    conn = backend_main._profile_conn()
    try:
        token = account.issue_token(conn, uid, "reset")
        account.reset_password(conn, token, "brand-new-password-789", pwned_check=lambda p: False)
    finally:
        conn.close()
    assert api.get("/auth/me").status_code == 401


def test_login_mints_current_session_version(backend_main, api):
    from aboyeur import auth

    conn = backend_main._profile_conn()
    try:
        uid = auth.get_or_create_user(conn, "login-sv@example.com")
        conn.execute("UPDATE users SET password_hash = ?, session_version = 7 WHERE id = ?",
                     (auth.hash_password("some-password-000"), uid))
        conn.commit()
    finally:
        conn.close()
    r = api.post("/auth/login", json={"email": "login-sv@example.com", "password": "some-password-000"})
    assert r.status_code == 200, r.text
    _, sv = auth.decode_token(api.cookies.get(backend_main._SESSION_COOKIE))
    assert sv == 7
    assert api.get("/auth/me").status_code == 200
