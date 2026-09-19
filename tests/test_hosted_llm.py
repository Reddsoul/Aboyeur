"""Hosted LLM: provider registry, resolution order, metering + debit,
top-up / refund webhooks. aboyeur/llm_providers.py + the metered half of
aboyeur/billing.py, plus the turn-recording path in backend/main.py."""

from __future__ import annotations

import json

import pytest

from aboyeur import billing, llm_providers
from aboyeur.migrate import migrate
from tests.test_billing import FakeStripe, event


@pytest.fixture()
def conn(db_conn):
    migrate(db_conn)
    return db_conn


@pytest.fixture()
def uid(conn, user_id):
    conn.execute("UPDATE users SET email_verified_at = created_at WHERE id = ?", (user_id,))
    conn.commit()
    return user_id


@pytest.fixture()
def provider(conn):
    return llm_providers.create_provider(
        conn, name="Fast", base_url="https://llm.example/v1", model="fast-1",
        api_key="sk-secret-1234", price_in_per_mtok=100, price_out_per_mtok=400, markup_pct=30,
    )


def _set(conn, uid, **cols):
    sets = ", ".join(f"{k} = ?" for k in cols)
    conn.execute(f"UPDATE users SET {sets} WHERE id = ?", (*cols.values(), uid))
    conn.commit()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_provider_public_shape_never_leaks_key(conn, provider):
    assert provider["key_last4"] == "1234"
    assert "api_key" not in provider and "api_key_encrypted" not in provider
    assert provider["is_default"] is True  # first one becomes default
    assert provider["user_price_in_per_mtok"] == 130
    row = conn.execute("SELECT api_key_encrypted FROM llm_providers").fetchone()[0]
    assert row != "sk-secret-1234"
    assert llm_providers.get_provider_secret(conn, provider["id"])["api_key"] == "sk-secret-1234"


def test_provider_update_default_and_validation(conn, provider):
    second = llm_providers.create_provider(
        conn, name="Smart", base_url="https://api.anthropic.com/v1", model="claude-haiku-4-5",
        api_key="k", price_in_per_mtok=80, price_out_per_mtok=400,
    )
    assert second["is_default"] is False
    llm_providers.set_default(conn, second["id"])
    assert [p["is_default"] for p in llm_providers.list_providers(conn)] == [True, False]
    llm_providers.update_provider(conn, second["id"], {"enabled": False})
    with pytest.raises(llm_providers.ProviderError):
        llm_providers.set_default(conn, second["id"])
    with pytest.raises(llm_providers.ProviderError):
        llm_providers.create_provider(conn, name="Fast", base_url="https://x", model="m",
                                      api_key="k", price_in_per_mtok=1, price_out_per_mtok=1)
    with pytest.raises(llm_providers.ProviderError):
        llm_providers.update_provider(conn, provider["id"], {"markup_pct": -1})
    updated = llm_providers.update_provider(conn, provider["id"], {"api_key": "sk-new-9999"})
    assert updated["key_last4"] == "9999"


# ---------------------------------------------------------------------------
# Resolution matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode,plan,status,credit,byok_saved,expect", [
    ("byok", "free", "free", 0, True, "byok"),
    ("byok", "pro", "active", 0, True, "byok"),
    ("byok", "free", "free", 0, False, 409),
    ("hosted", "free", "free", 500, False, 402),
    ("hosted", "pro", "active", 500, False, "hosted"),
    ("hosted", "pro", "active", 0, False, 402),
    ("hosted", "pro", "active", -3, False, 402),
    ("hosted", "pro", "past_due", 500, False, "hosted"),
    ("hosted", "pro", "comp", 0, False, "hosted"),
    ("hosted", "pro", "canceled", 500, False, 402),
    ("hosted", "pro", "active", 500, True, "hosted"),   # hosted mode ignores stored key
])
def test_resolution_matrix(conn, uid, provider, config, mode, plan, status, credit,
                           byok_saved, expect):
    from aboyeur import llm_settings
    if byok_saved:
        llm_settings.save_llm_settings(conn, uid, "https://mine/v1", "mine", "sk-mine")
    _set(conn, uid, llm_mode=mode, plan=plan, billing_status=status, credit_cents=credit)
    user = billing.get_user(conn, uid)
    if isinstance(expect, int):
        with pytest.raises(llm_providers.ProviderError) as e:
            llm_providers.resolve_llm(conn, config, user)
        assert e.value.status == expect
        return
    r = llm_providers.resolve_llm(conn, config, user)
    assert r.mode == expect
    if expect == "byok":
        assert r.config.llm_base_url == "https://mine/v1" and r.billable is False
        assert r.provider is None
    else:
        assert r.config.llm_base_url == "https://llm.example/v1"
        assert r.config.llm_api_key == "sk-secret-1234" and r.billable is True
        assert r.provider_id == provider["id"]


