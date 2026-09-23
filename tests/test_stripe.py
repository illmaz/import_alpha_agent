"""Stripe Checkout and webhook handling. No network, no real keys.

The webhook is the security boundary of this whole feature: it is the one
unauthenticated route, and it grants credits. These tests are mostly about
what it *refuses*.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict

import pytest
import stripe
from fastapi.testclient import TestClient

from app.main import app
from app.models import TransactionReason
from app.services import stripe_service
from app.services.auth import issue_api_key
from app.services.billing import (
    count_transactions,
    create_account,
    find_transaction,
    get_balance,
    list_transactions,
    reconcile,
)

CHECKOUT = "/v1/billing/checkout"
WEBHOOK = "/v1/webhooks/stripe"
ACCOUNT = "acct-stripe"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def account() -> str:
    run(create_account(ACCOUNT, 0))
    return ACCOUNT


@pytest.fixture
def headers(account: str) -> dict:
    key, _ = run(issue_api_key(account))
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def stripe_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake_for_tests")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_fake_for_tests")


def completed_event(
    event_id: str = "evt_test_1",
    account_id: str = ACCOUNT,
    pack_id: str = "report_pack",
    amount_total: int | None = None,
    metadata: Dict[str, Any] | None = None,
) -> stripe.Event:
    """A checkout.session.completed event as a REAL stripe.Event.

    Built with `construct_from`, not as a plain dict. This matters: since
    stripe-python v12 a StripeObject does not subclass dict, so `.get()`
    raises on it and only attribute access works. Dict fixtures let the whole
    suite pass while production crashed on the first real webhook — the bug
    this file now exists to catch.
    """
    pack = stripe_service.PRICE_TABLE.get(pack_id, {})
    payload = {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test_123",
                "object": "checkout.session",
                "amount_total": (
                    pack.get("amount_cents") if amount_total is None else amount_total
                ),
                "currency": "usd",
                "metadata": (
                    {"account_id": account_id, "pack_id": pack_id}
                    if metadata is None
                    else metadata
                ),
            }
        },
    }
    return stripe.Event.construct_from(payload, "sk_test_fake_for_tests")


def test_fixtures_are_real_stripe_objects_not_dicts() -> None:
    """Guards the fidelity gap that let the v12 breakage ship green.

    If this file ever goes back to plain dicts, the handler's attribute access
    would be exercised against something that also supports .get(), and the
    production failure mode would stop being reachable from the tests.
    """
    event = completed_event()
    assert isinstance(event, stripe.Event)
    assert not isinstance(event, dict), "a dict fixture cannot catch the v12 break"

    # The exact production crash:
    #   AttributeError: 'get' is a dict method, but a Event is not a dict.
    with pytest.raises(AttributeError, match="not a dict"):
        event.get("id")

    session = event.data.object
    assert not isinstance(session, dict)
    with pytest.raises(AttributeError, match="not a dict"):
        session.metadata.get("account_id")  # nested objects break too


def post_event(client: TestClient, monkeypatch: pytest.MonkeyPatch, event: dict):
    """Deliver an event, with signature verification stubbed to succeed."""
    monkeypatch.setattr(stripe_service, "verify_event", lambda payload, sig: event)
    return client.post(
        WEBHOOK,
        content=json.dumps(event.to_dict()),
        headers={"Stripe-Signature": "t=1,v1=stub"},
    )


# --- the price table ------------------------------------------------------


def test_price_table_matches_the_documented_offer() -> None:
    assert stripe_service.PRICE_TABLE["report_pack"]["amount_cents"] == 9900
    assert stripe_service.PRICE_TABLE["report_pack"]["credits"] == 10
    assert stripe_service.PRICE_TABLE["api_credits"]["amount_cents"] == 29900
    assert stripe_service.PRICE_TABLE["api_credits"]["credits"] == 100


def test_the_bespoke_feed_is_not_a_self_serve_pack() -> None:
    """The $999 custom feed is granted manually; it must not be buyable."""
    amounts = {p["amount_cents"] for p in stripe_service.PRICE_TABLE.values()}
    assert 99900 not in amounts


def test_packs_endpoint_lists_the_catalogue(client: TestClient, headers: dict) -> None:
    body = client.get("/v1/billing/packs", headers=headers).json()
    assert {i["pack_id"] for i in body["items"]} == set(stripe_service.PRICE_TABLE)


# --- live keys are refused ------------------------------------------------


def test_a_live_key_is_refused() -> None:
    with pytest.raises(stripe_service.LiveKeyRefused):
        stripe_service.assert_test_mode("sk_live_abc123")


def test_a_test_key_is_accepted() -> None:
    stripe_service.assert_test_mode("sk_test_abc123")  # must not raise


# --- checkout -------------------------------------------------------------


def test_checkout_requires_a_key(client: TestClient) -> None:
    client.headers.pop("Authorization", None)
    assert client.post(CHECKOUT, json={"pack_id": "report_pack"}).status_code == 401


def test_checkout_rejects_an_unknown_pack(
    client: TestClient, headers: dict, stripe_configured: None
) -> None:
    response = client.post(CHECKOUT, json={"pack_id": "free_money"}, headers=headers)
    assert response.status_code == 400
    assert "free_money" in response.json()["detail"]


def test_checkout_returns_a_url(
    client: TestClient, headers: dict, stripe_configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    class FakeSession:
        url = "https://checkout.stripe.com/c/pay/cs_test_123"
        id = "cs_test_123"

    def fake_create(**kwargs):
        captured.update(kwargs)
        return FakeSession()

    monkeypatch.setattr(stripe.checkout.Session, "create", staticmethod(fake_create))

    response = client.post(CHECKOUT, json={"pack_id": "report_pack"}, headers=headers)
    assert response.status_code == 200

    body = response.json()
    assert body["checkout_url"].startswith("https://checkout.stripe.com/")
    assert body["credits"] == 10
    assert body["amount_cents"] == 9900

    assert captured["mode"] == "payment", "subscriptions are out of scope for P2.3"
    assert captured["metadata"] == {"account_id": ACCOUNT, "pack_id": "report_pack"}
    assert captured["line_items"][0]["price_data"]["unit_amount"] == 9900


def test_checkout_credits_the_callers_own_account_only(
    client: TestClient, headers: dict, stripe_configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """account_id comes from the key, so a body cannot redirect the credits."""
    captured = {}

    class FakeSession:
        url = "https://checkout.stripe.com/c/pay/cs_test_123"
        id = "cs_test_123"

    monkeypatch.setattr(
        stripe.checkout.Session, "create",
        staticmethod(lambda **kw: (captured.update(kw), FakeSession())[1]),
    )

    client.post(
        CHECKOUT,
        json={"pack_id": "report_pack"},
        headers=headers,
    )
    assert captured["metadata"]["account_id"] == ACCOUNT


def test_checkout_503s_when_stripe_is_unconfigured(
    client: TestClient, headers: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    response = client.post(CHECKOUT, json={"pack_id": "report_pack"}, headers=headers)
    assert response.status_code == 503


# --- webhook: what it refuses --------------------------------------------


def test_webhook_needs_no_api_key(
    client: TestClient, account: str, stripe_configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stripe cannot send one; the signature is the credential."""
    client.headers.pop("Authorization", None)
    assert post_event(client, monkeypatch, completed_event()).status_code == 200


