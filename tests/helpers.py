"""Shared offline test doubles: no broker, no LLM, no wall clock."""

import json

from events import Event
from llm import FakeLLM
from orchestrator import Orchestrator

GOAL_ID = "G-TEST"


class Recorder:
    """Stands in for bus.produce and keeps everything that was published."""

    def __init__(self):
        self.sent = []

    def __call__(self, topic, event, key=None):
        self.sent.append((topic, event, key))

    def topics(self):
        return [topic for topic, _, _ in self.sent]

    def on(self, topic):
        return [event for sent_topic, event, _ in self.sent if sent_topic == topic]


class FakeClock:
    """Monotonic clock the test drives by hand."""

    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def goal_event(text="find home organization winners", goal_id=GOAL_ID):
    return Event(event_type="user.goal", task_id=goal_id, agent="cli", payload={"goal": text})


def artifact_for(task_id, summary="a brief"):
    return Event(
        event_type="artifact.created",
        task_id=task_id,
        agent="product_worker",
        payload={"summary": summary},
    )


def plan_json(step_count=2, role="product"):
    return json.dumps(
        {
            "reasoning": "because",
            "steps": [{"role": role, "task": f"step {i}"} for i in range(1, step_count + 1)],
        }
    )


def build(responses=None, clock=None):
    """An Orchestrator wired to a FakeLLM and a recording producer."""
    recorder = Recorder()
    kwargs = {"clock": clock} if clock is not None else {}
    return Orchestrator(llm=FakeLLM(responses), producer=recorder, **kwargs), recorder
