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

**Phase 2 (LLM-powered orchestrator daemon) — FINISHED 2026-09-21.**

- Added llm.py (`LLM` base, `FakeLLM` default, `AnthropicLLM`, `OpenAICompatibleLLM`; one static cache-friendly system prompt, hard `MAX_TOKENS` ceiling)
- Added `WorkerRole` / `PlanStep` / `GoalPlan` to events.py (role restricted to known worker roles)
- Rewrote orchestrator.py as a long-running daemon: consumes `user.goals`, plans via LLM, dispatches `task.created` per step, tracks `artifact.created` on a threaded consumer, publishes `goal.completed` with an LLM synthesis
- Loop guards: `MAX_STEPS_PER_GOAL=5`, `MAX_REPLANS=2`; a breach publishes `human.approval.required` and halts that goal only
- Added scripts/submit_goal.py (CLI to publish a plain-text goal)
- Added tests/test_orchestrator.py (20 tests, FakeLLM + recording producer, no broker needed)
- **Verified live**: goal `G-3f7086c2` planned into 2 steps, both routed and worked, `goal.completed` published with synthesis. 30/30 tests pass.

## In Progress

- None — Phase 2 is complete and awaiting review

## Next Actions

1. Add workers for the `engineering` and `landing` roles — only `product` has one, so any plan touching the other roles stalls forever (see docs/DECISIONS.md)
2. Then P1 (Product core): SQLite state store, FastAPI skeleton, OpenAPI contract for the 4 MVP endpoints

## Blocked

None.

## Decisions Log

Moved to [DECISIONS.md](DECISIONS.md) — kept in one place so it does not drift.
