"""ORM models.

The report body is stored as a JSON string rather than normalised into
columns. The report *is* a `ReportResponse` document — it is written once,
read whole, and never queried by its inner fields — so normalising it would
buy nothing and would have to be migrated every time the response schema
moves. The columns here are only what we filter or sort on.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now
    )

    __table_args__ = (Index("ix_reports_status", "status"),)

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
