"""Prepaid credits, backed by an append-only ledger. 1 credit = 1 report.

The ledger is the truth
-----------------------
Every balance change writes exactly one `CreditTransaction` and updates
`CreditAccount.balance` **inside the same database transaction**. The balance
column is a cache of `SUM(delta)`; `reconcile()` checks the two agree.

Before this, a balance was a bare integer with no history, so "why is my
balance 3" had no answer — not for a customer, and not for us. That is
tolerable while credits are granted by hand and merely annoying when a refund
looks wrong. It stops being tolerable the moment real money buys them, which
is why this landed before Stripe rather than after.

Append-only means append-only
-----------------------------
Nothing here updates or deletes a ledger row. A correction is a new
compensating row, which is what `TransactionReason.ADJUSTMENT` is for. The
single exception is `clear_ledger()`, an explicitly named test hook.

`_apply()` is the only function that writes a balance. Anything that changes
credits goes through it, so there is one place to audit and one place where
the invariant can break.
"""

from __future__ import annotations

import uuid
from typing import List, Optional, Tuple

from sqlalchemy import func, select, update

from app.database import get_sessionmaker
from app.models import CreditAccount, CreditTransaction, TransactionReason

REPORT_COST_CREDITS = 1


def _new_transaction_id() -> str:
    return f"T-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# The single mutation path
# ---------------------------------------------------------------------------


async def _apply(
    account_id: str,
    delta: int,
    reason: TransactionReason,
    reference: Optional[str] = None,
    *,
    require_funds: bool = False,
) -> Optional[CreditTransaction]:
    """Move a balance and record why, atomically. The only writer of balance.

    Args:
        delta: signed. Negative spends, positive credits.
        require_funds: when True the change only applies if the balance can
            cover it, using a conditional UPDATE so the check and the write are
            one statement. Two concurrent spenders cannot both take the last
            credit.

    Returns:
        The ledger row that was written, or None when nothing was applied —
        unknown account, or insufficient funds under `require_funds`. None is
        the only "did not happen" signal; callers must not re-read the balance
        to decide, or they reintroduce the race this exists to prevent.
    """
    if delta == 0:
        raise ValueError("delta must be non-zero")

    statement = update(CreditAccount).where(CreditAccount.account_id == account_id)
    if require_funds:
        statement = statement.where(CreditAccount.balance >= -delta)
    statement = statement.values(balance=CreditAccount.balance + delta)

    async with get_sessionmaker()() as session:
        async with session.begin():
            result = await session.execute(statement)
            if result.rowcount != 1:
                return None

            # Same transaction as the balance write: the ledger cannot
            # disagree with the cache, even if this process dies here.
            account = await session.get(CreditAccount, account_id)
            entry = CreditTransaction(
                id=_new_transaction_id(),
                account_id=account_id,
                delta=delta,
                reason=reason.value,
                reference=reference,
                balance_after=account.balance,
            )
            session.add(entry)

    return entry


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


async def create_account(
    account_id: str, initial_credits: int = 0, label: str = ""
) -> CreditAccount:
    """Open an account. Raises ValueError if it already exists.

    An opening balance is a ledger entry like any other, so a brand new
    account reconciles immediately.
    """
    if initial_credits < 0:
        raise ValueError(f"initial_credits must be >= 0, got {initial_credits}")

    async with get_sessionmaker()() as session:
        async with session.begin():
            if await session.get(CreditAccount, account_id) is not None:
                raise ValueError(f"account {account_id!r} already exists")
            account = CreditAccount(account_id=account_id, balance=0, label=label)
            session.add(account)

    if initial_credits:
        await _apply(
            account_id,
            initial_credits,
            TransactionReason.ADJUSTMENT,
            reference="account opening",
        )
        account.balance = initial_credits
    return account


async def get_account(account_id: str) -> Optional[CreditAccount]:
    async with get_sessionmaker()() as session:
        return await session.get(CreditAccount, account_id)


async def get_balance(account_id: str) -> int:
    """Cached balance. 0 for an unknown account.

    Unknown and empty are deliberately indistinguishable: both mean "cannot
    pay", and the endpoint answers 402 either way.
    """
    statement = select(CreditAccount.balance).where(
        CreditAccount.account_id == account_id
    )
    async with get_sessionmaker()() as session:
        balance = (await session.execute(statement)).scalar_one_or_none()
    return int(balance) if balance is not None else 0


async def list_accounts(limit: int = 100) -> List[CreditAccount]:
    statement = select(CreditAccount).order_by(CreditAccount.created_at.desc()).limit(limit)
    async with get_sessionmaker()() as session:
        return list((await session.execute(statement)).scalars().all())


# ---------------------------------------------------------------------------
# Balance changes — all thin wrappers over _apply
# ---------------------------------------------------------------------------


async def deduct_credits(account_id: str, amount: int = REPORT_COST_CREDITS) -> bool:
    """Spend credits. False if the balance is insufficient or unknown."""
    if amount <= 0:
        raise ValueError(f"amount must be > 0, got {amount}")
    entry = await _apply(
        account_id, -amount, TransactionReason.CHARGE, require_funds=True
    )
    return entry is not None


async def charge_for_report(account_id: str, report_id: str) -> bool:
    """Charge for a report, referencing it so the history is traceable."""
    entry = await _apply(
        account_id,
        -REPORT_COST_CREDITS,
        TransactionReason.CHARGE,
        reference=report_id,
        require_funds=True,
    )
    return entry is not None


