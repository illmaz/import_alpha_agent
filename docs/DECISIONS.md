# Decisions

Durable architectural decisions and their reasoning. `docs/STATE.md` tracks
what is done; this file records *why* things are the way they are.

## P2.1.5 — Alembic and refunds on failure (2026-09-22)

### The migration chain starts with a baseline, not with account_id

Two revisions, not one:

    f2c00b0a0a6e  initial schema      (reports, api_keys, credit_accounts)
    456aca293990  add account_id to reports

The baseline exists so a **fresh** database gets the full schema from
migrations alone. The live database already had all three tables and no
`alembic_version`, so it was stamped at the baseline and upgraded from there —
`alembic stamp f2c00b0a0a6e && alembic upgrade head`. That is the one-time
adoption step; it is not needed again.

### The generated migration would have failed, and was rewritten

Autogenerate produced a single `add_column(..., nullable=False)`. That fails
outright on a table with rows, and `reports` had 11. The migration now adds
the column nullable, backfills, then constrains it:

    add_column(account_id, nullable=True)
    UPDATE reports SET account_id = 'legacy-unknown' WHERE account_id IS NULL
    alter_column(account_id, nullable=False) + index

Rows written before billing existed carry the `legacy-unknown` sentinel. They
are never refunded — nobody was charged for them. The literal is duplicated in
the migration rather than imported from `report_store.LEGACY_ACCOUNT_ID`,
because a migration has to keep working when application constants move.

### alembic.ini has no URL; env.py asks the application

`sqlalchemy.url` in alembic.ini is deliberately empty and `alembic/env.py`
calls `app.database.get_database_url()`. One source of truth, `DATABASE_URL`
still wins, and the test suite can migrate a throwaway file. `render_as_batch`
is on because SQLite has no real ALTER COLUMN — batch mode rebuilds the table.

### create_all still runs, and one test guards the gap that creates

`init_models()` still calls `create_all` at startup, so tests and fresh dev
databases do not need the migration chain. That is convenient and it is a
trap: a model change then works everywhere except against a database with
rows in it, which is production.

`test_migrations_match_the_models` migrates a temp database with the Alembic
CLI and compares the result against `Base.metadata`. A model column with no
migration passes every other test in the suite and fails that one.

### Refunds are authorised by winning a state transition, not by observing one

`settle_if_pending()` moves a report out of `pending` with a conditional
UPDATE and returns the owning account only to the caller that actually
performed the change. Everyone else gets None and must not refund.

This is required, not defensive. Two independent daemons can settle the same
report: Kafka delivery is at-least-once, so `report_listener` can see the same
`goal.completed` twice, and a slow report can be reaped moments before its
event arrives. "Set status, then refund" would credit the account twice in
both cases — silently minting money.

Observed live during acceptance: the reaper failed and refunded a report, the
lane came back, the orchestrator completed the queued goal, and the listener
logged `was already settled; skipping` with the balance unchanged.

### refund_credits became a single atomic UPDATE

It previously called `add_credits`, which read the row, incremented in Python
and wrote it back — the same lost-update race `deduct_credits` was written to
avoid. With refunds now arriving from two daemons, that race is real. It is
still a separate function from `add_credits` despite near-identical SQL: at
the call site and in a future ledger, a refund is not a purchase.

### What is still not refunded

A report that fails for a reason *other* than the lane timing out or
escalating — for example a malformed request that somehow reached generation —
has no path here. There is also still no ledger: balances are current values
with no history, so "why is my balance 3" cannot be answered. Both matter more
once Stripe money is involved than they do now.

## Operational hardening — shutdown, secrets, LLM toggle (2026-09-22)

### Exit 137 was PID 1 ignoring SIGTERM

