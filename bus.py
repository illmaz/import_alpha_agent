"""Thin async pub/sub wrapper over Kafka used by all services."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator

from confluent_kafka import Consumer, KafkaError, KafkaException, Producer

from events import Event, Topic

BOOTSTRAP_SERVERS = os.environ.get("BOOTSTRAP_SERVERS", "localhost:9092")


class Bus:
    def __init__(self, bootstrap_servers: str = BOOTSTRAP_SERVERS) -> None:
        self._bootstrap_servers = bootstrap_servers
        self._producer: Producer | None = None
        self._consumer: Consumer | None = None

    def _get_producer(self) -> Producer:
        if self._producer is None:
            self._producer = Producer({"bootstrap.servers": self._bootstrap_servers})
        return self._producer

    def _get_consumer(self, topics: list[str], group_id: str = "default-group") -> Consumer:
        if self._consumer is None:
            config = {
                "bootstrap.servers": self._bootstrap_servers,
                "group.id": group_id,
                "auto.offset.reset": "earliest",
                "enable.auto.commit": True,
            }
            self._consumer = Consumer(config)
            self._consumer.subscribe(topics)
        return self._consumer

    async def publish(self, event: Event) -> None:
        producer = self._get_producer()
        producer.produce(event.topic.value, value=event.model_dump_json().encode("utf-8"))
        producer.flush()

    async def subscribe(self, *topics: Topic) -> AsyncIterator[Event]:
        consumer = self._get_consumer([t.value for t in topics])
        try:
            while True:
                msg = consumer.poll(1.0)
                if msg is None:
                    continue
                if msg.error():
                    if msg.error().code() == KafkaError._PARTITION_EOF:
                        continue
                    raise KafkaException(msg.error())
                data = json.loads(msg.value().decode("utf-8"))
                yield Event.model_validate(data)
        finally:
            if self._consumer is not None:
                self._consumer.close()
                self._consumer = None
