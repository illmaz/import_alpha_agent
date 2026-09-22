"""Landed cost heuristic tests: arithmetic, scaling and input contract."""

from __future__ import annotations

import pytest

from app.services.landed_cost import (
    DEFAULT_DUTY_PCT,
    FREIGHT_USD_PER_KG,
    estimate_landed_cost,
)


def test_known_order_matches_hand_calculation() -> None:
    # goods 2.40*500 = 1200 ; freight 0.35*0.50*500 = 87.50 ; duty 1200*6.5% = 78.00
    result = estimate_landed_cost(2.40, 0.35, 500)
    assert result["freight_per_unit_usd"] == pytest.approx(0.175)
    assert result["duty_usd"] == pytest.approx(78.00)
    assert result["total_landed_cost_usd"] == pytest.approx(1365.50)


def test_returns_every_documented_key() -> None:
    result = estimate_landed_cost(2.40, 0.35, 500)
    for key in (
        "estimated_unit_cost_usd",
        "freight_per_unit_usd",
        "duty_pct",
        "duty_usd",
        "total_landed_cost_usd",
    ):
        assert key in result


def test_confidence_is_low_because_no_real_tariff_data_exists() -> None:
    assert estimate_landed_cost(2.40, 0.35, 500)["confidence"] == "low"


def test_assumptions_travel_with_the_estimate() -> None:
    assumptions = estimate_landed_cost(2.40, 0.35, 500)["assumptions"]
    assert assumptions, "an estimate must never ship without its assumptions"
    assert any("Duty" in a for a in assumptions)


def test_duty_uses_the_documented_rate() -> None:
    result = estimate_landed_cost(10.0, 1.0, 100)
    assert result["duty_pct"] == DEFAULT_DUTY_PCT
    assert result["duty_usd"] == pytest.approx(10.0 * 100 * DEFAULT_DUTY_PCT / 100)


def test_freight_uses_the_documented_rate() -> None:
    result = estimate_landed_cost(5.0, 2.0, 10)
    assert result["freight_per_unit_usd"] == pytest.approx(2.0 * FREIGHT_USD_PER_KG)


# --- scaling behaviour ----------------------------------------------------


def test_total_scales_linearly_with_units() -> None:
    one = estimate_landed_cost(3.0, 0.5, 100)["total_landed_cost_usd"]
    ten = estimate_landed_cost(3.0, 0.5, 1000)["total_landed_cost_usd"]
    assert ten == pytest.approx(one * 10)


def test_per_unit_cost_is_quantity_independent() -> None:
    small = estimate_landed_cost(3.0, 0.5, 10)["landed_cost_per_unit_usd"]
    large = estimate_landed_cost(3.0, 0.5, 100_000)["landed_cost_per_unit_usd"]
    assert small == pytest.approx(large)


def test_heavier_units_cost_more_to_land() -> None:
    light = estimate_landed_cost(3.0, 0.2, 100)["total_landed_cost_usd"]
    heavy = estimate_landed_cost(3.0, 5.0, 100)["total_landed_cost_usd"]
    assert heavy > light


def test_landed_cost_always_exceeds_goods_cost() -> None:
    result = estimate_landed_cost(4.0, 0.8, 250)
    assert result["total_landed_cost_usd"] > 4.0 * 250


# --- input contract -------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        (0.0, 0.5, 100),      # unit cost must be > 0
        (-1.0, 0.5, 100),
        (3.0, 0.0, 100),      # weight must be > 0
        (3.0, -0.5, 100),
        (3.0, 0.5, 0),        # units must be > 0
        (3.0, 0.5, -10),
    ],
)
def test_non_positive_inputs_raise(args: tuple) -> None:
    with pytest.raises(ValueError):
        estimate_landed_cost(*args)


def test_unsupported_trade_lane_raises() -> None:
    with pytest.raises(ValueError, match="origin_country"):
        estimate_landed_cost(3.0, 0.5, 100, origin_country="VN")
    with pytest.raises(ValueError, match="destination_country"):
        estimate_landed_cost(3.0, 0.5, 100, destination_country="DE")


def test_country_codes_are_case_insensitive() -> None:
    result = estimate_landed_cost(3.0, 0.5, 100, origin_country="cn", destination_country="us")
    assert result["origin_country"] == "CN"
    assert result["destination_country"] == "US"


def test_is_deterministic() -> None:
    assert estimate_landed_cost(2.4, 0.35, 500) == estimate_landed_cost(2.4, 0.35, 500)
