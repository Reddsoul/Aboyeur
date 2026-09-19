"""The only place the `stripe` SDK is called.

billing.py takes one of these as an argument rather than importing stripe
itself, so tests hand it a recorder (tests/test_billing.py::FakeStripe) and
never touch the network. Every method returns plain dicts — the SDK's
StripeObject is dict-like already, and keeping the contract at "dict" is
what lets the fake stay simple.

Not a general wrapper: exactly the calls billing.py makes, no more.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("aboyeur")


class StripeError(Exception):
    """Stripe rejected or couldn't complete a call. Message is safe to show
    an admin; never includes keys."""


class StripeNotConfigured(StripeError):
    """STRIPE_SECRET_KEY is empty — billing routes 503."""


class StripeClient:
    def __init__(self, secret_key: str | None, webhook_secret: str | None = None):
        self._key = secret_key or None
        self._webhook_secret = webhook_secret or None
        self._stripe = None

    @property
    def configured(self) -> bool:
        return bool(self._key)

    def _sdk(self):
        if not self._key:
            raise StripeNotConfigured("STRIPE_SECRET_KEY is not set")
        if self._stripe is None:
            import stripe as _stripe
            _stripe.api_key = self._key
            _stripe.max_network_retries = 2
            self._stripe = _stripe
        return self._stripe

    def _call(self, fn, *args, **kwargs) -> dict[str, Any]:
        sdk = self._sdk()
        try:
            result = fn(*args, **kwargs)
        except sdk.error.StripeError as e:  # type: ignore[attr-defined]
            # user_message is Stripe's customer-safe text; str(e) can carry
            # request ids and param names but never the key.
            raise StripeError(getattr(e, "user_message", None) or str(e)) from e
        return _plain(result)

    # -- catalogue ---------------------------------------------------------

    def create_product(self, name: str, metadata: dict) -> dict:
        return self._call(self._sdk().Product.create, name=name, metadata=metadata)

    def create_price(self, *, product: str, unit_amount: int, interval: str,
                     lookup_key: str, metadata: dict) -> dict:
        return self._call(
            self._sdk().Price.create,
            product=product, currency="usd", unit_amount=unit_amount,
            recurring={"interval": interval}, lookup_key=lookup_key,
            transfer_lookup_key=True, metadata=metadata,
        )

    def archive_price(self, price_id: str) -> dict:
        return self._call(self._sdk().Price.modify, price_id, active=False)

    def create_coupon(self, *, percent_off: int, name: str) -> dict:
        return self._call(
            self._sdk().Coupon.create,
            percent_off=percent_off, duration="forever", name=name,
        )

    def delete_coupon(self, coupon_id: str) -> None:
        try:
            self._call(self._sdk().Coupon.delete, coupon_id)
        except StripeError as e:
            # Already gone is fine — the goal is "nobody can be given it again".
            logger.info("coupon %s delete: %s", coupon_id, e)

    # -- customers / subscriptions ------------------------------------------

    def create_customer(self, *, email: str, metadata: dict) -> dict:
        return self._call(self._sdk().Customer.create, email=email, metadata=metadata)

    def update_customer(self, customer_id: str, **fields) -> dict:
        return self._call(self._sdk().Customer.modify, customer_id, **fields)

    def retrieve_subscription(self, subscription_id: str) -> dict:
        return self._call(self._sdk().Subscription.retrieve, subscription_id)

    def set_subscription_discount(self, subscription_id: str, coupon_id: str | None) -> dict:
        # discounts=[] clears; a coupon replaces whatever discount was there.
        discounts = [{"coupon": coupon_id}] if coupon_id else []
        return self._call(self._sdk().Subscription.modify, subscription_id, discounts=discounts)

    def cancel_subscription(self, subscription_id: str, *, prorate: bool = True) -> dict:
        return self._call(self._sdk().Subscription.cancel, subscription_id, prorate=prorate)

    # -- checkout / portal ---------------------------------------------------

    def create_checkout_session(self, **params) -> dict:
        return self._call(self._sdk().checkout.Session.create, **params)

    def create_portal_session(self, *, customer: str, return_url: str) -> dict:
        return self._call(
            self._sdk().billing_portal.Session.create,
            customer=customer, return_url=return_url,
        )

    # -- webhooks ------------------------------------------------------------

    def construct_event(self, payload: bytes, sig_header: str | None) -> dict:
        """Verify the signature and return the event as a plain dict. Raises
        StripeError on a bad signature or a missing webhook secret."""
        if not self._webhook_secret:
            raise StripeError("STRIPE_WEBHOOK_SECRET is not set")
        sdk = self._sdk()
        try:
            event = sdk.Webhook.construct_event(payload, sig_header, self._webhook_secret)
        except (ValueError, sdk.error.SignatureVerificationError) as e:  # type: ignore[attr-defined]
            raise StripeError(f"webhook signature check failed: {type(e).__name__}") from e
        return _plain(event)


def _plain(obj: Any) -> Any:
    """StripeObject -> dict, recursively. Everything billing.py reads is a
    key path, and dicts are what the fake returns too."""
    if hasattr(obj, "to_dict_recursive"):
        return obj.to_dict_recursive()
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_plain(v) for v in obj]
    return obj
