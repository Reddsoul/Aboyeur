"""Subscriptions, prepaid credit, quotas, loyalty — pure functions over a
sqlite3.Connection plus a StripeClient (aboyeur/stripe_client.py).

Nothing here imports `stripe` or FastAPI. Routes in backend/main.py call in;
tests hand in a fake Stripe. Every function that changes a user's billing
state is idempotent — Stripe delivers webhooks at-least-once, and the
handlers below get replayed on purpose (see handle_event).

State model (users table):
  plan            free | pro                what they can use
  billing_status  free | active | past_due | canceled | comp   why
  paid_months     consecutive paid months; loyalty tenure. Reset to 0 on
                  subscription.deleted, never on past_due (dunning keeps it).
  loyalty_tier    ladder step currently applied as a Stripe coupon, or NULL.
  credit_cents    hosted-LLM wallet. May dip below zero on the final turn.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from math import ceil

from aboyeur.stripe_client import StripeClient, StripeError

logger = logging.getLogger("aboyeur")

PLANS = ("free", "pro")
INTERVALS = ("month", "year")
BILLING_STATUSES = ("free", "active", "past_due", "canceled", "comp")
TOPUP_AMOUNTS_CENTS = (500, 1000, 2500, 5000)
FREE_TURNS_PER_30D = 20
LOW_BALANCE_CENTS = 100
LOW_BALANCE_EMAIL_EVERY_DAYS = 7
DEFAULT_COUNTRY = "*"

# Stripe subscription.status -> ours. Anything not listed is treated as
# canceled (incomplete, incomplete_expired, unpaid, paused).
_SUB_STATUS = {
    "active": "active",
    "trialing": "active",
    "past_due": "past_due",
}


class BillingError(Exception):
    """A client-facing billing problem. `status` is the HTTP code to answer with."""

    def __init__(self, message: str, status: int = 400, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _since(days: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days * 86_400))


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

_USER_COLS = (
    "id, email, name, plan, billing_status, country, stripe_customer_id, "
    "stripe_subscription_id, paid_months, loyalty_tier, llm_mode, "
    "llm_provider_id, credit_cents, email_verified_at, tos_version, "
    "tos_accepted_at, deleted_at, low_balance_emailed_at"
)


def get_user(conn: sqlite3.Connection, user_id: int) -> dict:
    row = conn.execute(f"SELECT {_USER_COLS} FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise BillingError("no such user", 404)
    return dict(row)


def user_by_customer(conn: sqlite3.Connection, customer_id: str | None) -> dict | None:
    if not customer_id:
        return None
    row = conn.execute(
        f"SELECT {_USER_COLS} FROM users WHERE stripe_customer_id = ?", (customer_id,)
    ).fetchone()
    return dict(row) if row else None


def is_paid(user: dict) -> bool:
    """Pro by subscription or by comp — the gate hosted LLM checks."""
    return user["billing_status"] == "comp" or (
        user["plan"] == "pro" and user["billing_status"] in ("active", "past_due")
    )


# ---------------------------------------------------------------------------
# Quota — enforced in the chat entrypoint before a turn starts
# ---------------------------------------------------------------------------


def turns_used_30d(conn: sqlite3.Connection, user_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind = 'chat_turn' AND ok = 1 "
        "AND user_id = ? AND ts >= ?",
        (user_id, _since(30)),
    ).fetchone()
    return row[0] or 0


def check_turn_quota(conn: sqlite3.Connection, user: dict, byok: bool) -> dict:
    """Raise BillingError(402) when a free, non-BYOK user is over their
    rolling 30-day turn allowance. Returns {used, limit} for the UI.

    BYOK users pay their own tokens, so a free plan costs us nothing per
    turn and they're unlimited. Pro/comp are unlimited regardless."""
    if byok or is_paid(user):
        return {"used": None, "limit": None}
    used = turns_used_30d(conn, user["id"])
    if used >= FREE_TURNS_PER_30D:
        raise BillingError(
            "free plan allowance used up for this 30-day window", 402,
            error="quota", upgrade_url="/app/account", used=used, limit=FREE_TURNS_PER_30D,
        )
    return {"used": used, "limit": FREE_TURNS_PER_30D}


# ---------------------------------------------------------------------------
# Country + price lookup
# ---------------------------------------------------------------------------


def resolve_country(user: dict, headers: dict, trust_cf: bool) -> str:
    """users.country wins; else Cloudflare's edge country when we're told to
    trust it; else the default row. Two letters upper-case or '*'."""
    stored = (user.get("country") or "").strip().upper()
    if len(stored) == 2 and stored.isalpha():
        return stored
    if trust_cf:
        edge = (headers.get("cf-ipcountry") or "").strip().upper()
        if len(edge) == 2 and edge.isalpha():
            return edge
    return DEFAULT_COUNTRY


