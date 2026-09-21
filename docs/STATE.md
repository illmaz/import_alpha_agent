# Current State

## Phase

Initial setup — repo, context docs, Python/Kafka agentic workflow skeleton.

## Completed

- Defined wedge: ImportAlpha Lite (China-to-US product viability API, home organization first)
- Defined MVP offer and pricing ($99 / $299 / $999)
- Defined agent org structure
- Decided to start with Python + Kafka event bus (no LLM yet)
- Created project context docs

**Task 1 (BACKLOG P0 items 1-5 + Event schema tests) — FINISHED 2026-09-21.**

- Created Python project structure (root modules + `tests/`)
- Added requirements.txt (confluent-kafka, pydantic, fastapi, uvicorn, pytest — pinned floors)
- Added docker-compose.yml (single-node Kafka 3.9, KRaft mode, no ZooKeeper, port 9092, auto-create topics, replication factor 1)
- Added events.py (Pydantic v2 `Event`: event_id/event_type/task_id/agent/payload/created_at)
- Added bus.py (`produce(topic, event, key)` / `consume(topics, group_id, handler)`, BOOTSTRAP_SERVERS=localhost:9092)
- Added tests/test_events.py (10 tests, all passing under Python 3.12)
- Removed the broken Redis-era files (`orchestrator.py`, `router_worker.py`, `product_worker.py`, `artifact_logger.py`, `Dockerfile`); they are recoverable from commit `30c759c` if ever needed

**Task 2 (BACKLOG P0 items 5-8 + end-to-end flow) — FINISHED 2026-09-21.**

- Added orchestrator.py (publishes 3 dummy tasks as `task.created`: PROD-001/product, ENG-001/engineering, LAND-001/landing)
- Added router_worker.py (consumes `task.created`, re-emits `task.assigned` to `task.assigned.{role}`)
- Added product_worker.py (consumes `task.assigned.product`, simulates work, emits `artifact.created`)
- Added artifact_logger.py (consumes `artifact.created`, prints the artifact to console)
- **Verified the full flow end to end against a live broker**: PROD-001 travelled Orchestrator -> Router -> Product Worker -> Logger with no errors. `bitnamilegacy/kafka:3.9` starts and serves on localhost:9092 as configured, so P0 is now complete.

## In Progress

- None — all of BACKLOG P0 is complete and awaiting review

## Next Actions

1. Begin P1 (Product core): SQLite state store, then the FastAPI skeleton and the OpenAPI contract for the 4 MVP endpoints

## Blocked

None.

## Decisions Log

- 2026-09-21: Wedge = home organization category, US market.
- 2026-09-21: Sell report-first + API credits; x402 is phase 2.
- 2026-09-21: Build with Claude Code in normal VS Code first; Agent Sessions UI for parallel agents later.
- 2026-09-21: Local Python is 3.9 (system default); project stack requires 3.12. Use `/opt/homebrew/bin/python3.12 -m venv .venv` for local dev/test — plain `python3` will fail on this repo's type hints.
- 2026-09-21: `bitnami/kafka:3.9` is gone from Docker Hub (404). Bitnami retired versioned tags to the `bitnamilegacy` namespace in 2025, so docker-compose.yml pins `bitnamilegacy/kafka:3.9`. Alternative if that namespace is ever pulled: `apache/kafka:3.9.0` (verified available), which needs `KAFKA_*` env vars instead of Bitnami's `KAFKA_CFG_*`.

- 2026-09-21: Workers stay dumb for now — `product_worker.py` emits a `status: "stub"` artifact with an empty `datapoints` list rather than a plausible-looking fake brief. Inventing numbers here would violate the AGENTS.md sourcing rule and risks a placeholder being mistaken for real data later. Real datapoints land once sources are wired.
- 2026-09-21: `bus.consume` treats `UNKNOWN_TOPIC_OR_PART` as benign alongside `_PARTITION_EOF`. Consumers subscribe before a topic exists (it is auto-created on first produce), which otherwise logs a misleading ERROR on every worker startup.
- 2026-09-21: Deleted the Redis-era `orchestrator.py`, `router_worker.py`, `product_worker.py`, `artifact_logger.py` and `Dockerfile`. They were written against a Redis bus (contradicting the frozen Kafka stack) and were hard-broken after bus.py was rewritten — every one failed with `ImportError: cannot import name 'Bus' from 'bus'`. A broken file is worse than no file; P0 items 5-8 will rebuild them on Kafka from scratch. Recoverable at `30c759c`.