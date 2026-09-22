"""Heuristic China-to-US landed cost estimation.

Deliberately crude and labelled as such. Every rate here is a placeholder
constant, not a looked-up tariff line, so every result carries
confidence="low". Replacing these with real HTS classification and live
freight quotes is a separate backlog item; until then no caller should treat
an output as a quote.
"""

from __future__ import annotations

from typing import Any, Dict

# Flat blended rate. Real freight depends on mode, volume, lane and season;
# this ignores all four and prices only weight.
FREIGHT_USD_PER_KG = 0.50

# Rough average US ad-valorem duty. The real rate is per-HTS-code and ranges
# from 0% to >25% even inside home organization, so this is the single largest
# source of error in the estimate.
DEFAULT_DUTY_PCT = 6.5

CONFIDENCE = "low"

SUPPORTED_ORIGINS = frozenset({"CN"})
SUPPORTED_DESTINATIONS = frozenset({"US"})


def estimate_landed_cost(
    unit_cost_usd: float,
    unit_weight_kg: float,
    units: int,
    origin_country: str = "CN",
    destination_country: str = "US",
) -> Dict[str, Any]:
    """Estimate total landed cost for an order.

    Args:
        unit_cost_usd: Ex-works unit cost. Must be > 0.
        unit_weight_kg: Shipping weight per unit. Must be > 0.
        units: Order quantity. Must be > 0.
        origin_country / destination_country: ISO-3166 alpha-2.

    Returns:
        A dict of the cost breakdown plus `confidence`, `duty_pct` and the
        `assumptions` that produced it. The assumptions travel with the number
        so a caller cannot quote it without also seeing what it rests on.

    Raises:
        ValueError: on non-positive inputs or an unsupported trade lane.
    """
    if unit_cost_usd <= 0:
        raise ValueError(f"unit_cost_usd must be > 0, got {unit_cost_usd}")
    if unit_weight_kg <= 0:
        raise ValueError(f"unit_weight_kg must be > 0, got {unit_weight_kg}")
    if units <= 0:
        raise ValueError(f"units must be > 0, got {units}")

    origin = origin_country.strip().upper()
    destination = destination_country.strip().upper()
    if origin not in SUPPORTED_ORIGINS:
        raise ValueError(
            f"origin_country {origin!r} not supported; only {sorted(SUPPORTED_ORIGINS)}"
        )
    if destination not in SUPPORTED_DESTINATIONS:
        raise ValueError(
            f"destination_country {destination!r} not supported; "
            f"only {sorted(SUPPORTED_DESTINATIONS)}"
        )

    freight_per_unit = round(unit_weight_kg * FREIGHT_USD_PER_KG, 4)
    goods_total = unit_cost_usd * units
    duty_usd = round(goods_total * DEFAULT_DUTY_PCT / 100.0, 2)
    freight_total = freight_per_unit * units
    total = round(goods_total + freight_total + duty_usd, 2)

    return {
        "estimated_unit_cost_usd": round(float(unit_cost_usd), 4),
        "freight_per_unit_usd": freight_per_unit,
        "duty_pct": DEFAULT_DUTY_PCT,
        "duty_usd": duty_usd,
        "total_landed_cost_usd": total,
        "landed_cost_per_unit_usd": round(total / units, 4),
        "units": units,
        "origin_country": origin,
        "destination_country": destination,
        "currency": "USD",
        "confidence": CONFIDENCE,
        "assumptions": [
            f"Freight priced at a flat ${FREIGHT_USD_PER_KG:.2f}/kg, ignoring mode, "
            f"volume, lane and season.",
            f"Duty applied at a flat {DEFAULT_DUTY_PCT}% ad valorem, not an HTS "
            f"classification for this product.",
            "Excludes last-mile delivery, customs brokerage, insurance, tariff "
            "surcharges and FBA/fulfilment fees.",
        ],
    }
