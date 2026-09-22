"""Load curated fixtures and score them into ProductOpportunity objects.

The fixture file is `curated_synthetic`: plausible hand-written values, not
observations. Everything this module emits therefore carries
`status=ResultStatus.CURATED` and the fixture's own low confidence, and each
opportunity's `source_metadata` points at the `curated://` URI it came from
rather than a marketplace link it was never seen on.

Replacing the fixture with sourced data is the only change needed to make
these responses real; nothing downstream has to move.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any, Dict, List

from app.schemas import (
    CompetitionSignal,
    CostRange,
    ProductOpportunity,
    ResultStatus,
    RiskFlag,
    SourceMetadata,
)
from app.services.landed_cost import estimate_landed_cost
from app.services.scoring import calculate_opportunity_score

FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "fixtures"
    / "home_organization_products.json"
)

# Landed cost per unit is quantity-independent under the current flat-rate
# model, so this only fixes the order size the estimate is quoted against.
REFERENCE_UNITS = 500

# Competition score is 0-100; these cut points turn it into the coarse signal
# the API exposes. Kept here rather than in scoring.py because it is a
# presentation concern, not part of the score.
COMPETITION_MODERATE_AT = 45.0
COMPETITION_HIGH_AT = 65.0


def _competition_signal(score: float) -> CompetitionSignal:
    if score >= COMPETITION_HIGH_AT:
        return CompetitionSignal.HIGH
    if score >= COMPETITION_MODERATE_AT:
        return CompetitionSignal.MODERATE
    return CompetitionSignal.LOW


def _risk_flags(record: Dict[str, Any]) -> List[RiskFlag]:
    """Fixture flags plus LOW_DATA, which every curated record earns by definition."""
    flags = [RiskFlag(flag) for flag in record.get("risk_flags", [])]
    if RiskFlag.LOW_DATA not in flags:
        flags.append(RiskFlag.LOW_DATA)
    return flags


def _source_metadata(record: Dict[str, Any]) -> List[SourceMetadata]:
    return [
        SourceMetadata(
            source_url=url,
            observed_at=record["last_observed_at"],
            confidence=record["confidence"],
            note=record.get("provenance_note"),
        )
        for url in record["source_urls"]
    ]


def score_record(record: Dict[str, Any]) -> ProductOpportunity:
    """Turn one fixture row into a scored opportunity.

    Margin is derived, not read: the midpoint of the cost range goes through
    the landed-cost heuristic, and the result is compared against the curated
    retail price. So a change to freight or duty rates moves the score, which
    is the behaviour we want when those become real.
    """
    cost_low = float(record["estimated_unit_cost_usd_low"])
    cost_high = float(record["estimated_unit_cost_usd_high"])
    cost_mid = (cost_low + cost_high) / 2.0

    breakdown = estimate_landed_cost(
        unit_cost_usd=cost_mid,
        unit_weight_kg=float(record["unit_weight_kg"]),
        units=REFERENCE_UNITS,
        origin_country="CN",
        destination_country=record.get("market", "US"),
    )
    landed_per_unit = float(breakdown["landed_cost_per_unit_usd"])

    retail = float(record["estimated_retail_price_usd"])
    margin_pct = (retail - landed_per_unit) / retail * 100.0

    confidence = float(record["confidence"])
    score, explanation = calculate_opportunity_score(
        margin_pct=margin_pct,
        trend_score=float(record["trend_score"]),
        competition_score=float(record["competition_score"]),
        risk_level=record["risk_level"],
        confidence=confidence,
    )

    return ProductOpportunity(
        product_id=record["product_id"],
        title=record["product_title"],
        category=record.get("category", "home_organization"),
        opportunity_score=score,
        estimated_unit_cost_usd=CostRange(low_usd=cost_low, high_usd=cost_high),
        estimated_landed_cost_usd=round(landed_per_unit, 2),
        estimated_margin_pct=round(margin_pct, 1),
        competition_signal=_competition_signal(float(record["competition_score"])),
        trend_score=float(record["trend_score"]),
        competition_score=float(record["competition_score"]),
        estimated_retail_price_usd=retail,
        score_explanation=explanation,
        risk_flags=_risk_flags(record),
        confidence_score=confidence,
        source_metadata=_source_metadata(record),
        status=ResultStatus.CURATED,
    )


def load_fixture_records(path: Path | None = None) -> List[Dict[str, Any]]:
    """Read the raw fixture rows. Separate from scoring so tests can inject rows."""
    target = path or FIXTURE_PATH
    with target.open(encoding="utf-8") as handle:
        records = json.load(handle)
    if not isinstance(records, list):
        raise ValueError(f"{target} must contain a JSON array, got {type(records).__name__}")
    return records


@functools.lru_cache(maxsize=1)
def _cached_opportunities() -> tuple[ProductOpportunity, ...]:
    records = load_fixture_records()
    scored = [score_record(record) for record in records]
    scored.sort(key=lambda item: item.opportunity_score or 0, reverse=True)
    return tuple(scored)


def load_opportunities(path: Path | None = None) -> List[ProductOpportunity]:
    """Scored opportunities, best first.

    The default path is cached: the fixture is read-only at runtime and this is
    on the hot path of GET /v1/opportunities. Passing an explicit path bypasses
    the cache, which is what tests want.
    """
    if path is not None:
        return [score_record(record) for record in load_fixture_records(path)]
    return list(_cached_opportunities())
