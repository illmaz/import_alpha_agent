"""In-memory report store.

Process-local and volatile by design — P1 Part 2 explicitly defers the
database. Two consequences worth stating plainly, because they are invisible
until they bite:

  * reports vanish on restart, so a report_id is not durable; and
  * with more than one API replica, the replica that serves GET may not be the
    one that served POST, and the lookup 404s.

Both disappear when the SQLite state store lands. Until then run one replica.
"""

from __future__ import annotations

import threading
import uuid
from typing import Dict, Optional

from app.schemas import ReportResponse


class ReportStore:
    def __init__(self) -> None:
        # uvicorn serves handlers from a thread pool, so the dict is shared
        # mutable state across threads.
        self._lock = threading.Lock()
        self._reports: Dict[str, ReportResponse] = {}

    @staticmethod
    def new_report_id() -> str:
        return f"R-{uuid.uuid4().hex[:8]}"

    def save(self, report: ReportResponse) -> ReportResponse:
        with self._lock:
            self._reports[report.report_id] = report
        return report

    def get(self, report_id: str) -> Optional[ReportResponse]:
        with self._lock:
            return self._reports.get(report_id)

    def clear(self) -> None:
        """Test hook: reset between cases so ids cannot leak across tests."""
        with self._lock:
            self._reports.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._reports)


report_store = ReportStore()