Every daemon runs as PID 1 in its container (`python orchestrator.py` is the
image's command, so there is no init process above it). **The kernel does not
apply default signal dispositions to PID 1**: a signal whose action is the
default is *ignored* rather than terminating the process, unless the process
installs a handler.

So `docker compose stop` sent SIGTERM, nothing happened, the 10s grace period
expired, and Docker sent SIGKILL — exit 137, with `consumer.close()` never
reached and offsets never committed. On restart the group re-read from its
last committed position, which is how in-flight goals could be replayed.

Confirmed from inside the container before fixing:

    SigCgt: 0000000100000002     # caught signals
                           ^ bit 1 = SIGINT
    # bit 14 (0x4000) = SIGTERM is NOT set

SIGINT *was* caught, because Python installs its own SIGINT handler to raise
KeyboardInterrupt. That is exactly why Ctrl+C appeared to work locally while
Docker shutdowns did not — the interactive case hid the bug.

`bus.install_signal_handlers()` now installs explicit SIGTERM and SIGINT
handlers that set a process-wide `threading.Event`, and `bus.consume()` checks
it each iteration instead of looping on `while True`. Verified: all nine
containers now exit 0 on `docker compose stop`, down from 137.

The event is process-wide rather than per-consumer because the orchestrator
runs a second consumer on a thread; one signal has to stop both, and both
must commit their own offsets.

### The reaper waits on the event, not on time.sleep

`report_reaper` slept 60s between sweeps. A SIGTERM arriving one second in
would have waited out the remaining 59 and been SIGKILLed first. It now uses
`bus.wait_for_shutdown(INTERVAL_SECONDS)`, which returns early on a signal.

Any future daemon that polls on a timer has the same trap.

### .gitignore denies by pattern, not by filename

`.env` alone was never enough: `.env.backup.<ts>` written by `live_demo.sh`
was a different filename holding the same key, and it reached the index once
(caught before pushing). The rules are now `.env`, `.env.*` with an explicit
`!.env.example` negation, plus `*.backup*`, `*.log`, `*.dump`, `*.sql` and
`*.sqlite*` — the file types that capture environment contents in passing.

Naming one file is a guess about which filename a secret will land in. Denying
the shape is not.

### toggle_llm.sh reads the key with `read -s`

`export LLM_API_KEY=sk-...` puts the key in the terminal scrollback *and* in
shell history, where it survives long after the session. The toggle script
reads it with `read -rs` (no echo), writes it straight into `.env`, and prints
only a character count when reporting status.

`toggle_llm.sh fake` deliberately leaves the stored key in place. Switching
back should not require pasting it again, and FakeLLM never reads it — the
provider switch is what stops the spend, not deleting the credential.

## P2.1 — API key auth and prepaid credits (2026-09-22)

### SHA-256, not bcrypt, because these keys are not passwords

bcrypt exists to make *low-entropy human passwords* expensive to brute-force.
API keys here are 32 bytes from `secrets.token_urlsafe` — 256 bits, not
brute-forceable at any hash speed. bcrypt would add a deliberate ~100ms cost
to **every authenticated request** and buy nothing.

A fast hash over a high-entropy secret is the correct construction. The
reasoning depends entirely on the entropy assumption: if a user-chosen key is
ever accepted, this must become a slow hash. That condition is written at the
top of `app/services/auth.py` so it is seen by whoever would break it.

Plaintext is never stored. It is printed once at issuance and cannot be
recovered — a lost key is replaced, not looked up.

### Deduction is one conditional UPDATE, not read-modify-write

    UPDATE credit_accounts SET balance = balance - :n
     WHERE account_id = :id AND balance >= :n

with the outcome read from the row count. The obvious implementation — read
the balance, compare in Python, write the new value — has a race: two
concurrent requests both read balance 1, both decide they can afford it, both
write 0, and the customer gets two reports for one credit. Putting the
comparison in the WHERE clause makes check-and-write a single atomic
statement.

`test_concurrent_deductions_cannot_oversell` runs both deductions through
`asyncio.gather` and asserts exactly one succeeds.

`deduct_credits` returning False is the *only* signal for "cannot pay". A
caller must not re-read the balance to decide, or the race comes straight
back.

### Balances are integers

A money-like quantity must never accumulate binary rounding error. 1 credit =
1 report, so there is nothing to represent fractionally yet — and when
fractional pricing arrives the answer is smaller integer units, not floats.

### The auth dependency is declared on the router

`APIRouter(dependencies=[Depends(get_current_account)])` rather than
per-endpoint. A new endpoint is then authenticated by default and has to opt
*out* explicitly. The mistake that direction produces is a public endpoint
that 401s; the other direction produces an unauthenticated data leak.

`HTTPBearer(auto_error=False)` so a missing header reaches our handler: the
default emits a bare 403 for "no header", which tells a caller the wrong
thing. Missing, malformed, unknown and revoked keys all return the same 401 —
distinguishing them helps nobody but someone probing for valid keys.

`/health` stays public. A liveness probe must not need a credential.

### The credit is taken before the goal is published, and refunded on failure

Deducting first means a caller cannot queue work it has not paid for. But the
publish can still fail (broker down), and the customer should not pay for our
outage — so that path refunds before returning 503.

`refund_credits` is deliberately a separate function from `add_credits`
although the mechanics are identical: at the call site and in a future ledger,
a refund is not a purchase.

**Known gap, not built:** a report the *reaper* fails (`agent_lane_timeout`)
is **not** refunded. The customer paid, the lane never delivered, and the
credit is gone. That is wrong, and it is on the backlog. It was left out
because doing it properly means the reaper needs to know which account owns a
report — reports carry no account_id yet — and bolting on a lookup would be
worse than the explicit gap.

### Only report generation is metered

`GET /v1/opportunities`, `POST /v1/landed-cost` and `GET /v1/reports/{id}`
require a key but cost nothing. They are cheap, deterministic and read-only;
metering them would punish polling, and the poll loop is how a caller collects
the report they already paid for.

### Accounts and keys are wiped between tests

`tests/conftest.py` now clears `api_keys` and `credit_accounts` as well as
`reports`. Accounts leak between tests exactly as readily as report ids do —
this surfaced immediately as `account 'acct-7' already exists` in a
parametrized case.

## P1 Part 4.5 — Report reaper and live-run prep (2026-09-22)

### The liveness invariant now covers reports

Phase 2.5 established that every goal terminates. P1 Part 4 reopened the same
hole one level up: a report is persisted as `pending` and published as a goal,
and if the lane dies after publication nothing ever settles that row.

`scripts/report_reaper.py` closes it. Every 60s it fails any report pending
longer than `REPORT_STALE_AFTER_SECONDS` (default 300). **A report now always
terminates: ready, failed by the listener, or failed by the reaper.**

The threshold must stay comfortably above the orchestrator's own budget — a
goal can burn `MAX_REPLANS` planning attempts plus `STEP_TTL_SECONDS` (default
120) per stall. A reaper threshold below that would kill reports the lane was
still legitimately working on. A test asserts the ordering
(`test_threshold_exceeds_the_orchestrator_step_ttl`) so the two cannot drift
apart silently.

### The reaper merges the failure; it does not overwrite the body

The brief specified `payload_json={"error": "agent_lane_timeout"}`. Written
literally that would have made `GET /v1/reports/{id}` return **500**: the
endpoint validates the stored payload as a `ReportResponse`, which requires
`report_id` and forbids unknown fields. The caller would get an opaque server
error instead of the failure we were trying to report.

So the reaper does what the listener does for an escalation — keeps the body,
sets `report_status=failed`, and puts `agent_lane_timeout` in the summary. A
row that was created but never filled in gets a minimal valid body instead, so
it is servable too. Two tests cover the HTTP read path specifically, because
the unit-level write looked correct in both designs.

### Empty LLM_PROVIDER crashes the orchestrator, so the demo writes .env

`llm.get_llm()` falls through to `raise ValueError` on an empty
`LLM_PROVIDER`, so passing the LLM settings through compose as
`${LLM_PROVIDER:-}` would break every run where the variable is unset — the
common case. `scripts/live_demo.sh` writes them into `.env` (gitignored, and
already the documented mechanism, read through `env_file`) and backs up any
existing file first.

### The live demo asks before spending

AGENTS.md: no spending money without human approval. The script states the
provider, model and expected cost, then requires a `y` before it writes
anything or starts a container. `--yes` skips it for CI. Aborting touches
nothing.

### Model ids in .env.example are current, not the ones in the brief

The brief suggested `claude-3-5-haiku-20241022`. That id is stale and
contradicts this repo's own `llm.DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"`.
The current ids are `claude-opus-5`, `claude-sonnet-5` and `claude-haiku-4-5`,
used with no date suffix appended. `.env.example` suggests `claude-haiku-4-5`
for a first live run as the cheapest and fastest, and notes that the
orchestrator defaults to `claude-opus-5` when `LLM_MODEL` is unset.

## P1 Part 4 — API wired to the agent lane (2026-09-22)

### report_id rides on task_id, not in the payload

The task brief put `report_id` in the `user.goals` payload. That alone could
never have worked: `orchestrator._complete` builds a **fresh** payload for
`goal.completed` (`goal_id`, `goal`, `steps_completed`, `summary`) and does not
echo the payload it received. A report_id living only in the payload would be
dropped the moment the orchestrator planned the goal.

What the orchestrator *does* echo is the id it adopted:
`handle_goal` takes `goal_id = event.task_id or <generated>`, and that goal_id
appears on both `goal.completed` and `human.approval.required`. So the API
publishes with `task_id=report_id`, the orchestrator adopts it as its goal_id,
and it comes back on both outcome topics.

This needed **no change to the orchestrator**, and it mirrors the existing
decision that step completion is correlated by task_id rather than goal_id.
The report_id is still written into the payload as the brief asked, for any
consumer reading the goal event directly, and the listener prefers it when
present — but the working correlation is the task_id.

Consequence worth knowing: goal ids for API-raised goals are `R-` prefixed,
not `G-`. Step ids read `R-7c155375-S1`.

### POST stores the body up front, and the lane only adds the summary

`POST /v1/reports` computes the curated opportunities, stores them with
`status=pending`, publishes the goal, and returns 202. The listener later sets
`status=ready` and overwrites `summary` with the lane's synthesis.

The row is therefore never an empty shell, and a caller can see what the
report will contain while the lane works. It also means the scoring engine
stays on the API side where it is unit-tested, rather than being duplicated
into the listener.

**This is a real behaviour change:** before Part 4, POST returned a finished
report. Now a report is not readable until the agent lane settles it, which
takes ~10-30s with goals processed serially. If the lane is down, reports stay
pending indefinitely — nothing reaps them. A TTL-based sweep that fails
long-pending reports is the obvious next guard and is not built.

### A failed publish fails the report immediately

If the broker is unreachable, `POST` marks the row `failed` and returns 503
rather than leaving a pending row that nothing will ever complete. This is the
one piece of error handling deliberately added beyond "keep it simple", for
the same reason as the liveness invariant: no record may sit pending forever
with nothing that ends it.

### WAL does not work on a macOS bind mount with two processes

Found live, not reasoned about. With `report_listener` added, the second
process to open `./data/reports.db` started failing every INSERT with
`sqlite3.OperationalError: disk I/O error`. The first report of a fresh stack
succeeded; everything after it 500'd.

WAL coordinates readers and writers through a shared-memory `-shm` file and
requires working mmap plus POSIX locking on the database's directory. Docker
Desktop's bind mount does not provide that reliably, and the failure only
appears once a *second* process joins — which is exactly what Part 4 added.

`JOURNAL_MODE` now defaults to `DELETE`, which uses ordinary POSIX locks the
bind mount does handle. Three back-to-back POSTs and three concurrent goals
settled cleanly afterwards. Set `SQLITE_JOURNAL_MODE=WAL` where the file is on
a real local filesystem (a Docker named volume, or native Linux) to get WAL's
better read concurrency back.

The broader lesson: SQLite on a bind mount is a development convenience, not a
deployment posture. The Postgres move in CONTEXT.md is the real answer.

### The listener drops bad events rather than crashing

`handle()` catches everything around the database write and logs it. One
malformed or unsettleable event must not stop every later report from
settling. There is no retry and no dead-letter queue yet, so a dropped event
means a report stays pending — acceptable while generation is idempotent and
re-submittable, and the first thing to revisit when it is not.

Goals submitted from the CLI share these topics and have no report row; the
listener logs and skips them, which is normal traffic rather than an error.

## P1 Part 3 — SQLite persistence (2026-09-22)

### create_all is a deliberate short-term choice with a hard expiry

No Alembic yet: the lifespan handler calls `Base.metadata.create_all`, which
creates missing tables and **does nothing to an existing one**. Adding a
column to `reports` later will therefore appear to work, leave the table
unchanged, and fail at runtime on the first query for that column.

That is acceptable while the schema is one table and nothing is deployed. It
stops being acceptable at the first schema change against a file holding rows
anyone cares about. Bring in Alembic before that change, not after it.

### The report body is JSON text, not normalised columns

`reports.payload_json` holds a serialised `ReportResponse`. The report is a
document: written once, read whole, never queried by its inner fields. The
indexed columns are only what we filter or sort on — `status`, `created_at`.

Normalising the opportunities into rows would buy no query we need and would
demand a migration every time the response schema moved. If reports later need
to be searched by product or score, that is the point to revisit it.

### The row is created before the body exists

`POST /v1/reports` inserts a `pending` row, then generates, then updates to
`ready`. Generation is synchronous today, so the window is tiny — but the
ordering is what makes a crash mid-generation visible as a stuck `pending`
report instead of leaving no trace at all. It is also the shape the Kafka lane
needs when generation moves off the request path.

This is why `GET /v1/reports/{id}` answers **409** for a row with no body yet,
distinct from **404** for an id that never existed. A caller polling for
completion has to be able to tell "not ready" from "wrong id"; collapsing both
into 404 would make a poller give up on a report that was about to arrive.

### update_report raises instead of no-opping on a missing row

Updating an id that is not there is a bug in the caller, and a silent no-op
would strand the report as `pending` forever with nothing to indicate why.

### WAL mode and a busy timeout — SUPERSEDED in P1 Part 4

*Originally:* `PRAGMA journal_mode=WAL` plus a 5s busy timeout, so readers
proceed while a writer holds the lock and a competing writer waits rather than
failing with "database is locked".

The busy timeout stands. **WAL did not survive a second process opening the
same bind-mounted file** — see "WAL does not work on a macOS bind mount" under
P1 Part 4. The default is now DELETE.

### Tests use a temporary SQLite file, never :memory:

`tests/conftest.py` redirects `DATABASE_URL` at a temp **file** for the whole
session. An in-memory SQLite database is scoped to one connection, so the
persistence tests would pass without proving anything. A guard test
(`test_tests_never_touch_the_real_database`) asserts the redirection is in
effect, because a regression there would silently let the suite write to the
real `data/reports.db`.

### greenlet is an explicit dependency

SQLAlchemy's async bridge requires greenlet and does not reliably pull it in
on arm64 macOS. The failure is at first connect, not at import, so it surfaces
as a confusing runtime `ValueError` rather than a missing-module error at
startup. Pinned in requirements.txt rather than left to chance.

### ./data is bind-mounted on every app service

Not just `fastapi`. The workers do not read reports today, but the fixtures
live in the same directory and a worker that later writes artifacts to the
database already has the mount. The bind mount also means the database
survives `docker compose down`, which was verified rather than assumed.

One consequence worth knowing: the mount shadows `/app/data` from the image,
so the container reads the **host's** fixtures, not the copy baked into the
image. Editing `data/fixtures/*.json` takes effect without a rebuild — handy
in development, and a difference from how the rest of the code is delivered.

## P1 Part 2 — Scoring, fixtures and landed cost (2026-09-22)

### Curated data gets its own status, and non-web source URIs

`ResultStatus.CURATED` sits alongside `OK` and `STUB`. It is not a weaker
`OK` — it means the number was hand-written for development and never observed
anywhere.

The fixture's `source_urls` are `curated://importalpha/home-organization/v1#<slug>`,
deliberately not http(s). A plausible marketplace link would have satisfied the
letter of the AGENTS.md sourcing rule while breaking its purpose: a fabricated
URL is worse than a fabricated number, because a reader can check a number
against intuition but will take a URL as verifiable. Two tests enforce this
directly — `test_no_record_claims_a_real_web_source` and
`test_no_endpoint_presents_curated_data_as_observed` — so the rule cannot erode
quietly when real sources start arriving alongside curated ones.

Confidence is a uniform 0.25 across all 20 records. Varying it per product
would encode differential evidence that does not exist.

### Retail price is curated; margin is derived

Margin needs a price to measure against, and the fixture had none. The options
were to hardcode a margin per product (inventing the output directly) or to
curate a retail anchor and derive margin from it. The second is honest about
where the guess lives, and it means a change to freight or duty rates moves
every score — which is exactly what should happen once those rates are real.

### Margin currently does no ranking work

With this fixture every product lands above the 60% `MARGIN_SATURATION_PCT`,
so the margin component is 100 for all 20 and contributes a flat 35 points.
Ranking is therefore driven entirely by trend, competition and risk.

This is a property of the curated retail prices, not a bug in the formula, and
it will resolve on its own once real prices and fulfilment fees pull margins
down into the discriminating band. Worth knowing before anyone concludes the
margin weight is miscalibrated. If it persists with real data, lower
`MARGIN_SATURATION_PCT` rather than changing the weights.

### Out-of-range scoring inputs raise; a negative margin does not

`calculate_opportunity_score` raises `ValueError` on a trend, competition or
confidence value outside its range, or an unknown risk level. Those are caller
contract violations, and clamping them silently would hide a data-pipeline bug
behind a plausible score.

A negative margin is different — a loss-making product is a legitimate input,
not a bug — so it floors at zero and scores accordingly.

### The report store is in-memory, with two consequences

`report_store` is a process-local dict behind a lock. Reports are lost on
restart, and with more than one API replica the replica serving `GET` may not
be the one that served `POST`, which then 404s. Run one replica until the
SQLite store lands. Recorded here because both failure modes are invisible in
a single-replica dev environment and obvious only in production.

`POST /v1/reports` still answers 202 although generation is synchronous today.
The 202 is forward-looking: generation moves onto the Kafka lane later, and
callers already polling `GET /v1/reports/{id}` will not have to change.

### `docker compose up --build` does not rebuild profile-gated services

The `pytest` and `cli` services sit behind profiles, so `up --build` skips
them and `docker compose run --rm pytest` silently runs a stale image. This
was caught live: the container reported 65 passing tests against code that had
141. Rebuild them explicitly:

    docker compose build pytest cli

Anything that only ever runs through `docker compose run` needs this, and a
stale test image is the worst case of it — it reports success for code that is
not there.

## Phase 3 — Dockerized run lane (2026-09-22)

### PR #1 was discarded, not merged

Phase 3 was first built in a separate Qwen Coder session, on branch
`qwen-coder-vs-code-connection-fix-bc4eb` (`407f1e7`, PR #1). It was closed
without merging. Its merge base with `main` is `30c759c` — the commit *before*
Phase 1 — so it never saw the event bus, the LLM orchestrator or the liveness
work. It shipped its own async `Bus` class (`await bus.publish(Event)`) and
imported `Topic`, `RouterRequest`, `RouterResult` and `ProductResult`, none of
which exist in this `events.py`. A dry-run merge conflicted in 9 files, and
resolving them would still have left code that fails on import.

The branch is kept for reference. What was salvaged from it is design, not
code: compose-level healthcheck gating and the broker-resilience acceptance
test. Three bugs in it were fixed rather than carried over — two broker
listeners bound to the same port (the broker refuses to start), the
engineering and landing services both running `product_worker.py`, and a hard
`env_file: .env` that fails when no `.env` exists.

The lesson worth keeping: a parallel agent session must be branched from the
current head, and its work checked in before the main line moves on. Four
commits of divergence made an otherwise reasonable piece of work unusable.

### Compose is the only place bootstrap servers are configured

`bus.BOOTSTRAP_SERVERS` reads `BOOTSTRAP_SERVERS`, defaulting to
`localhost:9092`. Every compose service sets `kafka:9092`. That env var is the
*only* Python-visible change Phase 3 made; the orchestrator daemon and all
workers run byte-identical inside and outside Docker, so a container-only bug
cannot hide in a code path tests never take.

### The broker advertises kafka:9092, so host clients no longer work

`KAFKA_CFG_ADVERTISED_LISTENERS=PLAINTEXT://kafka:9092` is a single listener,
which is what makes container-to-container traffic correct and avoids PR #1's
same-port collision. The cost: a client on the host that connects to
`localhost:9092` is redirected to `kafka:9092`, which does not resolve outside
the compose network. Running `python scripts/submit_goal.py` from the host
therefore hangs; use `docker compose run --rm cli ...` instead. The port is
still published on `127.0.0.1:9092` for probes. Restoring host access means a
second `PLAINTEXT_HOST` listener on its own port (9093 is taken by the
controller, so 29092) — deliberately not done yet, since nothing needs it.

### Kafka has no restart policy; the app services do

The six application services are `restart: unless-stopped`. Kafka deliberately
is not: the broker-resilience check kills it and expects it to stay down until
brought back by hand. An auto-restarting broker would make that check untestable.

### The cli service is profile-gated

`profiles: ["cli"]` keeps the one-shot goal submitter out of
`docker compose up`, which would otherwise start it, submit a hardcoded goal
and leave an exited container in `ps` on every boot. It is reached only through
`docker compose run --rm cli ...`.

### BROKER-RESILIENCE CHECK — outcome

**PASSED, 2026-09-22.** Killed kafka, waited 15s, restarted it, submitted a new
goal. All six app containers stayed up (RestartCount 0) and the goal completed
end to end with no duplicate dispatches.

**Caveat worth knowing before relying on this:** recovery is not immediate.
librdkafka logs `Connection refused` throughout the outage, then needs a ~45s
consumer-group session timeout plus a rebalance before goals are consumed
again. A goal submitted during that window is not lost — it sits on the topic
and is picked up after the rebalance — but "resilient" here means *eventually
self-healing*, not *uninterrupted*. Note that 45s is well under the 120s
`STEP_TTL_SECONDS`, so an outage does not by itself trip stall detection; a
longer outage would, and the goal would replan rather than hang.

## Phase 2.5 — Liveness (2026-09-21)

### The liveness invariant

**Every goal terminates in exactly one of three states:**

1. **completed** — every step produced an artifact; `goal.completed` published.
2. **escalated** — `human.approval.required` published for an unplannable or
   stalled goal (`plan_validation_failed`, `step_stalled`, `empty_goal`).
3. **guard-breached** — `human.approval.required` published because a loop
   guard tripped (`max_steps_exceeded`).

No goal may sit pending forever. Before Phase 2.5 two paths violated this: a
plan targeting a role with no worker hung silently, and a crashed or slow worker
left its step pending with nothing watching. Both now terminate.

Anything added later that can leave a goal pending — new roles, sub-tasks,
nested depth, external calls — must come with the path that ends it. When
changing this area, the question to answer is "what ends this goal if the happy
path never happens?"

### ACTIVE_ROLES gates planning, not just dispatch

`events.ACTIVE_ROLES` (env-overridable, default all three roles) is enforced
inside `GoalPlan` validation, so an inactive role fails the plan rather than
being filtered out of it. A silently dropped step would give a goal that
completes while having skipped work the model thought was necessary — worse
than a loud escalation. It also feeds `llm.SYSTEM_PROMPT`, so the model is only
ever told about roles that can actually run.

Set `ACTIVE_ROLES=product,landing` to run a subset while a worker is down.

### Stall detection replans once, then halts

The orchestrator stamps a dispatch time per step. A ticker thread (10s, env
`STALL_TICK_SECONDS`) finds pending steps older than `STEP_TTL_SECONDS`
(default 120). The first stall on a goal triggers one replan, drawn from the
same `MAX_REPLANS` budget as validation retries — so a goal whose plan already
needed a retry escalates on its first stall instead of replanning. The second
stall always escalates.

Replanned steps get generation-tagged ids (`{goal_id}-R1-S1`). Without that, a
merely-slow worker's late artifact would arrive after the replan and satisfy a
step it never worked on, completing the goal on false evidence.

The TTL is wall-clock, not a worker heartbeat: a long-running legitimate step
looks identical to a dead one. Raise `STEP_TTL_SECONDS` before adding slow work,
or add heartbeats.

## Phase 2 — LLM orchestration (2026-09-21)

### FakeLLM is the default provider

`LLM_PROVIDER` unset means `FakeLLM`: deterministic canned JSON, no network, no
key. Tests and a fresh clone therefore run with zero credentials, and nobody
accidentally spends money by running the daemon. Real providers are opt-in via
`LLM_PROVIDER=anthropic|openai`.

### One static system prompt for both planning and synthesis

`llm.SYSTEM_PROMPT` is built once at import and sent byte-identical on every
call; the *user* message carries a `PLAN:` or `SUMMARIZE:` prefix to select the
mode. Splitting it into two prompts would have meant two cache prefixes.

**Known caveat:** the prompt is roughly 250 tokens, and Anthropic's minimum
cacheable prefix is 512–4096 tokens depending on model. The `cache_control:
ephemeral` marker is therefore correct but currently a no-op — nothing is
actually cached yet. It starts paying off once the system prompt grows (adding
scoring rules, category context, few-shot examples). Verify with
`usage.cache_read_input_tokens` before assuming it works.

### Step completion is correlated by task_id, not goal_id

`product_worker` builds a fresh payload for its artifact and does not echo
`goal_id`, but it does preserve `task_id`. Since Phase 2 was required to leave
workers unchanged, the orchestrator assigns deterministic step ids
(`{goal_id}-S{n}`) and keeps a `task_id -> goal_id` map. Tasks still carry
`goal_id` and `depth` in their payload for downstream consumers.

If workers later echo `goal_id`, this map can go away.

### The orchestrator owns goal_id, never the model

`GoalPlan.goal_id` is overwritten with our own id before validation. The model
never sees the real id, so trusting an echoed one would let a bad completion
misroute or collide with another goal.

### MAX_REPLANS is the planning-attempt ceiling

One counter covers both rules: a goal gets at most `MAX_REPLANS` (2) planning
attempts — the first, plus the single retry on invalid output. Exhausting them
publishes `human.approval.required` and drops the goal.

A `MAX_STEPS_PER_GOAL` (5) breach is different: it is a guard breach, not a bad
response, so it escalates *immediately* without burning a retry. Both halt only
the offending goal; other goals keep running.

### Default Anthropic model is claude-opus-5

Set in `llm.DEFAULT_ANTHROPIC_MODEL`, overridable with `LLM_MODEL`.

### LLM_BASE_URL is required for the openai provider

No default endpoint is hardcoded. The OpenAI-compatible path covers several
vendors (Qwen/DashScope among them) whose URLs differ by region, and guessing
one would be inventing configuration. Missing it raises at construction.

### Only the product role has a live worker

`WorkerRole` allows `product`, `engineering` and `landing`, and the router will
happily fan out to `task.assigned.engineering` / `.landing` — but no process
consumes those topics yet, so such steps never produce an artifact and their
goal never completes. `FakeLLM`'s canned plan uses `product` steps only, so the
default demo runs to completion. Add the missing workers before letting a real
model plan across all three roles.

## Phase 1 — Event bus foundation (2026-09-21)

- **Wedge:** home organization category, US market.
- **Monetization:** report-first + API credits; x402 is phase 2.
- **Tooling:** Claude Code in VS Code first; Agent Sessions UI for parallel agents later.
- **Python:** local system Python is 3.9 but the stack requires 3.12. Use
  `/opt/homebrew/bin/python3.12 -m venv .venv`; plain `python3` fails on this
  repo's type hints.
- **Kafka image:** `bitnami/kafka:3.9` is gone from Docker Hub (404) — Bitnami
  retired versioned tags to the `bitnamilegacy` namespace in 2025, so
  docker-compose pins `bitnamilegacy/kafka:3.9`. Verified alternative if that
  namespace is ever pulled: `apache/kafka:3.9.0`, which uses `KAFKA_*` env vars
  instead of Bitnami's `KAFKA_CFG_*`.
- **Stub artifacts:** `product_worker` emits `status: "stub"` with an empty
  `datapoints` list rather than a plausible-looking fake brief. Inventing
  numbers would violate the AGENTS.md sourcing rule and risks a placeholder
  being mistaken for real data later.
- **Benign Kafka errors:** `bus.consume` treats `UNKNOWN_TOPIC_OR_PART` as
  benign alongside `_PARTITION_EOF`. Consumers subscribe before a topic exists
  (auto-created on first produce), which otherwise logged a misleading ERROR on
  every worker startup.
- **Redis-era files deleted:** the original orchestrator/router/worker/logger
  and Dockerfile were written against a Redis bus and broke when bus.py was
  rewritten. Recoverable at commit `30c759c`.