def get_price(conn: sqlite3.Connection, plan: str, interval: str, country: str) -> dict:
    """The active plan_prices row: exact country, else '*'. No Stripe here —
    ensure_stripe_price adds the Price id when checkout actually needs it."""
    if plan not in PLANS or interval not in INTERVALS:
        raise BillingError("unknown plan or interval", 400)
    for c in (country.upper(), DEFAULT_COUNTRY):
        row = conn.execute(
            "SELECT * FROM plan_prices WHERE plan = ? AND interval = ? AND country = ? "
            "AND active = 1",
            (plan, interval, c),
        ).fetchone()
        if row is not None:
            return dict(row)
    raise BillingError(f"no price configured for {plan}/{interval}", 500)


def _setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def ensure_product(conn: sqlite3.Connection, stripe: StripeClient, plan: str) -> str:
    """One Stripe Product per plan, id cached in settings."""
    key = f"stripe_product_{plan}"
    cached = _setting(conn, key)
    if cached:
        return cached
    product = stripe.create_product(name=f"Aboyeur {plan.title()}", metadata={"plan": plan})
    _set_setting(conn, key, product["id"])
    conn.commit()
    return product["id"]


def ensure_stripe_price(conn: sqlite3.Connection, stripe: StripeClient, price_row: dict) -> str:
    """Lazily create the Stripe Price behind a plan_prices row and persist
    its id. Stripe Prices are immutable, so a row only ever gets one."""
    if price_row.get("stripe_price_id"):
        return price_row["stripe_price_id"]
    product_id = ensure_product(conn, stripe, price_row["plan"])
    price = stripe.create_price(
        product=product_id,
        unit_amount=price_row["amount_cents"],
        interval=price_row["interval"],
        lookup_key=f"{price_row['plan']}_{price_row['interval']}_{price_row['country']}_"
                   f"{price_row['amount_cents']}",
        metadata={"plan": price_row["plan"], "interval": price_row["interval"],
                  "country": price_row["country"]},
    )
    conn.execute(
        "UPDATE plan_prices SET stripe_price_id = ? WHERE id = ?", (price["id"], price_row["id"])
    )
    conn.commit()
    price_row["stripe_price_id"] = price["id"]
    return price["id"]


# ---------------------------------------------------------------------------
# Customers, checkout, portal
# ---------------------------------------------------------------------------


def ensure_customer(conn: sqlite3.Connection, stripe: StripeClient, user: dict, country: str) -> str:
    if user.get("stripe_customer_id"):
        return user["stripe_customer_id"]
    customer = stripe.create_customer(
        email=user["email"], metadata={"user_id": str(user["id"]), "country": country},
    )
    conn.execute(
        "UPDATE users SET stripe_customer_id = ? WHERE id = ?", (customer["id"], user["id"])
    )
    conn.commit()
    user["stripe_customer_id"] = customer["id"]
    return customer["id"]


def create_subscription_checkout(
    conn: sqlite3.Connection, stripe: StripeClient, user_id: int, interval: str,
    country: str, app_base_url: str,
) -> str:
    """Stripe Checkout URL for a Pro subscription. 409 for comp users and for
    anyone already subscribed (the portal is where they change plans)."""
    user = get_user(conn, user_id)
    if user["billing_status"] == "comp":
        raise BillingError("this account is complimentary — nothing to buy", 409)
    if user["stripe_subscription_id"] and user["billing_status"] in ("active", "past_due"):
        raise BillingError("already subscribed — manage it from the billing portal", 409)
    if not user.get("email_verified_at"):
        raise BillingError("verify your email address before subscribing", 403, error="unverified")
    price_row = get_price(conn, "pro", interval, country)
    price_id = ensure_stripe_price(conn, stripe, price_row)
    customer_id = ensure_customer(conn, stripe, user, country)
    stripe.update_customer(customer_id, metadata={"user_id": str(user_id), "country": country})
    session = stripe.create_checkout_session(
        mode="subscription",
        customer=customer_id,
        line_items=[{"price": price_id, "quantity": 1}],
        # Stripe Tax stays off until the owner turns it on in the dashboard.
        automatic_tax={"enabled": False},
        success_url=f"{app_base_url}/app/account?checkout=success",
        cancel_url=f"{app_base_url}/app/account?checkout=cancel",
        client_reference_id=str(user_id),
        subscription_data={"metadata": {"user_id": str(user_id), "country": country}},
        allow_promotion_codes=False,
    )
    return session["url"]


def create_topup_checkout(
    conn: sqlite3.Connection, stripe: StripeClient, user_id: int, amount_cents: int,
    country: str, app_base_url: str,
) -> str:
    if amount_cents not in TOPUP_AMOUNTS_CENTS:
        raise BillingError(f"amount must be one of {', '.join(map(str, TOPUP_AMOUNTS_CENTS))}", 400)
    user = get_user(conn, user_id)
    if not user.get("email_verified_at"):
        raise BillingError("verify your email address first", 403, error="unverified")
    customer_id = ensure_customer(conn, stripe, user, country)
    session = stripe.create_checkout_session(
        mode="payment",
        customer=customer_id,
        line_items=[{
            "quantity": 1,
            "price_data": {
                "currency": "usd",
                "unit_amount": amount_cents,
                "product_data": {"name": "Aboyeur hosted-model credit"},
            },
        }],
        automatic_tax={"enabled": False},
        success_url=f"{app_base_url}/app/settings?topup=success",
        cancel_url=f"{app_base_url}/app/settings?topup=cancel",
        client_reference_id=str(user_id),
        metadata={"user_id": str(user_id), "kind": "topup", "amount_cents": str(amount_cents)},
        payment_intent_data={"metadata": {"user_id": str(user_id), "kind": "topup"}},
    )
    return session["url"]


