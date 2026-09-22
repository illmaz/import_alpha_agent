"""ORM models.

The report body is stored as a JSON string rather than normalised into
columns. The report *is* a `ReportResponse` document — it is written once,
read whole, and never queried by its inner fields — so normalising it would
buy nothing and would have to be migrated every time the response schema
moves. The columns here are only what we filter or sort on.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Who paid for this report. Required so a failure can be refunded — before
    # this column the reaper knew a report had died but not whom to credit.
    account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now
    )

    __table_args__ = (
        Index("ix_reports_status", "status"),
        Index("ix_reports_account_id", "account_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Report id={self.id!r} status={self.status!r}>"


class ApiKey(Base):
    """An issued API key, stored only as a hash.

    The plaintext key is shown once at creation and never persisted, so a
    database leak yields no usable credentials. `key_hash` is the primary key
    because lookup is always by hash — the key itself is never known to the
    server after issuance.
    """

    __tablename__ = "api_keys"

    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now
    )

    __table_args__ = (Index("ix_api_keys_account_id", "account_id"),)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ApiKey account_id={self.account_id!r} active={self.active}>"


class CreditAccount(Base):
    """Prepaid credit balance. 1 credit = 1 report.

    `balance` is a plain integer, never a float: money-like quantities must
    not accumulate binary rounding error. Deduction is done with a conditional
    UPDATE rather than read-modify-write — see app/services/billing.py.
    """

    __tablename__ = "credit_accounts"

    account_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    balance: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    label: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<CreditAccount {self.account_id!r} balance={self.balance}>"


class TransactionReason(str, Enum):
    """Why a balance moved. Stored as the string value."""

    CHARGE = "charge"          # metered work, e.g. a report
    REFUND = "refund"          # work paid for but never delivered
    TOPUP = "topup"            # credits granted out of band (CLI)
    PURCHASE = "purchase"      # credits bought (Stripe — not wired yet)
    ADJUSTMENT = "adjustment"  # manual correction, incl. an opening balance


class CreditTransaction(Base):
    """Append-only ledger. One row per balance change, ever.

    `CreditAccount.balance` is a cache of `SUM(delta)` over these rows. The
    ledger is the truth: it is what answers "why is my balance N", which a
    bare integer cannot. Nothing updates or deletes a row here — a correction
    is a new compensating row, which is why `reason` has an `adjustment`
    value.

    `delta` is signed: negative spends, positive credits. Summing the column
    is therefore the whole reconciliation.
    """

    __tablename__ = "credit_transactions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    delta: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(16), nullable=False)
    # What this entry is about: a report_id, a Stripe event id, or a note.
    reference: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # Balance after this entry, so a history listing needs no running total
    # and a reconciliation mismatch can be traced to the row that diverged.
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now
    )

    __table_args__ = (
        Index("ix_credit_transactions_account_id", "account_id"),
        Index("ix_credit_transactions_created_at", "created_at"),
        # One purchase per payment event, enforced by the database. A
        # check-then-insert is not enough: SQLite takes its write lock at the
        # first write, so concurrent webhook retries both pass the check.
        # Partial because only purchases carry a unique reference — a topup
        # note may legitimately repeat.
        Index(
            "uq_credit_transactions_purchase_reference",
            "reference",
            unique=True,
            sqlite_where=text("reason = 'purchase' AND reference IS NOT NULL"),
            postgresql_where=text("reason = 'purchase' AND reference IS NOT NULL"),
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<CreditTransaction {self.account_id!r} {self.delta:+d} "
            f"{self.reason} ref={self.reference!r}>"
        )
