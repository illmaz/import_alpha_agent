"""Scoring engine tests: formula, weights, bounds and input contract."""

from __future__ import annotations

import pytest

from app.services.scoring import (
    MARGIN_SATURATION_PCT,
    RISK_INVERSE,
    W_COMPETITION,
    W_CONFIDENCE,
    W_MARGIN,
    W_RISK,
    W_TREND,
    calculate_opportunity_score,
    margin_component,
)


def test_weights_sum_to_one() -> None:
    total = W_MARGIN + W_TREND + W_COMPETITION + W_RISK + W_CONFIDENCE
    assert total == pytest.approx(1.0)


def test_perfect_inputs_score_100() -> None:
    score, _ = calculate_opportunity_score(100.0, 100.0, 0.0, "low", 1.0)
    assert score == 100


def test_worst_inputs_score_0() -> None:
    score, _ = calculate_opportunity_score(0.0, 0.0, 100.0, "high", 0.0)
    assert score == 0


def test_known_combination_matches_hand_calculation() -> None:
    # margin 30% -> 50.0 ; trend 80 ; competition 40 -> inverse 60 ; medium -> 50 ; conf .5 -> 50
    # 0.35*50 + 0.25*80 + 0.20*60 + 0.10*50 + 0.10*50 = 17.5 + 20 + 12 + 5 + 5 = 59.5 -> 60
    score, _ = calculate_opportunity_score(30.0, 80.0, 40.0, "medium", 0.5)
    assert score == 60


def test_explanation_reports_every_term_and_the_score() -> None:
    score, explanation = calculate_opportunity_score(45.0, 72.0, 45.0, "low", 0.3)
    assert f"score {score}/100" in explanation
    for term in ("margin", "trend", "competition_inverse", "risk_inverse", "confidence"):
        assert term in explanation


# --- margin component -----------------------------------------------------


def test_margin_saturates_at_the_cap() -> None:
    assert margin_component(MARGIN_SATURATION_PCT) == pytest.approx(100.0)
    assert margin_component(MARGIN_SATURATION_PCT * 2) == pytest.approx(100.0)


def test_negative_margin_floors_at_zero_rather_than_raising() -> None:
    assert margin_component(-40.0) == 0.0
    score, _ = calculate_opportunity_score(-40.0, 50.0, 50.0, "low", 0.5)
    assert 0 <= score <= 100


def test_margin_is_linear_below_the_cap() -> None:
    assert margin_component(MARGIN_SATURATION_PCT / 2) == pytest.approx(50.0)


# --- monotonicity: the formula must move the right way --------------------


def test_higher_trend_never_lowers_the_score() -> None:
    low, _ = calculate_opportunity_score(30.0, 20.0, 50.0, "low", 0.5)
    high, _ = calculate_opportunity_score(30.0, 90.0, 50.0, "low", 0.5)
    assert high > low


def test_more_competition_lowers_the_score() -> None:
    light, _ = calculate_opportunity_score(30.0, 50.0, 10.0, "low", 0.5)
    heavy, _ = calculate_opportunity_score(30.0, 50.0, 90.0, "low", 0.5)
    assert heavy < light


def test_higher_risk_lowers_the_score() -> None:
    scores = [
        calculate_opportunity_score(30.0, 50.0, 50.0, level, 0.5)[0]
        for level in ("low", "medium", "high")
    ]
    assert scores == sorted(scores, reverse=True)
    assert len(set(scores)) == 3


def test_higher_confidence_raises_the_score() -> None:
    unsure, _ = calculate_opportunity_score(30.0, 50.0, 50.0, "low", 0.0)
    sure, _ = calculate_opportunity_score(30.0, 50.0, 50.0, "low", 1.0)
    assert sure > unsure


# --- input contract -------------------------------------------------------


def test_risk_level_is_case_and_whitespace_insensitive() -> None:
    a, _ = calculate_opportunity_score(30.0, 50.0, 50.0, "  LOW  ", 0.5)
    b, _ = calculate_opportunity_score(30.0, 50.0, 50.0, "low", 0.5)
    assert a == b


@pytest.mark.parametrize(
    "kwargs",
    [
        {"trend_score": -1.0},
        {"trend_score": 101.0},
        {"competition_score": -1.0},
        {"competition_score": 101.0},
        {"confidence": -0.1},
        {"confidence": 1.1},
        {"risk_level": "catastrophic"},
    ],
)
def test_out_of_contract_inputs_raise(kwargs: dict) -> None:
    base = dict(
        margin_pct=30.0,
        trend_score=50.0,
        competition_score=50.0,
        risk_level="low",
        confidence=0.5,
    )
    base.update(kwargs)
    with pytest.raises(ValueError):
        calculate_opportunity_score(**base)


def test_scoring_is_deterministic() -> None:
    args = (37.5, 64.0, 51.0, "medium", 0.25)
    assert calculate_opportunity_score(*args) == calculate_opportunity_score(*args)


def test_every_risk_level_is_covered_by_the_inverse_table() -> None:
    assert set(RISK_INVERSE) == {"low", "medium", "high"}
