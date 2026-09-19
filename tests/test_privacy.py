"""PII at rest, admin-console redaction, the sensitivity classifier, and
the data-rights routes (export / delete / purge)."""

from __future__ import annotations

import json
import zipfile
from io import BytesIO
from types import SimpleNamespace

import pytest

from aboyeur import account, admin, chat_history, clients, crypto, sensitivity
from aboyeur.migrate import migrate
from aboyeur.tools.profile import get_profile, update_profile


@pytest.fixture()
def conn(db_conn):
    migrate(db_conn)
    return db_conn


# ---------------------------------------------------------------------------
# PII at rest
# ---------------------------------------------------------------------------


def test_client_pii_is_ciphertext_in_the_db_and_plaintext_through_the_accessor(conn, user_id):
    c = clients.create_client(conn, user_id, "Ada Lovelace", email="ada@example.com",
                              phone="555-0100", notes="allergic to shellfish")
    raw = conn.execute("SELECT name, email, phone, notes FROM clients WHERE id = ?", (c["id"],)).fetchone()
    for col in ("name", "email", "phone", "notes"):
        assert raw[col].startswith("enc:") and "Ada" not in raw[col] and "shellfish" not in raw[col]
    assert c["name"] == "Ada Lovelace" and c["notes"] == "allergic to shellfish"
    assert clients.client_name(conn, c["id"]) == "Ada Lovelace"
    assert clients.list_clients(conn, user_id)[0]["email"] == "ada@example.com"
    updated = clients.update_client(conn, user_id, c["id"], {"phone": None, "name": "Ada L."})
    assert updated["phone"] is None and updated["name"] == "Ada L."
    assert conn.execute("SELECT phone FROM clients WHERE id = ?", (c["id"],)).fetchone()[0] is None


def test_profile_data_is_encrypted_and_readable(conn, client_id):
    update_profile(conn, client_id, {"add_allergies": ["peanuts"]})
    raw = conn.execute("SELECT data_json FROM profile WHERE client_id = ?", (client_id,)).fetchone()[0]
    assert raw.startswith("enc:") and "peanuts" not in raw
    assert get_profile(conn, client_id)["allergies"] == ["peanuts"]


def test_migration_encrypts_legacy_plaintext_rows_idempotently(conn, user_id):
    from aboyeur.migrate import _encrypt_pii_rows
    now = "2026-01-01T00:00:00Z"
    conn.execute(
        "INSERT INTO clients (user_id, name, email, status, is_self, created_at, updated_at) "
        "VALUES (?, 'Plain Jane', 'jane@example.com', 'active', 0, ?, ?)", (user_id, now, now))
    cid = conn.execute("SELECT id FROM clients WHERE name = 'Plain Jane'").fetchone()[0]
    conn.execute("INSERT INTO profile (client_id, data_json, updated_at) VALUES (?, ?, ?)",
                 (cid, json.dumps({"allergies": ["milk"]}), now))
    conn.commit()
    _encrypt_pii_rows(conn)
    first = conn.execute("SELECT name FROM clients WHERE id = ?", (cid,)).fetchone()[0]
    assert first.startswith("enc:")
    _encrypt_pii_rows(conn)   # second pass: untouched (no double-encryption)
    assert conn.execute("SELECT name FROM clients WHERE id = ?", (cid,)).fetchone()[0] == first
    assert clients.client_name(conn, cid) == "Plain Jane"
    assert get_profile(conn, cid)["allergies"] == ["milk"]


def test_no_fts_table_indexes_pii(conn):
    ftses = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND sql LIKE '%fts5%'")]
    assert set(ftses) == {"recipes_fts", "nutrition_reference_fts"}


def test_field_crypto_roundtrip_and_legacy_passthrough():
    assert crypto.encrypt_field(None) is None
    token = crypto.encrypt_field("hello")
    assert token.startswith("enc:") and crypto.decrypt_field(token) == "hello"
    assert crypto.encrypt_field(token) == token          # idempotent
    assert crypto.decrypt_field("legacy plaintext") == "legacy plaintext"