def create_portal(conn: sqlite3.Connection, stripe: StripeClient, user_id: int,
                  app_base_url: str) -> str:
    user = get_user(conn, user_id)
    if not user.get("stripe_customer_id"):
        raise BillingError("no billing account yet — subscribe or top up first", 409)
    session = stripe.create_portal_session(
        customer=user["stripe_customer_id"], return_url=f"{app_base_url}/app/account",
    )
    return session["url"]


def billing_me(conn: sqlite3.Connection, user_id: int, country: str) -> dict:
    """What the Account page renders. No Stripe call: everything a user needs
    to see is already in our tables, updated by webhooks."""
    user = get_user(conn, user_id)
    tier = None
    if user["loyalty_tier"]:
        tier = conn.execute(
            "SELECT tier, percent_off FROM loyalty_ladder WHERE tier = ?", (user["loyalty_tier"],)
        ).fetchone()
    interval, renews_at = _subscription_facts(conn, user)
    price = None
    try:
        price = get_price(conn, "pro", interval or "month", country)
    except BillingError:
        pass
    ladder = [dict(r) for r in conn.execute(
        "SELECT tier, min_paid_months, percent_off FROM loyalty_ladder WHERE active = 1 "
        "ORDER BY min_paid_months"
    )]
    return {
        "plan": user["plan"],
        "billing_status": user["billing_status"],
        "interval": interval,
        "renews_at": renews_at,
        "paid_months": user["paid_months"],
        "loyalty_tier": user["loyalty_tier"],
        "percent_off": tier["percent_off"] if tier else 0,
        "price_cents": price["amount_cents"] if price else None,
        "prices": {
            iv: get_price(conn, "pro", iv, country)["amount_cents"] for iv in INTERVALS
        },
        "country": country,
        "country_source": "account" if user.get("country") else "detected",
        "llm_mode": user["llm_mode"],
        "credit_cents": user["credit_cents"],
        "email_verified": bool(user["email_verified_at"]),
        "has_customer": bool(user["stripe_customer_id"]),
        "ladder": ladder,
        "topup_amounts": list(TOPUP_AMOUNTS_CENTS),
        "free_turns_limit": FREE_TURNS_PER_30D,
        "turns_used_30d": turns_used_30d(conn, user_id),
        "support_access": [
            dict(r) for r in conn.execute(
                "SELECT ts, session_id FROM support_access WHERE user_id = ? "
                "ORDER BY ts DESC LIMIT 20", (user_id,)
            )
        ],
    }


def _subscription_facts(conn: sqlite3.Connection, user: dict) -> tuple[str | None, str | None]:
    """(interval, renews_at) as last recorded by a subscription webhook."""
    if not user.get("stripe_subscription_id"):
        return None, None
    raw = _setting(conn, f"sub_{user['stripe_subscription_id']}")
    if not raw:
        return None, None
    try:
        facts = json.loads(raw)
    except ValueError:
        return None, None
    return facts.get("interval"), facts.get("renews_at")


# ---------------------------------------------------------------------------
# Ledger + credit
# ---------------------------------------------------------------------------


def ledger_entry(
    conn: sqlite3.Connection, user_id: int, kind: str, charge_cents: int, *,
    cost_cents: int = 0, tokens_in: int = 0, tokens_out: int = 0,
    model: str = "-", provider_id: int | None = None, event_id: int | None = None,
    commit: bool = True,
) -> int:
    """Insert a ledger row and move users.credit_cents by -charge_cents.
    Positive charge = debit (turn), negative = credit (top-up, adjust up,
    refund reversal is positive again). One statement pair, the caller's
    transaction if commit=False."""
    cur = conn.execute(
        "INSERT INTO usage_ledger (ts, user_id, event_id, provider_id, model, tokens_in, "
        "tokens_out, cost_cents, charge_cents, kind) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (_now(), user_id, event_id, provider_id, model, tokens_in, tokens_out,
         cost_cents, charge_cents, kind),
    )
    conn.execute(
        "UPDATE users SET credit_cents = credit_cents - ? WHERE id = ?", (charge_cents, user_id)
    )
    if commit:
        conn.commit()
    return cur.lastrowid


def add_credit(conn: sqlite3.Connection, user_id: int, cents: int, kind: str = "adjust",
               model: str = "-") -> int:
    """Top-up / adjustment. `cents` positive adds credit."""
    return ledger_entry(conn, user_id, kind, -cents, model=model)