def test_admin_override_wins_over_default(conn, uid, provider, config):
    other = llm_providers.create_provider(
        conn, name="Other", base_url="https://other/v1", model="o", api_key="k",
        price_in_per_mtok=1, price_out_per_mtok=1,
    )
    _set(conn, uid, llm_mode="hosted", plan="pro", billing_status="active", credit_cents=100)
    billing.set_user_llm(conn, 1, uid, llm_provider_id=other["id"])
    r = llm_providers.resolve_llm(conn, config, billing.get_user(conn, uid))
    assert r.provider_id == other["id"]
    # A disabled override falls back to the default rather than failing.
    llm_providers.update_provider(conn, other["id"], {"enabled": False})
    r = llm_providers.resolve_llm(conn, config, billing.get_user(conn, uid))
    assert r.provider_id == provider["id"]
    # No enabled provider at all -> 503.
    llm_providers.update_provider(conn, provider["id"], {"enabled": False})
    with pytest.raises(llm_providers.ProviderError) as e:
        llm_providers.resolve_llm(conn, config, billing.get_user(conn, uid))
    assert e.value.status == 503


def test_hosted_offer_shows_name_and_price_not_endpoint(conn, uid, provider):
    offer = llm_providers.hosted_offer(conn, billing.get_user(conn, uid))
    assert offer["name"] == "Fast" and "base_url" not in offer
    assert offer["price_in_per_ktok_cents"] == 0.13 and offer["price_out_per_ktok_cents"] == 0.52


# ---------------------------------------------------------------------------
# Debit math
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tin,tout,cost,charge", [
    (0, 0, 0, 0),
    (1, 0, 1, 2),                  # ceil(0.0001) = 1c cost, ceil(1.3) = 2c charge
    (1_000_000, 0, 100, 130),
    (500_000, 250_000, 150, 195),  # 50 + 100 = 150; *1.3 = 195
    (10_000, 2_000, 2, 3),         # 1 + 0.8 = 1.8 -> 2; 2.6 -> 3
])
def test_turn_charge_rounds_up(tin, tout, cost, charge):
    assert billing.turn_charge(100, 400, 30, tin, tout) == (cost, charge)


def test_meter_turn_debits_and_comp_is_free(conn, uid, provider):
    _set(conn, uid, llm_mode="hosted", plan="pro", billing_status="active", credit_cents=100)
    p = llm_providers.get_provider_secret(conn, provider["id"])
    out = billing.meter_turn(conn, billing.get_user(conn, uid), p, 500_000, 250_000,
                             estimated=False, event_id=None)
    assert out["charge_cents"] == 195 and out["kind"] == "chat_turn"
    # Went negative on the last turn — allowed; the next resolve blocks it.
    assert billing.get_user(conn, uid)["credit_cents"] == -95
    _set(conn, uid, billing_status="comp", credit_cents=0)
    out = billing.meter_turn(conn, billing.get_user(conn, uid), p, 500_000, 250_000,
                             estimated=True, event_id=None)
    assert out["charge_cents"] == 0 and out["cost_cents"] == 150 and out["kind"] == "chat_turn_est"
    assert billing.get_user(conn, uid)["credit_cents"] == 0
    rows = billing.user_ledger(conn, uid)
    assert [r["kind"] for r in rows] == ["chat_turn_est", "chat_turn"]
    assert rows[0]["provider_name"] == "Fast"
    assert llm_providers.estimated_rows_count(conn) == 1


def test_low_balance_email_throttle(conn, uid):
    _set(conn, uid, llm_mode="hosted", credit_cents=50)
    assert billing.needs_low_balance_email(conn, uid) is True
    assert billing.needs_low_balance_email(conn, uid) is False   # 7-day throttle
    _set(conn, uid, credit_cents=500, low_balance_emailed_at=None)
    assert billing.needs_low_balance_email(conn, uid) is False   # not low
    _set(conn, uid, llm_mode="byok", credit_cents=0)
    assert billing.needs_low_balance_email(conn, uid) is False   # byok doesn't care


# ---------------------------------------------------------------------------
# Top-up + refund webhooks
# ---------------------------------------------------------------------------


