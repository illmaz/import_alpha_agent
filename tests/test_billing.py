"""Prepaid credit ledger."""

from __future__ import annotations

import asyncio

import pytest

from app.services.billing import (
    REPORT_COST_CREDITS,
    add_credits,
    create_account,
    deduct_credits,
    get_account,
    get_balance,
    list_accounts,
    refund_credits,
)


def run(coro):
    return asyncio.run(coro)


# --- accounts -------------------------------------------------------------


def test_create_account_starts_at_the_given_balance() -> None:
    run(create_account("acct-1", 100))
    assert run(get_balance("acct-1")) == 100


def test_create_account_defaults_to_zero() -> None:
    run(create_account("acct-zero"))
    assert run(get_balance("acct-zero")) == 0


def test_creating_the_same_account_twice_raises() -> None:
    run(create_account("acct-dup", 10))
    with pytest.raises(ValueError, match="already exists"):
        run(create_account("acct-dup", 10))


def test_negative_initial_credits_rejected() -> None:
    with pytest.raises(ValueError):
        run(create_account("acct-neg", -5))


def test_unknown_account_has_no_balance_and_no_row() -> None:
    assert run(get_balance("nope")) == 0
    assert run(get_account("nope")) is None


def test_list_accounts() -> None:
    run(create_account("acct-a", 1))
    run(create_account("acct-b", 2))
    assert {a.account_id for a in run(list_accounts())} == {"acct-a", "acct-b"}


# --- deduction ------------------------------------------------------------


def test_deduct_reduces_the_balance() -> None:
    run(create_account("acct-2", 5))
    assert run(deduct_credits("acct-2", 2)) is True
    assert run(get_balance("acct-2")) == 3


def test_deduct_defaults_to_one_credit() -> None:
    run(create_account("acct-3", 5))
    run(deduct_credits("acct-3"))
    assert run(get_balance("acct-3")) == 5 - REPORT_COST_CREDITS


def test_deduct_to_exactly_zero_is_allowed() -> None:
    run(create_account("acct-4", 1))
    assert run(deduct_credits("acct-4", 1)) is True
    assert run(get_balance("acct-4")) == 0


def test_deduct_beyond_the_balance_fails_and_changes_nothing() -> None:
    run(create_account("acct-5", 1))

    assert run(deduct_credits("acct-5", 2)) is False
    assert run(get_balance("acct-5")) == 1, "a failed deduction must not partially apply"


def test_deduct_from_an_empty_account_fails() -> None:
    run(create_account("acct-6", 0))
    assert run(deduct_credits("acct-6", 1)) is False


def test_deduct_from_an_unknown_account_fails() -> None:
    assert run(deduct_credits("ghost", 1)) is False


@pytest.mark.parametrize("amount", [0, -1, -100])
def test_non_positive_deduction_rejected(amount: int) -> None:
    run(create_account("acct-7", 10))
    with pytest.raises(ValueError):
        run(deduct_credits("acct-7", amount))


def test_balance_never_goes_negative_under_repeated_deduction() -> None:
    run(create_account("acct-8", 3))
    results = [run(deduct_credits("acct-8", 1)) for _ in range(5)]

    assert results == [True, True, True, False, False]
    assert run(get_balance("acct-8")) == 0


def test_concurrent_deductions_cannot_oversell() -> None:
    """The race the conditional UPDATE exists to prevent.

    Read-modify-write would let two callers both see balance 1, both decide
    they can afford it, and both write 0 — two reports for one credit.
    """
    run(create_account("acct-race", 1))

    async def both() -> list[bool]:
        return list(await asyncio.gather(
            deduct_credits("acct-race", 1),
            deduct_credits("acct-race", 1),
        ))

    results = run(both())

    assert sorted(results) == [False, True], "exactly one deduction may succeed"
    assert run(get_balance("acct-race")) == 0


# --- top ups and refunds --------------------------------------------------


def test_add_credits_returns_the_new_balance() -> None:
    run(create_account("acct-9", 5))
    assert run(add_credits("acct-9", 10)) == 15


def test_add_credits_to_an_unknown_account_raises() -> None:
    with pytest.raises(ValueError, match="no account"):
        run(add_credits("ghost", 10))


@pytest.mark.parametrize("amount", [0, -5])
def test_non_positive_top_up_rejected(amount: int) -> None:
    run(create_account("acct-10", 5))
    with pytest.raises(ValueError):
        run(add_credits("acct-10", amount))


def test_refund_restores_a_spent_credit() -> None:
    run(create_account("acct-11", 1))
    run(deduct_credits("acct-11", 1))

    run(refund_credits("acct-11", 1))

    assert run(get_balance("acct-11")) == 1


def test_refunding_a_vanished_account_does_not_raise() -> None:
    """A refund failure must not mask the error that triggered it."""
    run(refund_credits("ghost", 1))  # must not raise
