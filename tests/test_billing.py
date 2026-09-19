"""Subscriptions, loyalty, comp, quota — aboyeur/billing.py against a fake
Stripe that records calls and hands back ids.

Manual run against real Stripe (test mode), documented in DEPLOYMENT.md:
    stripe listen --forward-to localhost:8000/api/billing/webhook
    stripe trigger invoice.paid
"""

from __future__ import annotations

import itertools
import json
import time

import pytest

from aboyeur import billing
from aboyeur.migrate import migrate
from aboyeur.stripe_client import StripeError


class FakeStripe:
    """Records every call; returns dicts shaped like the SDK's."""

    def __init__(self, configured: bool = True):
        self.calls: list[tuple[str, dict]] = []
        self._ids = itertools.count(1)
        self.configured = configured
        self.fail_next: str | None = None

    def _rec(self, _op: str, **kw) -> dict:
        self.calls.append((_op, kw))
        if self.fail_next == _op:
            self.fail_next = None
            raise StripeError(f"{_op} failed")
        return {"id": f"{_op}_{next(self._ids)}", **kw}

    def create_product(self, name, metadata):
        return self._rec("prod", name=name, metadata=metadata)

    def create_price(self, **kw):
        return self._rec("price", **kw)

    def archive_price(self, price_id):
        return self._rec("archive_price", price_id=price_id)

    def create_coupon(self, **kw):
        return self._rec("coupon", **kw)

    def delete_coupon(self, coupon_id):
        self._rec("delete_coupon", coupon_id=coupon_id)

    def create_customer(self, **kw):
        return self._rec("cus", **kw)

    def update_customer(self, customer_id, **fields):
        return self._rec("update_cus", customer_id=customer_id, **fields)

    def retrieve_subscription(self, subscription_id):
        return self._rec("retrieve_sub", subscription_id=subscription_id)

    def set_subscription_discount(self, subscription_id, coupon_id):
        return self._rec("discount", subscription_id=subscription_id, coupon_id=coupon_id)

    def cancel_subscription(self, subscription_id, *, prorate=True):
        return self._rec("cancel", subscription_id=subscription_id, prorate=prorate)

    def create_checkout_session(self, **params):
        out = self._rec("checkout", **params)
        out["url"] = f"https://checkout.stripe.test/{out['id']}"
        return out

    def create_portal_session(self, *, customer, return_url):
        out = self._rec("portal", customer=customer, return_url=return_url)
        out["url"] = f"https://portal.stripe.test/{out['id']}"
        return out

    def construct_event(self, payload, sig_header):
        return json.loads(payload)

    def named(self, name: str) -> list[dict]:
        return [kw for n, kw in self.calls if n == name]


@pytest.fixture()
def conn(db_conn):
    migrate(db_conn)  # seeds plan_prices + loyalty_ladder
    return db_conn


@pytest.fixture()
def stripe():
    return FakeStripe()


@pytest.fixture()
def uid(conn, user_id):
    conn.execute("UPDATE users SET email_verified_at = created_at WHERE id = ?", (user_id,))
    conn.commit()
    return user_id


_ev = itertools.count(100)


def event(type_: str, obj: dict, event_id: str | None = None) -> dict:
    return {"id": event_id or f"evt_{next(_ev)}", "type": type_, "data": {"object": obj}}


def sub_obj(sub_id: str, user_id: int, status: str = "active", interval: str = "month",
            customer: str = "cus_x") -> dict:
    return {
        "id": sub_id, "status": status, "customer": customer,
        "metadata": {"user_id": str(user_id)},
        "current_period_end": int(time.time()) + 30 * 86400,
        "items": {"data": [{"price": {"recurring": {"interval": interval}}}]},
    }


def invoice_obj(sub_id: str, reason: str = "subscription_cycle", customer: str = "cus_x") -> dict:
    return {"id": f"in_{next(_ev)}", "subscription": sub_id, "billing_reason": reason,
            "customer": customer, "metadata": {}}


def deliver(conn, stripe, ev: dict) -> dict:
    assert billing.record_event_once(conn, ev)
    return billing.handle_event(conn, stripe, ev)


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------