def test_topup_checkout_and_webhook(conn, uid):
    stripe = FakeStripe()
    with pytest.raises(billing.BillingError):
        billing.create_topup_checkout(conn, stripe, uid, 700, "*", "https://app.test")
    url = billing.create_topup_checkout(conn, stripe, uid, 2500, "*", "https://app.test")
    assert url.startswith("https://checkout")
    session = stripe.named("checkout")[0]
    assert session["mode"] == "payment" and session["metadata"]["kind"] == "topup"
    assert session["line_items"][0]["price_data"]["unit_amount"] == 2500
    completed = {
        "id": "cs_top_1", "mode": "payment", "payment_status": "paid", "amount_total": 2500,
        "customer": billing.get_user(conn, uid)["stripe_customer_id"],
        "metadata": {"user_id": str(uid), "kind": "topup", "amount_cents": "2500"},
    }
    ev = event("checkout.session.completed", completed)
    billing.record_event_once(conn, ev)
    assert billing.handle_event(conn, stripe, ev)["topup"] == 2500
    assert billing.get_user(conn, uid)["credit_cents"] == 2500
    # Same session in a *different* event (Stripe can resend under a new id).
    ev2 = event("checkout.session.completed", completed)
    billing.record_event_once(conn, ev2)
    assert billing.handle_event(conn, stripe, ev2)["topup"] == "duplicate"
    assert billing.get_user(conn, uid)["credit_cents"] == 2500
    assert billing.user_ledger(conn, uid)[0]["kind"] == "topup"


def test_refund_debits_wallet_possibly_negative(conn, uid):
    stripe = FakeStripe()
    billing.add_credit(conn, uid, 2500, kind="topup")
    billing.add_credit(conn, uid, -2000, kind="adjust")   # spent most of it
    charge = {"id": "ch_1", "amount_refunded": 2500, "customer": None,
              "metadata": {"user_id": str(uid), "kind": "topup"}}
    ev = event("charge.refunded", charge)
    billing.record_event_once(conn, ev)
    assert billing.handle_event(conn, stripe, ev)["refund"] == 2500
    assert billing.get_user(conn, uid)["credit_cents"] == -2000
    # Partial second refund on the same charge only moves the delta.
    ev = event("charge.refunded", {**charge, "amount_refunded": 2500})
    billing.record_event_once(conn, ev)
    assert billing.handle_event(conn, stripe, ev)["refund"] == "duplicate"
    # Negative balance blocks hosted turns until topped up.
    _set(conn, uid, llm_mode="hosted", plan="pro", billing_status="active")
    with pytest.raises(llm_providers.ProviderError) as e:
        from aboyeur.config import Config
        llm_providers.resolve_llm(conn, Config.load(), billing.get_user(conn, uid))
    assert e.value.extra["error"] == "no_credit"


def test_admin_add_credit_audits(conn, uid):
    u = billing.admin_add_credit(conn, admin_id=9, user_id=uid, cents=1000, note="beta thanks")
    assert u["credit_cents"] == 1000
    row = conn.execute("SELECT * FROM admin_audit WHERE action = 'add_credit'").fetchone()
    assert row["admin_id"] == 9 and json.loads(row["meta_json"])["cents"] == 1000
    with pytest.raises(billing.BillingError):
        billing.admin_add_credit(conn, 9, uid, 0)


# ---------------------------------------------------------------------------
# Through the backend: the turn-recording path meters in one transaction
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def backend_main(tmp_path_factory):
    import os
    db_path = tmp_path_factory.mktemp("backend-hosted") / "aboyeur.db"
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


class _FakeAgent:
    def __init__(self, resolved, usage, model="fast-1"):
        self.resolved = resolved
        self.last_usage = usage
        self.last_tool_calls = 0
        from types import SimpleNamespace
        self.config = SimpleNamespace(llm_model=model)


def _hosted_user(backend_main, email: str, credit: int) -> tuple[int, dict]:
    from aboyeur import auth
    c = backend_main._profile_conn()
    try:
        uid = auth.get_or_create_user(c, email)
        if not llm_providers.list_providers(c):
            llm_providers.create_provider(
                c, name="P", base_url="https://p/v1", model="fast-1", api_key="k",
                price_in_per_mtok=100, price_out_per_mtok=400, markup_pct=30,
            )
        c.execute("UPDATE users SET llm_mode='hosted', plan='pro', billing_status='active', "
                  "credit_cents=? WHERE id=?", (credit, uid))
        c.commit()
        resolved = llm_providers.resolve_llm(c, backend_main._aboyeur_config, billing.get_user(c, uid))
    finally:
        c.close()
    return uid, resolved


