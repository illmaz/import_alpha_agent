"""Event schemas shared across the bus, orchestrator, and workers."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


class Topic(str, Enum):
    ROUTER_REQUEST = "router.request"
    ROUTER_RESULT = "router.result"
    PRODUCT_REQUEST = "product.request"
    PRODUCT_RESULT = "product.result"
    ARTIFACT_LOG = "artifact.log"


class Datapoint(BaseModel):
    """A single sourced fact. Never fabricated — always traceable."""

    name: str
    value: Any
    source_url: str
    observed_at: datetime
    confidence: float = Field(ge=0.0, le=1.0)


class Event(BaseModel):
    event_id: str = Field(default_factory=_new_id)
    topic: Topic
    created_at: datetime = Field(default_factory=_now)
    correlation_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class RouterRequest(BaseModel):
    query: str
    task_id: str = Field(default_factory=_new_id)


class RouterResult(BaseModel):
    task_id: str
    target_workers: list[str]
    reason: str


class ProductRequest(BaseModel):
    task_id: str
    product: str
    params: dict[str, Any] = Field(default_factory=dict)


class ProductResult(BaseModel):
    task_id: str
    product: str
    datapoints: list[Datapoint] = Field(default_factory=list)
    error: str | None = None


class ArtifactLogEntry(BaseModel):
    task_id: str
    stage: str
    detail: dict[str, Any] = Field(default_factory=dict)
    logged_at: datetime = Field(default_factory=_now)
