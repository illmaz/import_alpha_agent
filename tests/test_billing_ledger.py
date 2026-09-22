"""The append-only credit ledger.

Covers the invariant that makes the ledger worth having: every balance change
leaves exactly one row, nothing ever edits a row, and the cached balance
always equals SUM(delta).
"""

from __future__ import annotations

import asyncio
import inspect
import random

import pytest

from app.models import TransactionReason
from app.services import billing
from app.services.billing import (
    REPORT_COST_CREDITS,
    add_credits,
    adjust_balance,
    charge_for_report,
    count_transactions,
    create_account,
    deduct_credits,
    get_balance,
    ledger_balance,
    list_transactions,
    reconcile,
    reconcile_detail,
    record_purchase,
    refund_credits,
)

ACCOUNT = "acct-ledger"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def account() -> str:
    run(create_account(ACCOUNT, 10))
    return ACCOUNT


# --- one row per change ---------------------------------------------------


def test_opening_balance_writes_one_entry(account: str) -> None:
    rows = run(list_transactions(account))
    assert len(rows) == 1
    assert rows[0].delta == 10
    assert rows[0].reason == TransactionReason.ADJUSTMENT.value
    assert rows[0].reference == "account opening"


def test_account_opened_empty_has_no_entries() -> None:
    run(create_account("acct-empty", 0))
    assert run(count_transactions("acct-empty")) == 0
    assert run(reconcile("acct-empty")) is True


def test_charge_writes_exactly_one_entry(account: str) -> None:
    before = run(count_transactions(account))
    run(charge_for_report(account, "R-abc12345"))

    assert run(count_transactions(account)) == before + 1
    latest = run(list_transactions(account))[0]
    assert latest.delta == -REPORT_COST_CREDITS
    assert latest.reason == TransactionReason.CHARGE.value
    assert latest.reference == "R-abc12345"


def test_refund_writes_exactly_one_entry(account: str) -> None:
    run(charge_for_report(account, "R-abc12345"))
    before = run(count_transactions(account))

    run(refund_credits(account, 1, reference="R-abc12345:timeout"))

    assert run(count_transactions(account)) == before + 1
    latest = run(list_transactions(account))[0]
    assert latest.delta == +1
    assert latest.reason == TransactionReason.REFUND.value


def test_topup_writes_exactly_one_entry(account: str) -> None:
    before = run(count_transactions(account))
    run(add_credits(account, 25, reference="manual"))

    assert run(count_transactions(account)) == before + 1
    assert run(list_transactions(account))[0].reason == TransactionReason.TOPUP.value


def test_purchase_and_adjustment_are_recorded(account: str) -> None:
    run(record_purchase(account, 100, reference="evt_stripe_123"))
    assert run(list_transactions(account))[0].reason == TransactionReason.PURCHASE.value

    run(adjust_balance(account, -3, reference="goodwill correction"))
    assert run(list_transactions(account))[0].reason == TransactionReason.ADJUSTMENT.value


def test_a_failed_deduction_writes_nothing(account: str) -> None:
    """Insufficient funds must not leave a phantom entry."""
    before = run(count_transactions(account))

    assert run(deduct_credits(account, 999)) is False

    assert run(count_transactions(account)) == before
    assert run(get_balance(account)) == 10


def test_an_unknown_account_writes_nothing() -> None:
    assert run(refund_credits("ghost", 1)) is False
    assert run(count_transactions("ghost")) == 0


# --- balance_after and ordering -------------------------------------------


def test_balance_after_tracks_the_running_balance(account: str) -> None:
    run(charge_for_report(account, "R-1"))
    run(add_credits(account, 5))

    rows = list(reversed(run(list_transactions(account))))  # oldest first
    assert [r.balance_after for r in rows] == [10, 9, 14]


def test_history_is_newest_first(account: str) -> None:
    run(charge_for_report(account, "R-1"))
    run(charge_for_report(account, "R-2"))

    references = [r.reference for r in run(list_transactions(account))]
    assert references[:2] == ["R-2", "R-1"]


def test_history_paginates(account: str) -> None:
    for i in range(5):
        run(charge_for_report(account, f"R-{i}"))

    page = run(list_transactions(account, limit=2, offset=0))
    second = run(list_transactions(account, limit=2, offset=2))

    assert len(page) == 2 and len(second) == 2
    assert {r.id for r in page}.isdisjoint({r.id for r in second})


def test_ledger_is_scoped_per_account(account: str) -> None:
    run(create_account("acct-other", 7))
    run(charge_for_report(account, "R-mine"))

    assert all(r.account_id == "acct-other" for r in run(list_transactions("acct-other")))
    assert run(ledger_balance("acct-other")) == 7


# --- reconciliation -------------------------------------------------------


def test_new_account_reconciles(account: str) -> None:
    assert run(reconcile(account)) is True


def test_reconcile_detail_reports_both_numbers(account: str) -> None:
    assert run(reconcile_detail(account)) == (10, 10)


