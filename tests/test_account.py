"""Self-serve accounts: signup (+ invite gate), email verification gating
chat, password reset tokens, ToS re-acceptance, mail plumbing."""

from __future__ import annotations

import pytest

from aboyeur import account, auth, mail
from aboyeur.legal import LEGAL_VERSION
from aboyeur.migrate import migrate


@pytest.fixture()
def conn(db_conn):
    migrate(db_conn)
    return db_conn


NOT_PWNED = lambda pw: False  # noqa: E731
GOOD_PW = "correct-horse-battery"


# ---------------------------------------------------------------------------
# Signup
# ---------------------------------------------------------------------------


def test_signup_open_mode_happy_path(conn):
    uid = account.signup(conn, email="New@Example.com ", password=GOOD_PW, signup_mode="open",
                         tos_version=LEGAL_VERSION, country="us", pwned_check=NOT_PWNED)
    row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    assert row["email"] == "new@example.com" and row["country"] == "US"
    assert row["tos_version"] == LEGAL_VERSION and row["tos_accepted_at"]
    assert row["email_verified_at"] is None and row["plan"] == "free"
    assert auth.verify_login(conn, "new@example.com", GOOD_PW) == uid
    # A "Me" client exists from the start.
    from aboyeur import clients
    assert clients.list_clients(conn, uid)[0]["is_self"] is True


def test_signup_validation(conn):
    kw = dict(signup_mode="open", tos_version=LEGAL_VERSION, pwned_check=NOT_PWNED)
    with pytest.raises(account.AccountError):
        account.signup(conn, email="nope", password=GOOD_PW, **kw)
    with pytest.raises(account.AccountError) as e:
        account.signup(conn, email="a@b.co", password="short", **kw)
    assert "10 characters" in str(e.value)
    with pytest.raises(account.AccountError) as e:
        account.signup(conn, email="a@b.co", password=GOOD_PW, signup_mode="open",
                       tos_version="2000-01-01", pwned_check=NOT_PWNED)
    assert e.value.extra["error"] == "tos"
    with pytest.raises(account.AccountError) as e:
        account.signup(conn, email="a@b.co", password=GOOD_PW, signup_mode="open",
                       tos_version=LEGAL_VERSION, pwned_check=lambda pw: True)
    assert e.value.extra["error"] == "pwned"
    account.signup(conn, email="dupe@b.co", password=GOOD_PW, **kw)
    with pytest.raises(account.AccountError) as e:
        account.signup(conn, email="DUPE@b.co", password=GOOD_PW, **kw)
    assert e.value.status == 409


def test_invite_gate(conn):
    kw = dict(password=GOOD_PW, signup_mode="invite", tos_version=LEGAL_VERSION, pwned_check=NOT_PWNED)
    with pytest.raises(account.AccountError) as e:
        account.signup(conn, email="x@y.co", **kw)
    assert e.value.extra["error"] == "invite_required"
    with pytest.raises(account.AccountError) as e:
        account.signup(conn, email="x@y.co", invite_code="bogus", **kw)
    assert e.value.extra["error"] == "invite_invalid"
    inv = account.create_invite(conn, None, max_uses=1)
    account.signup(conn, email="x@y.co", invite_code=inv["code"], **kw)
    with pytest.raises(account.AccountError) as e:
        account.signup(conn, email="z@y.co", invite_code=inv["code"], **kw)
    assert e.value.extra["error"] == "invite_used"
    # A failed signup (duplicate email) doesn't burn the invite.
    multi = account.create_invite(conn, None, max_uses=2)
    with pytest.raises(account.AccountError):
        account.signup(conn, email="x@y.co", invite_code=multi["code"], **kw)
    assert conn.execute("SELECT uses FROM invite_codes WHERE code = ?", (multi["code"],)).fetchone()[0] == 0
    expired = account.create_invite(conn, None, max_uses=None, expires_days=1)
    conn.execute("UPDATE invite_codes SET expires_at = '2020-01-01T00:00:00Z' WHERE code = ?",
                 (expired["code"],))
    conn.commit()
    with pytest.raises(account.AccountError) as e:
        account.signup(conn, email="w@y.co", invite_code=expired["code"], **kw)
    assert e.value.extra["error"] == "invite_expired"
    account.revoke_invite(conn, multi["code"])
    assert all(i["code"] != multi["code"] for i in account.list_invites(conn))


def test_pwned_check_is_best_effort(monkeypatch):
    import httpx

    def boom(*a, **k):
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(httpx, "get", boom)
    assert account.password_is_pwned("anything") is False

    class R:
        text = "0018A45C4D1DEF81644B54AB7F969B88D65:3\nFFFFF:0\n"

        def raise_for_status(self):
            pass
    monkeypatch.setattr(httpx, "get", lambda *a, **k: R())
    # sha1("password") = 5BAA61E4C9B93F3F0682250B6CF8331B7EE68FD8 -> prefix 5BAA6, suffix 1E4C9...
    assert account.password_is_pwned("password") is False  # suffix not in the fake range
    R.text = "1E4C9B93F3F0682250B6CF8331B7EE68FD8:12345\n"
    assert account.password_is_pwned("password") is True


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


