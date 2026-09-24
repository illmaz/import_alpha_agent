"""The payment block that goes into every outbound draft, and the ban list.

Two jobs, both enforced in code rather than in a prompt:

1. `payment_block()` renders the x402 instructions from the *same* function
   `/v1/agent/info` serves. An outreach message that describes a different
   price, wallet or network from the endpoint it is pointing the reader at is
   worse than no message, and a model asked to paraphrase payment terms will
   eventually paraphrase one wrongly. So the model writes the letter and this
   module appends the terms verbatim, exactly once, at the end.

2. `human_only_violations()` rejects the human-shaped asks that the product
   does not have: signup forms, demo bookings, "contact us for a key". CONTEXT.md
   sells to agents. A draft that sends a prospect to a signup form is a draft
   that failed at the thing the discovery layer exists to prevent.

Both are checked again by the publisher at send time, because a file on disk
can be edited after a worker wrote it — the same reason the approval gate
re-hashes instead of trusting its own manifest.
"""

from __future__ import annotations

import re
from typing import List, Optional

from app.services.agent_offer import payment_terms
from app.services.x402 import MIN_CONFIRMATIONS, USDC_DECIMALS, get_usdc_contract

# `payment_terms()` is the single source: the same call backs
# /v1/agent/info, so a draft and a discovery document cannot drift apart.
_payment_block = payment_terms

BLOCK_START = "--- PAYMENT (machine-readable; verified against /v1/agent/info) ---"
BLOCK_END = "--- END PAYMENT ---"

# Phrases that mean "a human must intervene before you can use this". Each is
# a specific solicitation rather than a bare token, because our own honest
# line — "No signup, no browser step" — has to survive the check.
_HUMAN_ONLY_PATTERNS: List[tuple[re.Pattern, str]] = [
    (re.compile(r"\bsign[- ]?up\b(?!\s*(?:,|\.|\s+(?:no|needed))|s?\s+\(?no)", re.I), "signup"),
    (re.compile(r"\bcreate (?:a|an|your) (?:free )?account\b", re.I), "account creation ask"),
    (re.compile(r"\b(?:book|schedule|request|grab) (?:a|a free|some|time|a call|a demo)\b", re.I), "meeting/demo ask"),
    (re.compile(r"\bdemo (?:request|call) form\b", re.I), "demo form"),
    (re.compile(r"\bfill (?:out|in) (?:a|the) (?:form|application)\b", re.I), "form fill"),
    (re.compile(r"\bcontact us (?:for|to (?:get|obtain)) (?:an? )?(?:api )?key\b", re.I), "key by request"),
    (re.compile(r"\be-mail/?ing us (?:for|to get)\b", re.I), "key by request"),
    (re.compile(r"^\s*(?:name|company|job title|phone)\s*[:|]\s*$", re.I | re.M), "form field"),
]


class PaymentNotConfigured(RuntimeError):
    """No wallet/network is set, so there are no truthful terms to attach."""


def payment_block(base_url: Optional[str] = None) -> str:
    """The canonical terms, rendered from live configuration.

    Raises rather than emitting a block with a null wallet. A draft that told
    an agent to send USDC to `None` would be a real loss of their money, and
    the alternative — a vague "payment details on the site" — is exactly the
    friction the discovery layer removes.
    """
    block = _payment_block()
    pay_to = block.get("pay_to")
    network = block.get("network")
    header = block.get("header", "X-Payment-Hash")
    if not pay_to or not network:
        raise PaymentNotConfigured(
            f"cannot render payment terms: {block.get('unavailable', 'wallet or network unset')}"
        )

    amount_usdc = block["amount_usdc"]
    root = (base_url or "").rstrip("/")

    lines = [
        BLOCK_START,
        f"Pay {amount_usdc:.2f} USDC on {network} to {pay_to}",
        f"  asset contract: {get_usdc_contract()}",
        f"  one transfer = one credit; allow {MIN_CONFIRMATIONS} confirmations before use.",
        f'  then call the API with the header  {header}: <transaction hash>',
        "  no account, no key, no form. The hash is the credential.",
        "",
        f"  curl -X POST {root or 'https://<host>'}/v1/reports \\",
        f'       -H "{header}: 0xYOUR_TX_HASH" \\',
        '       -H "Content-Type: application/json" \\',
        '       -d \'{"category":"home_organization","max_products":10}\'',
        "",
        f"Discovery:  {root or 'https://<host>'}/llms.txt  ·  {root or 'https://<host>'}/agent-guide"
        f"  ·  {root or 'https://<host>'}/v1/agent/info",
        f"Full guide: {root or 'https://<host>'}/agent-guide",
        BLOCK_END,
    ]
    return "\n".join(lines)


def append_payment_block(body: str, base_url: Optional[str] = None) -> str:
    """Attach the terms, and only once, however the draft arrived."""
    text = (body or "").rstrip()
    if BLOCK_START in text:
        text = text.split(BLOCK_START, 1)[0].rstrip()
    return f"{text}\n\n{payment_block(base_url)}\n"


def payment_section(body: str) -> str:
    """The appended terms alone, or '' when the draft has none."""
    if BLOCK_START not in (body or ""):
        return ""
    return body.split(BLOCK_START, 1)[1]


def human_only_violations(body: str) -> List[str]:
    """Human-gated asks in the draft's own words.

    The payment block is excluded before scanning: it is code-generated, it is
    checked at render time, and it contains the sentence "no account, no key,
    no form" — which describes the absence of a form and must not read as one.
    """
    prose = (body or "").split(BLOCK_START, 1)[0]
    found = []
    for pattern, label in _HUMAN_ONLY_PATTERNS:
        if pattern.search(prose) and label not in found:
            found.append(label)
    return found


def describe_configuration() -> dict:
    """What a publisher log line should say the terms were built from."""
    block = _payment_block()
    return {
        "network": block.get("network"),
        "pay_to": block.get("pay_to"),
        "amount_usdc": block.get("amount_usdc"),
        "amount_base_units": block.get("amount_base_units"),
        "header": block.get("header"),
        "min_confirmations": block.get("min_confirmations", MIN_CONFIRMATIONS),
        "decimals": USDC_DECIMALS,
    }
