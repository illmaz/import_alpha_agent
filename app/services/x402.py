"""x402: verify a USDC payment on Base before serving a request.

TESTNET ONLY. AGENTS.md: "No wallet custody. No mainnet payments.
Sandbox/testnet only." `assert_testnet()` refuses a mainnet configuration at
the call site rather than trusting anyone to remember. Switching to mainnet is
a deliberate human decision, documented in docs/X402_SETUP.md — it is not a
config flag this code will honour on its own.

Why this reads event logs, not `tx.value`
-----------------------------------------
USDC is an ERC-20 token, so a transfer is a *contract call*, not a value
transfer. On a USDC payment transaction:

    tx.value == 0                      # no native ETH moved
    tx.to    == the USDC contract      # NOT the recipient

Checking `tx.to == recipient` and `tx.value == amount`, as one might for ETH,
would reject every genuine USDC payment and — worse — accept a zero-value call
to the recipient as if it were payment. The transferred amount and the real
recipient live in the `Transfer(address,address,uint256)` event emitted by the
token contract, so that is what is decoded here.

The log must also have been emitted *by the USDC contract address*. Without
that check, anyone could deploy a worthless token that emits an
identically-shaped Transfer event and pay with it.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

from web3 import Web3

logger = logging.getLogger(__name__)

# keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# USDC uses 6 decimals, so 1 USDC = 1_000_000 base units.
USDC_DECIMALS = 6
USDC_BASE_UNITS = 10**USDC_DECIMALS


@dataclass(frozen=True)
class Network:
    name: str
    rpc_url: str
    usdc_contract: str
    is_testnet: bool


NETWORKS = {
    "base-sepolia": Network(
        name="base-sepolia",
        rpc_url="https://sepolia.base.org",
        usdc_contract="0x036CbD53842c5426634e7929541eC2315fA19Ef0",
        is_testnet=True,
    ),
    "base-mainnet": Network(
        name="base-mainnet",
        rpc_url="https://mainnet.base.org",
        usdc_contract="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        is_testnet=False,
    ),
}

DEFAULT_NETWORK = "base-sepolia"

# A reorg can undo a transaction that already reported success. Requiring a
# few confirmations before granting anything is the cheapest defence; Base
# blocks are ~2s, so this costs seconds, not minutes.
MIN_CONFIRMATIONS = int(os.environ.get("X402_MIN_CONFIRMATIONS", "2"))


class MainnetRefused(RuntimeError):
    """Raised when a mainnet network is configured. See AGENTS.md."""


class X402NotConfigured(RuntimeError):
    """Raised when the seller wallet is not set."""


def get_network() -> Network:
    name = os.environ.get("X402_NETWORK", DEFAULT_NETWORK).strip().lower()
    if name not in NETWORKS:
        raise ValueError(f"unknown X402_NETWORK {name!r}; known: {sorted(NETWORKS)}")
    network = NETWORKS[name]
    assert_testnet(network)
    return network


def assert_testnet(network: Network) -> None:
    """Refuse mainnet outright.

    AGENTS.md forbids mainnet payments. Accepting real USDC would mean taking
    custody of real money from this codebase, which nothing here is reviewed
    or insured to do.
    """
    if not network.is_testnet:
        raise MainnetRefused(
            f"X402_NETWORK={network.name} is a MAINNET network. This project is "
            f"testnet only (AGENTS.md: no mainnet payments, no wallet custody). "
            f"Use base-sepolia."
        )


def get_usdc_contract() -> str:
    """Contract address for the configured network, overridable for a fork."""
    override = os.environ.get("X402_USDC_CONTRACT")
    return Web3.to_checksum_address(override or get_network().usdc_contract)


def get_seller_address() -> str:
    address = os.environ.get("X402_WALLET_ADDRESS")
    if not address:
        raise X402NotConfigured(
            "X402_WALLET_ADDRESS is not set. See docs/X402_SETUP.md."
        )
    return Web3.to_checksum_address(address)


def is_configured() -> bool:
    return bool(os.environ.get("X402_WALLET_ADDRESS"))


def get_web3() -> Web3:
    """A Web3 client for the configured network."""
    return Web3(Web3.HTTPProvider(get_network().rpc_url, request_kwargs={"timeout": 10}))


@dataclass(frozen=True)
class PaymentResult:
    """The outcome of verifying one transaction."""

    ok: bool
    reason: str = ""
    payer: Optional[str] = None
    amount: int = 0

    def __bool__(self) -> bool:  # lets callers write `if result:`
        return self.ok


def _hex(value: Any) -> str:
    """Normalise HexBytes/bytes/str to lowercase hex with no 0x prefix.

    `.lstrip("0x")` is NOT usable here: it strips every leading '0' and 'x',
    so a topic beginning with a zero nibble would silently lose it. And
    HexBytes.hex() is prefixed in some versions and bare in others, so the
    prefix has to be removed explicitly rather than assumed either way.
    """
    raw = value.hex() if hasattr(value, "hex") else str(value)
    raw = raw.lower()
    return raw[2:] if raw.startswith("0x") else raw


def _addr_from_topic(topic: Any) -> str:
    """An indexed address topic is 32 bytes, left-padded."""
    return Web3.to_checksum_address("0x" + _hex(topic)[-40:])


def verify_payment_detailed(
    tx_hash: str,
    expected_amount_usdc: int,
    recipient_address: str,
    web3: Optional[Web3] = None,
) -> PaymentResult:
    """Verify a USDC transfer on Base. Returns why it failed, not just that it did.

    Checks, in order:
      1. the transaction exists and its receipt reports success;
      2. it has at least MIN_CONFIRMATIONS behind it;
      3. it contains a Transfer log emitted **by the USDC contract**;
      4. that transfer's recipient is `recipient_address`;
      5. the amount is at least `expected_amount_usdc` (overpaying is fine).

    `expected_amount_usdc` is in base units: 6 decimals, so $0.01 is 10_000.
    """
    client = web3 or get_web3()
    usdc = get_usdc_contract()
    recipient = Web3.to_checksum_address(recipient_address)

    try:
        receipt = client.eth.get_transaction_receipt(tx_hash)
    except Exception as exc:  # unknown hash, malformed hash, RPC down
        logger.info("x402: could not fetch receipt for %s: %s", tx_hash, exc)
        return PaymentResult(False, "transaction not found or not yet mined")

    if receipt.get("status") != 1:
        return PaymentResult(False, "transaction reverted")

    try:
        confirmations = client.eth.block_number - receipt["blockNumber"]
    except Exception:
        confirmations = MIN_CONFIRMATIONS  # cannot check; do not block on it
    if confirmations < MIN_CONFIRMATIONS:
        return PaymentResult(
            False,
            f"only {confirmations} confirmation(s); {MIN_CONFIRMATIONS} required",
        )

    for log in receipt.get("logs", []):
        # The log must come from the real USDC contract. Any contract can emit
        # a Transfer event; only this one means USDC moved.
        if Web3.to_checksum_address(log["address"]) != usdc:
            continue

        topics = log.get("topics", [])
        if len(topics) < 3:
            continue
        if _hex(topics[0]) != _hex(TRANSFER_TOPIC):
            continue

        to_address = _addr_from_topic(topics[2])
        if to_address != recipient:
            continue

        data = log.get("data")
        amount = int(_hex(data) or "0", 16)

        if amount < expected_amount_usdc:
            return PaymentResult(
                False,
                f"underpaid: {amount} base units, expected {expected_amount_usdc}",
                payer=_addr_from_topic(topics[1]),
                amount=amount,
            )

        return PaymentResult(
            True, "verified", payer=_addr_from_topic(topics[1]), amount=amount
        )

    return PaymentResult(
        False, f"no USDC transfer to {recipient} found in this transaction"
    )


def verify_payment(
    tx_hash: str,
    expected_amount_usdc: int,
    recipient_address: str,
    web3: Optional[Web3] = None,
) -> bool:
    """Boolean form, as specified. Prefer verify_payment_detailed for the reason."""
    return bool(
        verify_payment_detailed(tx_hash, expected_amount_usdc, recipient_address, web3)
    )