def _user(conn, email="tok@example.com"):
    return account.signup(conn, email=email, password=GOOD_PW, signup_mode="open",
                          tos_version=LEGAL_VERSION, pwned_check=NOT_PWNED)


def test_verify_token_single_use_and_expiry(conn):
    uid = _user(conn)
    t1 = account.issue_token(conn, uid, "verify")
    t2 = account.issue_token(conn, uid, "verify")  # resend invalidates t1
    with pytest.raises(account.AccountError):
        account.verify_email(conn, t1)
    assert account.verify_email(conn, t2) == uid
    assert conn.execute("SELECT email_verified_at FROM users WHERE id = ?", (uid,)).fetchone()[0]
    with pytest.raises(account.AccountError) as e:
        account.verify_email(conn, t2)
    assert e.value.extra["error"] == "token"
    t3 = account.issue_token(conn, uid, "verify")
    # Only the hash is stored — the raw token must not appear in the table.
    assert conn.execute("SELECT 1 FROM email_tokens WHERE token = ?", (t3,)).fetchone() is None
    conn.execute("UPDATE email_tokens SET expires_at = '2020-01-01T00:00:00Z' WHERE token = ?",
                 (account._token_hash(t3),))
    conn.commit()
    with pytest.raises(account.AccountError) as e:
        account.verify_email(conn, t3)
    assert e.value.extra["error"] == "expired"
    # A reset token can't verify an email and vice versa.
    r = account.issue_token(conn, uid, "reset")
    with pytest.raises(account.AccountError):
        account.verify_email(conn, r)


def test_reset_flow(conn):
    uid = _user(conn, "reset@example.com")
    assert account.request_reset(conn, "nobody@example.com") is None
    got_uid, token = account.request_reset(conn, "RESET@example.com")
    assert got_uid == uid
    with pytest.raises(account.AccountError):
        account.reset_password(conn, token, "short", pwned_check=NOT_PWNED)
    account.reset_password(conn, token, "new-long-password-1", pwned_check=NOT_PWNED)
    assert auth.verify_login(conn, "reset@example.com", "new-long-password-1") == uid
    with pytest.raises(auth.AuthError):
        auth.verify_login(conn, "reset@example.com", GOOD_PW)
    with pytest.raises(account.AccountError):
        account.reset_password(conn, token, "another-long-password", pwned_check=NOT_PWNED)


def test_tos_gate(conn):
    uid = _user(conn, "tos@example.com")
    assert account.tos_status(conn, uid)["needs_acceptance"] is False
    conn.execute("UPDATE users SET tos_version = '2020-01-01' WHERE id = ?", (uid,))
    conn.commit()
    assert account.tos_status(conn, uid)["needs_acceptance"] is True
    with pytest.raises(account.AccountError) as e:
        account.accept_tos(conn, uid, "2020-01-01")
    assert e.value.status == 409
    assert account.accept_tos(conn, uid, LEGAL_VERSION)["needs_acceptance"] is False
    # CLI-provisioned accounts have never accepted anything.
    old = auth.get_or_create_user(conn, "cli@example.com")
    assert account.tos_status(conn, old)["needs_acceptance"] is True


def test_mail_logs_instead_of_sending_without_a_key(caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="aboyeur"):
        out = mail.send("to@example.com", "verify", {"link": "https://x/verify?token=abc"}, api_key=None)
    assert out == {"sent": False, "id": None}
    assert "token=abc" in caplog.text


def test_mail_posts_to_resend(monkeypatch):
    import httpx
    seen = {}

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"id": "email_1"}

    def post(url, headers, json, timeout):
        seen.update(url=url, headers=headers, json=json)
        return R()
    monkeypatch.setattr(httpx, "post", post)
    out = mail.send("to@example.com", "reset", {"email": "to@example.com", "link": "https://x/r?token=<t>"},
                    api_key="re_test", sender="Aboyeur <no-reply@aboyeur.test>")
    assert out == {"sent": True, "id": "email_1"}
    assert seen["headers"]["Authorization"] == "Bearer re_test"
    assert seen["json"]["to"] == ["to@example.com"] and "Reset" in seen["json"]["subject"]
    assert "&lt;t&gt;" in seen["json"]["html"] and "<t>" in seen["json"]["text"]