def turn_charge(price_in_per_mtok: int, price_out_per_mtok: int, markup_pct: int,
                tokens_in: int, tokens_out: int) -> tuple[int, int]:
    """(cost_cents, charge_cents), both rounded up. Cents are the unit, so a
    single cheap turn costs at least 1¢ when it costs anything at all —
    integer micro-rounding is deliberate and documented in the ToS."""
    raw_cost = tokens_in * price_in_per_mtok / 1e6 + tokens_out * price_out_per_mtok / 1e6
    cost = ceil(raw_cost - 1e-9) if raw_cost > 0 else 0
    charge = ceil(cost * (1 + markup_pct / 100) - 1e-9) if cost > 0 else 0
    return cost, charge


def meter_turn(
    conn: sqlite3.Connection, user: dict, provider: dict, tokens_in: int | None,
    tokens_out: int | None, *, estimated: bool, event_id: int | None,
    kind: str = "chat_turn",
) -> dict:
    """Debit one hosted turn. Comp users are metered at charge 0 so the cost
    is still visible in admin. Returns the ledger facts."""
    tin, tout = tokens_in or 0, tokens_out or 0
    cost, charge = turn_charge(
        provider["price_in_per_mtok"], provider["price_out_per_mtok"],
        provider["markup_pct"], tin, tout,
    )
    if user["billing_status"] == "comp":
        charge = 0
    ledger_kind = f"{kind}_est" if estimated else kind
    row_id = ledger_entry(
        conn, user["id"], ledger_kind, charge, cost_cents=cost, tokens_in=tin,
        tokens_out=tout, model=provider["model"], provider_id=provider["id"],
        event_id=event_id,
    )
    return {"ledger_id": row_id, "cost_cents": cost, "charge_cents": charge, "kind": ledger_kind}


