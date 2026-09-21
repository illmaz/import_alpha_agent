"""Append-only log of every stage transition for auditability."""

from __future__ import annotations

import asyncio
import logging
import os

from bus import Bus
from events import ArtifactLogEntry, Topic

LOG_PATH = os.environ.get("ARTIFACT_LOG_PATH", "artifacts.log.jsonl")

logging.basicConfig(level=logging.INFO, format="%(asctime)s artifact_logger %(message)s")
logger = logging.getLogger("artifact_logger")


def _append(entry: ArtifactLogEntry) -> None:
    with open(LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(entry.model_dump_json())
        fh.write("\n")


async def run() -> None:
    bus = Bus()
    async for event in bus.subscribe(Topic.ARTIFACT_LOG):
        entry = ArtifactLogEntry.model_validate(event.payload)
        _append(entry)
        logger.info("logged task=%s stage=%s", entry.task_id, entry.stage)


if __name__ == "__main__":
    asyncio.run(run())