def test_turn_event_and_ledger_land_together(backend_main):
    uid, resolved = _hosted_user(backend_main, "metered@example.com", 1000)
    agent = _FakeAgent(resolved, (500_000, 250_000))
    backend_main._record_turn_event(user_id=uid, client_id=None, session_id=None, agent=agent,
                                    latency_ms=10, ok=True)
    c = backend_main._profile_conn()
    try:
        ev = c.execute("SELECT * FROM events WHERE user_id = ? ORDER BY id DESC", (uid,)).fetchone()
        led = billing.user_ledger(c, uid)[0]
        assert led["event_id"] == ev["id"] and led["charge_cents"] == 195
        assert billing.get_user(c, uid)["credit_cents"] == 805
        assert json.loads(ev["meta_json"])["provider_id"] == resolved.provider_id
    finally:
        c.close()


def test_missing_usage_is_estimated_and_marked(backend_main):
    uid, resolved = _hosted_user(backend_main, "estimated@example.com", 1000)
    agent = _FakeAgent(resolved, None)
    backend_main._record_turn_event(user_id=uid, client_id=None, session_id=None, agent=agent,
                                    latency_ms=10, ok=True, answer_chars=4000, prompt_chars=400)
    c = backend_main._profile_conn()
    try:
        led = billing.user_ledger(c, uid)[0]
        assert led["kind"] == "chat_turn_est" and led["tokens_in"] == 100 and led["tokens_out"] == 1000
        assert led["charge_cents"] == 2  # cost ceil(0.01+0.4)=1 -> charge ceil(1.3)=2
    finally:
        c.close()


def test_byok_agent_is_never_metered(backend_main):
    from aboyeur import auth, llm_settings
    c = backend_main._profile_conn()
    try:
        uid = auth.get_or_create_user(c, "byok-meter@example.com")
        llm_settings.save_llm_settings(c, uid, "https://mine/v1", "m", "k")
        resolved = llm_providers.resolve_llm(c, backend_main._aboyeur_config, billing.get_user(c, uid))
    finally:
        c.close()
    backend_main._record_turn_event(user_id=uid, client_id=None, session_id=None,
                                    agent=_FakeAgent(resolved, (10, 10)), latency_ms=1, ok=True)
    c = backend_main._profile_conn()
    try:
        assert billing.user_ledger(c, uid) == []
    finally:
        c.close()


def test_http_llm_mode_switch_and_admin_provider_routes(backend_main):
    from fastapi.testclient import TestClient
    from aboyeur import admin as admin_mod, auth
    api = TestClient(backend_main.app)
    backend_main._stripe = FakeStripe()
    c = backend_main._profile_conn()
    try:
        uid = auth.get_or_create_user(c, "switcher@example.com")
        admin_id = auth.get_or_create_user(c, "prov-admin@example.com")
        admin_mod.set_admin(c, "prov-admin@example.com", True)
    finally:
        c.close()
    api.cookies.set(backend_main._SESSION_COOKIE, auth.issue_token(uid))
    # Free user can't pick hosted.
    assert api.put("/api/llm-settings/mode", json={"mode": "hosted"}).status_code == 402
    settings = api.get("/api/llm-settings").json()
    assert settings["llm_mode"] == "byok" and settings["hosted_offer"]["name"] == "P"
    # Admin: create + test + default + set user override; key never echoed.
    api.cookies.set(backend_main._SESSION_COOKIE, auth.issue_token(admin_id))
    res = api.post("/api/admin/llm-providers", json={
        "name": "Q", "base_url": "https://q/v1", "model": "q-1", "api_key": "sk-qqqq-5678",
        "price_in_per_mtok": 50, "price_out_per_mtok": 150,
    })
    assert res.status_code == 200 and res.json()["key_last4"] == "5678"
    assert "sk-qqqq" not in res.text
    q_id = res.json()["id"]
    assert api.post(f"/api/admin/llm-providers/{q_id}/default").json()["is_default"] is True
    assert api.post(f"/api/admin/users/{uid}/billing", json={"billing_status": "comp"}).status_code == 200
    assert api.post(f"/api/admin/users/{uid}/llm",
                    json={"llm_mode": "hosted", "llm_provider_id": q_id}).json()["llm_provider_id"] == q_id
    assert api.post(f"/api/admin/users/{uid}/credit", json={"cents": 500}).json()["credit_cents"] == 500
    assert api.get(f"/api/admin/users/{uid}/ledger").json()["ledger"][0]["kind"] == "adjust"
    overview = api.get("/api/admin/overview").json()
    assert "hosted_1d" in overview and "gross_margin_pct" in overview["hosted_1d"]
    # Comp user may now pick hosted themselves.
    api.cookies.set(backend_main._SESSION_COOKIE, auth.issue_token(uid))
    assert api.put("/api/llm-settings/mode", json={"mode": "hosted"}).json() == {"llm_mode": "hosted"}
    assert api.post("/api/billing/topup", json={"amount_cents": 1000}).status_code == 200
    assert api.post("/api/billing/topup", json={"amount_cents": 999}).status_code == 400
