# Current State

## Phase

Initial setup — repo, context docs, Python/Kafka agentic workflow skeleton.

## Completed

- Defined wedge: ImportAlpha Lite (China-to-US product viability API, home organization first)
- Defined MVP offer and pricing ($99 / $299 / $999)
- Defined agent org structure
- Decided to start with Python + Kafka event bus (no LLM yet)
- Created project context docs

## In Progress

- Set up repo and context docs
- Prepare first Python/Kafka event bus

## Next Actions

1. Create Python project structure
2. Add docker-compose.yml for single-node Kafka
3. Add requirements.txt (confluent-kafka, pydantic, fastapi, uvicorn, pytest)
4. Add events.py (Pydantic Event model) and bus.py (produce/consume helpers)
5. Add tests for the Event schema
6. Run local event flow successfully
7. Then: orchestrator.py, router_worker.py, product_worker.py, artifact_logger.py

## Blocked

None.

## Decisions Log

- 2026-09-21: Wedge = home organization category, US market.
- 2026-09-21: Sell report-first + API credits; x402 is phase 2.
- 2026-09-21: Build with Claude Code in normal VS Code first; Agent Sessions UI for parallel agents later.