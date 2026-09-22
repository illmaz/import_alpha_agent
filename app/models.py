"""ORM models.

The report body is stored as a JSON string rather than normalised into
columns. The report *is* a `ReportResponse` document — it is written once,
read whole, and never queried by its inner fields — so normalising it would
buy nothing and would have to be migrated every time the response schema
moves. The columns here are only what we filter or sort on.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, String, Text
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