# ---------------------------------------------------------------------------
# Admin console redaction
# ---------------------------------------------------------------------------


def _conversation(conn, user_id):
    cid = clients.create_client(conn, user_id, "Secret Client", email="sc@example.com")["id"]
    sid = chat_history.create_session(conn, cid)["id"]
    m1 = chat_history.append_message(conn, sid, "user", "hi, my email is me@example.com")
    chat_history.append_message(conn, sid, "assistant", "hello! call 555-123-4567")
    m2 = chat_history.append_message(conn, sid, "user", "I was diagnosed with type 2 diabetes")
    chat_history.append_message(conn, sid, "assistant", "noted, planning low-GI meals")
    chat_history.append_message(conn, sid, "user", "what about rice?")
    chat_history.append_message(conn, sid, "assistant", "brown rice works")
    sensitivity.store(conn, m1, "none", [])
    sensitivity.store(conn, m2, "sensitive", ["diagnosis"])
    # m3 left unclassified (NULL) -> treated as not flagged, masked only
    return cid, sid


def test_admin_never_sees_client_names_or_profiles(conn, user_id):
    cid, sid = _conversation(conn, user_id)
    update_profile(conn, cid, {"add_allergies": ["latex"]})
    u = admin.get_user(conn, user_id)
    dumped = json.dumps(u)
    assert "Secret Client" not in dumped and "sc@example.com" not in dumped and "latex" not in dumped
    assert u["clients"][0]["id"] == cid and "name" not in u["clients"][0]
    assert u["sensitive_sessions"] == 1
    assert u["sessions"][0]["sensitive"] is True and "client_name" not in u["sessions"][0]
    assert "Secret Client" not in json.dumps(admin.list_users(conn))


def test_transcript_redacts_flagged_and_masks_the_rest(conn, user_id):
    _, sid = _conversation(conn, user_id)
    t = admin.get_transcript(conn, sid)
    by_id = {m["id"]: m for m in t["messages"]}
    msgs = t["messages"]
    assert msgs[0]["content"] == "hi, my email is [email]" and msgs[0]["redacted"] is False
    assert msgs[1]["content"] == "hello! call [phone]"
    assert msgs[2]["redacted"] is True and msgs[2]["content"] is None
    assert msgs[2]["categories"] == ["diagnosis"]
    # The assistant reply to a flagged question is redacted with it.
    assert msgs[3]["redacted"] is True
    assert msgs[4]["content"] == "what about rice?" and msgs[5]["redacted"] is False
    assert t["redacted_count"] == 2 and t["revealed"] is False
    assert "client_name" not in t
    assert conn.execute("SELECT COUNT(*) FROM admin_audit").fetchone()[0] == 0
    assert by_id  # silence unused


def test_reveal_returns_content_and_is_audited_and_visible_to_user(conn, user_id):
    _, sid = _conversation(conn, user_id)
    t = admin.get_transcript(conn, sid, reveal=True, admin_id=42)
    assert t["messages"][2]["content"] == "I was diagnosed with type 2 diabetes"
    assert t["messages"][0]["content"] == "hi, my email is me@example.com"
    audit = conn.execute("SELECT * FROM admin_audit").fetchone()
    assert audit["action"] == "reveal_transcript" and audit["target"] == f"session:{sid}"
    assert audit["admin_id"] == 42
    seen = conn.execute("SELECT * FROM support_access WHERE user_id = ?", (user_id,)).fetchone()
    assert seen["session_id"] == sid and seen["admin_id"] == 42
    with pytest.raises(admin.AdminError):
        admin.get_transcript(conn, sid, reveal=True)