def needs_low_balance_email(conn: sqlite3.Connection, user_id: int) -> bool:
    """True once when the wallet dips under LOW_BALANCE_CENTS, then not again
    for LOW_BALANCE_EMAIL_EVERY_DAYS. Stamps the throttle column itself."""
    row = conn.execute(
        "SELECT credit_cents, low_balance_emailed_at, llm_mode FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if row is None or row["llm_mode"] != "hosted" or row["credit_cents"] >= LOW_BALANCE_CENTS:
        return False
    last = row["low_balance_emailed_at"]
    if last and last >= _since(LOW_BALANCE_EMAIL_EVERY_DAYS):
        return False
    conn.execute("UPDATE users SET low_balance_emailed_at = ? WHERE id = ?", (_now(), user_id))
    conn.commit()
    return True


# ---------------------------------------------------------------------------
# Loyalty
# ---------------------------------------------------------------------------


def tier_for(conn: sqlite3.Connection, paid_months: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM loyalty_ladder WHERE active = 1 AND min_paid_months <= ? "
        "ORDER BY min_paid_months DESC LIMIT 1",
        (paid_months,),
    ).fetchone()
    return dict(row) if row else None


def ensure_coupon(conn: sqlite3.Connection, stripe: StripeClient, tier: dict) -> str:
    if tier.get("stripe_coupon_id"):
        return tier["stripe_coupon_id"]
    coupon = stripe.create_coupon(percent_off=tier["percent_off"], name=f"Loyalty {tier['tier']}")
    conn.execute(
        "UPDATE loyalty_ladder SET stripe_coupon_id = ? WHERE id = ?", (coupon["id"], tier["id"])
    )
    conn.commit()
    tier["stripe_coupon_id"] = coupon["id"]
    return coupon["id"]


def apply_loyalty(conn: sqlite3.Connection, stripe: StripeClient, user_id: int) -> str | None:
    """Move the user's subscription onto the tier their tenure earns. Returns
    the tier id now applied (None = base price). A no-op when nothing changed,
    so it's safe to call on every invoice.paid."""
    user = get_user(conn, user_id)
    tier = tier_for(conn, user["paid_months"])
    wanted = tier["tier"] if tier else None
    if wanted == user["loyalty_tier"]:
        # Same tier — but an edited ladder may have re-created the coupon.
        # Re-apply lazily here (never in bulk), and only if the id moved.
        if tier and user.get("stripe_subscription_id"):
            applied = _setting(conn, f"sub_{user['stripe_subscription_id']}_coupon")
            coupon_id = ensure_coupon(conn, stripe, tier)
            if applied != coupon_id:
                stripe.set_subscription_discount(user["stripe_subscription_id"], coupon_id)
                _set_setting(conn, f"sub_{user['stripe_subscription_id']}_coupon", coupon_id)
                conn.commit()
        return wanted
    if user.get("stripe_subscription_id"):
        coupon_id = ensure_coupon(conn, stripe, tier) if tier else None
        stripe.set_subscription_discount(user["stripe_subscription_id"], coupon_id)
        _set_setting(conn, f"sub_{user['stripe_subscription_id']}_coupon", coupon_id or "")
    conn.execute("UPDATE users SET loyalty_tier = ? WHERE id = ?", (wanted, user_id))
    conn.commit()
    return wanted


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------


def record_event_once(conn: sqlite3.Connection, event: dict) -> bool:
    """Insert into stripe_events; False if this id was already seen."""
    try:
        conn.execute(
            "INSERT INTO stripe_events (id, type, received_at) VALUES (?, ?, ?)",
            (event["id"], event["type"], _now()),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        return False
    return True


def handle_event(conn: sqlite3.Connection, stripe: StripeClient, event: dict) -> dict:
    """Dispatch one verified, not-yet-seen Stripe event. Errors are recorded
    on the stripe_events row and re-raised for the route to log; the route
    still answers 200 so Stripe stops retrying something we can't process."""
    handler = _HANDLERS.get(event["type"])
    if handler is None:
        _mark(conn, event["id"], processed=True)
        return {"handled": False}
    try:
        result = handler(conn, stripe, event["data"]["object"]) or {}
    except Exception as e:
        _mark(conn, event["id"], processed=False, error=f"{type(e).__name__}: {e}"[:1000])
        raise
    _mark(conn, event["id"], processed=True)
    return {"handled": True, **result}


def _mark(conn: sqlite3.Connection, event_id: str, *, processed: bool, error: str | None = None) -> None:
    conn.execute(
        "UPDATE stripe_events SET processed = ?, error = ? WHERE id = ?",
        (1 if processed else 0, error, event_id),
    )
    conn.commit()


def _user_for(conn: sqlite3.Connection, obj: dict) -> dict | None:
    """Find the account an object belongs to: metadata.user_id first (we
    stamp it everywhere), then the customer id."""
    meta = obj.get("metadata") or {}
    uid = meta.get("user_id") or obj.get("client_reference_id")
    if uid:
        try:
            return get_user(conn, int(uid))
        except (ValueError, BillingError):
            pass
    customer = obj.get("customer")
    if isinstance(customer, dict):
        customer = customer.get("id")
    return user_by_customer(conn, customer)


def _on_checkout_completed(conn, stripe, session: dict) -> dict:
    user = _user_for(conn, session)
    if user is None:
        raise BillingError("checkout.session.completed for unknown user")
    customer = session.get("customer")
    if isinstance(customer, dict):
        customer = customer.get("id")
    if customer and not user.get("stripe_customer_id"):
        conn.execute("UPDATE users SET stripe_customer_id = ? WHERE id = ?", (customer, user["id"]))
    meta = session.get("metadata") or {}
    if session.get("mode") == "payment" and meta.get("kind") == "topup":
        if session.get("payment_status") not in (None, "paid"):
            conn.commit()
            return {"topup": "unpaid"}
        # Idempotent per session: a replayed event must not double-credit.
        key = f"topup_{session['id']}"
        if _setting(conn, key):
            conn.commit()
            return {"topup": "duplicate"}
        amount = int(session.get("amount_total") or meta.get("amount_cents") or 0)
        ledger_entry(conn, user["id"], "topup", -amount, model="-", commit=False)
        _set_setting(conn, key, str(amount))
        conn.commit()
        return {"topup": amount}
    if session.get("mode") == "subscription":
        sub = session.get("subscription")
        if isinstance(sub, dict):
            sub = sub.get("id")
        if sub:
            conn.execute(
                "UPDATE users SET stripe_subscription_id = ? WHERE id = ?", (sub, user["id"])
            )
    conn.commit()
    return {}


def _sub_interval(sub: dict) -> str | None:
    items = (sub.get("items") or {}).get("data") or []
    for item in items:
        rec = (item.get("price") or {}).get("recurring") or {}
        if rec.get("interval"):
            return rec["interval"]
    return None


def _iso(ts: int | None) -> str | None:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else None


def _on_subscription_upsert(conn, stripe, sub: dict) -> dict:
    user = _user_for(conn, sub)
    if user is None:
        raise BillingError("subscription event for unknown user")
    status = sub.get("status")
    ours = _SUB_STATUS.get(status, "canceled")
    if user["billing_status"] == "comp":
        # Comp is app-side; a stray Stripe object must not demote them.
        return {"ignored": "comp"}
    if ours == "canceled":
        plan = "free"
    else:
        plan = "pro"
    # renews_at: Stripe's current_period_end (top-level pre-2025 API, per-item after).
    period_end = sub.get("current_period_end")
    if not period_end:
        items = (sub.get("items") or {}).get("data") or []
        period_end = items[0].get("current_period_end") if items else None
    _set_setting(conn, f"sub_{sub['id']}", json.dumps({
        "interval": _sub_interval(sub), "renews_at": _iso(period_end),
        "cancel_at_period_end": bool(sub.get("cancel_at_period_end")),
    }))
    conn.execute(
        "UPDATE users SET plan = ?, billing_status = ?, stripe_subscription_id = ? WHERE id = ?",
        (plan, ours, sub["id"], user["id"]),
    )
    conn.commit()
    return {"plan": plan, "billing_status": ours}


def _on_subscription_deleted(conn, stripe, sub: dict) -> dict:
    user = _user_for(conn, sub)
    if user is None:
        raise BillingError("subscription.deleted for unknown user")
    if user["billing_status"] == "comp":
        return {"ignored": "comp"}
    # Lapse resets tenure: a re-subscribe starts at base price. Requested.
    conn.execute(
        "UPDATE users SET plan = 'free', billing_status = 'canceled', paid_months = 0, "
        "loyalty_tier = NULL, stripe_subscription_id = NULL WHERE id = ?",
        (user["id"],),
    )
    conn.commit()
    return {"plan": "free", "billing_status": "canceled"}


_TENURE_REASONS = ("subscription_cycle", "subscription_create")


def _on_invoice_paid(conn, stripe, invoice: dict) -> dict:
    reason = invoice.get("billing_reason")
    if reason not in _TENURE_REASONS:
        # manual invoices, one-time top-ups, subscription_update prorations:
        # money moved but no month of tenure was earned.
        return {"tenure": "skipped", "reason": reason}
    user = _user_for(conn, invoice)
    if user is None:
        # Invoices carry no metadata of ours; fall back via the subscription.
        sub_id = invoice.get("subscription")
        if isinstance(sub_id, dict):
            sub_id = sub_id.get("id")
        if sub_id:
            row = conn.execute(
                f"SELECT {_USER_COLS} FROM users WHERE stripe_subscription_id = ?", (sub_id,)
            ).fetchone()
            user = dict(row) if row else None
    if user is None:
        raise BillingError("invoice.paid for unknown user")
    if user["billing_status"] == "comp":
        return {"ignored": "comp"}
    # A paid invoice also ends any dunning: past_due -> active, tenure kept.
    conn.execute(
        "UPDATE users SET paid_months = paid_months + 1, plan = 'pro', "
        "billing_status = 'active' WHERE id = ?",
        (user["id"],),
    )
    conn.commit()
    tier = apply_loyalty(conn, stripe, user["id"])
    return {"paid_months": user["paid_months"] + 1, "loyalty_tier": tier}


def _on_invoice_failed(conn, stripe, invoice: dict) -> dict:
    user = _user_for(conn, invoice)
    if user is None:
        sub_id = invoice.get("subscription")
        if isinstance(sub_id, dict):
            sub_id = sub_id.get("id")
        row = conn.execute(
            f"SELECT {_USER_COLS} FROM users WHERE stripe_subscription_id = ?", (sub_id,)
        ).fetchone() if sub_id else None
        user = dict(row) if row else None
    if user is None:
        raise BillingError("invoice.payment_failed for unknown user")
    if user["billing_status"] == "comp" or invoice.get("billing_reason") not in _TENURE_REASONS:
        return {"ignored": True}
    # Stripe's own dunning retries; the eventual cancel resets tenure, not this.
    conn.execute("UPDATE users SET billing_status = 'past_due' WHERE id = ?", (user["id"],))
    conn.commit()
    return {"billing_status": "past_due"}


def _on_charge_refunded(conn, stripe, charge: dict) -> dict:
    """A refunded top-up takes the credit back — the wallet may go negative,
    which blocks hosted turns until they top up again."""
    meta = charge.get("metadata") or {}
    if meta.get("kind") != "topup":
        return {"ignored": "not a topup"}
    user = _user_for(conn, charge)
    if user is None:
        raise BillingError("charge.refunded for unknown user")
    key = f"refund_{charge['id']}"
    already = int(_setting(conn, key) or 0)
    refunded = int(charge.get("amount_refunded") or 0)
    delta = refunded - already
    if delta <= 0:
        return {"refund": "duplicate"}
    ledger_entry(conn, user["id"], "refund", delta, model="-", commit=False)
    _set_setting(conn, key, str(refunded))
    conn.commit()
    return {"refund": delta}


_HANDLERS = {
    "checkout.session.completed": _on_checkout_completed,
    "customer.subscription.created": _on_subscription_upsert,
    "customer.subscription.updated": _on_subscription_upsert,
    "customer.subscription.deleted": _on_subscription_deleted,
    "invoice.paid": _on_invoice_paid,
    "invoice.payment_failed": _on_invoice_failed,
    "charge.refunded": _on_charge_refunded,
}


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------


def audit(conn: sqlite3.Connection, admin_id: int, action: str, target: str | None = None,
          meta: dict | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO admin_audit (ts, admin_id, action, target, meta_json) VALUES (?,?,?,?,?)",
        (_now(), admin_id, action, target, json.dumps(meta) if meta else None),
    )
    conn.commit()
    return cur.lastrowid


def set_comp(conn: sqlite3.Connection, stripe: StripeClient, admin_id: int, user_id: int,
             comp: bool) -> dict:
    """comp=True: cancel any live subscription (prorated) and make them Pro
    with no Stripe object. comp=False: back to free."""
    user = get_user(conn, user_id)
    cancelled = None
    if comp:
        if user.get("stripe_subscription_id") and user["billing_status"] in ("active", "past_due"):
            try:
                stripe.cancel_subscription(user["stripe_subscription_id"], prorate=True)
                cancelled = user["stripe_subscription_id"]
            except StripeError as e:
                logger.warning("comp: couldn't cancel %s: %s", user["stripe_subscription_id"], e)
        conn.execute(
            "UPDATE users SET billing_status = 'comp', plan = 'pro', "
            "stripe_subscription_id = NULL, loyalty_tier = NULL WHERE id = ?",
            (user_id,),
        )
    else:
        if user["billing_status"] != "comp":
            raise BillingError("user is not comped", 409)
        conn.execute(
            "UPDATE users SET billing_status = 'free', plan = 'free' WHERE id = ?", (user_id,)
        )
    conn.commit()
    audit(conn, admin_id, "set_comp", f"user:{user_id}",
          {"comp": comp, "cancelled_subscription": cancelled})
    return get_user(conn, user_id)


def list_prices(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM plan_prices WHERE active = 1 ORDER BY plan, interval, country != '*', country"
    )]


def upsert_price(conn: sqlite3.Connection, stripe: StripeClient, admin_id: int, *,
                 plan: str, interval: str, country: str, amount_cents: int,
                 replace_id: int | None = None) -> dict:
    """Archive the active row for (plan, interval, country) and insert a new
    one. Stripe Prices are immutable, so the old one is archived there too
    and the new row gets its Price lazily. Existing subscribers keep the
    Price their subscription was created with — nobody is repriced."""
    if plan not in PLANS or interval not in INTERVALS:
        raise BillingError("unknown plan or interval", 400)
    if amount_cents < 0:
        raise BillingError("amount must be >= 0", 400)
    country = (country or "*").strip().upper()
    if country != "*" and not (len(country) == 2 and country.isalpha()):
        raise BillingError("country must be ISO-3166 alpha-2 or '*'", 400)
    old = conn.execute(
        "SELECT * FROM plan_prices WHERE plan = ? AND interval = ? AND country = ? AND active = 1",
        (plan, interval, country),
    ).fetchone()
    if old is not None and old["amount_cents"] == amount_cents:
        return dict(old)
    if old is not None:
        conn.execute("UPDATE plan_prices SET active = 0, updated_at = ? WHERE id = ?",
                     (_now(), old["id"]))
        if old["stripe_price_id"]:
            try:
                stripe.archive_price(old["stripe_price_id"])
            except StripeError as e:
                logger.warning("couldn't archive Stripe price %s: %s", old["stripe_price_id"], e)
    cur = conn.execute(
        "INSERT INTO plan_prices (plan, interval, country, amount_cents, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (plan, interval, country, amount_cents, _now()),
    )
    conn.commit()
    row = dict(conn.execute("SELECT * FROM plan_prices WHERE id = ?", (cur.lastrowid,)).fetchone())
    if stripe.configured:
        try:
            ensure_stripe_price(conn, stripe, row)
        except StripeError as e:
            logger.warning("Stripe price creation deferred: %s", e)
    audit(conn, admin_id, "edit_price", f"price:{row['id']}",
          {"plan": plan, "interval": interval, "country": country,
           "amount_cents": amount_cents, "replaced": old["id"] if old else None})
    return row


def delete_price(conn: sqlite3.Connection, stripe: StripeClient, admin_id: int, price_id: int) -> None:
    row = conn.execute("SELECT * FROM plan_prices WHERE id = ? AND active = 1", (price_id,)).fetchone()
    if row is None:
        raise BillingError("no such price", 404)
    if row["country"] == "*":
        raise BillingError("the default ('*') price can be edited but not removed", 409)
    conn.execute("UPDATE plan_prices SET active = 0, updated_at = ? WHERE id = ?", (_now(), price_id))
    conn.commit()
    if row["stripe_price_id"]:
        try:
            stripe.archive_price(row["stripe_price_id"])
        except StripeError as e:
            logger.warning("couldn't archive Stripe price %s: %s", row["stripe_price_id"], e)
    audit(conn, admin_id, "delete_price", f"price:{price_id}", dict(row))


def list_ladder(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM loyalty_ladder ORDER BY min_paid_months"
    )]


