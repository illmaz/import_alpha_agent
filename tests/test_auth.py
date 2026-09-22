"""API key issuance and verification."""

from __future__ import annotations

import asyncio

import pytest

from app.services.auth import (
    KEY_PREFIX,
    generate_api_key,
    hash_api_key,
    issue_api_key,
    list_api_keys,
    looks_like_api_key,
    revoke_api_key,
    verify_api_key,
)


def run(coro):
    return asyncio.run(coro)


# --- key generation -------------------------------------------------------


def test_generated_keys_are_prefixed() -> None:
    assert generate_api_key().startswith(KEY_PREFIX)


def test_generated_keys_are_unique() -> None:
    assert len({generate_api_key() for _ in range(200)}) == 200


def test_generated_keys_carry_real_entropy() -> None:
    """Short keys would be guessable, and the hash choice assumes they are not."""
    key = generate_api_key()
    assert len(key) - len(KEY_PREFIX) >= 40


# --- hashing --------------------------------------------------------------


def test_hash_is_stable() -> None:
    key = generate_api_key()
    assert hash_api_key(key) == hash_api_key(key)


def test_hash_is_sha256_hex() -> None:
    assert len(hash_api_key(generate_api_key())) == 64


def test_different_keys_hash_differently() -> None:
    assert hash_api_key(generate_api_key()) != hash_api_key(generate_api_key())


def test_surrounding_whitespace_is_ignored() -> None:
    key = generate_api_key()
    assert hash_api_key(f"  {key}\n") == hash_api_key(key)


def test_issued_key_is_never_stored_in_plaintext() -> None:
    """The whole point: a database dump must yield nothing usable."""
    key, key_hash = run(issue_api_key("acct-plain"))

    rows = run(list_api_keys("acct-plain"))
    assert len(rows) == 1
    assert rows[0].key_hash == key_hash
    assert key not in rows[0].key_hash
    for value in vars(rows[0]).values():
        assert value != key


# --- shape check ----------------------------------------------------------


@pytest.mark.parametrize(
    "candidate", ["", "   ", "not-a-key", "ia_", "ia_short", "sk-ant-something"]
)
def test_malformed_keys_are_rejected_before_a_lookup(candidate: str) -> None:
    assert looks_like_api_key(candidate) is False


# --- verification ---------------------------------------------------------


def test_valid_key_resolves_to_its_account() -> None:
    key, _ = run(issue_api_key("acct-1"))
    assert run(verify_api_key(key)) == "acct-1"


def test_unknown_key_returns_none() -> None:
    assert run(verify_api_key(generate_api_key())) is None


@pytest.mark.parametrize("candidate", ["", "garbage", "ia_tooshort"])
def test_malformed_key_returns_none(candidate: str) -> None:
    assert run(verify_api_key(candidate)) is None


def test_revoked_key_stops_working() -> None:
    key, key_hash = run(issue_api_key("acct-2"))
    assert run(verify_api_key(key)) == "acct-2"

    assert run(revoke_api_key(key_hash)) is True
    assert run(verify_api_key(key)) is None


def test_revoking_an_unknown_hash_returns_false() -> None:
    assert run(revoke_api_key("0" * 64)) is False


def test_revoking_one_key_leaves_the_others_working() -> None:
    first, first_hash = run(issue_api_key("acct-3"))
    second, _ = run(issue_api_key("acct-3"))

    run(revoke_api_key(first_hash))

    assert run(verify_api_key(first)) is None
    assert run(verify_api_key(second)) == "acct-3"


def test_an_account_can_hold_several_keys() -> None:
    run(issue_api_key("acct-4", label="ci"))
    run(issue_api_key("acct-4", label="laptop"))
    assert len(run(list_api_keys("acct-4"))) == 2


def test_keys_are_scoped_to_their_account() -> None:
    run(issue_api_key("acct-a"))
    run(issue_api_key("acct-b"))
    assert len(run(list_api_keys("acct-a"))) == 1


def test_a_near_miss_key_does_not_authenticate() -> None:
    """One flipped character must not resolve to the account."""
    key, _ = run(issue_api_key("acct-5"))
    tampered = key[:-1] + ("A" if key[-1] != "A" else "B")
    assert run(verify_api_key(tampered)) is None
