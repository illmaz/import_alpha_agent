"""x402 USDC payments. No network, no chain, no real money.

Every web3 call is mocked. These are mostly about what the verifier *refuses*,
because the verifier is what stands between a public transaction hash and a
free report.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient
from web3 import Web3

from app.main import app
from app.models import TransactionReason
from app.services import x402, x402_middleware
from app.services.auth import issue_api_key
from app.services.billing import (
    charge_for_report,
    create_account,
    find_transaction,
    get_balance,
    reconcile,
)

SELLER = "0x1111111111111111111111111111111111111111"
PAYER = "0x2222222222222222222222222222222222222222"
OTHER = "0x3333333333333333333333333333333333333333"
USDC_SEPOLIA = "0x036CbD53842c5426634e7929541eC2315fA19Ef0"
TX = "0x" + "ab" * 32
PRICE = x402_middleware.CREDIT_PRICE_BASE_UNITS


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def x402_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X402_NETWORK", "base-sepolia")
    monkeypatch.setenv("X402_WALLET_ADDRESS", SELLER)
    monkeypatch.delenv("X402_USDC_CONTRACT", raising=False)


@pytest.fixture(scope="module")
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


# --- fake chain -----------------------------------------------------------


def _topic(address: str) -> bytes:
    """An indexed address topic: 32 bytes, left-padded."""
    return bytes.fromhex("00" * 12 + address[2:])


def transfer_log(
    to_address: str = SELLER,
    from_address: str = PAYER,
    amount: int = PRICE,
    contract: str = USDC_SEPOLIA,
) -> Dict[str, Any]:
    return {
        "address": contract,
        "topics": [
            bytes.fromhex(x402.TRANSFER_TOPIC[2:]),
            _topic(from_address),
            _topic(to_address),
        ],
        "data": amount.to_bytes(32, "big"),
    }


class FakeEth:
    def __init__(self, receipt: Any, block_number: int = 1000) -> None:
        self._receipt = receipt
        self.block_number = block_number

    def get_transaction_receipt(self, tx_hash: str) -> Any:
        if isinstance(self._receipt, Exception):
            raise self._receipt
        return self._receipt


class FakeWeb3:
    def __init__(self, receipt: Any, block_number: int = 1000) -> None:
        self.eth = FakeEth(receipt, block_number)


def receipt(
    status: int = 1,
    logs: List[Dict[str, Any]] | None = None,
    block_number: int = 900,
) -> Dict[str, Any]:
    return {
        "status": status,
        "blockNumber": block_number,
        "logs": [transfer_log()] if logs is None else logs,
    }


def verify(rcpt: Any, amount: int = PRICE, recipient: str = SELLER):
    return x402.verify_payment_detailed(TX, amount, recipient, web3=FakeWeb3(rcpt))


# --- network configuration ------------------------------------------------


def test_default_network_is_testnet() -> None:
    assert x402.get_network().name == "base-sepolia"
    assert x402.get_network().is_testnet is True


def test_mainnet_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """AGENTS.md: no mainnet payments, no wallet custody."""
    monkeypatch.setenv("X402_NETWORK", "base-mainnet")
    with pytest.raises(x402.MainnetRefused):
        x402.get_network()


def test_unknown_network_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X402_NETWORK", "ethereum")
    with pytest.raises(ValueError, match="unknown X402_NETWORK"):
        x402.get_network()


def test_sepolia_usdc_contract_is_the_documented_one() -> None:
    assert x402.get_usdc_contract() == Web3.to_checksum_address(USDC_SEPOLIA)


def test_missing_wallet_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("X402_WALLET_ADDRESS", raising=False)
    with pytest.raises(x402.X402NotConfigured):
        x402.get_seller_address()


# --- verification: the happy path ----------------------------------------


def test_valid_transfer_verifies() -> None:
    result = verify(receipt())
    assert result.ok is True
    assert result.payer == Web3.to_checksum_address(PAYER)
    assert result.amount == PRICE


def test_overpayment_is_accepted() -> None:
    """Paying more than asked is not a reason to refuse service."""
    assert verify(receipt(logs=[transfer_log(amount=PRICE * 5)])).ok is True


def test_boolean_wrapper_matches() -> None:
    assert x402.verify_payment(TX, PRICE, SELLER, web3=FakeWeb3(receipt())) is True


# --- verification: what it refuses ---------------------------------------


def test_unknown_transaction_is_refused() -> None:
    result = verify(Exception("not found"))
    assert result.ok is False
    assert "not found" in result.reason


def test_reverted_transaction_is_refused() -> None:
    result = verify(receipt(status=0))
    assert result.ok is False
    assert "reverted" in result.reason


def test_unconfirmed_transaction_is_refused() -> None:
    """A reorg can undo a transaction that already reported success."""
    result = x402.verify_payment_detailed(
        TX, PRICE, SELLER, web3=FakeWeb3(receipt(block_number=1000), block_number=1000)
    )
    assert result.ok is False
    assert "confirmation" in result.reason


def test_underpayment_is_refused() -> None:
    result = verify(receipt(logs=[transfer_log(amount=PRICE - 1)]))
    assert result.ok is False
    assert "underpaid" in result.reason


def test_transfer_to_someone_else_is_refused() -> None:
    result = verify(receipt(logs=[transfer_log(to_address=OTHER)]))
    assert result.ok is False
    assert "no USDC transfer" in result.reason


def test_transfer_from_a_different_token_is_refused() -> None:
    """Anyone can deploy a token that emits an identical Transfer event.

    Only a log emitted by the real USDC contract means USDC actually moved.
    """
    fake_token = "0x9999999999999999999999999999999999999999"
    result = verify(receipt(logs=[transfer_log(contract=fake_token)]))
    assert result.ok is False
    assert "no USDC transfer" in result.reason


def test_transaction_with_no_logs_is_refused() -> None:
    """A plain ETH send to the seller is not a USDC payment."""
    assert verify(receipt(logs=[])).ok is False


def test_non_transfer_events_are_ignored() -> None:
    approval = transfer_log()
    approval["topics"] = [bytes.fromhex("11" * 32)] + approval["topics"][1:]
    assert verify(receipt(logs=[approval])).ok is False


def test_the_right_transfer_is_found_among_several() -> None:
    logs = [
        transfer_log(to_address=OTHER),
        transfer_log(contract="0x9999999999999999999999999999999999999999"),
        transfer_log(),  # the real one
    ]
    assert verify(receipt(logs=logs)).ok is True


# --- middleware: settlement ----------------------------------------------


@pytest.fixture
def chain_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(x402, "get_web3", lambda: FakeWeb3(receipt()))


@pytest.fixture
def chain_bad(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(x402, "get_web3", lambda: FakeWeb3(receipt(status=0)))


def test_settlement_credits_the_payers_own_account(chain_ok: None) -> None:
    account_id, reason = run(x402_middleware.settle_payment(TX))

    assert reason == "verified"
    assert account_id == x402_middleware.account_id_for(PAYER)
    assert run(get_balance(account_id)) == 1


def test_settlement_writes_a_purchase_row_referencing_the_tx(chain_ok: None) -> None:
    run(x402_middleware.settle_payment(TX))

    entry = run(find_transaction(TransactionReason.PURCHASE, TX))
    assert entry is not None
    assert entry.delta == 1


def test_payers_are_isolated_from_each_other(chain_ok: None, monkeypatch) -> None:
    """A shared account would let one agent spend another's credits."""
    run(x402_middleware.settle_payment(TX))
    mine = x402_middleware.account_id_for(PAYER)

    monkeypatch.setattr(
        x402, "get_web3",
        lambda: FakeWeb3(receipt(logs=[transfer_log(from_address=OTHER)])),
    )
    other_tx = "0x" + "cd" * 32
    theirs, _ = run(x402_middleware.settle_payment(other_tx))

    assert theirs != mine
    assert run(get_balance(mine)) == 1
    assert run(get_balance(theirs)) == 1


