# Agent Instructions

Before doing anything, read these files in order:

1. docs/CONTEXT.md
2. docs/STATE.md
3. docs/BACKLOG.md

## Hard rules

- Work ONLY on the assigned task. Do not expand scope.
- Do not change the stack unless explicitly told to.
- Do not invent data. Every datapoint must include `source_url`, `observed_at`, and `confidence`.
- No wallet custody. No mainnet payments. Sandbox/testnet only.
- No outbound messages, no public posting, no spending money without human approval.
- No production deploys without human approval.
- Prefer small, testable changes. Add tests when logic changes.
- Prefer deterministic Python first, LLM calls second.
- Use Kafka for events, not as the main database.

## When done with a task

- List the files you created or changed.
- Update docs/STATE.md (move items between Completed / In Progress / Next).
- If blocked, state exactly what is missing and stop.

## Stack (frozen)

Python 3.12 · FastAPI · Pydantic v2 · SQLAlchemy 2 · SQLite now (Postgres later) · Kafka via Docker · pytest · Stripe prepaid credits first · x402/USDC sandbox later.