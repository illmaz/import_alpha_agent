"""v1 endpoint stubs.

Wiring and contracts only. Every handler returns `status="stub"` with no
numbers attached; the scoring engine, fixture dataset and landed-cost
estimator are separate backlog items. Nothing here touches Kafka or a
database yet.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from app.schemas import (
    LandedCostRequest,
    LandedCostResponse,
    OpportunitiesResponse,
    ReportRequest,
    ReportResponse,
    ReportStatus,
    ResultStatus,
)

router = APIRouter(prefix="/v1", tags=["v1"])

_STUB_NOTE = "Skeleton endpoint: no sourced data attached yet."


@router.get(
    "/opportunities",
    response_model=OpportunitiesResponse,
    summary="List scored product opportunities",
)
def list_opportunities(limit: int = 20, offset: int = 0) -> OpportunitiesResponse:
    """Empty until the fixture dataset and scoring engine land."""
    return OpportunitiesResponse(items=[], count=0, status=ResultStatus.STUB)


@router.post(
    "/landed-cost",
    response_model=LandedCostResponse,
    summary="Estimate China-to-US landed cost",
)
def estimate_landed_cost(payload: LandedCostRequest) -> LandedCostResponse:
    """Echoes the request identity; every estimate stays None.

    Returning a plausible-looking number here would be inventing data, and a
    caller could not tell it from a real estimate. Branch on `status`.
    """
    return LandedCostResponse(
        product_title=payload.product_title,
        units=payload.units,
        status=ResultStatus.STUB,
    )


@router.post(
    "/reports",
    response_model=ReportResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Request a product viability report",
)
def create_report(payload: ReportRequest) -> ReportResponse:
    """Allocates a report id. No generation pipeline behind it yet.

    202 rather than 201: report generation will be asynchronous over the
    Kafka lane, so the id is an acknowledgement, not a finished resource.
    """
    return ReportResponse(
        report_id=f"R-{uuid.uuid4().hex[:8]}",
        report_status=ReportStatus.PENDING,
        category=payload.category,
        query=payload.query,
        summary=_STUB_NOTE,
        status=ResultStatus.STUB,
    )


@router.get(
    "/reports/{report_id}",
    response_model=ReportResponse,
    summary="Fetch a previously requested report",
)
def get_report(report_id: str) -> ReportResponse:
    """Echoes the id back as pending. No store to look it up in yet.

    Once the state store exists this must 404 on an unknown id; today it
    cannot distinguish one, so it does not pretend to.
    """
    return ReportResponse(
        report_id=report_id,
        report_status=ReportStatus.PENDING,
        summary=_STUB_NOTE,
        status=ResultStatus.STUB,
    )