def upsert_tier(conn: sqlite3.Connection, stripe: StripeClient, admin_id: int, *,
                tier: str, min_paid_months: int, percent_off: int, active: bool = True) -> dict:
    """Coupons are immutable: a changed percent drops the old coupon id so
    the next invoice.paid for anyone on this tier re-creates and re-applies
    it (apply_loyalty's lazy path). Nothing is touched in bulk."""
    tier = (tier or "").strip()
    if not tier or not (0 <= percent_off <= 100) or min_paid_months < 0:
        raise BillingError("tier, min_paid_months >= 0, percent_off 0-100 required", 400)
    old = conn.execute("SELECT * FROM loyalty_ladder WHERE tier = ?", (tier,)).fetchone()
    if old is None:
        conn.execute(
            "INSERT INTO loyalty_ladder (tier, min_paid_months, percent_off, active) "
            "VALUES (?, ?, ?, ?)",
            (tier, min_paid_months, percent_off, 1 if active else 0),
        )
    else:
        coupon = old["stripe_coupon_id"]
        if old["percent_off"] != percent_off and coupon:
            stripe.delete_coupon(coupon)
            coupon = None
        conn.execute(
            "UPDATE loyalty_ladder SET min_paid_months = ?, percent_off = ?, active = ?, "
            "stripe_coupon_id = ? WHERE tier = ?",
            (min_paid_months, percent_off, 1 if active else 0, coupon, tier),
        )
    conn.commit()
    audit(conn, admin_id, "edit_ladder", f"tier:{tier}",
          {"min_paid_months": min_paid_months, "percent_off": percent_off, "active": active})
    return dict(conn.execute("SELECT * FROM loyalty_ladder WHERE tier = ?", (tier,)).fetchone())