def test_seed_prices_and_ladder(conn):
    prices = billing.list_prices(conn)
    assert {(p["interval"], p["country"], p["amount_cents"]) for p in prices} == {
        ("month", "*", 1000), ("year", "*", 8000),
    }
    assert [t["tier"] for t in billing.list_ladder(conn)] == ["m3", "m6", "m12"]
    # Re-running the migration never re-seeds over an operator's edits.
    conn.execute("UPDATE plan_prices SET amount_cents = 1234 WHERE interval = 'month'")
    conn.commit()
    migrate(conn)
    assert billing.get_price(conn, "pro", "month", "*")["amount_cents"] == 1234


def test_price_lookup_falls_back_to_default_country(conn, stripe):
    billing.upsert_price(conn, stripe, admin_id=1, plan="pro", interval="month",
                         country="de", amount_cents=900)
    assert billing.get_price(conn, "pro", "month", "DE")["amount_cents"] == 900
    assert billing.get_price(conn, "pro", "month", "FR")["amount_cents"] == 1000
    assert billing.get_price(conn, "pro", "year", "DE")["amount_cents"] == 8000
    with pytest.raises(billing.BillingError):
        billing.get_price(conn, "pro", "week", "*")


def test_stripe_price_is_created_lazily_once(conn, stripe):
    row = billing.get_price(conn, "pro", "month", "*")
    assert row["stripe_price_id"] is None
    pid = billing.ensure_stripe_price(conn, stripe, row)
    assert pid.startswith("price_")
    assert len(stripe.named("prod")) == 1
    created = stripe.named("price")[0]
    assert created["unit_amount"] == 1000 and created["interval"] == "month"
    assert created["lookup_key"] == "pro_month_*_1000"
    # Second call: nothing new at Stripe, same id.
    again = billing.ensure_stripe_price(conn, stripe, billing.get_price(conn, "pro", "month", "*"))
    assert again == pid and len(stripe.named("price")) == 1
    # One product per plan, cached in settings.
    billing.ensure_stripe_price(conn, stripe, billing.get_price(conn, "pro", "year", "*"))
    assert len(stripe.named("prod")) == 1


def test_editing_a_price_archives_old_row_and_stripe_price(conn, stripe):
    old = billing.get_price(conn, "pro", "month", "*")
    billing.ensure_stripe_price(conn, stripe, old)
    new = billing.upsert_price(conn, stripe, admin_id=7, plan="pro", interval="month",
                               country="*", amount_cents=1200)
    assert new["id"] != old["id"] and new["amount_cents"] == 1200
    assert stripe.named("archive_price")[0]["price_id"] == old["stripe_price_id"]
    assert conn.execute("SELECT active FROM plan_prices WHERE id = ?", (old["id"],)).fetchone()[0] == 0
    assert billing.get_price(conn, "pro", "month", "*")["id"] == new["id"]
    audit = conn.execute("SELECT * FROM admin_audit WHERE action = 'edit_price'").fetchall()
    assert audit and audit[0]["admin_id"] == 7
    # Same amount again is a no-op (no second archive).
    same = billing.upsert_price(conn, stripe, admin_id=7, plan="pro", interval="month",
                                country="*", amount_cents=1200)
    assert same["id"] == new["id"]


def test_country_resolution_order(conn):
    user = {"country": "ca"}
    assert billing.resolve_country(user, {"cf-ipcountry": "DE"}, trust_cf=True) == "CA"
    user = {"country": None}
    assert billing.resolve_country(user, {"cf-ipcountry": "DE"}, trust_cf=True) == "DE"
    assert billing.resolve_country(user, {"cf-ipcountry": "DE"}, trust_cf=False) == "*"
    assert billing.resolve_country(user, {}, trust_cf=True) == "*"


# ---------------------------------------------------------------------------
# Checkout / portal
# ---------------------------------------------------------------------------


def test_checkout_creates_customer_and_session(conn, stripe, uid):
    url = billing.create_subscription_checkout(conn, stripe, uid, "month", "US", "https://app.test")
    assert url.startswith("https://checkout.stripe.test/")
    cus = stripe.named("cus")[0]
    assert cus["metadata"] == {"user_id": str(uid), "country": "US"}
    session = stripe.named("checkout")[0]
    assert session["mode"] == "subscription"
    assert session["client_reference_id"] == str(uid)
    assert session["automatic_tax"] == {"enabled": False}
    assert session["subscription_data"]["metadata"]["user_id"] == str(uid)
    assert session["success_url"].startswith("https://app.test/")
    assert billing.get_user(conn, uid)["stripe_customer_id"].startswith("cus_")
    # A second checkout reuses the customer.
    billing.create_subscription_checkout(conn, stripe, uid, "year", "US", "https://app.test")
    assert len(stripe.named("cus")) == 1


