"""Contract tests for the v1 API.

P1 Part 2: the endpoints now return curated, scored data rather than stubs.
These assert the wire format, the pagination and error contracts, and — most
importantly — that nothing curated can present itself as observed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas import (
    LandedCostResponse,
    OpportunitiesResponse,
    ReportResponse,
    ResultStatus,
)


@pytest.fixture(scope="module")
def client() -> TestClient:
    # The context manager runs the lifespan, which creates the schema against
    # the temporary database configured in conftest.
    with TestClient(app) as test_client:
        yield test_client


VALID_LANDED_COST = {
    "product_title": "bamboo drawer organizer",
    "unit_cost_usd": 2.40,
    "units": 500,
    "unit_weight_kg": 0.35,
    "destination_country": "US",
}


# --- health ---------------------------------------------------------------


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# --- GET /v1/opportunities ------------------------------------------------


def test_opportunities_returns_twenty_scored_products(client: TestClient) -> None:
    response = client.get("/v1/opportunities", params={"limit": 100})
    assert response.status_code == 200

    parsed = OpportunitiesResponse.model_validate(response.json())
    assert len(parsed.items) == 20
    assert parsed.count == 20
    assert parsed.status is ResultStatus.CURATED
    assert all(0 <= item.opportunity_score <= 100 for item in parsed.items)


def test_opportunities_are_sorted_best_first(client: TestClient) -> None:
    body = client.get("/v1/opportunities", params={"limit": 100}).json()
    scores = [item["opportunity_score"] for item in body["items"]]
    assert scores == sorted(scores, reverse=True)


def test_opportunities_carry_costs_margin_and_explanation(client: TestClient) -> None:
    item = client.get("/v1/opportunities").json()["items"][0]
    assert item["estimated_unit_cost_usd"]["low_usd"] > 0
    assert item["estimated_landed_cost_usd"] > 0
    assert item["estimated_margin_pct"] is not None
    assert item["score_explanation"]
    assert item["competition_signal"] in {"low", "moderate", "high", "unknown"}


def test_opportunities_pagination_slices_without_changing_count(client: TestClient) -> None:
    body = client.get("/v1/opportunities", params={"limit": 5, "offset": 0}).json()
    assert len(body["items"]) == 5
    assert body["count"] == 20, "count is the total, not the page size"

    second = client.get("/v1/opportunities", params={"limit": 5, "offset": 5}).json()
    assert [i["product_id"] for i in body["items"]] != [
        i["product_id"] for i in second["items"]
    ]


def test_opportunities_offset_past_the_end_returns_empty_page(client: TestClient) -> None:
    body = client.get("/v1/opportunities", params={"offset": 999}).json()
    assert body["items"] == []
    assert body["count"] == 20


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"offset": -1}])
def test_opportunities_rejects_bad_pagination(client: TestClient, params: dict) -> None:
    assert client.get("/v1/opportunities", params=params).status_code == 422


# --- POST /v1/landed-cost -------------------------------------------------


def test_landed_cost_returns_a_real_estimate(client: TestClient) -> None:
    response = client.post("/v1/landed-cost", json=VALID_LANDED_COST)
    assert response.status_code == 200

    parsed = LandedCostResponse.model_validate(response.json())
    assert parsed.product_title == VALID_LANDED_COST["product_title"]
    assert parsed.units == 500
    # goods 1200 + freight 87.50 + duty 78.00
    assert parsed.estimated_landed_cost_usd == pytest.approx(1365.50)
    assert parsed.estimated_landed_cost_per_unit_usd == pytest.approx(2.731)
    assert parsed.freight_per_unit_usd == pytest.approx(0.175)
    assert parsed.duty_pct == pytest.approx(6.5)


def test_landed_cost_flags_low_confidence_and_states_assumptions(client: TestClient) -> None:
    body = client.post("/v1/landed-cost", json=VALID_LANDED_COST).json()
    assert body["status"] == "curated"
    assert body["confidence_score"] <= 0.5
    assert "low_data" in body["risk_flags"]
    assert body["assumptions"], "an estimate must never ship without its assumptions"


def test_landed_cost_requires_weight_to_price_freight(client: TestClient) -> None:
    payload = {k: v for k, v in VALID_LANDED_COST.items() if k != "unit_weight_kg"}
    response = client.post("/v1/landed-cost", json=payload)
    assert response.status_code == 422
    assert "unit_weight_kg" in response.json()["detail"]


def test_landed_cost_rejects_an_unsupported_destination(client: TestClient) -> None:
    payload = {**VALID_LANDED_COST, "destination_country": "DE"}
    assert client.post("/v1/landed-cost", json=payload).status_code == 422


@pytest.mark.parametrize(
    "bad_payload",
    [
        {"product_title": "x", "unit_cost_usd": 0, "units": 10, "unit_weight_kg": 0.3},
        {"product_title": "x", "unit_cost_usd": 2.0, "units": 0, "unit_weight_kg": 0.3},
        {"product_title": "", "unit_cost_usd": 2.0, "units": 10, "unit_weight_kg": 0.3},
        {"unit_cost_usd": 2.0, "units": 10, "unit_weight_kg": 0.3},
        {"product_title": "x", "unit_cost_usd": 2.0, "units": 1, "junk": 1},
    ],
)
def test_landed_cost_rejects_invalid_requests(client: TestClient, bad_payload: dict) -> None:
    assert client.post("/v1/landed-cost", json=bad_payload).status_code == 422


# --- POST /v1/reports -----------------------------------------------------


def test_create_report_returns_202_with_scored_opportunities(client: TestClient) -> None:
    response = client.post("/v1/reports", json={"category": "home_organization"})
    assert response.status_code == 202

    parsed = ReportResponse.model_validate(response.json())
    assert parsed.report_id.startswith("R-")
    assert parsed.report_status.value == "ready"
    assert parsed.status is ResultStatus.CURATED
    assert len(parsed.opportunities) == 20
    assert parsed.summary


def test_create_report_honours_max_products(client: TestClient) -> None:
    body = client.post("/v1/reports", json={"max_products": 5}).json()
    assert len(body["opportunities"]) == 5


def test_create_report_ids_are_unique(client: TestClient) -> None:
    first = client.post("/v1/reports", json={}).json()["report_id"]
    second = client.post("/v1/reports", json={}).json()["report_id"]
    assert first != second


def test_create_report_rejects_out_of_range_max_products(client: TestClient) -> None:
    assert client.post("/v1/reports", json={"max_products": 0}).status_code == 422
    assert client.post("/v1/reports", json={"max_products": 101}).status_code == 422


# --- GET /v1/reports/{report_id} ------------------------------------------


def test_created_report_can_be_fetched_back(client: TestClient) -> None:
    created = client.post("/v1/reports", json={"query": "under-sink"}).json()

    response = client.get(f"/v1/reports/{created['report_id']}")
    assert response.status_code == 200

    fetched = ReportResponse.model_validate(response.json())
    assert fetched.report_id == created["report_id"]
    assert fetched.query == "under-sink"
    assert len(fetched.opportunities) == len(created["opportunities"])


def test_unknown_report_id_returns_404(client: TestClient) -> None:
    response = client.get("/v1/reports/R-doesnotexist")
    assert response.status_code == 404
    assert "R-doesnotexist" in response.json()["detail"]


# --- contract-wide --------------------------------------------------------


def test_openapi_exposes_all_four_mvp_endpoints(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    assert schema["info"]["title"] == "ImportAlpha Lite"

    paths = schema["paths"]
    assert "get" in paths["/v1/opportunities"]
    assert "post" in paths["/v1/landed-cost"]
    assert "post" in paths["/v1/reports"]
    assert "get" in paths["/v1/reports/{report_id}"]


def test_docs_page_is_served(client: TestClient) -> None:
    assert client.get("/docs").status_code == 200


def test_no_endpoint_presents_curated_data_as_observed(client: TestClient) -> None:
    """AGENTS.md: curated numbers must never look sourced.

    Every payload that carries a number must say status="curated", stay at low
    confidence, and cite only curated:// URIs — never an http(s) link that a
    reader could mistake for a verifiable observation.
    """
    created = client.post("/v1/reports", json={}).json()
    bodies = [
        client.get("/v1/opportunities", params={"limit": 100}).json(),
        client.post("/v1/landed-cost", json=VALID_LANDED_COST).json(),
        created,
        client.get(f"/v1/reports/{created['report_id']}").json(),
    ]
    for body in bodies:
        assert body["status"] == "curated"
        # The opportunities envelope is a list wrapper and carries no aggregate
        # confidence; its items each carry their own, asserted below.
        confidence = body.get("confidence_score")
        assert confidence is None or confidence <= 0.5

    for item in bodies[0]["items"]:
        assert item["status"] == "curated"
        assert item["source_metadata"], item["product_id"]
        for source in item["source_metadata"]:
            assert source["source_url"].startswith("curated://")
            assert source["observed_at"]
            assert source["confidence"] <= 0.5


# --- persistence ----------------------------------------------------------


def test_report_survives_a_process_restart(client: TestClient) -> None:
    """End-to-end version of the property P1 Part 3 exists to deliver.

    Disposing the engine and rebuilding it is what restarting the container
    does to the connection pool. The report must come back over HTTP, which
    proves it reached the SQLite file and not just a session cache.
    """
    import asyncio

    from app.database import init_models, reset_engine

    created = client.post("/v1/reports", json={"max_products": 4}).json()
    report_id = created["report_id"]

    asyncio.run(reset_engine())
    asyncio.run(init_models())

    response = client.get(f"/v1/reports/{report_id}")
    assert response.status_code == 200, "report did not survive the restart"

    fetched = ReportResponse.model_validate(response.json())
    assert fetched.report_id == report_id
    assert len(fetched.opportunities) == 4


def test_report_row_is_written_to_sqlite(client: TestClient) -> None:
    """POST must leave a durable row, not just return a body."""
    import asyncio

    from app.services.report_store import STATUS_READY, get_report

    created = client.post("/v1/reports", json={"max_products": 2}).json()

    row = asyncio.run(get_report(created["report_id"]))
    assert row is not None
    assert row.status == STATUS_READY
    assert row.created_at is not None


def test_pending_report_returns_409_not_404(client: TestClient) -> None:
    """A report being generated is a different answer from one that never existed."""
    import asyncio

    from app.services.report_store import create_report, new_report_id

    report_id = new_report_id()
    asyncio.run(create_report(report_id))

    response = client.get(f"/v1/reports/{report_id}")
    assert response.status_code == 409
    assert "pending" in response.json()["detail"]

    assert client.get("/v1/reports/R-neverexisted").status_code == 404
