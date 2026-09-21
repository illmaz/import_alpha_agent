import json
import uuid
from datetime import datetime

import pytest
from pydantic import ValidationError

from events import Event


def test_creates_valid_event():
    event = Event(
        event_type="task.created",
        task_id="t-1",
        agent="orchestrator",
        payload={"query": "drawer organizers"},
    )

    assert event.event_type == "task.created"
    assert event.task_id == "t-1"
    assert event.agent == "orchestrator"
    assert event.payload == {"query": "drawer organizers"}


def test_event_id_is_a_uuid4():
    event = Event(event_type="task.created")

    assert uuid.UUID(event.event_id).version == 4


def test_event_ids_are_unique():
    ids = {Event(event_type="task.created").event_id for _ in range(100)}

    assert len(ids) == 100


def test_optional_fields_default_to_none():
    event = Event(event_type="task.created")

    assert event.task_id is None
    assert event.agent is None


def test_payload_defaults_to_empty_dict():
    event = Event(event_type="task.created")

    assert event.payload == {}


def test_payload_default_is_not_shared_between_instances():
    first = Event(event_type="task.created")
    second = Event(event_type="task.created")

    first.payload["role"] = "product"

    assert second.payload == {}


def test_created_at_is_an_iso_utc_timestamp():
    event = Event(event_type="task.created")
    parsed = datetime.fromisoformat(event.created_at)

    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0


def test_event_type_is_required():
    with pytest.raises(ValidationError):
        Event()


def test_json_round_trip_preserves_every_field():
    original = Event(
        event_type="artifact.created",
        task_id="t-42",
        agent="product_worker",
        payload={"artifact_id": "a-1", "score": 0.87},
    )

    restored = Event.model_validate_json(original.model_dump_json())

    assert restored == original


def test_serializes_to_json_with_expected_keys():
    event = Event(event_type="task.assigned", task_id="t-1", agent="router")
    data = json.loads(event.model_dump_json())

    assert set(data) == {"event_id", "event_type", "task_id", "agent", "payload", "created_at"}
    assert data["event_type"] == "task.assigned"
