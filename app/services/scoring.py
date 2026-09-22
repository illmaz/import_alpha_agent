"""Deterministic opportunity scoring.

Pure functions, no I/O, no LLM. AGENTS.md prefers deterministic Python first,
so the score a caller pays for is reproducible and explainable rather than a
model's opinion that changes between runs.

Weights (sum to 1.0):
    margin              0.35
    trend               0.25
    competition_inverse 0.20
    risk_inverse        0.10
    confidence          0.10
"""

from __future__ import annotations

from typing import Dict, Tuple

W_MARGIN = 0.35
W_TREND = 0.25
W_COMPETITION = 0.20
W_RISK = 0.10
W_CONFIDENCE = 0.10

# A 60% gross margin scores full marks on the margin component. Above that the
# component saturates: the difference between 60% and 80% margin is not what
# decides whether a product is worth sourcing, and letting it run to 100 would
# let one spectacular margin mask bad trend and competition scores.
MARGIN_SATURATION_PCT = 60.0

# risk_level is categorical, not a scale — these are the only three values the
# fixtures and scoring agree on.
RISK_INVERSE: Dict[str, float] = {
    "low": 100.0,
    "medium": 50.0,
    "high": 0.0,
}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def margin_component(margin_pct: float) -> float:
    """Normalise a gross margin percentage to 0–100.

    A negative margin floors at 0 rather than raising: a loss-making product is
    a legitimate input, it simply scores nothing here.
    """
    return _clamp(margin_pct / MARGIN_SATURATION_PCT * 100.0, 0.0, 100.0)


def calculate_opportunity_score(
    margin_pct: float,
    trend_score: float,
    competition_score: float,
    risk_level: str,
    confidence: float,
) -> Tuple[int, str]:
    """Score a product opportunity from 0–100 with a human-readable rationale.

    Args:
        margin_pct: Gross margin percentage. Saturates at 60%; may be negative.
        trend_score: 0–100, higher means stronger demand trend.
        competition_score: 0–100, higher means MORE competition (inverted here).
        risk_level: "low" | "medium" | "high".
        confidence: 0.0–1.0 confidence in the underlying datapoints.

    Returns:
        (score, explanation). Score is a rounded int in 0–100.

    Raises:
        ValueError: on an out-of-range score or an unknown risk_level. These
            are contract violations by the caller, not weak data, so they fail
            loudly rather than being silently clamped.
    """
    if not 0.0 <= trend_score <= 100.0:
        raise ValueError(f"trend_score must be 0-100, got {trend_score}")
    if not 0.0 <= competition_score <= 100.0:
        raise ValueError(f"competition_score must be 0-100, got {competition_score}")
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence must be 0.0-1.0, got {confidence}")

    key = risk_level.strip().lower()
    if key not in RISK_INVERSE:
        raise ValueError(
            f"risk_level must be one of {sorted(RISK_INVERSE)}, got {risk_level!r}"
        )

    margin_part = margin_component(margin_pct)
    trend_part = trend_score
    competition_part = 100.0 - competition_score
    risk_part = RISK_INVERSE[key]
    confidence_part = confidence * 100.0

    raw = (
        W_MARGIN * margin_part
        + W_TREND * trend_part
        + W_COMPETITION * competition_part
        + W_RISK * risk_part
        + W_CONFIDENCE * confidence_part
    )
    score = int(round(_clamp(raw, 0.0, 100.0)))

    explanation = (
        f"score {score}/100 = "
        f"margin {margin_part:.0f}x{W_MARGIN:.2f} + "
        f"trend {trend_part:.0f}x{W_TREND:.2f} + "
        f"competition_inverse {competition_part:.0f}x{W_COMPETITION:.2f} + "
        f"risk_inverse {risk_part:.0f}x{W_RISK:.2f} + "
        f"confidence {confidence_part:.0f}x{W_CONFIDENCE:.2f}"
        f" (margin {margin_pct:.1f}% saturates at {MARGIN_SATURATION_PCT:.0f}%, "
        f"risk={key})"
    )
    return score, explanation
