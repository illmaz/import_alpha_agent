"""Contract tests for the v1 API.

P1 Part 2: the endpoints now return curated, scored data rather than stubs.
These assert the wire format, the pagination and error contracts, and — most
importantly — that nothing curated can present itself as observed.
"""

from __future__ import annotations

import json

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
def _raw_client() -> TestClient:
    # The context manager runs the lifespan, which creates the schema against
    # the temporary database configured in conftest.
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def client(_raw_client: TestClient, auth_headers: dict) -> TestClient:
    """Authenticated client. Use `_raw_client` to test the unauthenticated path."""
    _raw_client.headers.update(auth_headers)
    return _raw_client


ACCOUNT_ID = "acct-test"
STARTING_CREDITS = 50


@pytest.fixture(autouse=True)
def account() -> str:
    """A funded account for each test. The table is wiped between tests."""
    import asyncio

    from app.services.billing import create_account

    asyncio.run(create_account(ACCOUNT_ID, STARTING_CREDITS, label="pytest"))
    return ACCOUNT_ID


@pytest.fixture(autouse=True)
def auth_headers(account: str) -> dict:
    """Issue a key and make it the default header on every request.

    Patching the client's headers rather than threading them through every
    call keeps the existing tests readable — they are testing endpoint
    behaviour, not the auth handshake, which has its own tests below.
    """
    import asyncio

    from app.services.auth import issue_api_key

    key, _ = asyncio.run(issue_api_key(account, label="pytest"))
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture(autouse=True)
def published(monkeypatch: pytest.MonkeyPatch) -> list:
    """Capture goal events instead of publishing them.

    No test may touch a real broker: the suite has to run with nothing else
    up, and a test that silently published would be invisible until CI had no
    Kafka.
    """
    from app.services import kafka_publisher

    captured: list = []

    async def fake_publish(event, topic=kafka_publisher.GOALS_TOPIC):
        captured.append((topic, event))

    monkeypatch.setattr(kafka_publisher, "publish_goal", fake_publish)
    return captured


def settle_report(report_id: str, summary: str = "Synthesis from the agent lane.") -> None:
    """Simulate the agent lane completing a goal, as report_listener would."""
    import asyncio

    from app.services.report_store import STATUS_READY, get_report, update_report

    async def _run() -> None:
        row = await get_report(report_id)
        payload = json.loads(row.payload_json)
        payload["report_status"] = "ready"
        payload["summary"] = summary
        await update_report(report_id, STATUS_READY, payload)

    asyncio.run(_run())


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

    body = response.json()
    assert body["status"] == "ok"
    assert body["version"]
    assert body["uptime_seconds"] >= 0
    assert body["services"] == {"api": "ok", "database": "ok"}


def test_health_answers_head_probes(client: TestClient) -> None:
    """Monitors often probe with HEAD.

    The StaticFiles mount at "/" claims every request the API routes decline
    on method, so without HEAD declared explicitly this returns 404 — a health
    check that reports the service missing while it is perfectly healthy.
    """
    assert client.head("/health").status_code == 200