def test_unknown_label_is_treated_as_sensitive(conn, user_id):
    _, sid = _conversation(conn, user_id)
    m = conn.execute("SELECT id FROM chat_messages WHERE session_id = ? AND role='user' "
                     "ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
    sensitivity.mark_unknown(conn, m["id"])
    t = admin.get_transcript(conn, sid)
    assert t["messages"][4]["redacted"] is True and t["messages"][4]["sensitivity"] == "unknown"


def test_mask_pii_patterns():
    s = ("mail a.b+c@ex-ample.co.uk or +1 (415) 555-0199, key sk-proj-abcdefghijklmnop12, "
         "Authorization: Bearer eyJhbGci.xyz.123, id 12345")
    out = sensitivity.mask_pii(s)
    assert "[email]" in out and "[phone]" in out and "[key]" in out and "[redacted]" in out
    assert "a.b+c@" not in out and "555-0199" not in out and "abcdefghij" not in out and "eyJ" not in out
    assert "id 12345" in out
    assert admin.sanitize_log_lines(["user x@y.io failed"]) == ["user [email] failed"]


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,expect", [
    ('{"sensitive": true, "categories": ["diagnosis"]}', (True, ["diagnosis"])),
    ('```json\n{"sensitive": false, "categories": ["none"]}\n```', (False, [])),
    ('{"sensitive": false, "categories": ["medication"]}', (True, ["medication"])),  # categories win
    ('{"sensitive": "yes", "categories": []}', None),
    ('{"sensitive": true}', None),
    ('not json', None),
    ('', None),
    ('{"sensitive": true, "categories": ["made_up", "lab"]}', (True, ["lab"])),
])
def test_parse_reply_is_strict(text, expect):
    assert sensitivity.parse_reply(text) == expect


class _FakeLlm:
    def __init__(self, reply=None, raise_=None, usage=(30, 8)):
        self.reply, self.raise_, self.usage_t = reply, raise_, usage
        self.calls = []

    def with_options(self, **kw):
        return self

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kw):
        self.calls.append(kw)
        if self.raise_:
            raise self.raise_
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.reply))],
            usage=SimpleNamespace(prompt_tokens=self.usage_t[0], completion_tokens=self.usage_t[1]),
        )


def test_classify_text_labels_and_fails_closed():
    llm = _FakeLlm('{"sensitive": true, "categories": ["pregnancy"]}')
    label, cats, usage = sensitivity.classify_text(llm, "m", "I'm 12 weeks pregnant")
    assert (label, cats, usage) == ("sensitive", ["pregnancy"], (30, 8))
    call = llm.calls[0]
    assert call["max_tokens"] == 60 and call["temperature"] == 0
    assert "I'm 12 weeks pregnant" in call["messages"][1]["content"]
    assert sensitivity.classify_text(_FakeLlm('{"sensitive": false, "categories": []}'), "m", "rice?")[0] == "none"
    assert sensitivity.classify_text(_FakeLlm("garbage"), "m", "x")[0] == "unknown"
    assert sensitivity.classify_text(_FakeLlm(raise_=RuntimeError("down")), "m", "x")[0] == "unknown"


@pytest.fixture(scope="module")
def backend_main(tmp_path_factory):
    import os
    db_path = tmp_path_factory.mktemp("backend-privacy") / "aboyeur.db"
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


