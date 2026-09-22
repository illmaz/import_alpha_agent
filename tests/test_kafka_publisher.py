"""Tests for the API-side Kafka publisher.

The correlation contract lives here: report_id must be published as the
event's task_id, because that is the only field the orchestrator echoes back.
"""

from __future__ import annotations

import asyncio

import pytest

from app.schemas import UserGoalPayload
from app.services import kafka_publisher


def test_goal_event_carries_report_id_as_task_id() -> None:
    event = kafka_publisher.build_goal_event("R-abc12345", "do the thing")
    assert event.task_id == "R-abc12345"


def test_goal_event_also_carries_report_id_in_the_payload() -> None:
    event = kafka_publisher.build_goal_event("R-abc12345", "do the thing")
    assert event.payload["report_id"] == "R-abc12345"
    assert event.payload["goal"] == "do the thing"


def test_goal_event_is_typed_as_a_user_goal() -> None:
    event = kafka_publisher.build_goal_event("R-abc12345", "do the thing")
    assert event.event_type == "user.goal"
    assert event.agent == "api"


def test_optional_fields_are_omitted_when_unset() -> None:
    event = kafka_publisher.build_goal_event("R-abc12345", "do the thing")
    assert "category" not in event.payload
    assert "max_products" not in event.payload


def test_optional_fields_are_included_when_set() -> None:
    event = kafka_publisher.build_goal_event(
        "R-abc12345", "do the thing", category="home_organization", max_products=5
    )
    assert event.payload["category"] == "home_organization"
    assert event.payload["max_products"] == 5


def test_payload_validates_against_the_schema() -> None:
    event = kafka_publisher.build_goal_event("R-abc12345", "do the thing")
    parsed = UserGoalPayload.model_validate(event.payload)
    assert parsed.report_id == "R-abc12345"


def test_empty_goal_text_is_rejected() -> None:
    with pytest.raises(ValueError):
        kafka_publisher.build_goal_event("R-abc12345", "")


def test_publish_runs_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The blocking produce+flush must not run on the loop thread."""
    import threading

    loop_thread = None
    called_on = {}

    def fake_produce(topic, event, key=None):
        called_on["thread"] = threading.current_thread().name

    monkeypatch.setattr(kafka_publisher, "produce", fake_produce)

    async def run() -> None:
        nonlocal loop_thread
        loop_thread = threading.current_thread().name
        await kafka_publisher.publish_goal(
            kafka_publisher.build_goal_event("R-abc12345", "do the thing")
        )

    asyncio.run(run())
    assert called_on["thread"] != loop_thread, "produce blocked the event loop"


def test_publish_uses_the_report_id_as_the_partition_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same report always lands on the same partition, so events stay ordered."""
    seen = {}

    def fake_produce(topic, event, key=None):
        seen["topic"] = topic
        seen["key"] = key

    monkeypatch.setattr(kafka_publisher, "produce", fake_produce)
    asyncio.run(
        kafka_publisher.publish_goal(
            kafka_publisher.build_goal_event("R-abc12345", "do the thing")
        )
    )
    assert seen["topic"] == "user.goals"
    assert seen["key"] == "R-abc12345"