def test_reconciles_after_a_randomized_mixed_sequence(account: str) -> None:
    """The invariant under arbitrary traffic, not just a scripted path."""
    rng = random.Random(20260922)

    for step in range(120):
        choice = rng.choice(["charge", "refund", "topup", "purchase", "adjust", "deduct"])
        if choice == "charge":
            run(charge_for_report(account, f"R-{step}"))
        elif choice == "refund":
            run(refund_credits(account, rng.randint(1, 3), reference=f"R-{step}"))
        elif choice == "topup":
            run(add_credits(account, rng.randint(1, 50)))
        elif choice == "purchase":
            run(record_purchase(account, rng.randint(1, 20), reference=f"evt_{step}"))
        elif choice == "adjust":
            run(adjust_balance(account, rng.choice([-2, -1, 1, 2]), reference="drift"))
        else:
            run(deduct_credits(account, rng.randint(1, 10)))

        cached, derived = run(reconcile_detail(account))
        assert cached == derived, f"diverged at step {step}: {cached} != {derived}"

    assert run(get_balance(account)) >= 0


def test_balance_never_goes_negative_in_a_random_sequence(account: str) -> None:
    rng = random.Random(7)
    for _ in range(80):
        if rng.random() < 0.7:
            run(deduct_credits(account, rng.randint(1, 5)))
        else:
            run(add_credits(account, rng.randint(1, 4)))
        assert run(get_balance(account)) >= 0


def test_concurrent_charges_cannot_oversell_or_double_log() -> None:
    run(create_account("acct-race", 1))

    async def both() -> list[bool]:
        return list(await asyncio.gather(
            charge_for_report("acct-race", "R-a"),
            charge_for_report("acct-race", "R-b"),
        ))

    results = run(both())

    assert sorted(results) == [False, True]
    assert run(get_balance("acct-race")) == 0
    # The loser must not have logged anything.
    assert run(count_transactions("acct-race")) == 2  # opening + one charge
    assert run(reconcile("acct-race")) is True


# --- append-only ----------------------------------------------------------


def test_no_production_path_updates_or_deletes_a_ledger_row() -> None:
    """The append-only invariant, asserted against the module's own source.

    `clear_ledger` is the one permitted DELETE and exists only for test
    isolation. Anything else touching credit_transactions with update() or
    delete() is a bug: a correction is a new compensating row.
    """
    source = inspect.getsource(billing)
    body = source.split("def clear_ledger")[0]

    assert "delete(CreditTransaction)" not in body
    assert "update(CreditTransaction)" not in body


def test_entries_are_immutable_in_practice(account: str) -> None:
    """Re-reading an entry after later activity returns identical values."""
    run(charge_for_report(account, "R-frozen"))
    original = run(list_transactions(account))[0]
    snapshot = (original.id, original.delta, original.reason, original.balance_after)

    run(add_credits(account, 5))
    run(charge_for_report(account, "R-later"))

    again = [r for r in run(list_transactions(account)) if r.id == snapshot[0]][0]
    assert (again.id, again.delta, again.reason, again.balance_after) == snapshot


def test_a_correction_is_a_new_row_not_an_edit(account: str) -> None:
    run(charge_for_report(account, "R-wrong"))
    before = run(count_transactions(account))

    run(adjust_balance(account, 1, reference="reversing R-wrong, charged in error"))

    assert run(count_transactions(account)) == before + 1
    assert run(reconcile(account)) is True


# --- input contract -------------------------------------------------------


@pytest.mark.parametrize("amount", [0, -1])
def test_non_positive_amounts_rejected(account: str, amount: int) -> None:
    for fn in (deduct_credits, refund_credits):
        with pytest.raises(ValueError):
            run(fn(account, amount))
    with pytest.raises(ValueError):
        run(add_credits(account, amount))


def test_zero_delta_rejected(account: str) -> None:
    with pytest.raises(ValueError):
        run(adjust_balance(account, 0, reference="nothing"))


# --- purchase idempotency (the Stripe retry case) ------------------------


def test_purchase_requires_a_reference(account: str) -> None:
    from app.services.billing import record_purchase

    with pytest.raises(ValueError, match="reference"):
        run(record_purchase(account, 10, ""))


def test_same_reference_records_once(account: str) -> None:
    from app.services.billing import record_purchase

    assert run(record_purchase(account, 10, "evt_x")) is not None
    assert run(record_purchase(account, 10, "evt_x")) is None
    assert run(get_balance(account)) == 20  # 10 opening + 10 purchase


def test_concurrent_deliveries_of_one_event_credit_once(account: str) -> None:
    """A DB constraint, not a check.

    SQLite takes its write lock at the first write, so a check-then-insert in
    a transaction is not enough — verified: six concurrent deliveries all
    passed the check and credited. The partial unique index makes it
    impossible rather than unlikely.
    """
    from app.services.billing import record_purchase

    async def deliver_six() -> list:
        return list(await asyncio.gather(
            *[record_purchase(account, 10, "evt_race") for _ in range(6)]
        ))

    results = run(deliver_six())

    assert sum(r is not None for r in results) == 1
    assert run(get_balance(account)) == 20
    assert run(reconcile(account)) is True


def test_a_rejected_duplicate_leaves_the_balance_untouched(account: str) -> None:
    from app.services.billing import record_purchase

    run(record_purchase(account, 10, "evt_y"))
    before = run(get_balance(account))

    run(record_purchase(account, 999, "evt_y"))

    assert run(get_balance(account)) == before


def test_topup_references_may_repeat(account: str) -> None:
    """Only purchases carry a unique reference; a note is free text."""
    run(add_credits(account, 5, reference="manual"))
    run(add_credits(account, 5, reference="manual"))
    assert run(get_balance(account)) == 20