def test_classifier_runs_after_turn_and_meters_hosted(backend_main):
    from aboyeur import auth, billing, llm_providers
    c = backend_main._profile_conn()
    try:
        uid = auth.get_or_create_user(c, "classify@example.com")
        cid = clients.create_client(c, uid, "C")["id"]
        sid = chat_history.create_session(c, cid)["id"]
        chat_history.append_message(c, sid, "user", "my lab results say A1C 7.1")
        chat_history.append_message(c, sid, "assistant", "ok")
        p = llm_providers.create_provider(c, name="CP", base_url="https://p", model="m", api_key="k",
                                          price_in_per_mtok=1_000_000, price_out_per_mtok=1_000_000)
        # Pin the provider: backend.main is imported once per pytest process,
        # so this DB is shared with other backend test modules and their
        # default provider.
        c.execute("UPDATE users SET llm_mode='hosted', plan='pro', billing_status='active', "
                  "credit_cents=1000, llm_provider_id=? WHERE id=?", (p["id"], uid))
        c.commit()
        resolved = llm_providers.resolve_llm(c, backend_main._aboyeur_config, billing.get_user(c, uid))
    finally:
        c.close()
    agent = SimpleNamespace(
        client=_FakeLlm('{"sensitive": true, "categories": ["lab"]}', usage=(30, 8)),
        config=SimpleNamespace(llm_model="m"), resolved=resolved,
    )
    backend_main._classify_session_messages(agent, uid, sid)
    c = backend_main._profile_conn()
    try:
        row = c.execute("SELECT sensitivity, sensitivity_json FROM chat_messages WHERE session_id=? "
                        "AND role='user'", (sid,)).fetchone()
        assert row["sensitivity"] == "sensitive" and json.loads(row["sensitivity_json"]) == ["lab"]
        led = billing.user_ledger(c, uid)[0]
        # 30 in + 8 out at 1_000_000c/Mtok = 38c cost -> ceil(38 * 1.3) = 50c charge
        assert led["kind"] == "classify" and led["cost_cents"] == 38 and led["charge_cents"] == 50
        # Already-labeled messages are not re-run.
        n = len(agent.client.calls)
        backend_main._classify_session_messages(agent, uid, sid)
        assert len(agent.client.calls) == n
    finally:
        c.close()


def test_http_transcript_reveal_flag_and_log_tail_masking(backend_main):
    from fastapi.testclient import TestClient
    from aboyeur import auth
    api = TestClient(backend_main.app)
    c = backend_main._profile_conn()
    try:
        uid = auth.get_or_create_user(c, "victim@example.com")
        _, sid = _conversation(c, uid)
        op = auth.get_or_create_user(c, "operator-p@example.com")
        admin.set_admin(c, "operator-p@example.com", True)
    finally:
        c.close()
    api.cookies.set(backend_main._SESSION_COOKIE, auth.issue_token(op))
    t = api.get(f"/api/admin/sessions/{sid}").json()
    assert t["messages"][2]["redacted"] is True
    t = api.get(f"/api/admin/sessions/{sid}?reveal=1").json()
    assert t["messages"][2]["content"].startswith("I was diagnosed")
    # The user sees it on their account page.
    api.cookies.set(backend_main._SESSION_COOKIE, auth.issue_token(uid))
    acct = api.get("/api/account").json()
    assert acct["support_access"][0]["session_id"] == sid
    assert api.get("/api/billing/me").json()["support_access"][0]["session_id"] == sid


# ---------------------------------------------------------------------------
# Data rights
# ---------------------------------------------------------------------------


def test_export_contains_only_own_rows(conn, user_id):
    from aboyeur import auth
    other = auth.get_or_create_user(conn, "other-export@example.com")
    _conversation(conn, user_id)
    _conversation(conn, other)
    mine = clients.list_clients(conn, user_id)
    other_clients = clients.list_clients(conn, other)
    update_profile(conn, other_clients[0]["id"], {"add_allergies": ["other-secret"]})
    update_profile(conn, mine[0]["id"], {"add_allergies": ["mine-only"]})
    blob = account.export_zip(conn, user_id)
    zf = zipfile.ZipFile(BytesIO(blob))
    names = zf.namelist()
    assert "account.json" in names and "clients.json" in names
    assert any(n.startswith(f"clients/{mine[0]['id']}/") for n in names)
    assert not any(n.startswith(f"clients/{other_clients[0]['id']}/") for n in names)
    everything = "".join(zf.read(n).decode() for n in names)
    assert "mine-only" in everything and "other-secret" not in everything
    assert "Secret Client" in everything            # decrypted, their own data
    assert "I was diagnosed" in everything          # transcripts included, unredacted
    acct = json.loads(zf.read("account.json"))
    assert "password_hash" not in acct and "stripe_customer_id" not in acct


