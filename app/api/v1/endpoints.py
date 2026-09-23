"""v1 endpoints.

Every route requires `Authorization: Bearer <key>`; the dependency is declared
on the router so a new endpoint is authenticated by default.

Data is still curated: responses carry `status="curated"` — plausible
hand-written values, never observed. See app/services/data_loader.py.
"""

from __future__ import annotations

from typing import Optional

import stripe

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.schemas import (
    AccountTransactionsResponse,
    CheckoutRequest,
    CheckoutResponse,
    CreditPack,
    CreditPacksResponse,
    CreditTransactionOut,
    LandedCostRequest,
    LandedCostResponse,
    OpportunitiesResponse,
    ReportRequest,
    ReportResponse,
    ReportStatus,
    ResultStatus,
    RiskFlag,
)
from app.services.data_loader import load_opportunities
from app.services.landed_cost import estimate_landed_cost
from app import billing_pages
from app.services import billing, kafka_publisher, report_store, stripe_service
from app.services.auth import verify_api_key

# auto_error=False so a missing header reaches our handler and gets the same
# 401 shape as a bad one; the default would emit a bare 403 for "no header",
# which tells a caller the wrong thing.
_bearer = HTTPBearer(auto_error=False, description="API key issued by ImportAlpha.")


async def get_current_account(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> str:
    """Resolve the caller to an account_id, or 401.

    Two ways in, checked in this order:

      1. `Authorization: Bearer <key>` — an existing customer.
      2. A verified x402 USDC payment, which the middleware has already
         settled and recorded on `request.state`. By the time this runs, the
         on-chain transfer is confirmed and the credit is in the ledger.

    Missing, malformed, unknown and revoked keys are deliberately
    indistinguishable in the response: telling a caller which one it was helps
    nobody but someone probing for valid keys.
    """
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=(
            "Missing or invalid API key. Send 'Authorization: Bearer <key>', "
            "or pay per call with an 'X-Payment-Hash' header."
        ),
        headers={"WWW-Authenticate": "Bearer"},
    )

    if credentials is None or not credentials.credentials:
        # No key — but the x402 middleware may have paid for this request.
        paid_account = getattr(request.state, "x402_account_id", None)
        if paid_account:
            return paid_account
        raise unauthorized

    account_id = await verify_api_key(credentials.credentials)
    if account_id is None:
        raise unauthorized
    return account_id


# Applied at the router, so a new endpoint is authenticated by default. An
# endpoint that should be public has to opt out explicitly, which is the safer
# direction for the mistake to run in.
router = APIRouter(
    prefix="/v1",
    tags=["v1"],
    dependencies=[Depends(get_current_account)],
    responses={401: {"description": "Missing or invalid API key."}},
)


@router.get(
    "/opportunities",
    response_model=OpportunitiesResponse,
    summary="List scored product opportunities",
)
def list_opportunities(
    limit: int = Query(default=20, gt=0, le=100),
    offset: int = Query(default=0, ge=0),
) -> OpportunitiesResponse:
    """Curated home-organization opportunities, highest score first.

    `count` is the total before pagination, so a caller can page without
    guessing how many there are.
    """
    items = load_opportunities()
    return OpportunitiesResponse(
        items=items[offset : offset + limit],
        count=len(items),
        status=ResultStatus.CURATED,
    )


