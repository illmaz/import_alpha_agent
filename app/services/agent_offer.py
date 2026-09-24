"""The one definition of what we charge an agent, and how to pay it.

Extracted from the discovery endpoint so that a second consumer — the outreach
and marketing drafts in app/services/payment_facts.py — can read the same
values instead of restating them. Two copies of a wallet address or a price,
one of them stale, is the specific failure this exists to prevent: the reader
of an outreach email is about to move real money on what the text says.

The shape mirrors the `accepts` block in
`x402_middleware.payment_required_response`, so an agent that learns our terms
from a discovery document and an agent that learns them from a 402 rejection
see the same field names. A test pins the two together.
"""

from __future__ import annotations

from typing import Any, Dict

from app.services import x402
from app.services.x402_middleware import CREDIT_PRICE_BASE_UNITS, PAYMENT_HEADER


def payment_terms() -> Dict[str, Any]:
    """Live payment parameters, built from the configuration the middleware
    actually charges against — never from a constant restated here.

    An unconfigured deployment still gets an answer rather than an exception:
    `pay_to` comes back None with an `unavailable` reason, which is what lets
    `/v1/agent/info` serve 200 and say "payment is not set up here" instead of
    500-ing at the one client that cannot do anything about it.
    """
    block: Dict[str, Any] = {
        "scheme": "x402-usdc-transfer",
        "asset": "USDC",
        "decimals": x402.USDC_DECIMALS,
        "amount_base_units": CREDIT_PRICE_BASE_UNITS,
        "amount_usdc": CREDIT_PRICE_BASE_UNITS / (10**x402.USDC_DECIMALS),
        "credits_granted": 1,
        "header": PAYMENT_HEADER,
        "min_confirmations": x402.MIN_CONFIRMATIONS,
    }

    try:
        block["network"] = x402.get_network().name
        block["asset_contract"] = x402.get_usdc_contract()
    except Exception as exc:  # noqa: BLE001 - surfaced as a field, not a 500
        block["network"] = None
        block["unavailable"] = f"network not configured: {type(exc).__name__}"

    try:
        block["pay_to"] = x402.get_seller_address()
    except x402.X402NotConfigured:
        block["pay_to"] = None
        block.setdefault("unavailable", "seller wallet not configured on this deployment")

    return block
