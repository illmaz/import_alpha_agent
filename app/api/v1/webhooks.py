"""Stripe webhook receiver.

On its own router with **no authentication dependency**: Stripe cannot send an
API key, so the signature on the payload is the credential. This is why the
route lives here rather than on the v1 router, which authenticates everything
by default — an unauthenticated route has to be a deliberate, visible choice.

The handler does as little as possible and returns 200 quickly. Stripe retries
any non-2xx, so slow work here turns into duplicate deliveries.
"""

from __future__ import annotations

import logging

import stripe
from fastapi import APIRouter, HTTPException, Request, status

from app.services import billing, stripe_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])


@router.post(
    "/stripe",
    summary="Stripe webhook receiver (signature-verified, unauthenticated)",
    responses={400: {"description": "Missing, malformed or unverifiable payload."}},
)
async def stripe_webhook(request: Request) -> dict:
    """Credit an account when a Checkout Session completes.

    Nothing in the payload is read until the signature verifies. After that,
    metadata is used only to identify the account and the pack — the credit
    amount is re-derived from PRICE_TABLE server-side, so a tampered session
    cannot mint credits.
    """
    payload = await request.body()
    signature = request.headers.get("Stripe-Signature")

    try:
        event = stripe_service.verify_event(payload, signature)
    except stripe_service.StripeNotConfigured as exc:
        # Not the caller's fault, and not something a retry will fix.
        logger.error("stripe webhook received but not configured: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Stripe webhooks are not configured on this deployment.",
        ) from exc
    except (ValueError, stripe.error.SignatureVerificationError) as exc:
        # Forged, corrupted, or replayed past the tolerance window.
        logger.warning("rejected stripe webhook: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Signature verification failed.",
        ) from exc

    event_id = event.id
    event_type = event.type

    if event_type != stripe_service.CHECKOUT_SESSION_COMPLETED:
        # Acknowledged and ignored. A 4xx here would make Stripe retry an
        # event we will never act on.
        logger.info("ignoring stripe event %s of type %s", event_id, event_type)
        return {"received": True, "handled": False, "reason": "event type not handled"}

    session = event.data.object

    try:
        account_id, pack_id, credits = stripe_service.credits_for_completed_session(
            session
        )
    except ValueError as exc:
        # A verified event we cannot act on: unknown pack, missing metadata or
        # a price that does not match the pack. 400 so it is visible in the
        # Stripe dashboard rather than silently dropped.
        logger.error("unusable stripe session on event %s: %s", event_id, exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    entry = await billing.record_purchase(account_id, credits, reference=event_id)

    if entry is None:
        # Already credited by an earlier delivery of this same event. 200, or
        # Stripe keeps retrying something that already succeeded.
        logger.info("stripe event %s already recorded; not crediting again", event_id)
        return {"received": True, "handled": True, "duplicate": True}

    print(
        f"[stripe] {event_id}: +{credits} credits to {account_id} "
        f"({pack_id}) -> balance {entry.balance_after}"
    )
    return {
        "received": True,
        "handled": True,
        "duplicate": False,
        "credits": credits,
        "balance": entry.balance_after,
    }