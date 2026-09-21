"""Tests for Event schema validation."""

import pytest
from datetime import datetime, timezone

from events import (
    Event,
    Topic,
    RouterRequest,
    RouterResult,
    ProductRequest,
    ProductResult,
    Datapoint,
    ArtifactLogEntry,
)


class TestRouterRequest:
    def test_create_with_defaults(self):
        req = RouterRequest(query="test query")
        assert req.query == "test query"
        assert req.task_id is not None
        assert len(req.task_id) > 0

    def test_create_with_task_id(self):
        req = RouterRequest(query="test", task_id="custom-id")
        assert req.task_id == "custom-id"


class TestRouterResult:
    def test_create(self):
        result = RouterResult(
            task_id="task-1",
            target_workers=["product_a", "product_b"],
            reason="matched keywords"
        )
        assert result.task_id == "task-1"
        assert len(result.target_workers) == 2


class TestProductRequest:
    def test_create(self):
        req = ProductRequest(task_id="task-1", product="widget")
        assert req.task_id == "task-1"
        assert req.product == "widget"
        assert req.params == {}

    def test_create_with_params(self):
        req = ProductRequest(task_id="task-1", product="widget", params={"key": "value"})
        assert req.params == {"key": "value"}


class TestProductResult:
    def test_create_empty(self):
        result = ProductResult(task_id="task-1", product="widget")
        assert result.datapoints == []
        assert result.error is None

    def test_create_with_datapoints(self):
        dp = Datapoint(
            name="price",
            value=9.99,
            source_url="https://example.com",
            observed_at=datetime.now(timezone.utc),
            confidence=0.95
        )
        result = ProductResult(task_id="task-1", product="widget", datapoints=[dp])
        assert len(result.datapoints) == 1

    def test_create_with_error(self):
        result = ProductResult(task_id="task-1", product="widget", error="timeout")
        assert result.error == "timeout"
        assert result.datapoints == []


class TestDatapoint:
    def test_valid_datapoint(self):
        dp = Datapoint(
            name="weight",
            value="100g",
            source_url="https://supplier.com/product",
            observed_at=datetime.now(timezone.utc),
            confidence=0.8
        )
        assert dp.name == "weight"
        assert dp.confidence == 0.8

    def test_confidence_bounds(self):
        with pytest.raises(Exception):
            Datapoint(
                name="test",
                value=1,
                source_url="https://example.com",
                observed_at=datetime.now(timezone.utc),
                confidence=1.5
            )


class TestEvent:
    def test_create_event(self):
        event = Event(
            topic=Topic.ROUTER_REQUEST,
            correlation_id="corr-1",
            payload={"query": "test"}
        )
        assert event.topic == Topic.ROUTER_REQUEST
        assert event.correlation_id == "corr-1"

    def test_event_serialization(self):
        event = Event(
            topic=Topic.PRODUCT_RESULT,
            correlation_id="task-1",
            payload={"result": "ok"}
        )
        json_str = event.model_dump_json()
        assert "PRODUCT_RESULT" in json_str or "product.result" in json_str


class TestArtifactLogEntry:
    def test_create(self):
        entry = ArtifactLogEntry(
            task_id="task-1",
            stage="router.routed",
            detail={"workers": ["a", "b"]}
        )
        assert entry.task_id == "task-1"
        assert entry.stage == "router.routed"
        assert entry.detail == {"workers": ["a", "b"]}
