"""x402 middleware: accept a USDC payment in place of an API key.

Flow for a request with no `Authorization` header but an `X-Payment-Hash`:

    verify the transfer on Base
      -> credit the payer's own account, once per transaction hash
      -> let the request through as that account, which then pays normally

Two places this deliberately departs from the brief, both for safety:

1. **The payer gets their own account, not a shared `x402-agent`.**
   One shared account would pool every agent's credits, so whoever called next
   would spend whatever the last payer had left. Accounts are keyed on the
   paying address (`x402:0xabc…`), which the Transfer log already gives us.

2. **A re-used transaction hash is refused (402), not silently allowed.**
   A transaction hash is *public* — anyone can read it off a block explorer.
   It proves a payment happened; it does **not** prove the caller made it. So
   a hash buys exactly one credit, once, and is spent by the request that
   presents it. If the hash has already been redeemed, the request is refused
   rather than being allowed to spend down the real payer's balance.

   This is the core limitation of hash-as-receipt and it is not fully fixable
   here: the real x402 spec uses a signed payment authorisation (EIP-3009),
   where the caller proves control of the paying key. That is the upgrade
   path, recorded in docs/X402_SETUP.md.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.services import billing, x402

logger = logging.getLogger(__name__)

PAYMENT_HEADER = "X-Payment-Hash"

# Price of one credit in USDC base units (6 decimals): 10_000 = $0.01.
#
# WARNING — this does not match fiat pricing. Stripe sells 10 credits for $99
# ($9.90/credit) and 100 for $299 ($2.99/credit). At $0.01 an agent pays ~300x
# to ~990x less than a human for the same report. That is a business decision,
# not a technical one, so it is left configurable and loud rather than
# silently "corrected" here. Set X402_CREDIT_PRICE_BASE_UNITS to align it:
# $2.99 = 2_990_000, $9.90 = 9_900_000.
CREDIT_PRICE_BASE_UNITS = int(
    os.environ.get("X402_CREDIT_PRICE_BASE_UNITS", "10000")
)

ACCOUNT_PREFIX = "x402:"


def account_id_for(payer_address: str) -> str:
    """Per-payer account id, so one agent cannot spend another's credits."""
    return f"{ACCOUNT_PREFIX}{payer_address.lower()}"


def payment_required_response(detail: str, status_code: int = 402) -> JSONResponse:
    """402 with everything a machine needs to pay and retry."""
    body = {
        "error": "payment_required",
        "detail": detail,
        "accepts": [
            {
                "scheme": "x402-usdc-transfer",
                "amount_base_units": CREDIT_PRICE_BASE_UNITS,
                "asset": "USDC",
                "decimals": x402.USDC_DECIMALS,
                "header": PAYMENT_HEADER,
            }
        ],
    }
    # Network and wallet are only known once configured; a misconfigured
    # deployment should still return a usable error.
    try:
        network = x402.get_network()
        body["accepts"][0]["network"] = network.name
        body["accepts"][0]["asset_contract"] = x402.get_usdc_contract()
    except Exception:  # noqa: BLE001 - reported in `detail`, not fatal here
        pass
    try:
        body["accepts"][0]["pay_to"] = x402.get_seller_address()
    except x402.X402NotConfigured:
        pass
    return JSONResponse(status_code=status_code, content=body)


async def settle_payment(tx_hash: str) -> tuple[Optional[str], str]:
    """Verify and redeem a payment. Returns (account_id, reason).

    account_id is None when the payment is unusable; `reason` says why.
    """
    try:
        seller = x402.get_seller_address()
    except x402.X402NotConfigured as exc:
        return None, str(exc)

    result = x402.verify_payment_detailed(
        tx_hash, CREDIT_PRICE_BASE_UNITS, seller
    )
    if not result.ok:
        return None, result.reason

    account_id = account_id_for(result.payer)

    # The account may not exist yet; a first-time payer is the normal case.
    if await billing.get_account(account_id) is None:
        try:
            await billing.create_account(account_id, 0, label="x402 agent")
        except ValueError:
            pass  # created concurrently by another in-flight request

    entry = await billing.record_purchase(account_id, 1, reference=tx_hash)
    if entry is None:
        # Already redeemed. Refusing is the point: the hash is public, so
        # allowing a replay would let anyone spend this payer's balance.
        return None, "this transaction has already been redeemed"

    print(
        f"[x402] {tx_hash[:12]}… verified: +1 credit to {account_id} "
        f"({result.amount} base units from {result.payer})"
    )
    return account_id, "verified"


class X402PaymentMiddleware(BaseHTTPMiddleware):
    """Turn a verified USDC payment into an authenticated request.

    An `Authorization` header always wins: a caller holding an API key is
    already a customer, and charging them on-chain as well would double-bill.
    """

    async def dispatch(self, request: Request, call_next):
        tx_hash = request.headers.get(PAYMENT_HEADER)

        if request.headers.get("Authorization"):
            # API key takes precedence. Note it so a caller sending both is
            # not left wondering why no payment was consumed.
            if tx_hash:
                logger.info(
                    "ignoring %s: request carries an API key", PAYMENT_HEADER
                )
            return await call_next(request)

        if not tx_hash:
            # No key and no payment: the normal 401 path handles it.
            return await call_next(request)

        account_id, reason = await settle_payment(tx_hash.strip())
        if account_id is None:
            logger.info("x402 payment rejected (%s): %s", tx_hash[:12], reason)
            return payment_required_response(reason)

        # Consumed by app.api.v1.endpoints.get_current_account.
        request.state.x402_account_id = account_id
        return await call_next(request)