def test_checkout_refused_for_comp_and_unverified(conn, stripe, uid):
    conn.execute("UPDATE users SET email_verified_at = NULL WHERE id = ?", (uid,))
    conn.commit()
    with pytest.raises(billing.BillingError) as e:
        billing.create_subscription_checkout(conn, stripe, uid, "month", "*", "x")
    assert e.value.status == 403 and e.value.extra["error"] == "unverified"
    conn.execute("UPDATE users SET email_verified_at = created_at WHERE id = ?", (uid,))
    billing.set_comp(conn, stripe, admin_id=1, user_id=uid, comp=True)
    with pytest.raises(billing.BillingError) as e:
        billing.create_subscription_checkout(conn, stripe, uid, "month", "*", "x")
    assert e.value.status == 409


def test_portal_needs_a_customer(conn, stripe, uid):
    with pytest.raises(billing.BillingError) as e:
        billing.create_portal(conn, stripe, uid, "https://app.test")
    assert e.value.status == 409
    billing.create_subscription_checkout(conn, stripe, uid, "month", "*", "https://app.test")
    assert billing.create_portal(conn, stripe, uid, "https://app.test").startswith("https://portal")


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------


def test_webhook_replay_is_a_noop(conn, stripe, uid):
    ev = event("customer.subscription.created", sub_obj("sub_1", uid), event_id="evt_dup")
    assert billing.record_event_once(conn, ev)
    billing.handle_event(conn, stripe, ev)
    assert not billing.record_event_once(conn, ev)
    row = conn.execute("SELECT processed FROM stripe_events WHERE id = 'evt_dup'").fetchone()
    assert row["processed"] == 1


def test_unknown_event_type_is_marked_processed(conn, stripe):
    out = deliver(conn, stripe, event("payment_method.attached", {}))
    assert out == {"handled": False}


def test_handler_error_is_recorded_and_reraised(conn, stripe):
    ev = event("customer.subscription.created", {"id": "sub_x", "status": "active",
                                                 "customer": "cus_nobody", "metadata": {}})
    billing.record_event_once(conn, ev)
    with pytest.raises(billing.BillingError):
        billing.handle_event(conn, stripe, ev)
    row = conn.execute("SELECT processed, error FROM stripe_events WHERE id = ?", (ev["id"],)).fetchone()
    assert row["processed"] == 0 and "unknown user" in row["error"]
    assert billing.unprocessed_events(conn)[0]["id"] == ev["id"]


def test_checkout_completed_links_ids(conn, stripe, uid):
    deliver(conn, stripe, event("checkout.session.completed", {
        "id": "cs_1", "mode": "subscription", "customer": "cus_9", "subscription": "sub_9",
        "client_reference_id": str(uid), "metadata": {},
    }))
    u = billing.get_user(conn, uid)
    assert u["stripe_customer_id"] == "cus_9" and u["stripe_subscription_id"] == "sub_9"


@pytest.mark.parametrize("status,plan,ours", [
    ("active", "pro", "active"),
    ("trialing", "pro", "active"),
    ("past_due", "pro", "past_due"),
    ("canceled", "free", "canceled"),
    ("unpaid", "free", "canceled"),
    ("incomplete_expired", "free", "canceled"),
])
def test_subscription_status_mapping(conn, stripe, uid, status, plan, ours):
    deliver(conn, stripe, event("customer.subscription.updated", sub_obj("sub_1", uid, status)))
    u = billing.get_user(conn, uid)
    assert (u["plan"], u["billing_status"]) == (plan, ours)


def test_subscription_facts_feed_billing_me(conn, stripe, uid):
    deliver(conn, stripe, event("customer.subscription.created", sub_obj("sub_1", uid, interval="year")))
    me = billing.billing_me(conn, uid, "*")
    assert me["plan"] == "pro" and me["interval"] == "year" and me["renews_at"]
    assert me["price_cents"] == 8000 and me["prices"] == {"month": 1000, "year": 8000}


def test_invoice_paid_increments_tenure_only_for_subscription_reasons(conn, stripe, uid):
    deliver(conn, stripe, event("customer.subscription.created", sub_obj("sub_1", uid)))
    deliver(conn, stripe, event("invoice.paid", invoice_obj("sub_1", "subscription_create")))
    assert billing.get_user(conn, uid)["paid_months"] == 1
    deliver(conn, stripe, event("invoice.paid", invoice_obj("sub_1", "manual")))
    deliver(conn, stripe, event("invoice.paid", invoice_obj("sub_1", "subscription_update")))
    assert billing.get_user(conn, uid)["paid_months"] == 1
    deliver(conn, stripe, event("invoice.paid", invoice_obj("sub_1", "subscription_cycle")))
    assert billing.get_user(conn, uid)["paid_months"] == 2


