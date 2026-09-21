"""Thin async pub/sub wrapper over Redis used by all services."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator

import redis.asyncio as redis

from events import Event, Topic

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")


class Bus:
    def __init__(self, url: str = REDIS_URL) -> None:
        self._url = url
        self._client: redis.Redis | None = None

    async def connect(self) -> None:
        if self._client is None:
            self._client = redis.from_url(self._url, decode_responses=True)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def publish(self, event: Event) -> None:
        await self.connect()
        assert self._client is not None
        await self._client.publish(event.topic.value, event.model_dump_json())

    async def subscribe(self, *topics: Topic) -> AsyncIterator[Event]:
        await self.connect()
        assert self._client is not None
        pubsub = self._client.pubsub()
        await pubsub.subscribe(*[t.value for t in topics])
        try:
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                data = json.loads(message["data"])
                yield Event.model_validate(data)
        finally:
            await pubsub.unsubscribe(*[t.value for t in topics])
            await pubsub.aclose()
