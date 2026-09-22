"""GET /v1/account/transactions — the ledger over HTTP."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.auth import issue_api_key
from app.services.billing import (
    add_credits,
    charge_for_report,
    create_account,
    refund_credits,
)

PATH = "/v1/account/transactions"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


def make_account(account_id: str, credits: int = 10) -> dict:
    """Create an account and return headers carrying its key."""
    run(create_account(account_id, credits))
    key, _ = run(issue_api_key(account_id))
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def headers() -> dict:
    return make_account("acct-history")


# --- auth -----------------------------------------------------------------


def test_401_without_a_key(client: TestClient) -> None:
    client.headers.pop("Authorization", None)
    assert client.get(PATH).status_code == 401


def test_401_with_a_bad_key(client: TestClient) -> None:
    response = client.get(PATH, headers={"Authorization": "Bearer ia_nope000000000000000000"})
    assert response.status_code == 401


# --- own account only -----------------------------------------------------


def test_returns_only_the_callers_own_ledger(client: TestClient, headers: dict) -> None:
    other = make_account("acct-someone-else", 99)
    run(charge_for_report("acct-someone-else", "R-theirs"))
    run(charge_for_report("acct-history", "R-mine"))

    body = client.get(PATH, headers=headers).json()

    assert body["account_id"] == "acct-history"
    references = [i["reference"] for i in body["items"]]
    assert "R-mine" in references
    assert "R-theirs" not in references

    their_body = client.get(PATH, headers=other).json()
    assert their_body["account_id"] == "acct-someone-else"
    assert their_body["balance"] == 99 - 1


def test_account_id_cannot_be_overridden_by_a_query_param(
    client: TestClient, headers: dict
) -> None:
    """There is no such parameter; supplying one must not change the scope."""
    make_account("acct-victim", 500)

    body = client.get(PATH, params={"account_id": "acct-victim"}, headers=headers).json()

    assert body["account_id"] == "acct-history"
    assert body["balance"] != 500


# --- content --------------------------------------------------------------


def test_reports_balance_and_total_count(client: TestClient, headers: dict) -> None:
    run(charge_for_report("acct-history", "R-1"))
    run(add_credits("acct-history", 5))

    body = client.get(PATH, headers=headers).json()

    assert body["balance"] == 10 - 1 + 5
    assert body["count"] == 3  # opening + charge + topup


def test_entries_are_newest_first_with_running_balance(
    client: TestClient, headers: dict
) -> None:
    run(charge_for_report("acct-history", "R-1"))
    run(refund_credits("acct-history", 1, reference="R-1:timeout"))

    items = client.get(PATH, headers=headers).json()["items"]

    assert [i["reason"] for i in items] == ["refund", "charge", "adjustment"]
    assert [i["balance_after"] for i in items] == [10, 9, 10]


def test_entry_shape(client: TestClient, headers: dict) -> None:
    run(charge_for_report("acct-history", "R-shape"))
    item = client.get(PATH, headers=headers).json()["items"][0]

    assert set(item) == {"id", "delta", "reason", "reference", "balance_after", "created_at"}
    assert item["delta"] == -1
    assert item["reference"] == "R-shape"


def test_empty_ledger_is_a_valid_empty_response(client: TestClient) -> None:
    empty = make_account("acct-nothing", 0)
    body = client.get(PATH, headers=empty).json()

    assert body["items"] == []
    assert body["count"] == 0
    assert body["balance"] == 0


# --- pagination -----------------------------------------------------------


def test_pagination_slices_without_changing_count(
    client: TestClient, headers: dict
) -> None:
    for i in range(6):
        run(charge_for_report("acct-history", f"R-{i}"))

    first = client.get(PATH, params={"limit": 2}, headers=headers).json()
    second = client.get(PATH, params={"limit": 2, "offset": 2}, headers=headers).json()

    assert len(first["items"]) == 2
    assert first["count"] == 7  # opening + 6 charges
    assert {i["id"] for i in first["items"]}.isdisjoint({i["id"] for i in second["items"]})


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 201}, {"offset": -1}])
def test_rejects_bad_pagination(client: TestClient, headers: dict, params: dict) -> None:
    assert client.get(PATH, params=params, headers=headers).status_code == 422


# --- end to end -----------------------------------------------------------


def test_history_answers_why_is_my_balance_n(client: TestClient, headers: dict) -> None:
    """The acceptance case: the ledger explains the number, line by line."""
    run(charge_for_report("acct-history", "R-a"))
    run(charge_for_report("acct-history", "R-b"))
    run(refund_credits("acct-history", 1, reference="R-b:agent_lane_timeout"))
    run(add_credits("acct-history", 20, reference="manual top-up"))

    body = client.get(PATH, headers=headers).json()

    assert body["balance"] == 10 - 1 - 1 + 1 + 20
    assert sum(i["delta"] for i in body["items"]) == body["balance"]
