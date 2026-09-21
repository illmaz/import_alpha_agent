# Decision Log

## 2026-09-21: Wedge = home organization category, US market.

## 2026-09-21: Sell report-first + API credits; x402 is phase 2.

## 2026-09-21: Build with Claude Code in normal VS Code first; Agent Sessions UI for parallel agents later.

## Phase 3 Decisions

### P1: `step.started` ack as primary completion signal
**Rationale:** Provides deterministic end-to-end workflow completion detection without requiring heartbeat coordination across all workers.

### P2: Heartbeats as secondary liveness indicator  
**Rationale:** Useful for detecting stalled workers during long-running tasks but not required for basic completion signaling; deferred to avoid complexity in Phase 3.

### BROKER-RESILIENCE CHECK (Phase 3 acceptance)
**Outcome:** [To be recorded after running acceptance test]
**Test:** With all services running, kill Kafka (`docker compose kill kafka`), wait ~10s, restart Kafka (`docker compose up -d kafka`), submit another goal. Must complete with no manual service restarts and no duplicate dispatches.