def test_health_reports_a_degraded_database_without_failing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe must describe the problem, not become one.

    200 is deliberate: on a single VPS a 503 here would pull out the only
    node, and a degraded API still serves the landing page and the public
    endpoints.
    """

    def boom() -> None:
        raise RuntimeError("disk gone")

    monkeypatch.setattr("app.main.get_engine", boom)
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["services"]["api"] == "ok"
    assert body["services"]["database"].startswith("unavailable:")


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


def test_create_report_returns_202_pending_with_scored_opportunities(
    client: TestClient,
) -> None:
    response = client.post("/v1/reports", json={"category": "home_organization"})
    assert response.status_code == 202

    parsed = ReportResponse.model_validate(response.json())
    assert parsed.report_id.startswith("R-")
    # Pending, not ready: the agent lane settles it asynchronously now.
    assert parsed.report_status.value == "pending"
    assert parsed.status is ResultStatus.CURATED
    assert len(parsed.opportunities) == 20
    assert parsed.summary


def test_create_report_publishes_a_goal_keyed_by_report_id(
    client: TestClient, published: list
) -> None:
    """The report_id must ride on task_id, or the lane can never settle the row.

    The orchestrator adopts task_id as its goal_id and echoes that; it does
    not echo the payload. A report_id that lived only in the payload would
    never come back.
    """
    created = client.post("/v1/reports", json={"query": "cable management"}).json()

    assert len(published) == 1
    topic, event = published[0]
    assert topic == "user.goals"
    assert event.task_id == created["report_id"]
    assert event.payload["report_id"] == created["report_id"]
    assert event.payload["goal"] == "cable management"


def test_failed_publish_marks_the_report_failed_and_returns_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A report nothing will ever generate must not be left pending."""
    import asyncio

    from app.services import kafka_publisher
    from app.services.report_store import STATUS_FAILED, get_report, list_reports

    async def boom(event, topic=kafka_publisher.GOALS_TOPIC):
        raise RuntimeError("broker unreachable")

    monkeypatch.setattr(kafka_publisher, "publish_goal", boom)

    response = client.post("/v1/reports", json={})
    assert response.status_code == 503
    assert "broker unreachable" in response.json()["detail"]

    rows = asyncio.run(list_reports())
    assert len(rows) == 1
    assert rows[0].status == STATUS_FAILED


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


def test_report_is_409_until_the_lane_settles_it(client: TestClient) -> None:
    created = client.post("/v1/reports", json={"query": "under-sink"}).json()
    report_id = created["report_id"]

    pending = client.get(f"/v1/reports/{report_id}")
    assert pending.status_code == 409
    assert "pending" in pending.json()["detail"]

    settle_report(report_id)

    response = client.get(f"/v1/reports/{report_id}")
    assert response.status_code == 200
    fetched = ReportResponse.model_validate(response.json())
    assert fetched.report_id == report_id
    assert fetched.query == "under-sink"
    assert fetched.report_status.value == "ready"
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
    settle_report(created["report_id"])
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
    settle_report(report_id)

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

    from app.services.report_store import STATUS_PENDING, get_report

    created = client.post("/v1/reports", json={"max_products": 2}).json()

    row = asyncio.run(get_report(created["report_id"]))
    assert row is not None
    # Pending until the listener settles it, but the body is already stored so
    # the row is never an empty shell.
    assert row.status == STATUS_PENDING
    assert row.created_at is not None
    assert json.loads(row.payload_json)["report_id"] == created["report_id"]


def test_pending_report_returns_409_not_404(client: TestClient) -> None:
    """A report being generated is a different answer from one that never existed."""
    import asyncio

    from app.services.report_store import create_report, new_report_id

    report_id = new_report_id()
    asyncio.run(create_report(report_id, ACCOUNT_ID))

    response = client.get(f"/v1/reports/{report_id}")
    assert response.status_code == 409
    assert "pending" in response.json()["detail"]

    assert client.get("/v1/reports/R-neverexisted").status_code == 404


# --- authentication -------------------------------------------------------


V1_ENDPOINTS = [
    ("get", "/v1/opportunities", None),
    ("post", "/v1/landed-cost", VALID_LANDED_COST),
    ("post", "/v1/reports", {}),
    ("get", "/v1/reports/R-anything", None),
]


@pytest.fixture
def anon_client(_raw_client: TestClient) -> TestClient:
    """The same client with no Authorization header."""
    _raw_client.headers.pop("Authorization", None)
    return _raw_client


@pytest.mark.parametrize("method,path,body", V1_ENDPOINTS)
def test_every_v1_endpoint_401s_without_a_key(
    anon_client: TestClient, method: str, path: str, body: dict | None
) -> None:
    response = getattr(anon_client, method)(path, **({"json": body} if body is not None else {}))
    assert response.status_code == 401, f"{method.upper()} {path} was reachable anonymously"


def test_401_names_the_expected_header(anon_client: TestClient) -> None:
    response = anon_client.get("/v1/opportunities")
    assert "Authorization" in response.json()["detail"]
    assert response.headers.get("WWW-Authenticate") == "Bearer"


