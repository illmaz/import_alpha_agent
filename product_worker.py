"""Executes a single product's data-gathering work and reports datapoints.

Every datapoint must carry source_url, observed_at, and confidence — no
invented data is published on the bus (see repo instructions).
"""

from __future__ import annotations

import asyncio
import logging

from bus import Bus
from events import (
    ArtifactLogEntry,
    Datapoint,
    Event,
    ProductRequest,
    ProductResult,
    Topic,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s product_worker %(message)s")
logger = logging.getLogger("product_worker")


async def gather_datapoints(request: ProductRequest) -> list[Datapoint]:
    """Placeholder: wire this up to real sourced data before use.

    Must never fabricate a Datapoint — only return ones backed by a real
    source_url and observed_at.
    """
    return []


async def handle(bus: Bus, event: Event) -> None:
    request = ProductRequest.model_validate(event.payload)
    try:
        datapoints = await gather_datapoints(request)
        result = ProductResult(task_id=request.task_id, product=request.product, datapoints=datapoints)
    except Exception as exc:  # noqa: BLE001 - surfaced on the bus, not swallowed
        logger.exception("product worker failed task=%s", request.task_id)
        result = ProductResult(task_id=request.task_id, product=request.product, error=str(exc))

    await bus.publish(
        Event(topic=Topic.PRODUCT_RESULT, correlation_id=request.task_id, payload=result.model_dump())
    )

    await bus.publish(
        Event(
            topic=Topic.ARTIFACT_LOG,
            correlation_id=request.task_id,
            payload=ArtifactLogEntry(
                task_id=request.task_id,
                stage="product.completed",
                detail={"product": request.product, "datapoint_count": len(result.datapoints)},
            ).model_dump(),
        )
    )
    logger.info("completed task=%s product=%s", request.task_id, request.product)


async def run() -> None:
    bus = Bus()
    async for event in bus.subscribe(Topic.PRODUCT_REQUEST):
        await handle(bus, event)


if __name__ == "__main__":
    asyncio.run(run())
