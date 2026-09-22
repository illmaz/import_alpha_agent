"""Prepaid credit ledger. 1 credit = 1 report.

Balances are integers, never floats — a money-like quantity must not
accumulate binary rounding error.

Deduction is a single conditional UPDATE
----------------------------------------
    UPDATE credit_accounts
       SET balance = balance - :amount
     WHERE account_id = :id AND balance >= :amount

and the outcome is read from the affected row count. The obvious alternative —
read the balance, compare it in Python, then write the new value — has a race:
two concurrent requests both read balance 1, both decide they can afford it,
and both write 0. The customer gets two reports for one credit. Pushing the
comparison into the WHERE clause makes the check and the write one atomic
statement, so exactly one of the two wins.

There is no double-entry ledger and no transaction history yet: this is the
simple integer balance the task called for. A real ledger (append-only
entries, balance derived) is the right shape once refunds and Stripe
top-ups exist, because "why is my balance wrong" is unanswerable without one.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import select, update

from app.database import get_sessionmaker
from app.models import CreditAccount

REPORT_COST_CREDITS = 1


async def create_account(
    account_id: str, initial_credits: int = 0, label: str = ""
) -> CreditAccount:
    """Open an account. Raises ValueError if it already exists."""
    if initial_credits < 0:
        raise ValueError(f"initial_credits must be >= 0, got {initial_credits}")

    async with get_sessionmaker()() as session:
        async with session.begin():
            if await session.get(CreditAccount, account_id) is not None:
                raise ValueError(f"account {account_id!r} already exists")
            account = CreditAccount(
                account_id=account_id, balance=initial_credits, label=label
            )
            session.add(account)
    return account


async def get_account(account_id: str) -> Optional[CreditAccount]:
    async with get_sessionmaker()() as session:
        return await session.get(CreditAccount, account_id)


async def get_balance(account_id: str) -> int:
    """Current balance. 0 for an unknown account.

    An unknown account and an empty one are deliberately indistinguishable
    here: both mean "cannot pay", and the endpoint answers 402 either way.
    """
    statement = select(CreditAccount.balance).where(
        CreditAccount.account_id == account_id
    )
    async with get_sessionmaker()() as session:
        balance = (await session.execute(statement)).scalar_one_or_none()
    return int(balance) if balance is not None else 0


async def deduct_credits(account_id: str, amount: int = REPORT_COST_CREDITS) -> bool:
    """Atomically spend `amount` credits. False if the balance is insufficient.

    False is the only signal for "cannot pay" — the caller must not re-read
    the balance to decide, or it reintroduces the race this guards against.
    """
    if amount <= 0:
        raise ValueError(f"amount must be > 0, got {amount}")

    statement = (
        update(CreditAccount)
        .where(CreditAccount.account_id == account_id, CreditAccount.balance >= amount)
        .values(balance=CreditAccount.balance - amount)
    )
    async with get_sessionmaker()() as session:
        async with session.begin():
            result = await session.execute(statement)
    return result.rowcount == 1


async def add_credits(account_id: str, amount: int) -> int:
    """Top up. Returns the new balance. Raises ValueError if unknown."""
    if amount <= 0:
        raise ValueError(f"amount must be > 0, got {amount}")

    async with get_sessionmaker()() as session:
        async with session.begin():
            account = await session.get(CreditAccount, account_id)
            if account is None:
                raise ValueError(f"no account {account_id!r}")
            account.balance += amount
            new_balance = account.balance
    return new_balance


async def refund_credits(account_id: str, amount: int = REPORT_COST_CREDITS) -> bool:
    """Return credits taken for work that never happened.

    A single atomic UPDATE, for the same reason `deduct_credits` is one: the
    previous implementation read the row, incremented in Python and wrote it
    back, so two refunds landing together could both read the same balance and
    one increment would be lost. Refunds now arrive from two independent
    daemons (report_listener and report_reaper), which makes that race real
    rather than theoretical.

    Returns False when there is no such account — an unknown account (or the
    `legacy-unknown` sentinel on pre-billing rows) is not an error here, and
    must not turn into a 500 on top of whatever failure triggered the refund.

    Kept separate from `add_credits` although the SQL is nearly identical: at
    the call site, and in a future ledger, a refund is not a purchase.
    """
    if amount <= 0:
        raise ValueError(f"amount must be > 0, got {amount}")

    statement = (
        update(CreditAccount)
        .where(CreditAccount.account_id == account_id)
        .values(balance=CreditAccount.balance + amount)
    )
    async with get_sessionmaker()() as session:
        async with session.begin():
            result = await session.execute(statement)
    return result.rowcount == 1


async def list_accounts(limit: int = 100) -> list[CreditAccount]:
    statement = select(CreditAccount).order_by(CreditAccount.created_at.desc()).limit(limit)
    async with get_sessionmaker()() as session:
        return list((await session.execute(statement)).scalars().all())


async def clear_accounts() -> None:
    """Delete every account. Test hook."""
    from sqlalchemy import delete

    async with get_sessionmaker()() as session:
        async with session.begin():
            await session.execute(delete(CreditAccount))


async def refund_report(report_id: str, account_id: Optional[str], reason: str) -> bool:
    """Refund one credit for a failed report, logging the outcome.

    Shared by report_listener and report_reaper so the log line and the
    sentinel check cannot drift between them. The caller must have already
    won the pending -> failed transition (see report_store.settle_if_pending),
    or this will credit an account twice.
    """
    from app.services.report_store import LEGACY_ACCOUNT_ID

    if not account_id or account_id == LEGACY_ACCOUNT_ID:
        # Pre-billing rows: nobody was charged, so there is nothing to return.
        print(f"[refund] {report_id} not refunded ({reason}): no billable account")
        return False

    if await refund_credits(account_id, REPORT_COST_CREDITS):
        print(
            f"[refund] {report_id} refunded {REPORT_COST_CREDITS} credit to "
            f"account {account_id} ({reason})"
        )
        return True

    print(f"[refund] {report_id} not refunded ({reason}): no account {account_id!r}")
    return False
