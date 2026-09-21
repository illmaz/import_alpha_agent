"""Routes incoming requests to the relevant product worker(s)."""

from __future__ import annotations

import asyncio
import logging

from bus import Bus
from events import (
    ArtifactLogEntry,
    Event,
    ProductRequest,
    RouterRequest,
    RouterResult,
    Topic,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s router_worker %(message)s")
logger = logging.getLogger("router_worker")

KNOWN_PRODUCTS = ["default"]


def choose_products(query: str) -> list[str]:
    """Placeholder routing rule: extend with real product matching logic."""
    return KNOWN_PRODUCTS


async def handle(bus: Bus, event: Event) -> None:
    request = RouterRequest.model_validate(event.payload)
    products = choose_products(request.query)
    result = RouterResult(
        task_id=request.task_id,
        target_workers=products,
        reason="default routing rule",
    )

    await bus.publish(
        Event(topic=Topic.ROUTER_RESULT, correlation_id=request.task_id, payload=result.model_dump())
    )

    for product in products:
        product_request = ProductRequest(task_id=request.task_id, product=product)
        await bus.publish(
            Event(
                topic=Topic.PRODUCT_REQUEST,
                correlation_id=request.task_id,
                payload=product_request.model_dump(),
            )
        )

    await bus.publish(
        Event(
            topic=Topic.ARTIFACT_LOG,
            correlation_id=request.task_id,
            payload=ArtifactLogEntry(
                task_id=request.task_id,
                stage="router.routed",
                detail={"target_workers": products},
            ).model_dump(),
        )
    )
    logger.info("routed task=%s to=%s", request.task_id, products)


async def run() -> None:
    bus = Bus()
    async for event in bus.subscribe(Topic.ROUTER_REQUEST):
        await handle(bus, event)


if __name__ == "__main__":
    asyncio.run(run())
