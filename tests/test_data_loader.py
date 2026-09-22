"""Fixture loading and scoring-integration tests.

Also guards the provenance rules from AGENTS.md: curated data must not be able
to masquerade as observed data.
"""

from __future__ import annotations

import json

import pytest

from app.schemas import ProductOpportunity, ResultStatus, RiskFlag
from app.services.data_loader import (
    FIXTURE_PATH,
    load_fixture_records,
    load_opportunities,
    score_record,
)

REQUIRED_FIELDS = (
    "product_title",
    "category",
    "market",
    "trend_score",
    "competition_score",
    "estimated_unit_cost_usd_low",
    "estimated_unit_cost_usd_high",
    "unit_weight_kg",
    "risk_level",
    "source_urls",
    "last_observed_at",
)


@pytest.fixture(scope="module")
def records() -> list[dict]:
    return load_fixture_records()


# --- the fixture file itself ---------------------------------------------


def test_fixture_exists_and_is_a_json_array() -> None:
    assert FIXTURE_PATH.exists(), f"missing fixture at {FIXTURE_PATH}"
    assert isinstance(json.loads(FIXTURE_PATH.read_text(encoding="utf-8")), list)


def test_fixture_has_twenty_products(records: list[dict]) -> None:
    assert len(records) == 20


def test_every_record_has_every_required_field(records: list[dict]) -> None:
    for record in records:
        missing = [f for f in REQUIRED_FIELDS if f not in record]
        assert not missing, f"{record.get('product_id')} missing {missing}"


def test_product_ids_are_unique(records: list[dict]) -> None:
    ids = [r["product_id"] for r in records]
    assert len(ids) == len(set(ids))


def test_scores_are_within_range(records: list[dict]) -> None:
    for record in records:
        assert 0 <= record["trend_score"] <= 100
        assert 0 <= record["competition_score"] <= 100


def test_cost_ranges_are_ordered_and_positive(records: list[dict]) -> None:
    for record in records:
        low = record["estimated_unit_cost_usd_low"]
        high = record["estimated_unit_cost_usd_high"]
        assert 0 < low <= high, record["product_id"]


def test_risk_levels_are_valid(records: list[dict]) -> None:
    for record in records:
        assert record["risk_level"] in {"low", "medium", "high"}


def test_retail_price_exceeds_cost(records: list[dict]) -> None:
    for record in records:
        assert record["estimated_retail_price_usd"] > record["estimated_unit_cost_usd_high"]


# --- provenance honesty (AGENTS.md) --------------------------------------


def test_every_record_is_labelled_curated_synthetic(records: list[dict]) -> None:
    for record in records:
        assert record["data_class"] == "curated_synthetic"
        assert "CURATED SYNTHETIC" in record["provenance_note"]


def test_no_record_claims_a_real_web_source(records: list[dict]) -> None:
    """A plausible marketplace URL would be fabricated provenance.

    Worse than a fabricated number, because it looks verifiable. Curated rows
    must point at the curated:// scheme and nothing else.
    """
    for record in records:
        assert record["source_urls"], record["product_id"]
        for url in record["source_urls"]:
            assert url.startswith("curated://"), f"{record['product_id']}: {url}"
            assert not url.startswith(("http://", "https://"))


def test_curated_confidence_stays_low(records: list[dict]) -> None:
    for record in records:
        assert 0.0 < record["confidence"] <= 0.5


# --- loading and scoring --------------------------------------------------


def test_load_opportunities_returns_twenty_scored_models() -> None:
    items = load_opportunities()
    assert len(items) == 20
    assert all(isinstance(item, ProductOpportunity) for item in items)
    assert all(item.opportunity_score is not None for item in items)


def test_results_are_sorted_best_first() -> None:
    scores = [item.opportunity_score for item in load_opportunities()]
    assert scores == sorted(scores, reverse=True)


def test_every_opportunity_is_marked_curated() -> None:
    for item in load_opportunities():
        assert item.status is ResultStatus.CURATED


def test_every_opportunity_carries_full_provenance() -> None:
    """AGENTS.md: source_url + observed_at + confidence on every datapoint."""
    for item in load_opportunities():
        assert item.source_metadata, item.product_id
        assert item.confidence_score is not None
        for source in item.source_metadata:
            assert source.source_url
            assert source.observed_at
            assert 0.0 <= source.confidence <= 1.0


def test_every_opportunity_is_flagged_low_data() -> None:
    for item in load_opportunities():
        assert RiskFlag.LOW_DATA in item.risk_flags


def test_fixture_risk_flags_survive_into_the_model() -> None:
    by_id = {item.product_id: item for item in load_opportunities()}
    assert RiskFlag.OVERSIZED in by_id["HO-rolling-storage-cart-3tier"].risk_flags


def test_landed_cost_and_margin_are_derived() -> None:
    for item in load_opportunities():
        assert item.estimated_landed_cost_usd is not None
        assert item.estimated_margin_pct is not None
        # Landed cost must sit above the low end of the ex-works range.
        assert item.estimated_landed_cost_usd > item.estimated_unit_cost_usd.low_usd


def test_score_explanation_is_present_and_mentions_the_score() -> None:
    for item in load_opportunities():
        assert item.score_explanation
        assert f"score {int(item.opportunity_score)}/100" in item.score_explanation


def test_competition_signal_tracks_the_competition_score() -> None:
    for item in load_opportunities():
        if item.competition_score >= 65:
            assert item.competition_signal.value == "high"
        elif item.competition_score >= 45:
            assert item.competition_signal.value == "moderate"
        else:
            assert item.competition_signal.value == "low"


def test_loading_is_deterministic() -> None:
    first = [(i.product_id, i.opportunity_score) for i in load_opportunities()]
    second = [(i.product_id, i.opportunity_score) for i in load_opportunities()]
    assert first == second


def test_explicit_path_bypasses_the_cache(tmp_path) -> None:
    records = load_fixture_records()[:2]
    target = tmp_path / "subset.json"
    target.write_text(json.dumps(records), encoding="utf-8")
    assert len(load_opportunities(path=target)) == 2
    assert len(load_opportunities()) == 20


def test_malformed_fixture_raises(tmp_path) -> None:
    target = tmp_path / "bad.json"
    target.write_text(json.dumps({"not": "an array"}), encoding="utf-8")
    with pytest.raises(ValueError, match="JSON array"):
        load_fixture_records(target)


def test_score_record_is_pure() -> None:
    record = load_fixture_records()[0]
    snapshot = json.dumps(record, sort_keys=True)
    score_record(record)
    assert json.dumps(record, sort_keys=True) == snapshot