# ---------------------------------------------------------------------------
# Through the routes
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def backend_main(tmp_path_factory):
    import os
    db_path = tmp_path_factory.mktemp("backend-account") / "aboyeur.db"
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
def api(backend_main, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(account, "password_is_pwned", NOT_PWNED)
    sent: list[dict] = []
    monkeypatch.setattr(backend_main._aboyeur_mail, "send",
                        lambda to, template, ctx, **k: sent.append({"to": to, "template": template, **ctx}))
    client = TestClient(backend_main.app)
    client.sent = sent
    return client


def _invite(backend_main):
    c = backend_main._profile_conn()
    try:
        return account.create_invite(c, None, max_uses=1)["code"]
    finally:
        c.close()


def test_http_signup_requires_invite_then_verification_gates_chat(backend_main, api):
    assert api.get("/auth/signup-mode").json() == {"mode": "invite", "legal_version": LEGAL_VERSION}
    body = {"email": "web@example.com", "password": GOOD_PW, "tos_version": LEGAL_VERSION}
    res = api.post("/auth/signup", json=body)
    assert res.status_code == 403 and res.json()["detail"]["error"] == "invite_required"
    res = api.post("/auth/signup", json={**body, "invite_code": _invite(backend_main)})
    assert res.status_code == 200 and res.json()["email_verified"] is False
    assert api.cookies.get(backend_main._SESSION_COOKIE)
    me = api.get("/auth/me").json()
    assert me["email_verified"] is False and me["tos"]["needs_acceptance"] is False
    # Logged in, but chat and checkout are closed until verified.
    res = api.post("/chat", json={"message": "hi"})
    assert res.status_code == 403 and res.json()["detail"]["error"] == "unverified"
    # Verify via the emailed link.
    assert api.sent[0]["template"] == "verify"
    token = api.sent[0]["link"].split("token=")[1]
    assert api.post("/auth/verify", json={"token": "wrong"}).status_code == 400
    assert api.post("/auth/verify", json={"token": token}).json()["ok"] is True
    assert api.get("/auth/me").json()["email_verified"] is True
    # Chat now gets past the gate (409 = no LLM configured, i.e. the next gate).
    assert api.post("/chat", json={"message": "hi"}).status_code == 409
    # Resend is a no-op once verified.
    assert api.post("/auth/resend-verification").json()["already_verified"] is True


def test_http_reset_and_tos(backend_main, api):
    code = _invite(backend_main)
    api.post("/auth/signup", json={"email": "r@example.com", "password": GOOD_PW,
                                   "tos_version": LEGAL_VERSION, "invite_code": code})
    api.cookies.clear()
    assert api.post("/auth/reset", json={"email": "ghost@example.com"}).status_code == 200
    assert api.post("/auth/reset", json={"email": "r@example.com"}).status_code == 200
    reset_mail = [m for m in api.sent if m["template"] == "reset"]
    assert len(reset_mail) == 1
    token = reset_mail[0]["link"].split("token=")[1]
    res = api.post("/auth/reset/confirm", json={"token": token, "new_password": "brand-new-pass-9"})
    assert res.status_code == 200
    assert api.post("/auth/login", json={"email": "r@example.com", "password": "brand-new-pass-9"}).status_code == 200
    # ToS re-acceptance.
    c = backend_main._profile_conn()
    try:
        c.execute("UPDATE users SET tos_version = '2020-01-01' WHERE email = 'r@example.com'")
        c.commit()
    finally:
        c.close()
    assert api.get("/auth/me").json()["tos"]["needs_acceptance"] is True
    assert api.post("/api/account/accept-tos", json={"version": "2020-01-01"}).status_code == 409
    assert api.post("/api/account/accept-tos", json={"version": LEGAL_VERSION}).json()["needs_acceptance"] is False


def test_http_legal_docs_served(backend_main, api):
    idx = api.get("/api/legal").json()
    assert idx["version"] == LEGAL_VERSION and "terms" in idx["documents"]
    res = api.get("/api/legal/terms")
    assert res.status_code == 200 and "DRAFT" in res.text and "[ENTITY NAME]" in res.text
    assert api.get("/api/legal/nope").status_code == 404
    for slug in idx["documents"]:
        assert api.get(f"/api/legal/{slug}").status_code == 200


def test_http_admin_invites(backend_main, api):
    from aboyeur import admin as admin_mod
    c = backend_main._profile_conn()
    try:
        op = auth.get_or_create_user(c, "inviter@example.com")
        admin_mod.set_admin(c, "inviter@example.com", True)
    finally:
        c.close()
    api.cookies.set(backend_main._SESSION_COOKIE, auth.issue_token(op))
    inv = api.post("/api/admin/invites", json={"max_uses": 3, "note": "beta wave 2"}).json()
    assert inv["max_uses"] == 3 and inv["note"] == "beta wave 2"
    assert any(i["code"] == inv["code"] for i in api.get("/api/admin/invites").json()["invites"])
    assert api.delete(f"/api/admin/invites/{inv['code']}").json()["revoked"] == inv["code"]
    assert api.get("/api/admin/audit").json()["audit"][0]["action"] == "revoke_invite"


def test_health(backend_main, api):
    res = api.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True and body["db"] is True and "version" in body
    assert "secret" not in res.text.lower()