def test_past_due_recovery_keeps_tenure(conn, stripe, uid):
    deliver(conn, stripe, event("customer.subscription.created", sub_obj("sub_1", uid)))
    for _ in range(2):
        deliver(conn, stripe, event("invoice.paid", invoice_obj("sub_1")))
    deliver(conn, stripe, event("invoice.payment_failed", invoice_obj("sub_1")))
    u = billing.get_user(conn, uid)
    assert u["billing_status"] == "past_due" and u["paid_months"] == 2 and u["plan"] == "pro"
    # Stripe's dunning retry succeeds inside the window: back to active, +1.
    deliver(conn, stripe, event("invoice.paid", invoice_obj("sub_1")))
    u = billing.get_user(conn, uid)
    assert u["billing_status"] == "active" and u["paid_months"] == 3


def test_ladder_promotes_at_boundary_and_swaps_coupon(conn, stripe, uid):
    deliver(conn, stripe, event("customer.subscription.created", sub_obj("sub_1", uid)))
    deliver(conn, stripe, event("invoice.paid", invoice_obj("sub_1")))
    deliver(conn, stripe, event("invoice.paid", invoice_obj("sub_1")))
    assert billing.get_user(conn, uid)["loyalty_tier"] is None
    assert stripe.named("discount") == []
    # Month 3: m3 (10%) — coupon created lazily, applied to the subscription.
    deliver(conn, stripe, event("invoice.paid", invoice_obj("sub_1")))
    u = billing.get_user(conn, uid)
    assert u["paid_months"] == 3 and u["loyalty_tier"] == "m3"
    coupon = stripe.named("coupon")[0]
    assert coupon == {"percent_off": 10, "name": "Loyalty m3"}
    assert stripe.named("discount")[-1]["coupon_id"].startswith("coupon_")
    # Months 4, 5: same tier — no new Stripe calls.
    n_before = len(stripe.calls)
    deliver(conn, stripe, event("invoice.paid", invoice_obj("sub_1")))
    deliver(conn, stripe, event("invoice.paid", invoice_obj("sub_1")))
    assert len(stripe.calls) == n_before
    # Month 6: m6.
    deliver(conn, stripe, event("invoice.paid", invoice_obj("sub_1")))
    assert billing.get_user(conn, uid)["loyalty_tier"] == "m6"
    assert stripe.named("coupon")[-1]["percent_off"] == 15
    assert billing.billing_me(conn, uid, "*")["percent_off"] == 15


def test_cancel_resets_tenure_and_resubscribe_starts_at_base(conn, stripe, uid):
    deliver(conn, stripe, event("customer.subscription.created", sub_obj("sub_1", uid)))
    for _ in range(3):
        deliver(conn, stripe, event("invoice.paid", invoice_obj("sub_1")))
    assert billing.get_user(conn, uid)["loyalty_tier"] == "m3"
    deliver(conn, stripe, event("customer.subscription.deleted", sub_obj("sub_1", uid, "canceled")))
    u = billing.get_user(conn, uid)
    assert (u["plan"], u["billing_status"], u["paid_months"], u["loyalty_tier"],
            u["stripe_subscription_id"]) == ("free", "canceled", 0, None, None)
    # New subscription: month 1 at base price, no discount call.
    n = len(stripe.named("discount"))
    deliver(conn, stripe, event("customer.subscription.created", sub_obj("sub_2", uid)))
    deliver(conn, stripe, event("invoice.paid", invoice_obj("sub_2", "subscription_create")))
    u = billing.get_user(conn, uid)
    assert u["paid_months"] == 1 and u["loyalty_tier"] is None and u["plan"] == "pro"
    assert len(stripe.named("discount")) == n


