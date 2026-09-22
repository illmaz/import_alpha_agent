"""Request/response contracts for the four MVP endpoints (docs/CONTEXT.md).

Skeleton only: these models define the wire format, not how any number is
produced. No scoring, no database, no sourcing yet.

Every estimate is Optional and every response carries a `status`. A stub
response sets `status="stub"` and leaves the numbers as None rather than
inventing plausible ones — AGENTS.md forbids invented data, and a placeholder
that looks like a real estimate is the exact failure mode that rule exists to
prevent. Callers should branch on `status`, never on a number being non-null.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ResultStatus(str, Enum):
    """How much a payload's numbers can be trusted.

    CURATED is not a lesser OK — it means the numbers were hand-written for
    development and never observed anywhere. Nothing marked CURATED may be
    quoted to a customer.
    """

    OK = "ok"
    CURATED = "curated"
    STUB = "stub"


class RiskFlag(str, Enum):
    """Category-level risk signals surfaced on an opportunity."""

    COMPLIANCE = "compliance"
    IP_INFRINGEMENT = "ip_infringement"
    OVERSIZED = "oversized"
    FRAGILE = "fragile"
    BATTERY = "battery"
    SEASONAL = "seasonal"
    THIN_MARGIN = "thin_margin"
    HIGH_COMPETITION = "high_competition"
    LOW_DATA = "low_data"


class CompetitionSignal(str, Enum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    UNKNOWN = "unknown"


class SourceMetadata(BaseModel):
    """Provenance for a single datapoint.

    AGENTS.md: every datapoint must carry source_url, observed_at and
    confidence. This model is the enforcement point — a datapoint that cannot
    name its source cannot be represented.
    """

    model_config = ConfigDict(extra="forbid")

    source_url: str = Field(description="Where this datapoint was observed.")
    observed_at: str = Field(description="ISO-8601 UTC timestamp of observation.")
    confidence: float = Field(ge=0.0, le=1.0, description="0.0–1.0.")
    note: Optional[str] = Field(default=None, description="Optional caveat.")


class CostRange(BaseModel):
    """China unit cost is quoted as a range, never a point estimate."""

    model_config = ConfigDict(extra="forbid")

    low_usd: float = Field(ge=0.0)
    high_usd: float = Field(ge=0.0)
    currency: str = Field(default="USD")


# --------------------------------------------------------------------------
# GET /v1/opportunities
# --------------------------------------------------------------------------


class ProductOpportunity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: str
    title: str
    category: str = Field(default="home_organization")

    opportunity_score: Optional[float] = Field(
        default=None, ge=0.0, le=100.0, description="0–100. None while status=stub."
    )
    estimated_unit_cost_usd: Optional[CostRange] = None
    estimated_landed_cost_usd: Optional[float] = Field(default=None, ge=0.0)
    estimated_margin_pct: Optional[float] = None
    competition_signal: CompetitionSignal = CompetitionSignal.UNKNOWN

    trend_score: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    competition_score: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    estimated_retail_price_usd: Optional[float] = Field(default=None, ge=0.0)
    score_explanation: Optional[str] = Field(
        default=None, description="Term-by-term breakdown of opportunity_score."
    )

    risk_flags: List[RiskFlag] = Field(default_factory=list)
    confidence_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    source_metadata: List[SourceMetadata] = Field(default_factory=list)

    status: ResultStatus = ResultStatus.STUB


class OpportunitiesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[ProductOpportunity] = Field(default_factory=list)
    count: int = 0
    status: ResultStatus = ResultStatus.STUB
    generated_at: str = Field(default_factory=_utc_now_iso)


# --------------------------------------------------------------------------
# POST /v1/landed-cost
# --------------------------------------------------------------------------


class LandedCostRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_title: str = Field(min_length=1)
    unit_cost_usd: float = Field(gt=0.0)
    units: int = Field(gt=0, description="Order quantity.")
    unit_weight_kg: Optional[float] = Field(default=None, gt=0.0)
    destination_country: str = Field(default="US", min_length=2, max_length=2)
    freight_mode: Optional[str] = Field(default=None, description="sea | air")


class LandedCostResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_title: str
    units: int

    estimated_unit_cost_usd: Optional[CostRange] = None
    estimated_landed_cost_usd: Optional[float] = Field(default=None, ge=0.0)
    estimated_landed_cost_per_unit_usd: Optional[float] = Field(default=None, ge=0.0)
    estimated_margin_pct: Optional[float] = None

    freight_per_unit_usd: Optional[float] = Field(default=None, ge=0.0)
    duty_pct: Optional[float] = Field(default=None, ge=0.0)
    duty_usd: Optional[float] = Field(default=None, ge=0.0)
    assumptions: List[str] = Field(
        default_factory=list,
        description="What the estimate rests on. Travels with every number.",
    )

    risk_flags: List[RiskFlag] = Field(default_factory=list)
    confidence_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    source_metadata: List[SourceMetadata] = Field(default_factory=list)

    status: ResultStatus = ResultStatus.STUB
    generated_at: str = Field(default_factory=_utc_now_iso)


# --------------------------------------------------------------------------
# POST /v1/reports  and  GET /v1/reports/{report_id}
# --------------------------------------------------------------------------


class ReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str = Field(default="home_organization")
    query: Optional[str] = Field(default=None, description="Free-text focus.")
    max_products: int = Field(default=20, gt=0, le=100)


class ReportStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


class ReportResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_id: str
    report_status: ReportStatus = ReportStatus.PENDING
    category: str = "home_organization"
    query: Optional[str] = None

    opportunities: List[ProductOpportunity] = Field(default_factory=list)
    summary: Optional[str] = None

    risk_flags: List[RiskFlag] = Field(default_factory=list)
    confidence_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    source_metadata: List[SourceMetadata] = Field(default_factory=list)

    status: ResultStatus = ResultStatus.STUB
    created_at: str = Field(default_factory=_utc_now_iso)


class UserGoalPayload(BaseModel):
    """Payload of a `user.goals` event published by the API.

    `report_id` is carried here for any downstream consumer that reads the
    goal event directly. It is NOT how the report is correlated on the way
    back: the orchestrator builds a fresh payload for `goal.completed` and
    does not echo this one. Correlation rides on the event's `task_id`, which
    the orchestrator adopts as its `goal_id` and does echo. See
    app/services/kafka_publisher.py and docs/DECISIONS.md.
    """

    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, description="Plain-text goal for the planner.")
    report_id: Optional[str] = Field(
        default=None, description="Report this goal was raised for, if any."
    )
    category: Optional[str] = None
    max_products: Optional[int] = Field(default=None, gt=0, le=100)


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = "ok"


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    detail: str
    extra: Dict[str, Any] = Field(default_factory=dict)