async def add_credits(
    account_id: str, amount: int, reference: Optional[str] = None
) -> int:
    """Top up. Returns the new balance. Raises ValueError if unknown."""
    if amount <= 0:
        raise ValueError(f"amount must be > 0, got {amount}")
    entry = await _apply(account_id, amount, TransactionReason.TOPUP, reference)
    if entry is None:
        raise ValueError(f"no account {account_id!r}")
    return entry.balance_after


async def refund_credits(
    account_id: str,
    amount: int = REPORT_COST_CREDITS,
    reference: Optional[str] = None,
) -> bool:
    """Return credits taken for work that never happened.

    False for an unknown account — not an error here, and it must not turn
    into a 500 on top of whatever failure triggered the refund.
    """
    if amount <= 0:
        raise ValueError(f"amount must be > 0, got {amount}")
    entry = await _apply(account_id, amount, TransactionReason.REFUND, reference)
    return entry is not None


async def record_purchase(
    account_id: str, amount: int, reference: str
) -> Optional[CreditTransaction]:
    """Credits bought with money. Unused until Stripe lands (P2.3)."""
    if amount <= 0:
        raise ValueError(f"amount must be > 0, got {amount}")
    return await _apply(account_id, amount, TransactionReason.PURCHASE, reference)


async def adjust_balance(
    account_id: str, delta: int, reference: str
) -> Optional[CreditTransaction]:
    """Manual correction. The append-only answer to a mistake."""
    return await _apply(account_id, delta, TransactionReason.ADJUSTMENT, reference)


# ---------------------------------------------------------------------------
# History and reconciliation
# ---------------------------------------------------------------------------


async def list_transactions(
    account_id: str, limit: int = 50, offset: int = 0
) -> List[CreditTransaction]:
    """Ledger rows for one account, newest first."""
    statement = (
        select(CreditTransaction)
        .where(CreditTransaction.account_id == account_id)
        .order_by(CreditTransaction.created_at.desc(), CreditTransaction.id.desc())
        .limit(limit)
        .offset(offset)
    )
    async with get_sessionmaker()() as session:
        return list((await session.execute(statement)).scalars().all())


async def count_transactions(account_id: str) -> int:
    statement = select(func.count()).select_from(CreditTransaction).where(
        CreditTransaction.account_id == account_id
    )
    async with get_sessionmaker()() as session:
        return int((await session.execute(statement)).scalar_one())


async def ledger_balance(account_id: str) -> int:
    """SUM(delta) — the authoritative balance, derived from the ledger."""
    statement = select(func.coalesce(func.sum(CreditTransaction.delta), 0)).where(
        CreditTransaction.account_id == account_id
    )
    async with get_sessionmaker()() as session:
        return int((await session.execute(statement)).scalar_one())


async def reconcile(account_id: str) -> bool:
    """True when the cached balance equals the ledger sum.

    False means the cache and the ledger have diverged, which should be
    impossible — they are written in one transaction. If it ever returns
    False, trust the ledger and write an adjustment; do not edit the cache by
    hand, because that loses the evidence of what went wrong.
    """
    cached, derived = await reconcile_detail(account_id)
    return cached == derived


async def reconcile_detail(account_id: str) -> Tuple[int, int]:
    """(cached_balance, ledger_sum), for reporting a mismatch."""
    return await get_balance(account_id), await ledger_balance(account_id)


# ---------------------------------------------------------------------------
# Report refunds
# ---------------------------------------------------------------------------


async def refund_report(report_id: str, account_id: Optional[str], reason: str) -> bool:
    """Refund one credit for a failed report, logging the outcome.

    Shared by report_listener and report_reaper so the log line and the
    sentinel check cannot drift. The caller must already have won the
    pending -> failed transition (report_store.settle_if_pending), or this
    credits an account twice.

    `reason` is recorded on the ledger row, not branched on: **every** failure
    refunds, whatever caused it.
    """
    from app.services.report_store import LEGACY_ACCOUNT_ID

    if not account_id or account_id == LEGACY_ACCOUNT_ID:
        # Pre-billing rows: nobody was charged, so there is nothing to return.
        print(f"[refund] {report_id} not refunded ({reason}): no billable account")
        return False

    if await refund_credits(
        account_id, REPORT_COST_CREDITS, reference=f"{report_id}:{reason}"
    ):
        print(
            f"[refund] {report_id} refunded {REPORT_COST_CREDITS} credit to "
            f"account {account_id} ({reason})"
        )
        return True

    print(f"[refund] {report_id} not refunded ({reason}): no account {account_id!r}")
    return False


# ---------------------------------------------------------------------------
# Test hooks
# ---------------------------------------------------------------------------


async def clear_accounts() -> None:
    """Delete every account. Test hook only — never call from application code."""
    from sqlalchemy import delete

    async with get_sessionmaker()() as session:
        async with session.begin():
            await session.execute(delete(CreditAccount))


async def clear_ledger() -> None:
    """Delete every ledger row. Test hook only.

    This is the sole DELETE against credit_transactions in the codebase, and
    it exists so tests start from an empty table. It is deliberately not
    reachable from any application path; `test_billing_ledger.py` asserts the
    production surface contains no other mutation of this table.
    """
    from sqlalchemy import delete

    async with get_sessionmaker()() as session:
        async with session.begin():
            await session.execute(delete(CreditTransaction))