def test_editing_ladder_percent_recreates_coupon_lazily(conn, stripe, uid):
    deliver(conn, stripe, event("customer.subscription.created", sub_obj("sub_1", uid)))
    for _ in range(3):
        deliver(conn, stripe, event("invoice.paid", invoice_obj("sub_1")))
    old_coupon = billing.list_ladder(conn)[0]["stripe_coupon_id"]
    assert old_coupon
    billing.upsert_tier(conn, stripe, admin_id=1, tier="m3", min_paid_months=3, percent_off=12)
    assert stripe.named("delete_coupon")[0]["coupon_id"] == old_coupon
    assert billing.list_ladder(conn)[0]["stripe_coupon_id"] is None
    # Not re-applied in bulk — only on this user's next invoice.
    assert stripe.named("discount")[-1]["coupon_id"] == old_coupon
    deliver(conn, stripe, event("invoice.paid", invoice_obj("sub_1")))
    new_coupon = billing.list_ladder(conn)[0]["stripe_coupon_id"]
    assert new_coupon and new_coupon != old_coupon
    assert stripe.named("discount")[-1]["coupon_id"] == new_coupon
    assert stripe.named("coupon")[-1]["percent_off"] == 12


def test_comp_user_ignores_stripe_events(conn, stripe, uid):
    billing.set_comp(conn, stripe, admin_id=1, user_id=uid, comp=True)
    deliver(conn, stripe, event("customer.subscription.deleted", sub_obj("sub_1", uid, "canceled")))
    u = billing.get_user(conn, uid)
    assert u["billing_status"] == "comp" and u["plan"] == "pro"


# ---------------------------------------------------------------------------
# Comp + quota
# ---------------------------------------------------------------------------


def test_comp_toggle_cancels_live_subscription_and_audits(conn, stripe, uid):
    deliver(conn, stripe, event("customer.subscription.created", sub_obj("sub_1", uid)))
    u = billing.set_comp(conn, stripe, admin_id=3, user_id=uid, comp=True)
    assert (u["billing_status"], u["plan"], u["stripe_subscription_id"]) == ("comp", "pro", None)
    assert stripe.named("cancel") == [{"subscription_id": "sub_1", "prorate": True}]
    assert billing.is_paid(u)
    u = billing.set_comp(conn, stripe, admin_id=3, user_id=uid, comp=False)
    assert (u["billing_status"], u["plan"]) == ("free", "free")
    actions = [r["action"] for r in conn.execute("SELECT action FROM admin_audit")]
    assert actions.count("set_comp") == 2
    with pytest.raises(billing.BillingError):
        billing.set_comp(conn, stripe, admin_id=3, user_id=uid, comp=False)


def _turns(conn, uid, n):
    from aboyeur import admin
    for _ in range(n):
        admin.record_event(conn, "chat_turn", user_id=uid, ok=True)


def test_quota_gate(conn, uid):
    user = billing.get_user(conn, uid)
    assert billing.check_turn_quota(conn, user, byok=False) == {"used": 0, "limit": 20}
    _turns(conn, uid, 19)
    assert billing.check_turn_quota(conn, user, byok=False)["used"] == 19
    _turns(conn, uid, 1)
    with pytest.raises(billing.BillingError) as e:
        billing.check_turn_quota(conn, user, byok=False)
    assert e.value.status == 402
    assert e.value.extra["error"] == "quota" and e.value.extra["upgrade_url"]
    # Failed turns don't count.
    from aboyeur import admin
    admin.record_event(conn, "chat_turn", user_id=uid, ok=False)
    assert billing.turns_used_30d(conn, uid) == 20
    # BYOK on free: unlimited. Pro/comp: unlimited.
    assert billing.check_turn_quota(conn, user, byok=True) == {"used": None, "limit": None}
    conn.execute("UPDATE users SET plan = 'pro', billing_status = 'active' WHERE id = ?", (uid,))
    conn.commit()
    assert billing.check_turn_quota(conn, billing.get_user(conn, uid), byok=False)["limit"] is None


def test_billing_health_mrr(conn, stripe, uid, user_id):
    from aboyeur import auth
    other = auth.get_or_create_user(conn, "other@example.com")
    deliver(conn, stripe, event("customer.subscription.created", sub_obj("sub_1", uid, interval="year")))
    deliver(conn, stripe, event("customer.subscription.created", sub_obj("sub_2", other)))
    for _ in range(3):
        deliver(conn, stripe, event("invoice.paid", invoice_obj("sub_2")))
    h = billing.billing_health(conn)
    # year 8000/12 = 666.67 + month 1000 * 0.9 = 900 -> 1567
    assert h["mrr_cents"] == 1567 and h["active"] == 2 and h["unprocessed_webhooks"] == 0