def admin_add_credit(conn: sqlite3.Connection, admin_id: int, user_id: int, cents: int,
                     note: str | None = None) -> dict:
    if cents == 0:
        raise BillingError("amount must be non-zero", 400)
    get_user(conn, user_id)
    add_credit(conn, user_id, cents, kind="adjust")
    audit(conn, admin_id, "add_credit", f"user:{user_id}", {"cents": cents, "note": note})
    return get_user(conn, user_id)


def set_user_llm(conn: sqlite3.Connection, admin_id: int, user_id: int, *,
                 llm_mode: str | None = None, llm_provider_id: int | None | str = "unset") -> dict:
    get_user(conn, user_id)
    sets, params = [], []
    if llm_mode is not None:
        if llm_mode not in ("byok", "hosted"):
            raise BillingError("llm_mode must be byok or hosted", 400)
        sets.append("llm_mode = ?")
        params.append(llm_mode)
    if llm_provider_id != "unset":
        if llm_provider_id is not None:
            if conn.execute("SELECT 1 FROM llm_providers WHERE id = ?", (llm_provider_id,)).fetchone() is None:
                raise BillingError("no such provider", 404)
        sets.append("llm_provider_id = ?")
        params.append(llm_provider_id)
    if sets:
        params.append(user_id)
        conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", params)
        conn.commit()
        audit(conn, admin_id, "set_provider", f"user:{user_id}",
              {"llm_mode": llm_mode, "llm_provider_id": llm_provider_id})
    return get_user(conn, user_id)