def test_a_reused_hash_is_refused(chain_ok: None) -> None:
    """A transaction hash is public; it proves payment, not identity."""
    first, _ = run(x402_middleware.settle_payment(TX))
    assert first is not None

    second, reason = run(x402_middleware.settle_payment(TX))

    assert second is None
    assert "already been redeemed" in reason
    assert run(get_balance(first)) == 1, "a replay must not credit again"


def test_settlement_reports_a_bad_payment(chain_bad: None) -> None:
    account_id, reason = run(x402_middleware.settle_payment(TX))
    assert account_id is None
    assert "reverted" in reason


def test_settlement_without_a_wallet_configured(
    chain_ok: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("X402_WALLET_ADDRESS", raising=False)
    account_id, reason = run(x402_middleware.settle_payment(TX))
    assert account_id is None
    assert "X402_WALLET_ADDRESS" in reason


# --- end to end through the API ------------------------------------------


def test_no_key_and_no_payment_is_401(client: TestClient) -> None:
    client.headers.pop("Authorization", None)
    assert client.get("/v1/opportunities").status_code == 401


def test_valid_payment_grants_access(client: TestClient, chain_ok: None) -> None:
    client.headers.pop("Authorization", None)

    response = client.get("/v1/opportunities", headers={"X-Payment-Hash": TX})

    assert response.status_code == 200
    assert len(response.json()["items"]) == 20


def test_valid_payment_writes_a_ledger_row(client: TestClient, chain_ok: None) -> None:
    client.headers.pop("Authorization", None)
    client.get("/v1/opportunities", headers={"X-Payment-Hash": TX})

    account_id = x402_middleware.account_id_for(PAYER)
    assert run(get_balance(account_id)) == 1
    assert run(reconcile(account_id)) is True


def test_invalid_payment_returns_402(client: TestClient, chain_bad: None) -> None:
    client.headers.pop("Authorization", None)

    response = client.get("/v1/opportunities", headers={"X-Payment-Hash": TX})

    assert response.status_code == 402
    assert response.json()["error"] == "payment_required"


def test_wrong_amount_returns_402(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.headers.pop("Authorization", None)
    monkeypatch.setattr(
        x402, "get_web3",
        lambda: FakeWeb3(receipt(logs=[transfer_log(amount=PRICE - 1)])),
    )

    response = client.get("/v1/opportunities", headers={"X-Payment-Hash": TX})

    assert response.status_code == 402
    assert "underpaid" in response.json()["detail"]


def test_402_tells_a_machine_how_to_pay(client: TestClient, chain_bad: None) -> None:
    """The point of 402: the response is actionable without a human."""
    client.headers.pop("Authorization", None)

    body = client.get("/v1/opportunities", headers={"X-Payment-Hash": TX}).json()
    accepts = body["accepts"][0]

    assert accepts["amount_base_units"] == PRICE
    assert accepts["asset"] == "USDC"
    assert accepts["network"] == "base-sepolia"
    assert accepts["pay_to"] == Web3.to_checksum_address(SELLER)
    assert accepts["header"] == "X-Payment-Hash"


def test_an_api_key_takes_precedence_over_payment(
    client: TestClient, chain_ok: None
) -> None:
    """A customer with a key must not also be charged on-chain."""
    run(create_account("acct-keyholder", 5))
    key, _ = run(issue_api_key("acct-keyholder"))

    response = client.get(
        "/v1/opportunities",
        headers={"Authorization": f"Bearer {key}", "X-Payment-Hash": TX},
    )

    assert response.status_code == 200
    assert run(find_transaction(TransactionReason.PURCHASE, TX)) is None, (
        "the payment must not have been consumed"
    )


def test_a_paid_request_can_buy_a_report(
    client: TestClient, chain_ok: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The credit bought on-chain is spendable like any other."""
    from app.services import kafka_publisher

    async def fake_publish(event, topic=kafka_publisher.GOALS_TOPIC):
        return None

    monkeypatch.setattr(kafka_publisher, "publish_goal", fake_publish)
    client.headers.pop("Authorization", None)

    response = client.post(
        "/v1/reports", json={"max_products": 2}, headers={"X-Payment-Hash": TX}
    )

    assert response.status_code == 202
    account_id = x402_middleware.account_id_for(PAYER)
    assert run(get_balance(account_id)) == 0, "the credit was spent on the report"
    assert run(reconcile(account_id)) is True


def test_health_is_unaffected_by_the_middleware(client: TestClient) -> None:
    client.headers.pop("Authorization", None)
    assert client.get("/health").status_code == 200


# --- hex normalisation ----------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("0xDDF252AD", "ddf252ad"),
        ("ddf252ad", "ddf252ad"),
        (b"\xdd\xf2\x52\xad", "ddf252ad"),
        ("0x0df252ab", "0df252ab"),  # lstrip("0x") would eat the leading zero
        (b"\x0d\xf2", "0df2"),
    ],
)
def test_hex_normalisation(value, expected: str) -> None:
    assert x402._hex(value) == expected


def test_a_topic_with_a_leading_zero_is_not_mangled() -> None:
    """`.lstrip("0x")` strips every leading 0 and x, not just the prefix."""
    assert x402._hex("0x0000ff") == "0000ff"
    assert "0x0000ff".lstrip("0x") == "ff", "the bug this guards against"


def test_prefixed_and_bare_topics_both_match() -> None:
    """HexBytes.hex() is prefixed in some web3 versions and bare in others."""
    log = transfer_log()
    log["topics"] = ["0x" + x402.TRANSFER_TOPIC[2:]] + list(log["topics"][1:])
    assert verify(receipt(logs=[log])).ok is True