@pytest.mark.parametrize(
    "header",
    [
        "Bearer ia_totallymadeupkeythatdoesnotexist000000",
        "Bearer not-even-the-right-shape",
        "Bearer ",
        "ia_missing_the_bearer_scheme_entirely_0000000",
        "Basic dXNlcjpwYXNz",
    ],
)
def test_bad_authorization_headers_401(anon_client: TestClient, header: str) -> None:
    response = anon_client.get("/v1/opportunities", headers={"Authorization": header})
    assert response.status_code == 401


def test_revoked_key_stops_working(anon_client: TestClient, account: str) -> None:
    import asyncio

    from app.services.auth import hash_api_key, issue_api_key, revoke_api_key

    key, _ = asyncio.run(issue_api_key(account, label="doomed"))
    headers = {"Authorization": f"Bearer {key}"}

    assert anon_client.get("/v1/opportunities", headers=headers).status_code == 200

    asyncio.run(revoke_api_key(hash_api_key(key)))

    assert anon_client.get("/v1/opportunities", headers=headers).status_code == 401


def test_health_stays_public(anon_client: TestClient) -> None:
    """Liveness probes must not need a credential."""
    assert anon_client.get("/health").status_code == 200


def test_openapi_documents_the_bearer_requirement(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    assert "HTTPBearer" in schema.get("components", {}).get("securitySchemes", {})


# --- billing --------------------------------------------------------------


def _balance(account_id: str) -> int:
    import asyncio

    from app.services.billing import get_balance

    return asyncio.run(get_balance(account_id))


def test_creating_a_report_costs_one_credit(client: TestClient, account: str) -> None:
    before = _balance(account)

    assert client.post("/v1/reports", json={}).status_code == 202

    assert _balance(account) == before - 1


def test_each_report_costs_a_credit(client: TestClient, account: str) -> None:
    before = _balance(account)
    for _ in range(3):
        client.post("/v1/reports", json={})
    assert _balance(account) == before - 3


def test_empty_balance_returns_402(client: TestClient, account: str) -> None:
    import asyncio

    from app.services.billing import deduct_credits

    asyncio.run(deduct_credits(account, STARTING_CREDITS))  # spend it all
    assert _balance(account) == 0

    response = client.post("/v1/reports", json={})
    assert response.status_code == 402
    detail = response.json()["detail"]
    assert "Insufficient credits" in detail
    assert "balance is 0" in detail


def test_402_does_not_create_a_report(client: TestClient, account: str) -> None:
    import asyncio

    from app.services.billing import deduct_credits
    from app.services.report_store import count_reports

    asyncio.run(deduct_credits(account, STARTING_CREDITS))
    client.post("/v1/reports", json={})

    assert asyncio.run(count_reports()) == 0, "an unpaid request must not queue work"


def test_402_publishes_no_goal(client: TestClient, account: str, published: list) -> None:
    import asyncio

    from app.services.billing import deduct_credits

    asyncio.run(deduct_credits(account, STARTING_CREDITS))
    client.post("/v1/reports", json={})

    assert published == [], "an unpaid request must not reach the agent lane"


def test_reading_endpoints_are_free(client: TestClient, account: str) -> None:
    """Only report generation is metered in P2.1."""
    before = _balance(account)

    client.get("/v1/opportunities")
    client.post("/v1/landed-cost", json=VALID_LANDED_COST)
    client.get("/v1/reports/R-nothing")

    assert _balance(account) == before


def test_failed_publish_refunds_the_credit(
    client: TestClient, account: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broker outage must not cost the customer a credit."""
    from app.services import kafka_publisher

    async def boom(event, topic=kafka_publisher.GOALS_TOPIC):
        raise RuntimeError("broker unreachable")

    monkeypatch.setattr(kafka_publisher, "publish_goal", boom)
    before = _balance(account)

    assert client.post("/v1/reports", json={}).status_code == 503

    assert _balance(account) == before, "the credit should have been refunded"


def test_accounts_are_isolated(client: TestClient, account: str) -> None:
    """Spending on one account must not touch another."""
    import asyncio

    from app.services.auth import issue_api_key
    from app.services.billing import create_account, get_balance

    asyncio.run(create_account("acct-other", 5))
    other_key, _ = asyncio.run(issue_api_key("acct-other"))

    client.post("/v1/reports", json={})

    assert asyncio.run(get_balance("acct-other")) == 5
