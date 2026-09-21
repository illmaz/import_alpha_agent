"""Shared event envelope for the Kafka agent bus.

Event flow (docs/CONTEXT.md):
    orchestrator publishes task.created
    router routes to task.assigned.{role}
    worker agents emit artifact.created
    logger/state agent records events
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


def _new_event_id() -> str:
    return str(uuid.uuid4())


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Event(BaseModel):
    event_id: str = Field(default_factory=_new_event_id)
    event_type: str
    task_id: Optional[str] = None
    agent: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=_utc_now_iso)