@router.post(
    "/landed-cost",
    response_model=LandedCostResponse,
    summary="Estimate China-to-US landed cost",
)
def estimate_landed_cost_endpoint(payload: LandedCostRequest) -> LandedCostResponse:
    """Flat-rate freight and duty heuristic. confidence_score is deliberately low.

    `unit_weight_kg` is required for a real estimate; without it there is
    nothing to price freight against, so the request is rejected rather than
    silently assuming a weight.
    """
    if payload.unit_weight_kg is None:
        raise HTTPException(
            status_code=422,
            detail="unit_weight_kg is required to estimate freight.",
        )

    try:
        breakdown = estimate_landed_cost(
            unit_cost_usd=payload.unit_cost_usd,
            unit_weight_kg=payload.unit_weight_kg,
            units=payload.units,
            origin_country="CN",
            destination_country=payload.destination_country,
        )
    except ValueError as exc:
        # Unsupported trade lane — a valid request we cannot serve.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return LandedCostResponse(
        product_title=payload.product_title,
        units=payload.units,
        estimated_unit_cost_usd=None,
        estimated_landed_cost_usd=breakdown["total_landed_cost_usd"],
        estimated_landed_cost_per_unit_usd=breakdown["landed_cost_per_unit_usd"],
        estimated_margin_pct=None,
        freight_per_unit_usd=breakdown["freight_per_unit_usd"],
        duty_pct=breakdown["duty_pct"],
        duty_usd=breakdown["duty_usd"],
        assumptions=breakdown["assumptions"],
        # No retail price in the request, so margin cannot be derived here.
        risk_flags=[RiskFlag.LOW_DATA],
        confidence_score=0.25,
        status=ResultStatus.CURATED,
    )


@router.post(
    "/reports",
    response_model=ReportResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Request a product viability report",
)
async def create_report(
    payload: ReportRequest,
    account_id: str = Depends(get_current_account),
) -> ReportResponse:
    """Charge one credit, persist the request as pending, hand it to the lane.

    The response is the pending report, not the finished one: generation
    happens asynchronously on Kafka and completes when report_listener sees
    `goal.completed`. Poll GET /v1/reports/{id}, which answers 409 until then.

    The credit is taken *before* the goal is published, so a caller cannot
    queue work it has not paid for. If publishing then fails the credit is
    refunded — see below.
    """
    # The id is allocated before the charge so the ledger row can reference
    # the report it paid for; without that, a history line is just "-1 charge"
    # with nothing to trace it to.
    report_id = report_store.new_report_id()

    if not await billing.charge_for_report(account_id, report_id):
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                f"Insufficient credits: a report costs "
                f"{billing.REPORT_COST_CREDITS} and your balance is "
                f"{await billing.get_balance(account_id)}. Top up to continue."
            ),
        )

    await report_store.create_report(report_id, account_id)

    items = load_opportunities()[: payload.max_products]
    report = ReportResponse(
        report_id=report_id,
        report_status=ReportStatus.PENDING,
        category=payload.category,
        query=payload.query,
        opportunities=items,
        summary=(
            f"{len(items)} curated {payload.category} opportunities scored by the "
            f"deterministic engine. Awaiting the agent lane's synthesis. Curated "
            f"synthetic inputs: no datapoint here was observed from a marketplace, "
            f"supplier or customs source."
        ),
        risk_flags=[RiskFlag.LOW_DATA],
        confidence_score=0.25,
        status=ResultStatus.CURATED,
    )
    await report_store.update_report(
        report_id, report_store.STATUS_PENDING, report.model_dump(mode="json")
    )

    goal_text = (
        payload.query
        or f"Produce a product viability report for the {payload.category} category."
    )
    try:
        await kafka_publisher.publish_goal(
            kafka_publisher.build_goal_event(
                report_id=report_id,
                goal_text=goal_text,
                category=payload.category,
                max_products=payload.max_products,
            )
        )
    except Exception as exc:  # broker down or unreachable
        # Without a published goal nothing will ever complete this report, so
        # it is marked failed now rather than left pending forever — and the
        # credit goes back, because the customer is not paying for an outage.
        await report_store.update_report(
            report_id, report_store.STATUS_FAILED, report.model_dump(mode="json")
        )
        await billing.refund_credits(
            account_id, billing.REPORT_COST_CREDITS, reference=f"{report_id}:publish_failed"
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Could not queue report {report_id!r} for generation: {exc}",
        ) from exc

    return report


@router.get(
    "/reports/{report_id}",
    response_model=ReportResponse,
    responses={404: {"description": "No report with that id in this process."}},
    summary="Fetch a previously requested report",
)
async def get_report(report_id: str) -> ReportResponse:
    """404 unknown, 409 while the agent lane is still working, 200 once settled.

    A caller polling for completion has to be able to tell "not ready yet" from
    "wrong id"; collapsing both into 404 would make a poller give up on a
    report that was about to arrive.
    """
    row = await report_store.get_report(report_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No report {report_id!r}.",
        )

    if row.status == report_store.STATUS_PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Report {report_id!r} is pending: the agent lane has not "
                f"reported back yet."
            ),
        )

    report = await report_store.get_report_response(report_id)
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Report {report_id!r} is {row.status} with no stored body.",
        )
    return report


