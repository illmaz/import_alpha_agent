"""Shared event envelope for the Kafka agent bus.

Event flow (docs/CONTEXT.md):
    orchestrator publishes task.created
    router routes to task.assigned.{role}
    worker agents emit artifact.created
    logger/state agent records events
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, FrozenSet, List, Optional

from pydantic import BaseModel, Field, field_validator


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


class WorkerRole(str, Enum):
    PRODUCT = "product"
    ENGINEERING = "engineering"
    LANDING = "landing"


WORKER_ROLES = tuple(role.value for role in WorkerRole)


def _parse_active_roles() -> FrozenSet[str]:
    """Roles that actually have a worker process listening.

    Override with ACTIVE_ROLES="product,landing" to run a subset, e.g. while a
    worker is down. Planning against a role nobody consumes hangs the goal
    forever, so this is the registry that keeps plans executable.
    """
    raw = os.environ.get("ACTIVE_ROLES", "").strip()
    if not raw:
        return frozenset(WORKER_ROLES)

    roles = frozenset(part.strip() for part in raw.split(",") if part.strip())
    unknown = roles - set(WORKER_ROLES)
    if unknown:
        raise ValueError(f"ACTIVE_ROLES names unknown roles: {', '.join(sorted(unknown))}")
    if not roles:
        raise ValueError("ACTIVE_ROLES is set but empty")
    return roles


ACTIVE_ROLES = _parse_active_roles()


class PlanStep(BaseModel):
    role: WorkerRole
    task: str = Field(min_length=1)


class GoalPlan(BaseModel):
    goal_id: str
    reasoning: str
    steps: List[PlanStep] = Field(min_length=1)

    @field_validator("steps")
    @classmethod
    def _reject_inactive_roles(cls, steps: List[PlanStep]) -> List[PlanStep]:
        inactive = sorted({step.role.value for step in steps} - set(ACTIVE_ROLES))
        if inactive:
            raise ValueError(
                f"steps target roles with no active worker: {', '.join(inactive)}"
            )
        return steps