# ---------------------------------------------------------------------------
# Through the HTTP layer: 402 quota, checkout redirect, webhook route
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def backend_main(tmp_path_factory):
    import os
    db_path = tmp_path_factory.mktemp("backend-billing") / "aboyeur.db"
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
    backend_main._stripe = FakeStripe()
    return TestClient(backend_main.app)


def _login(backend_main, api, email: str, admin: bool = False) -> int:
    from aboyeur import admin as admin_mod, auth
    c = backend_main._profile_conn()
    try:
        user_id = auth.get_or_create_user(c, email)
        admin_mod.set_admin(c, email, admin)
    finally:
        c.close()
    api.cookies.set(backend_main._SESSION_COOKIE, auth.issue_token(user_id))
    return user_id


def test_http_checkout_and_me(backend_main, api):
    uid = _login(backend_main, api, "buyer@example.com")
    res = api.post("/api/billing/checkout", json={"interval": "month"})
    assert res.status_code == 200 and res.json()["url"].startswith("https://checkout")
    me = api.get("/api/billing/me").json()
    assert me["plan"] == "free" and me["has_customer"] and me["billing_enabled"]
    assert me["topup_amounts"] == [500, 1000, 2500, 5000]
    # Country is user-editable and shows in /me.
    assert api.put("/api/account/country", json={"country": "de"}).json() == {"country": "DE"}
    assert api.get("/api/billing/me").json()["country"] == "DE"
    assert api.put("/api/account/country", json={"country": "Germany"}).status_code == 400


def test_http_webhook_route_records_and_dedupes(backend_main, api):
    uid = _login(backend_main, api, "hook@example.com")
    ev = event("customer.subscription.created", sub_obj("sub_h", uid), event_id="evt_http_1")
    res = api.post("/api/billing/webhook", content=json.dumps(ev),
                   headers={"stripe-signature": "t=1,v1=fake"})
    assert res.status_code == 200 and res.json()["handled"] is True
    assert api.post("/api/billing/webhook", content=json.dumps(ev)).json()["duplicate"] is True
    assert api.get("/api/billing/me").json()["plan"] == "pro"


def test_http_quota_402_on_chat(backend_main, api):
    from aboyeur import admin
    uid = _login(backend_main, api, "quota@example.com")
    c = backend_main._profile_conn()
    try:
        for _ in range(20):
            admin.record_event(c, "chat_turn", user_id=uid, ok=True)
    finally:
        c.close()
    res = api.post("/chat", json={"message": "hi"})
    assert res.status_code == 402
    detail = res.json()["detail"]
    assert detail["error"] == "quota" and detail["upgrade_url"]


def test_http_admin_comp_and_prices(backend_main, api):
    target = _login(backend_main, api, "comped@example.com")
    _login(backend_main, api, "op@example.com", admin=True)
    res = api.post(f"/api/admin/users/{target}/billing", json={"billing_status": "comp"})
    assert res.status_code == 200 and res.json()["billing_status"] == "comp"
    users = api.get("/api/admin/users").json()["users"]
    assert next(u for u in users if u["id"] == target)["plan"] == "pro"
    prices = api.get("/api/admin/prices").json()["prices"]
    month = next(p for p in prices if p["interval"] == "month" and p["country"] == "*")
    res = api.put(f"/api/admin/prices/{month['id']}", json={"amount_cents": 1500})
    assert res.status_code == 200 and res.json()["amount_cents"] == 1500
    assert api.post("/api/admin/prices", json={"interval": "month", "country": "br",
                                               "amount_cents": 500}).status_code == 200
    assert api.get("/api/admin/overview").json()["billing"]["comp"] >= 1
    assert api.put("/api/admin/loyalty/m3", json={"tier": "m3", "min_paid_months": 3,
                                                  "percent_off": 11}).json()["percent_off"] == 11
    # Non-admin is 403 on all of it.
    _login(backend_main, api, "civ@example.com")
    assert api.get("/api/admin/prices").status_code == 403
    assert api.post(f"/api/admin/users/{target}/billing", json={"billing_status": "free"}).status_code == 403


def test_http_rate_limit_on_login(backend_main, api):
    limiter = backend_main._limiter
    limiter.enabled = True
    try:
        limiter.reset()
        codes = [api.post("/auth/login", json={"email": "x@y.z", "password": "nope"}).status_code
                 for _ in range(11)]
    finally:
        limiter.enabled = False
    assert codes[:10] == [401] * 10 and codes[10] == 429
