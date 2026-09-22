"""Kafka produce/consume helpers for the agent bus."""

from __future__ import annotations

import logging
import os
import signal
import threading
from typing import Callable, List, Optional

from confluent_kafka import Consumer, KafkaError, Producer

from events import Event

# Defaults to the host-side port so a plain `python orchestrator.py` still works
# against the composed broker; inside compose every service sets kafka:9092.
BOOTSTRAP_SERVERS = os.environ.get("BOOTSTRAP_SERVERS", "localhost:9092")

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
# Each daemon runs as PID 1 in its container, and the kernel does NOT apply
# default signal dispositions to PID 1. An uncaught SIGTERM is therefore
# silently *ignored* rather than terminating the process: `docker compose
# stop` waits out its grace period and then SIGKILLs, which is exit 137 and
# means consumer.close() never ran, so offsets were never committed.
#
# Installing an explicit handler is what makes SIGTERM terminate at all here.
# SIGINT is caught by Python already (it raises KeyboardInterrupt), which is
# why Ctrl+C worked locally while Docker shutdowns did not — the local case
# hid the bug.
#
# The event is process-wide on purpose: the orchestrator runs a second
# consumer on a thread, and one signal has to stop both.
_shutdown = threading.Event()


def request_shutdown() -> None:
    """Ask every consume loop in this process to finish and return."""
    _shutdown.set()


def is_shutting_down() -> bool:
    return _shutdown.is_set()


def wait_for_shutdown(timeout: float) -> bool:
    """Sleep up to `timeout`, waking early on shutdown. Returns True if asked to stop.

    Used instead of time.sleep by daemons that poll on a timer, so a signal is
    acted on immediately rather than after the remainder of the interval.
    """
    return _shutdown.wait(timeout)


def install_signal_handlers(name: str = "daemon") -> None:
    """Make SIGTERM and SIGINT stop the consume loops cleanly.

    Call once from a daemon's main() before consuming. Safe to call from the
    main thread only, which is where signal handlers must be installed.
    """

    def _handle(signum, _frame) -> None:
        signal_name = signal.Signals(signum).name
        print(f"[{name}] {signal_name} received - finishing current message and closing")
        request_shutdown()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handle)
        except ValueError:
            # Not the main thread; the main thread's handler covers us.
            logger.debug("could not install %s handler off the main thread", sig)


_producer: Optional[Producer] = None


def get_producer() -> Producer:
    global _producer
    if _producer is None:
        _producer = Producer({"bootstrap.servers": BOOTSTRAP_SERVERS})
    return _producer


def produce(topic: str, event: Event, key: str | None = None) -> None:
    producer = get_producer()
    producer.produce(
        topic,
        key=key.encode("utf-8") if key is not None else None,
        value=event.model_dump_json().encode("utf-8"),
    )
    producer.flush()


def consume(topics: List[str], group_id: str, handler: Callable[[Event, str], None]) -> None:
    consumer = Consumer(
        {
            "bootstrap.servers": BOOTSTRAP_SERVERS,
            "group.id": group_id,
            "auto.offset.reset": "earliest",
        }
    )
    consumer.subscribe(topics)

    try:
        while not _shutdown.is_set():
            # The 1s timeout bounds how long a shutdown waits: the loop can
            # only notice the flag between polls.
            message = consumer.poll(1.0)
            if message is None:
                continue

            if message.error():
                # Neither is a failure: partition EOF just means fully read, and
                # an unknown topic is expected until auto-create fires on first
                # produce, which consumers routinely wait through at startup.
                if message.error().code() in (
                    KafkaError._PARTITION_EOF,
                    KafkaError.UNKNOWN_TOPIC_OR_PART,
                ):
                    continue
                logger.error("kafka error: %s", message.error())
                continue

            try:
                event = Event.model_validate_json(message.value())
            except ValueError:
                logger.exception("skipping malformed message on %s", message.topic())
                continue

            handler(event, message.topic())
    except KeyboardInterrupt:
        # Only reachable if a handler was not installed; kept so an
        # interactive run without install_signal_handlers still exits cleanly.
        logger.info("consumer interrupted, shutting down")
    finally:
        # Commits offsets and leaves the consumer group, so a restart resumes
        # where this process stopped instead of replaying or skipping.
        consumer.close()
        logger.info("consumer closed for group %s", group_id)
