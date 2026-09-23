"""Stripe Checkout for credit packs. TEST MODE ONLY.

Trust model
-----------
The webhook signature is the only thing that makes any of this trustworthy.
Everything inside the payload — including `metadata` — is attacker-controlled
until `stripe.Webhook.construct_event` has verified it against
`STRIPE_WEBHOOK_SECRET`, and an unverified event is discarded before anything
reads a field off it.

Even after verification, metadata is treated as *identifiers only*:

  * `account_id` says who to credit;
  * `pack_id` says which pack was bought.

The number of credits is never read from the payload. It is re-derived
server-side from `PRICE_TABLE[pack_id]`, so a session created with tampered or
stale metadata cannot mint credits. As a second check the webhook compares the
amount Stripe actually charged against the pack's price and refuses on a
mismatch — that catches a session whose price was altered after creation, which
the pack_id lookup alone would not.

A live key must never reach this code. `assert_test_mode()` enforces it.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

import stripe

# --------------------------------------------------------------------------
# Packs
# --------------------------------------------------------------------------
# Seeded from docs/CONTEXT.md. The $999 custom category feed is deliberately
# absent: it is a bespoke engagement, not a self-serve pack, and is granted
# with `billing.adjust_balance()` after the work is agreed.
#
# NOTE: CONTEXT.md describes api_credits as "$299/month". This is implemented
# as a one-time 100-credit purchase, because P2.3 is explicitly one-time
# payments only. Recurring billing is a separate piece of work.
PRICE_TABLE: Dict[str, Dict[str, Any]] = {
    "report_pack": {
        "amount_cents": 9900,
        "currency": "usd",
        "credits": 10,
        "name": "Product viability report pack (10 reports)",
    },
    "api_credits": {
        "amount_cents": 29900,
        "currency": "usd",
        "credits": 100,
        "name": "API credits (100 reports)",
    },
}

CHECKOUT_SESSION_COMPLETED = "checkout.session.completed"


class StripeNotConfigured(RuntimeError):
    """Raised when a Stripe call is attempted with no key configured."""


class LiveKeyRefused(RuntimeError):
    """Raised when a live-mode key is supplied. This project is test mode only."""


def get_secret_key() -> Optional[str]:
    return os.environ.get("STRIPE_SECRET_KEY") or None


def get_webhook_secret() -> Optional[str]:
    return os.environ.get("STRIPE_WEBHOOK_SECRET") or None


def is_configured() -> bool:
    return get_secret_key() is not None


def assert_test_mode(key: str) -> None:
    """Refuse a live key outright.

    Test keys are `sk_test_...`; live keys are `sk_live_...`. Charging a real
    customer from this codebase would be an accident with real money attached,
    so it fails at the call site rather than being caught in review.
    """
    if key.startswith("sk_live_"):
        raise LiveKeyRefused(
            "STRIPE_SECRET_KEY is a LIVE key. This project is test mode only; "
            "use an sk_test_ key."
        )


def get_client() -> Any:
    """The configured stripe module. Raises if unset or live."""
    key = get_secret_key()
    if key is None:
        raise StripeNotConfigured(
            "STRIPE_SECRET_KEY is not set. See .env.example; use a test key."
        )
    assert_test_mode(key)
    stripe.api_key = key
    return stripe


def get_pack(pack_id: str) -> Dict[str, Any]:
    """Look up a pack. Raises KeyError for an unknown id."""
    return PRICE_TABLE[pack_id]


def create_checkout_session(
    account_id: str,
    pack_id: str,
    success_url: str,
    cancel_url: str,
) -> Any:
    """Create a one-time Checkout Session for a credit pack.

    `price_data` is built from PRICE_TABLE rather than a Stripe Price object,
    so the catalogue lives in one place in this repo and cannot drift from a
    dashboard someone edited.
    """
    pack = get_pack(pack_id)
    client = get_client()

    return client.checkout.Session.create(
        mode="payment",  # one-time; no subscriptions in P2.3
        line_items=[
            {
                "price_data": {
                    "currency": pack["currency"],
                    "unit_amount": pack["amount_cents"],
                    "product_data": {"name": pack["name"]},
                },
                "quantity": 1,
            }
        ],
        # Identifiers only. Credits are re-derived from pack_id on the way
        # back in; nothing here is trusted as an amount.
        metadata={"account_id": account_id, "pack_id": pack_id},
        success_url=success_url,
        cancel_url=cancel_url,
    )


def verify_event(payload: bytes, signature: Optional[str]) -> Any:
    """Verify and parse a webhook payload.

    Raises:
        StripeNotConfigured: no STRIPE_WEBHOOK_SECRET.
        ValueError: malformed payload.
        stripe.error.SignatureVerificationError: bad or missing signature.

    Everything downstream depends on this having succeeded — an unverified
    payload is forged until proven otherwise.
    """
    secret = get_webhook_secret()
    if secret is None:
        raise StripeNotConfigured("STRIPE_WEBHOOK_SECRET is not set.")
    if not signature:
        raise stripe.error.SignatureVerificationError(
            "Missing Stripe-Signature header", sig_header=None
        )
    return stripe.Webhook.construct_event(payload, signature, secret)


def credits_for_completed_session(session: Any) -> tuple[str, str, int]:
    """Re-derive (account_id, pack_id, credits) from a verified session.

    `session` is a `stripe.StripeObject`, **not a dict**. Since stripe-python
    v12 these objects no longer subclass dict: `.get()` raises
    "'get' is a dict method, but a StripeObject is not a dict". That applies
    to nested values too — `session.metadata` is itself a StripeObject, so
    `metadata.get("account_id")` fails exactly like the outer call did.

    Fields are therefore read with `getattr(..., None)`, which returns None for
    an absent key instead of raising AttributeError.

    Raises:
        ValueError: metadata is missing, the pack is unknown, or the amount
            Stripe charged does not match the pack's price.
    """
    metadata = getattr(session, "metadata", None)
    account_id = getattr(metadata, "account_id", None) if metadata else None
    pack_id = getattr(metadata, "pack_id", None) if metadata else None

    if not account_id or not pack_id:
        raise ValueError("session metadata is missing account_id or pack_id")

    try:
        pack = get_pack(pack_id)
    except KeyError:
        raise ValueError(f"unknown pack_id {pack_id!r}") from None

    # Defence in depth: the pack lookup fixes the credit count, and this fixes
    # the price. A session created against a tampered amount is refused rather
    # than silently granting a pack that was not paid for.
    amount_total = getattr(session, "amount_total", None)
    if amount_total is not None and amount_total != pack["amount_cents"]:
        raise ValueError(
            f"amount mismatch for {pack_id!r}: charged {amount_total}, "
            f"expected {pack['amount_cents']}"
        )

    return account_id, pack_id, int(pack["credits"])