# --------------------------------------------------------------------------
# GET /v1/account/transactions
# --------------------------------------------------------------------------


@router.get(
    "/account/transactions",
    response_model=AccountTransactionsResponse,
    summary="Your credit ledger, newest first",
)
async def list_own_transactions(
    limit: int = Query(default=50, gt=0, le=200),
    offset: int = Query(default=0, ge=0),
    account_id: str = Depends(get_current_account),
) -> AccountTransactionsResponse:
    """Answers "why is my balance N" for the caller's own account.

    There is deliberately no account_id parameter. The account comes from the
    API key, so reading someone else's ledger is not a permission check that
    could be got wrong — it is unrepresentable.
    """
    rows = await billing.list_transactions(account_id, limit=limit, offset=offset)
    return AccountTransactionsResponse(
        account_id=account_id,
        balance=await billing.get_balance(account_id),
        count=await billing.count_transactions(account_id),
        items=[
            CreditTransactionOut(
                id=row.id,
                delta=row.delta,
                reason=row.reason,
                reference=row.reference,
                balance_after=row.balance_after,
                created_at=row.created_at.isoformat(),
            )
            for row in rows
        ],
    )


# --------------------------------------------------------------------------
# Billing: credit packs and Stripe Checkout
# --------------------------------------------------------------------------


@router.get(
    "/billing/packs",
    response_model=CreditPacksResponse,
    summary="Credit packs available for purchase",
)
async def list_packs() -> CreditPacksResponse:
    """The catalogue, served from PRICE_TABLE so it cannot drift from checkout."""
    return CreditPacksResponse(
        items=[
            CreditPack(
                pack_id=pack_id,
                name=pack["name"],
                credits=pack["credits"],
                amount_cents=pack["amount_cents"],
                currency=pack["currency"],
            )
            for pack_id, pack in stripe_service.PRICE_TABLE.items()
        ]
    )


@router.post(
    "/billing/checkout",
    response_model=CheckoutResponse,
    summary="Start a Stripe Checkout Session for a credit pack",
    responses={
        400: {"description": "Unknown pack_id."},
        503: {"description": "Stripe is not configured on this deployment."},
    },
)
async def create_checkout(
    payload: CheckoutRequest,
    account_id: str = Depends(get_current_account),
) -> CheckoutResponse:
    """Create a one-time Checkout Session for the caller's own account.

    The account comes from the API key, never from the request body, so a
    caller cannot start a session that credits someone else.
    """
    try:
        pack = stripe_service.get_pack(payload.pack_id)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Unknown pack_id {payload.pack_id!r}. Available: "
                f"{sorted(stripe_service.PRICE_TABLE)}."
            ),
        ) from None

    try:
        session = stripe_service.create_checkout_session(
            account_id=account_id,
            pack_id=payload.pack_id,
            # Real local pages, not example.com. The placeholder was why a
            # successful payment landed on "Example Domain" and looked like a
            # broken checkout URL — the payment had in fact completed.
            # {CHECKOUT_SESSION_ID} is substituted by Stripe on redirect.
            success_url=payload.success_url
            or f"{billing_pages.PUBLIC_BASE_URL}{billing_pages.SUCCESS_PATH}"
            f"?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=payload.cancel_url
            or f"{billing_pages.PUBLIC_BASE_URL}{billing_pages.CANCEL_PATH}",
        )
    except stripe_service.StripeNotConfigured as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except stripe_service.LiveKeyRefused as exc:
        # Deliberately loud: this project must never touch real money.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except stripe.error.StripeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Stripe rejected the checkout request: {exc}",
        ) from exc

    return CheckoutResponse(
        checkout_url=session.url,
        session_id=session.id,
        pack_id=payload.pack_id,
        credits=pack["credits"],
        amount_cents=pack["amount_cents"],
        currency=pack["currency"],
    )