def test_soft_delete_then_purge(conn, user_id):
    cid, sid = _conversation(conn, user_id)
    update_profile(conn, cid, {"add_allergies": ["x"]})
    conn.execute("INSERT INTO feedback (created_at, updated_at, user_id, client_id, session_id, "
                 "message_id, rating, body) VALUES ('t','t',?,?,?,1,-1,'bad')", (user_id, cid, sid))
    admin.record_event(conn, "chat_turn", user_id=user_id, client_id=cid)
    conn.commit()
    out = account.soft_delete(conn, user_id)
    assert out["deleted_at"] and out["purge_after"] > out["deleted_at"][:10]
    assert account.is_deleted(conn, user_id)
    # Idempotent.
    assert account.soft_delete(conn, user_id)["deleted_at"] == out["deleted_at"]
    # Not old enough yet: nothing purged.
    assert account.purge_deleted(conn, older_than_days=30) == []
    conn.execute("UPDATE users SET deleted_at = '2020-01-01T00:00:00Z' WHERE id = ?", (user_id,))
    conn.commit()
    assert account.purge_deleted(conn, older_than_days=30) == [user_id]
    assert conn.execute("SELECT COUNT(*) FROM users WHERE id = ?", (user_id,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM clients WHERE id = ?", (cid,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM chat_messages WHERE session_id = ?", (sid,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM profile WHERE client_id = ?", (cid,)).fetchone()[0] == 0
    fb = conn.execute("SELECT user_id, body, client_id FROM feedback").fetchone()
    assert fb["user_id"] == user_id and fb["body"] is None and fb["client_id"] is None
    ev = conn.execute("SELECT user_id, client_id FROM events").fetchone()
    assert ev["user_id"] is None and ev["client_id"] is None
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_http_delete_cancels_stripe_and_locks_the_session(backend_main):
    from fastapi.testclient import TestClient
    from aboyeur import auth
    from tests.test_billing import FakeStripe
    api = TestClient(backend_main.app)
    backend_main._stripe = FakeStripe()
    c = backend_main._profile_conn()
    try:
        uid = auth.get_or_create_user(c, "leaver@example.com")
        c.execute("UPDATE users SET password_hash = ?, stripe_subscription_id = 'sub_leave', "
                  "plan='pro', billing_status='active' WHERE id = ?",
                  (auth.hash_password("correct horse"), uid))
        c.commit()
    finally:
        c.close()
    api.cookies.set(backend_main._SESSION_COOKIE, auth.issue_token(uid))
    assert api.request("DELETE", "/api/account", json={"confirm": "nope", "password": "x"}).status_code == 400
    assert api.request("DELETE", "/api/account", json={"confirm": "DELETE", "password": "wrong"}).status_code == 403
    res = api.request("DELETE", "/api/account", json={"confirm": "DELETE", "password": "correct horse"})
    assert res.status_code == 200 and res.json()["purge_after"]
    assert backend_main._stripe.named("cancel") == [{"subscription_id": "sub_leave", "prorate": False}]
    # Same cookie no longer works; neither does the password.
    api.cookies.set(backend_main._SESSION_COOKIE, auth.issue_token(uid))
    assert api.get("/api/account").status_code == 401
    assert api.post("/auth/login", json={"email": "leaver@example.com",
                                         "password": "correct horse"}).status_code == 401
    # Export still works for a live account, as a zip.
    c = backend_main._profile_conn()
    try:
        live = auth.get_or_create_user(c, "exporter@example.com")
    finally:
        c.close()
    api.cookies.set(backend_main._SESSION_COOKIE, auth.issue_token(live))
    res = api.get("/api/account/export")
    assert res.status_code == 200 and res.headers["content-type"] == "application/zip"
    assert zipfile.ZipFile(BytesIO(res.content)).namelist()
