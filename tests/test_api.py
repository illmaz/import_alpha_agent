"""Contract tests for the FastAPI skeleton.

These assert the wire format and the honesty of the stubs — that no endpoint
emits a number it did not source. They deliberately do not assert business
values; there is no scoring engine yet. Runs fully offline: the API process
does not touch Kafka.
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
    return TestClient(app)


# --- health ---------------------------------------------------------------


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# --- GET /v1/opportunities ------------------------------------------------


def test_opportunities_returns_valid_empty_payload(client: TestClient) -> None:
    response = client.get("/v1/opportunities")
    assert response.status_code == 200

    body = response.json()
    parsed = OpportunitiesResponse.model_validate(body)
    assert parsed.items == []
    assert parsed.count == 0
    assert parsed.status is ResultStatus.STUB


def test_opportunities_accepts_pagination_params(client: TestClient) -> None:
    response = client.get("/v1/opportunities", params={"limit": 5, "offset": 10})
    assert response.status_code == 200
    OpportunitiesResponse.model_validate(response.json())


# --- POST /v1/landed-cost -------------------------------------------------


def test_landed_cost_echoes_identity_and_estimates_nothing(client: TestClient) -> None:
    payload = {
        "product_title": "bamboo drawer organizer",
        "unit_cost_usd": 2.4,
        "units": 500,
        "unit_weight_kg": 0.35,
        "destination_country": "US",
    }
    response = client.post("/v1/landed-cost", json=payload)
    assert response.status_code == 200

    parsed = LandedCostResponse.model_validate(response.json())
    assert parsed.product_title == payload["product_title"]
    assert parsed.units == payload["units"]

    # The point of the stub: identity echoes back, estimates stay absent.
    assert parsed.status is ResultStatus.STUB
    assert parsed.estimated_landed_cost_usd is None
    assert parsed.estimated_unit_cost_usd is None
    assert parsed.confidence_score is None
    assert parsed.source_metadata == []


@pytest.mark.parametrize(
    "bad_payload",
    [
        {"product_title": "x", "unit_cost_usd": 0, "units": 10},      # cost must be > 0
        {"product_title": "x", "unit_cost_usd": 2.0, "units": 0},     # units must be > 0
        {"product_title": "", "unit_cost_usd": 2.0, "units": 10},     # title required
        {"unit_cost_usd": 2.0, "units": 10},                          # title missing
        {"product_title": "x", "unit_cost_usd": 2.0, "units": 1, "junk": 1},  # extra forbidden
    ],
)
def test_landed_cost_rejects_invalid_requests(client: TestClient, bad_payload: dict) -> None:
    assert client.post("/v1/landed-cost", json=bad_payload).status_code == 422


# --- POST /v1/reports -----------------------------------------------------


def test_create_report_allocates_pending_id(client: TestClient) -> None:
    response = client.post("/v1/reports", json={"category": "home_organization"})
    assert response.status_code == 202

    parsed = ReportResponse.model_validate(response.json())
    assert parsed.report_id.startswith("R-")
    assert parsed.report_status.value == "pending"
    assert parsed.status is ResultStatus.STUB
    assert parsed.opportunities == []
    assert parsed.source_metadata == []


def test_create_report_ids_are_unique(client: TestClient) -> None:
    first = client.post("/v1/reports", json={}).json()["report_id"]
    second = client.post("/v1/reports", json={}).json()["report_id"]
    assert first != second


def test_create_report_rejects_out_of_range_max_products(client: TestClient) -> None:
    assert client.post("/v1/reports", json={"max_products": 0}).status_code == 422
    assert client.post("/v1/reports", json={"max_products": 101}).status_code == 422


# --- GET /v1/reports/{report_id} ------------------------------------------


def test_get_report_echoes_requested_id(client: TestClient) -> None:
    response = client.get("/v1/reports/R-abc12345")
    assert response.status_code == 200

    parsed = ReportResponse.model_validate(response.json())
    assert parsed.report_id == "R-abc12345"
    assert parsed.status is ResultStatus.STUB


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


def test_no_stub_endpoint_emits_an_unsourced_number(client: TestClient) -> None:
    """AGENTS.md: no datapoint without source_url/observed_at/confidence.

    A stub that quietly returned a plausible estimate would pass every other
    test in this file, so this asserts the rule directly.
    """
    bodies = [
        client.get("/v1/opportunities").json(),
        client.post(
            "/v1/landed-cost",
            json={"product_title": "x", "unit_cost_usd": 1.0, "units": 1},
        ).json(),
        client.post("/v1/reports", json={}).json(),
        client.get("/v1/reports/R-abc12345").json(),
    ]
    for body in bodies:
        assert body["status"] == "stub"
        assert body.get("confidence_score") is None
        assert body.get("source_metadata", []) == []
        for field in (
            "opportunity_score",
            "estimated_landed_cost_usd",
            "estimated_unit_cost_usd",
            "estimated_margin_pct",
        ):
            assert body.get(field) is None, f"{field} was populated in a stub"
