"""Entry point: submits a query as a task and waits for product results."""

from __future__ import annotations

import asyncio
import logging
import sys

from bus import Bus
from events import Event, ProductResult, RouterRequest, RouterResult, Topic

logging.basicConfig(level=logging.INFO, format="%(asctime)s orchestrator %(message)s")
logger = logging.getLogger("orchestrator")


async def run_task(query: str) -> None:
    bus = Bus()
    request = RouterRequest(query=query)

    listener = asyncio.create_task(_collect_results(bus, request.task_id))

    await bus.publish(Event(topic=Topic.ROUTER_REQUEST, correlation_id=request.task_id, payload=request.model_dump()))
    logger.info("submitted task=%s query=%r", request.task_id, query)

    await listener


async def _collect_results(bus: Bus, task_id: str) -> None:
    expected: set[str] | None = None
    received: set[str] = set()

    async for event in bus.subscribe(Topic.ROUTER_RESULT, Topic.PRODUCT_RESULT):
        if event.correlation_id != task_id:
            continue

        if event.topic == Topic.ROUTER_RESULT:
            router_result = RouterResult.model_validate(event.payload)
            expected = set(router_result.target_workers)
            logger.info("task=%s expecting results from=%s", task_id, expected)

        elif event.topic == Topic.PRODUCT_RESULT:
            product_result = ProductResult.model_validate(event.payload)
            received.add(product_result.product)
            if product_result.error:
                logger.warning("task=%s product=%s error=%s", task_id, product_result.product, product_result.error)
            else:
                logger.info(
                    "task=%s product=%s datapoints=%d",
                    task_id,
                    product_result.product,
                    len(product_result.datapoints),
                )

        if expected is not None and received >= expected:
            logger.info("task=%s complete", task_id)
            return


if __name__ == "__main__":
    query = sys.argv[1] if len(sys.argv) > 1 else "example query"
    asyncio.run(run_task(query))