def user_ledger(conn: sqlite3.Connection, user_id: int, limit: int = 200) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT l.*, p.name AS provider_name FROM usage_ledger l "
        "LEFT JOIN llm_providers p ON p.id = l.provider_id "
        "WHERE l.user_id = ? ORDER BY l.ts DESC, l.id DESC LIMIT ?",
        (user_id, max(1, min(limit, 1000))),
    )]


def unprocessed_events(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM stripe_events WHERE processed = 0 ORDER BY received_at DESC LIMIT ?",
        (limit,),
    )]


def billing_health(conn: sqlite3.Connection) -> dict:
    """MRR + status counts for the admin overview. MRR = each active sub's
    monthly-normalised price × (1 - discount). Uses the current active
    plan_prices for the user's country as the best available proxy — the
    exact Stripe amount lives on the subscription and isn't mirrored."""
    counts = {s: 0 for s in BILLING_STATUSES}
    for r in conn.execute(
        "SELECT billing_status, COUNT(*) n FROM users WHERE deleted_at IS NULL GROUP BY billing_status"
    ):
        counts[r["billing_status"]] = r["n"]
    mrr = 0.0
    for u in conn.execute(
        "SELECT id, country, loyalty_tier, stripe_subscription_id FROM users "
        "WHERE billing_status = 'active' AND plan = 'pro' AND deleted_at IS NULL"
    ):
        facts = _subscription_facts(conn, dict(u))
        interval = facts[0] or "month"
        try:
            price = get_price(conn, "pro", interval, u["country"] or "*")
        except BillingError:
            continue
        monthly = price["amount_cents"] / (12 if interval == "year" else 1)
        off = 0
        if u["loyalty_tier"]:
            t = conn.execute(
                "SELECT percent_off FROM loyalty_ladder WHERE tier = ?", (u["loyalty_tier"],)
            ).fetchone()
            off = t["percent_off"] if t else 0
        mrr += monthly * (1 - off / 100)
    return {
        "mrr_cents": int(round(mrr)),
        "active": counts["active"],
        "past_due": counts["past_due"],
        "canceled": counts["canceled"],
        "comp": counts["comp"],
        "free": counts["free"],
        "unprocessed_webhooks": conn.execute(
            "SELECT COUNT(*) FROM stripe_events WHERE processed = 0"
        ).fetchone()[0],
        "credit_outstanding_cents": conn.execute(
            "SELECT COALESCE(SUM(credit_cents), 0) FROM users WHERE credit_cents > 0"
        ).fetchone()[0],
    }
