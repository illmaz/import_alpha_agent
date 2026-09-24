"""Public marketing endpoints. No API key, no credits, no account.

Everything here is material a stranger must be able to read before they have
any reason to trust us, so it is deliberately unauthenticated. Nothing served
from this router touches an account, a balance or a stored report.

The sample reports are `curated_synthetic`: hand-written specimens that show
the shape and depth of a real report. `ResultStatus.CURATED` exists precisely
so that unobserved numbers cannot be passed off as measurements, and these
carry a disclaimer in the payload as well as on the page. They demonstrate the
product; they are not intelligence.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from app.services import stripe_service

router = APIRouter(prefix="/v1/public", tags=["public"])

FIXTURE_DIR = Path(__file__).resolve().parents[3] / "data" / "fixtures"

# Order matters: this is the order the landing page shows them in, leading with
# the wedge category.
SAMPLE_REPORT_FILES = (
    "home_organizers_report.json",
    "kitchen_gadgets_report.json",
    "pet_accessories_report.json",
)

# CONTEXT.md offers a $999 custom category feed, but it is deliberately absent
# from stripe_service.PRICE_TABLE: that table drives /v1/billing/checkout, and
# a bespoke feed cannot be fulfilled by handing someone report credits. Listing
# it here as a non-self-serve tier keeps it visible to buyers without creating
# a checkout that would take money for something no code delivers.
CUSTOM_FEED_TIER: Dict[str, Any] = {
    "tier_id": "custom_feed",
    "name": "Custom category feed",
    "amount_cents": 99900,
    "currency": "usd",
    "credits": None,
    "self_serve": False,
    "description": "Ongoing sourced feed for a category we do not cover yet.",
}


class PricingTier(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tier_id: str
    name: str
    amount_cents: int
    currency: str
    credits: Optional[int] = Field(
        default=None, description="Report credits included. None for bespoke tiers."
    )
    self_serve: bool = Field(
        description="True if buyable via /v1/billing/checkout. False means contact sales."
    )
    description: Optional[str] = None


class PricingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tiers: List[PricingTier]
    currency: str = "usd"


class SampleReportsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reports: List[Dict[str, Any]]
    count: int
    data_class: str = Field(
        default="curated_synthetic",
        description="These are specimens, not observations.",
    )


@functools.lru_cache(maxsize=1)
def load_sample_reports() -> List[Dict[str, Any]]:
    """Read the sample fixtures once and hold them.

    They are static files that ship with the image, so re-reading per request
    would buy nothing. Cleared in tests via `load_sample_reports.cache_clear()`.
    """
    return [json.loads((FIXTURE_DIR / name).read_text()) for name in SAMPLE_REPORT_FILES]


@router.get(
    "/sample-reports",
    response_model=SampleReportsResponse,
    summary="Example reports (public, synthetic)",
)
async def sample_reports() -> SampleReportsResponse:
    """The three published specimen reports. No auth, no credit charged."""
    reports = load_sample_reports()
    return SampleReportsResponse(reports=reports, count=len(reports))


@router.get(
    "/pricing",
    response_model=PricingResponse,
    summary="Public price table",
)
async def pricing() -> PricingResponse:
    """Self-serve packs come from PRICE_TABLE so the page cannot quote a price
    that checkout would not honour. The bespoke tier is appended separately."""
    tiers = [
        PricingTier(
            tier_id=pack_id,
            name=pack["name"],
            amount_cents=pack["amount_cents"],
            currency=pack["currency"],
            credits=pack["credits"],
            self_serve=True,
        )
        for pack_id, pack in stripe_service.PRICE_TABLE.items()
    ]
    tiers.append(PricingTier(**CUSTOM_FEED_TIER))
    return PricingResponse(tiers=tiers)