def test_bad_signature_is_rejected(
    client: TestClient, stripe_configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(payload, sig):
        raise stripe.error.SignatureVerificationError("bad sig", sig_header=sig)

    monkeypatch.setattr(stripe_service, "verify_event", boom)

    response = client.post(
        WEBHOOK, content=json.dumps(completed_event().to_dict()),
        headers={"Stripe-Signature": "t=1,v1=forged"},
    )
    assert response.status_code == 400


def test_missing_signature_header_is_rejected(
    client: TestClient, stripe_configured: None
) -> None:
    response = client.post(
        WEBHOOK, content=json.dumps(completed_event().to_dict())
    )
    assert response.status_code == 400


def test_bad_signature_credits_nothing(
    client: TestClient, account: str, stripe_configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(payload, sig):
        raise stripe.error.SignatureVerificationError("bad sig", sig_header=sig)

    monkeypatch.setattr(stripe_service, "verify_event", boom)
    client.post(
        WEBHOOK, content=json.dumps(completed_event().to_dict()),
        headers={"Stripe-Signature": "forged"},
    )

    assert run(get_balance(ACCOUNT)) == 0
    assert run(count_transactions(ACCOUNT)) == 0


def test_unknown_pack_is_rejected_and_credits_nothing(
    client: TestClient, account: str, stripe_configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = completed_event(pack_id="free_money", amount_total=1)
    response = post_event(client, monkeypatch, event)

    assert response.status_code == 400
    assert run(get_balance(ACCOUNT)) == 0


def test_amount_mismatch_is_rejected(
    client: TestClient, account: str, stripe_configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session charged 1 cent must not buy a 10-credit pack."""
    event = completed_event(amount_total=1)
    response = post_event(client, monkeypatch, event)

    assert response.status_code == 400
    assert run(get_balance(ACCOUNT)) == 0


def test_missing_metadata_is_rejected(
    client: TestClient, account: str, stripe_configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = completed_event(metadata={})

    assert post_event(client, monkeypatch, event).status_code == 400
    assert run(get_balance(ACCOUNT)) == 0


def test_other_event_types_are_acknowledged_not_acted_on(
    client: TestClient, account: str, stripe_configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 4xx would make Stripe retry an event we will never handle."""
    event = completed_event()
    event.type = "payment_intent.created"

    response = post_event(client, monkeypatch, event)

    assert response.status_code == 200
    assert response.json()["handled"] is False
    assert run(get_balance(ACCOUNT)) == 0


# --- webhook: the happy path and idempotency -----------------------------


def test_completed_session_credits_the_account(
    client: TestClient, account: str, stripe_configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = post_event(client, monkeypatch, completed_event())

    assert response.status_code == 200
    assert run(get_balance(ACCOUNT)) == 10


def test_purchase_is_recorded_on_the_ledger_referencing_the_event(
    client: TestClient, account: str, stripe_configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post_event(client, monkeypatch, completed_event(event_id="evt_abc"))

    entry = run(find_transaction(TransactionReason.PURCHASE, "evt_abc"))
    assert entry is not None
    assert entry.delta == 10
    assert entry.account_id == ACCOUNT


def test_api_credits_pack_grants_100(
    client: TestClient, account: str, stripe_configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post_event(client, monkeypatch, completed_event(pack_id="api_credits"))
    assert run(get_balance(ACCOUNT)) == 100


def test_duplicate_event_credits_exactly_once(
    client: TestClient, account: str, stripe_configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stripe retries until it gets a 2xx; a retry must not pay twice."""
    event = completed_event(event_id="evt_retry")

    first = post_event(client, monkeypatch, event)
    second = post_event(client, monkeypatch, event)
    third = post_event(client, monkeypatch, event)

    assert [first.status_code, second.status_code, third.status_code] == [200, 200, 200]
    assert second.json()["duplicate"] is True

    assert run(get_balance(ACCOUNT)) == 10
    purchases = [
        t for t in run(list_transactions(ACCOUNT))
        if t.reason == TransactionReason.PURCHASE.value
    ]
    assert len(purchases) == 1


def test_distinct_events_each_credit(
    client: TestClient, account: str, stripe_configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post_event(client, monkeypatch, completed_event(event_id="evt_1"))
    post_event(client, monkeypatch, completed_event(event_id="evt_2"))

    assert run(get_balance(ACCOUNT)) == 20


def test_ledger_reconciles_after_a_purchase(
    client: TestClient, account: str, stripe_configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post_event(client, monkeypatch, completed_event())
    post_event(client, monkeypatch, completed_event())  # duplicate
    post_event(client, monkeypatch, completed_event(event_id="evt_other"))

    assert run(reconcile(ACCOUNT)) is True
    assert run(get_balance(ACCOUNT)) == 20


def test_purchased_credits_are_spendable(
    client: TestClient, account: str, stripe_configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The point of the whole feature."""
    from app.services.billing import charge_for_report

    post_event(client, monkeypatch, completed_event())
    assert run(charge_for_report(ACCOUNT, "R-bought")) is True
    assert run(get_balance(ACCOUNT)) == 9
