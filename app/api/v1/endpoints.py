"""v1 endpoints.

Backed by the curated fixture set and the deterministic scoring engine. No
database and no live sourcing yet, so every response carries
`status="curated"` — plausible numbers, never observed. See
app/services/data_loader.py.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status

from app.schemas import (
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
from app.services.report_store import report_store

router = APIRouter(prefix="/v1", tags=["v1"])


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
def create_report(payload: ReportRequest) -> ReportResponse:
    """Generate and store a report synchronously, but answer 202.

    The 202 is forward-looking: generation moves onto the Kafka lane later, and
    callers that already poll GET /v1/reports/{id} will not need to change.
    """
    items = load_opportunities()[: payload.max_products]
    report = ReportResponse(
        report_id=report_store.new_report_id(),
        report_status=ReportStatus.READY,
        category=payload.category,
        query=payload.query,
        opportunities=items,
        summary=(
            f"{len(items)} curated {payload.category} opportunities scored by the "
            f"deterministic engine. Curated synthetic inputs: no datapoint here "
            f"was observed from a marketplace, supplier or customs source."
        ),
        risk_flags=[RiskFlag.LOW_DATA],
        confidence_score=0.25,
        status=ResultStatus.CURATED,
    )
    return report_store.save(report)


@router.get(
    "/reports/{report_id}",
    response_model=ReportResponse,
    responses={404: {"description": "No report with that id in this process."}},
    summary="Fetch a previously requested report",
)
def get_report(report_id: str) -> ReportResponse:
    """404s on an unknown id — now that reports are stored, it can tell."""
    report = report_store.get(report_id)
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No report {report_id!r}. Reports are held in memory only and "
                f"are lost on restart."
            ),
        )
    return report
