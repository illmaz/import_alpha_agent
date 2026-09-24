"""P3.1: the public marketing surface.

Two things matter here beyond "it renders". The samples must stay valid against
the real `ProductOpportunity` contract, so the page cannot quietly advertise a
payload shape the API no longer returns; and they must keep saying, in the
payload itself, that their numbers were never observed.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.api.v1.public_endpoints import (
    CUSTOM_FEED_TIER,
    FIXTURE_DIR,
    SAMPLE_REPORT_FILES,
    load_sample_reports,
)
from app.main import app
from app.schemas import ProductOpportunity
from app.services import stripe_service

client = TestClient(app)


@pytest.fixture(autouse=True)
def _clear_report_cache():
    load_sample_reports.cache_clear()
    yield
    load_sample_reports.cache_clear()


# ---------- landing page ----------


def test_landing_page_is_served_at_root():
    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_landing_page_shows_the_hero_headline():
    body = client.get("/").text

    assert "China-to-US product intelligence API for e-commerce agents" in body


def test_landing_page_advertises_all_three_prices():
    body = client.get("/").text

    # Rendered client-side from /v1/public/pricing, so assert the page wires
    # itself to that endpoint rather than hardcoding the numbers twice.
    assert "/v1/public/pricing" in body
    assert "/v1/public/sample-reports" in body


def test_static_mount_does_not_shadow_the_api():
    """The mount at "/" is registered last; earlier routes must still win."""
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/v1/public/pricing").status_code == 200


def test_unknown_path_is_not_swallowed_into_the_landing_page():
    assert client.get("/no-such-page").status_code == 404


# ---------- sample reports endpoint ----------


def test_sample_reports_need_no_api_key():
    response = client.get("/v1/public/sample-reports")

    assert response.status_code == 200


def test_sample_reports_returns_three_reports():
    body = client.get("/v1/public/sample-reports").json()

    assert body["count"] == 3
    assert len(body["reports"]) == 3
    assert body["data_class"] == "curated_synthetic"


def test_sample_reports_cover_three_distinct_categories():
    reports = client.get("/v1/public/sample-reports").json()["reports"]

    assert {r["category"] for r in reports} == {
        "home_organization",
        "kitchen_gadgets",
        "pet_accessories",
    }


def test_every_sample_report_has_five_products():
    reports = client.get("/v1/public/sample-reports").json()["reports"]

    for report in reports:
        assert len(report["products"]) == 5, report["report_id"]


def test_sample_products_validate_against_the_real_schema():
    """Marketing must not drift from the contract the API actually serves."""
    reports = client.get("/v1/public/sample-reports").json()["reports"]

    for report in reports:
        for raw in report["products"]:
            ProductOpportunity.model_validate(raw)


# ---------- honesty ----------


def test_every_sample_report_is_marked_curated_not_ok():
    reports = client.get("/v1/public/sample-reports").json()["reports"]

    for report in reports:
        assert report["status"] == "curated"
        assert report["data_class"] == "curated_synthetic"


def test_every_sample_report_carries_a_disclaimer():
    reports = client.get("/v1/public/sample-reports").json()["reports"]

    for report in reports:
        disclaimer = report["disclaimer"].lower()
        assert "synthetic" in disclaimer
        assert "not" in disclaimer


def test_no_sample_product_claims_to_be_observed():
    """A curated number must not carry an http(s) source or full confidence."""
    reports = client.get("/v1/public/sample-reports").json()["reports"]

    for report in reports:
        for product in report["products"]:
            assert product["status"] == "curated"
            assert product["confidence_score"] < 0.5
            assert product["source_metadata"], product["product_id"]
            for source in product["source_metadata"]:
                assert source["source_url"].startswith("curated://"), source["source_url"]


def test_landing_page_states_the_samples_are_synthetic():
    body = client.get("/").text.lower()

    assert "synthetic" in body


# ---------- fixture integrity ----------


def test_sample_margins_agree_with_their_own_cost_and_price():
    """Guards against a typo that a buyer would notice before we did."""
    reports = client.get("/v1/public/sample-reports").json()["reports"]

    for report in reports:
        for p in report["products"]:
            landed, retail = p["estimated_landed_cost_usd"], p["estimated_retail_price_usd"]
            expected = (retail - landed) / retail * 100
            assert p["estimated_margin_pct"] == pytest.approx(expected, abs=0.05), p["product_id"]


def test_sample_cost_ranges_are_ordered_and_below_retail():
    reports = client.get("/v1/public/sample-reports").json()["reports"]

    for report in reports:
        for p in report["products"]:
            cost = p["estimated_unit_cost_usd"]
            assert cost["low_usd"] <= cost["high_usd"], p["product_id"]
            assert p["estimated_landed_cost_usd"] < p["estimated_retail_price_usd"]


def test_competition_signal_matches_the_competition_score():
    reports = client.get("/v1/public/sample-reports").json()["reports"]

    for report in reports:
        for p in report["products"]:
            score = p["competition_score"]
            expected = "high" if score >= 65 else "moderate" if score >= 45 else "low"
            assert p["competition_signal"] == expected, p["product_id"]


def test_every_declared_fixture_file_exists_on_disk():
    for name in SAMPLE_REPORT_FILES:
        assert (FIXTURE_DIR / name).is_file(), name


def test_fixture_files_are_valid_json():
    for name in SAMPLE_REPORT_FILES:
        json.loads((FIXTURE_DIR / name).read_text())


# ---------- pricing endpoint ----------


def test_pricing_needs_no_api_key():
    assert client.get("/v1/public/pricing").status_code == 200


def test_pricing_lists_99_299_and_999():
    tiers = client.get("/v1/public/pricing").json()["tiers"]

    assert [t["amount_cents"] for t in tiers] == [9900, 29900, 99900]


def test_self_serve_tiers_come_from_the_stripe_price_table():
    """The page must not be able to quote a price checkout would not honour."""
    tiers = client.get("/v1/public/pricing").json()["tiers"]
    self_serve = {t["tier_id"]: t for t in tiers if t["self_serve"]}

    assert set(self_serve) == set(stripe_service.PRICE_TABLE)
    for pack_id, pack in stripe_service.PRICE_TABLE.items():
        assert self_serve[pack_id]["amount_cents"] == pack["amount_cents"]
        assert self_serve[pack_id]["credits"] == pack["credits"]


def test_custom_feed_tier_is_not_self_serve():
    """$999 has no Stripe path, so it must not look purchasable."""
    tiers = client.get("/v1/public/pricing").json()["tiers"]
    custom = next(t for t in tiers if t["tier_id"] == "custom_feed")

    assert custom["self_serve"] is False
    assert custom["credits"] is None


def test_custom_feed_is_absent_from_the_checkout_price_table():
    assert CUSTOM_FEED_TIER["tier_id"] not in stripe_service.PRICE_TABLE
