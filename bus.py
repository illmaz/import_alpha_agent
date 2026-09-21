"""Kafka produce/consume helpers for the agent bus."""

from __future__ import annotations

import logging
from typing import Callable, List, Optional

from confluent_kafka import Consumer, KafkaError, Producer

from events import Event

BOOTSTRAP_SERVERS = "localhost:9092"

logger = logging.getLogger(__name__)

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
        while True:
            message = consumer.poll(1.0)
            if message is None:
                continue

            if message.error():
                # Reaching the end of a partition is normal, not a failure.
                if message.error().code() == KafkaError._PARTITION_EOF:
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
        logger.info("consumer interrupted, shutting down")
    finally:
        consumer.close()
