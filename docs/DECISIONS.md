# Decisions

Durable architectural decisions and their reasoning. `docs/STATE.md` tracks
what is done; this file records *why* things are the way they are.

## P3.6 — Autonomous outreach, marketing, agent SEO (2026-09-24)

### The approval event is a notification; the decision log is the authority

`scripts/approve.py` emits `human.approval.approved` after a human decides, and
`publisher_worker` consumes it. The consumer takes **only the goal id** from the
event and re-reads everything else — body, recipient, channel — from the outbox,
then checks `approval.is_approved(path, sha256, goal)` against `decisions.jsonl`
before a byte is sent.

Anyone who can reach the broker can produce an event, so an event cannot be what
authorises a message. `decisions.jsonl` is written by the process a human ran,
with the hash of the text they were shown. The forged-event test drives the
publisher with `PUBLISHER_DRY_RUN=false` and real backends attached to prove it:
the refusal is not a convenient flag, it is the log lookup.

### Dry-run is the default, and a key that looks like a sample is not a key

`PUBLISHER_DRY_RUN` is true unless the environment says otherwise, and a backend
is only constructed when dry-run is off *and* its channel's key is real.
`_real()` refuses empty keys and the `dummy` / `your-` / `your_` prefixes, so
copying `.env.example` — which is what everyone does — cannot produce a sender.

A dry run logs the channel, the target, the full body and the budget it would
have charged. Anything less and the log stops being evidence of what would
happen.

### Payment terms are appended by code, before the hash

The model writes the letter; `payment_facts.append_payment_block()` attaches the
x402 terms rendered from `payment_terms()`, the same function
`/v1/agent/info` answers with. Then the bytes are hashed and offered for
approval.

A model asked to paraphrase payment terms will eventually paraphrase one wrong,
and a letter that points an agent at a wallet or price the endpoint contradicts
loses their money or our credibility. Appending before hashing means the text a
human approved is the text that leaves, and it is why there is no "generate the
payment paragraph" tool a worker could misuse. With no wallet configured,
`payment_block()` raises `PaymentNotConfigured` rather than rendering a null
address.

### Two kinds of gate entry, two decision rules

`merge` entries are all-or-nothing — half a repository change is a broken tree.
`publish` entries are decided per file: five prospects are five independent
judgements, and "approve two, reject three" is the normal case. A publish entry
carries `target_path = None` whatever the worker sent, because a draft leaves
via a *recipient*; letting it also name a repo path would give one approved file
two ways out when the publisher only checks one.

The consequence: a partial approve *is* the whole decision. `approve <goal>
--only` two of five queues those two and appends a second log line refusing the
other three, so the batch closes rather than hanging with three drafts in
limbo. `rejected_files` is merged rather than rewritten, because a goal can be
decided more than once — one draft withdrawn first, the rest approved after —
and the earlier refusals are part of the record a human reads back.

### A draft has no target path, and nothing downstream may assume one

Publish entries carry `target_path: None` deliberately: a letter has a recipient,
and letting it also name a repository path would give one approved file two ways
out of the system, only one of which the publisher checks. The consequence was
found live rather than in review — `orchestrator._request_review()` printed its
"REVIEW REQUIRED for …" line with `", ".join(f["target_path"] …)`, raised
`TypeError` on the first outreach goal, and took the daemon's artifact-consumer
thread with it. The manifest for that goal had already been written, so the run
looked half-successful; every goal after it completed quietly with no manifest at
all, and a worker kept drafting into a queue no human could see.

Anything that speaks about a manifest entry now goes through
`orchestrator.destination()`: a merge entry by its target path, a draft by
`channel->recipient`. The `human.approval.required` payload carries it as
`destination` so a consumer never has to derive — and therefore never has to
guess — where a file was headed.

### A flag must not be able to make a decision the human did not make

`approve.py --only` started as a single-value, comma-separated flag. The other
spelling is the obvious one — `--only a --only b` — and argparse keeps the last
value, so the gate approved one letter and recorded the other as a human
**rejection** in an append-only log that exists precisely because those decisions
are not revisitable. Selection flags are now `action="append"` and both spellings
mean the same set; a `--only` that names no path is refused rather than read as
"everything", because the failure it prevents is approving a batch nobody looked
at.

### No worker can send, structurally

`outreach_worker` and `marketing_worker` have two tools — `search_web`,
`read_url` — and no transport import. `app.services.publisher` is imported by
exactly one module, `publisher_worker.py`, and an AST scan over the repo fails
the suite if that ever stops being true. The publisher is a separate container
for the same reason a payment provider gets a different key from the app: a
compromised drafting lane still has no path to a message.

### FakeSearchAPI is the default, and its data says so

Search sits behind a provider interface (`SEARCH_PROVIDER=fake|serper|brave|tavily`)
whose offline default returns a fixture corpus on RFC-2606 `.example` domains, so
the dogfood runs need no network and no key. Every result carries
`confidence=0.25` and `data_class="fixture"`, so a fabricated prospect cannot be
mistaken for a sourced one later — the AGENTS.md rule is that data carries its
provenance, not that a test double has to be identified by reading code.

### Directory submissions have no backend, and say so

A directory listing is a form on someone else's website. `DirectoryFormBackend`
exists so that a live run refuses with instructions instead of logging something
that reads as a completed submission: claiming a listing was filed when it
wasn't is worse than not filing it, and the dry-run log is only useful if it can
be trusted.

## P3.7 — Agent discovery (2026-09-24)

### Publishing this makes the x402 price a public offer

`x402_middleware` has carried a warning since P2.4: one credit costs 10,000
USDC base units ($0.01) over x402, while the card tiers work out at $2.99 to
$9.90 per credit. An agent pays roughly **300x to 990x less than a human for
the same report.**

Until now that was an internal default nobody outside could see. `llms.txt`,
`/agent-guide` and `/v1/agent/info` turn it into a machine-readable offer that
any agent can find and act on at scale, which is the entire point of the
layer — and also what makes the number urgent rather than theoretical. It is
still `X402_CREDIT_PRICE_BASE_UNITS`, still configurable, and still unchanged
here, because pricing is a business decision and quietly "fixing" it inside a
discovery task would be the wrong way to make it. "Decide the x402 price"
moves up the Next Actions list instead.

Testnet-only settlement is the thing currently standing between that price and
real money.

### base-mainnet is advertised as known and refused, not omitted

The brief asked for "supported networks (base-sepolia, base-mainnet)". The code
refuses mainnet — `assert_testnet` raises `MainnetRefused` — and AGENTS.md
forbids mainnet custody, so listing it as supported would invite an agent to
send real USDC for a call that then fails, with no way for us to return it.

Omitting it is no better: an agent that asks about a network we do not mention
learns nothing, and may assume. So `/v1/agent/info` returns every network the
code knows, each with an explicit `accepted` boolean and, when false, a note
saying the funds are not recoverable. `test_every_accepted_network_is_a_testnet`
makes an accepted mainnet a test failure rather than a judgement call.

### Static files do not carry live payment parameters

`llms.txt` is checked in. A wallet address is not: it changes, and a stale one
in a file an agent trusts sends someone's USDC somewhere we do not control.
So the static documents describe the *shape* of payment and point at
`/v1/agent/info` for the values, which reads them from the same configuration
the middleware charges against.

`/v1/agent/info` deliberately mirrors the field names of the `accepts` block in
the 402 challenge. An agent that reads discovery and an agent that reads a
rejection see the same vocabulary, and
`test_agent_info_price_matches_the_402_challenge` pins them together.

### No static public/openapi.json

The brief listed one. FastAPI generates `/openapi.json` from the code on every
request, so a checked-in copy would be wrong the first time a route changed —
and wrong in the worst way, since the thing agents parse to learn the contract
would disagree with the server. The task's own wording ("FastAPI already
generates this") points the same way. What was needed was a test that it is
public and valid, which exists.

### landing_worker drafted the section, because marketing_worker does not exist

The brief named a `marketing_worker`. The roles are `product`, `engineering`
and `landing`, enforced by `events.ACTIVE_ROLES` and `GoalPlan` validation, and
`landing_worker` is already the conversion-focused web-design role that owns
this page. Adding a fourth role would have meant a new worker, a new compose
service and a new topic for one section of copy.

### HEAD, for the third time

`HEAD /llms.txt`, `/agent-guide` and `/v1/agent/info` all returned 404 on first
run — the same defect as `/health` in P4.1. The StaticFiles mount at `/` claims
any request the API routes decline on method, so a 405 becomes a 404.

It matters more here than it did for `/health`: crawlers and agents probe with
HEAD to check a document exists and what type it is before fetching it, so a
discovery manifest that 404s on HEAD tells a machine it does not exist and
discovery stops. Fixed per route again.

Three occurrences is the point at which the per-route fix stops being the right
one. The systemic answer is for the catch-all mount to decline paths an API
route already claimed, so a method mismatch surfaces as 405. That is a change
to P3.1's mount with real regression surface, so it is on the backlog rather
than bolted onto this task.

## P4.1 — Deployment scaffolding (2026-09-24)

### deploy.sh deploys a remote ref, so it must refuse an unpushed HEAD

`deploy.sh` runs `git fetch origin && git reset --hard origin/$BRANCH` on the
server. That is the right design — what lands on the box is a commit you can
name, not whatever was on a laptop — but it has a consequence that bit us the
day after it was written: **the deploy reports success for code that is not
there.**

When the discovery layer shipped, `deploy/` and `public/` existed only in two
local commits. `origin/main` was still the previous day's work, with no
`deploy/`, no `public/` and a `/health` returning bare `{"status":"ok"}`. Run
the deploy in that state and the server would have reset to a tree with no
discovery layer, built cleanly, started, and passed the script's own `/health`
poll — because the health check passes either way. The feature being deployed
is exactly the one that would have gone missing, and the only symptom is that
`https://…/llms.txt` 404s.

So `deploy.sh` now fetches the branch and refuses unless `HEAD` is an ancestor
of `origin/$BRANCH`, printing the commits that would be skipped. The general
rule: when a tool's failure mode is a *silent* success, the fix is a guard that
fails before the work, not a check that runs after it.

`docs/DEPLOYMENT.md` had the same blind spot in prose — its post-deploy
verification listed `/health` and `/` and never the discovery URLs. It now
carries a section that checks GET *and* HEAD on all five, because a manifest
that 404s on HEAD tells a probing agent it does not exist.

### The live-credential guards stay, and deploying does not touch them

The brief asked to document `STRIPE_SECRET_KEY=sk_live_...` and a mainnet
`X402_WALLET_ADDRESS`. Both are refused at the call site today —
`assert_test_mode` raises `LiveKeyRefused`, `assert_testnet` raises
`MainnetRefused` — and AGENTS.md says "No wallet custody. No mainnet payments.
Sandbox/testnet only."

Neither guard was weakened. `docs/DEPLOYMENT.md` documents the *procedure* for
going live as a gated runbook whose first two steps are completing KYC and
amending AGENTS.md, because the rule and the code have to agree before either
changes. Taking real money should cost a deliberate commit that a reviewer can
find, not an edit to a `.env` file on a server.

### /health returns 200 even when degraded

`status` goes to `degraded` and `services.database` says why, but the status
code stays 200. This is a single-VPS deployment: a 503 would pull the only node
out of rotation, and a degraded API still serves the landing page and the
public endpoints, which is strictly better than serving nothing. Monitors are
told to alert on the body. On a multi-node deployment this should be revisited.

The database is probed with `SELECT 1` because an unreachable SQLite file is
the failure most likely to be silent. Kafka deliberately is not probed: the API
neither produces nor consumes, so a broker outage does not make this process
unhealthy, and dialling a broker on every probe would make the check slow.

### Three bugs that only surfaced by running it

Config that parses is not config that works. Each of these passed review and
failed the first real request.

**nginx `add_header` does not merge across levels.** A location that sets any
`add_header` silently discards every one inherited from the server block. The
landing page — the single most-visited URL — was the only page served without
`X-Content-Type-Options`, `X-Frame-Options` and `Referrer-Policy`, because its
location sets `Cache-Control`. They are now repeated inside that block.

**The Stripe webhook path was wrong, and wrong silently.** The config
special-cased `location = /webhooks/stripe`; the route is
`/v1/webhooks/stripe`. The exemption matched nothing and webhooks fell into the
rate-limited `/v1/` zone — so under load Stripe would have been 429'd, and
because Stripe retries, a payment that had already succeeded would have been
credited late rather than never. The failure mode is a delay, which is exactly
the kind that reaches production unnoticed.

**`HEAD /health` returned 404.** The catch-all StaticFiles mount at `/` claims
any request the API routes decline on method, turning a 405 into a 404 — so a
monitor probing with HEAD, which many do by default, would have reported the
service missing while it was perfectly healthy. `/health` now declares GET and
HEAD.

### Memory limits are ceilings, not reservations

They sum to more than the droplet's 2 GB on purpose. Measured idle usage of the
whole stack is 767 MiB (Kafka 449 of it). Sizing each limit to its fair share
of RAM would make every service OOM-prone during normal spikes, while the point
of the limits is to stop one runaway container taking the box down.

## P3.0 — Workers with brains and hands (2026-09-24)

### The lane writes code and copy. People write data.

This is the line, and it is not a stylistic preference.

The lane may author HTML, CSS, JS, Markdown and prose. It may not author the
numbers we sell. Everything this product promises rests on `SourceMetadata` —
source_url, observed_at, confidence — and on `ResultStatus.CURATED` meaning
"hand-written by a person, never observed". A worker that can emit a fixture
can quietly become the origin of figures we tell buyers are hand-checked, and
nobody would be able to tell afterwards which numbers a human chose.

Enforced in three places, deliberately overlapping:

- `worker_core.DELIVERABLE_RE` does not match `.json`, so a step cannot even
  name a data file as its deliverable.
- `tools.FORBIDDEN_SEGMENTS` refuses any workspace path containing `fixtures`.
- `approval.PROTECTED_TARGET_PREFIXES` refuses `data/fixtures` at the moment
  of merge, which is the only check that matters if the other two are ever
  edited away.

The P3.1 fixtures remain the curated source. A worker wanting different sample
data files a request with a human; it does not write one.

### Capabilities the worker does not have cannot be prompted into existing

`app/services/tools.py` offers read, write and list, confined to
`data/work/<goal_id>/`. There is no network tool and no shell tool — not
disabled, absent. Prompt injection cannot talk a worker into using a tool that
was never passed to it, and the page content a worker writes is untrusted text
from a model, so the smaller the surface it can act on, the less there is to
reason about.

Escapes raise `SandboxViolation` and log, rather than clamping to a safe path:
a worker that believes it wrote `../../.env` and silently wrote
`work/G-1/.env` instead is a worker whose artifacts lie.

**The model supplies content; the code supplies the path.** `extract_deliverable`
parses the target from the *step text*, never from model output. A model that
tries to redirect its own output has nowhere to put the instruction.

One known limit: a step writes exactly one file, and the target path equals the
workspace path. Multi-file changes need a second pass.

### Merging is a separate act from producing

A completed goal that wrote files emits `human.approval.required` with a
manifest, and stops. `scripts/approve.py` shows a unified diff against what the
repository serves today, and only `approve` copies anything onto a real path.

Properties worth keeping:

- **Re-validated at merge.** Targets are checked again in `approve()`, not only
  when the manifest was written. The manifest is a file on disk and could have
  been edited in between.
- **Content-addressed.** Each entry carries a sha256, re-checked before the
  copy, so a workspace edited after review cannot ride in on an old approval.
- **All or nothing.** Every entry is validated and read before any file is
  written, so a poisoned second entry cannot leave the first applied.
- **Logged.** `data/work/decisions.jsonl` records goal, decision, targets,
  hashes and timestamp — and `reject` logs before it deletes.

### `artifact_review` is not an escalation, and the liveness invariant still holds

The approval request rides `human.approval.required`, the same topic as stall
and plan-failure escalations, because it needs the same pair of eyes. It is
told apart by `reason`.

This does not weaken the P2.5 invariant. A goal still terminates exactly once,
as `completed`; the review request is a downstream consequence of a goal that
*finished*, not a fourth terminal state. Escalations halt a goal that could not
finish. A `reason` of `artifact_review` always follows a `goal.completed` for
the same goal id.

### What the first live run taught (and why the gate earned its keep)

Three things only showed up once a real model was driving.

**The planner never named a file.** Asked to rewrite the landing page,
gpt-4o-mini produced five steps of the shape "Design the layout and structure
of the landing page". `extract_deliverable` parses the target from step text,
so none of them produced a file and the goal delivered nothing — it completed
cleanly, with prose. FakeLLM's canned plan had always named the path, which is
exactly the class of gap an offline fixture hides. `llm.SYSTEM_PROMPT` now
states that a file-producing step must name its exact relative path, and after
that the same goal planned one step and wrote the file.

**A worker will quietly hardcode what it should fetch.** The first accepted
page inlined $99/$299/$999 as literals. They were right that day, and would
have silently gone wrong the first time `PRICE_TABLE` changed — the precise
drift `/v1/public/pricing` exists to prevent. The landing role's house rules
now forbid hardcoding prices and name the endpoint.

**The live page was rejected, and should have been.** gpt-4o-mini's attempt
called `data.forEach` on both endpoints, which return objects (`{tiers: […]}`,
`{reports: […]}`) — so pricing and samples would both have rendered empty. It
also invented field names that do not exist (`option.price`,
`report.productName`, `report.opportunityScore`) and advertised "Real-time
pricing insights" over data the API explicitly labels `curated`, plus a © 2023
on a page written in 2026. A broken, overclaiming page shipped to strangers is
the failure this whole phase exists to prevent, and `reject` cost one command.

The lesson for the backlog is not "use a better model". It is that a worker
writing a client for our own API is doing it blind: it has no way to see the
response shapes. Letting a worker read the target file and the relevant schema
before writing is the fix, and it is a bigger change than a prompt tweak.

### A near miss: `lstrip` strips characters, not prefixes

`validate_target` normalised `./x` with `target_path.lstrip("./")`. That strips
every leading `.` and `/` *character*, so `.git/config` became `git/config` and
`.env` became `env` — both sailing past the protected-prefix check that existed
specifically to stop them. Caught by the parametrised protected-target test,
which is the argument for testing each protected path by name rather than
asserting the list is non-empty.

## P3.1 — Landing page and sample reports (2026-09-24)

### Published samples are labelled synthetic, in the payload and on the page

`ResultStatus.CURATED` is documented as "nothing marked CURATED may be quoted
to a customer", and the original fixture carries "Do not quote, publish or
sell." A public landing page is the most customer-facing surface there is, so
publishing curated numbers unlabelled would have broken the one rule this
project has held since P0.

The resolution is that the samples are published as **specimens of the report
format**, not as intelligence. Each file carries `status: "curated"`,
`data_class: "curated_synthetic"` and a `disclaimer` field; the page renders an
amber banner above the tables and repeats the disclaimer under each one. Every
`source_url` is a `curated://` URI, never an http(s) link that would imply an
observation, and `confidence_score` is 0.25 throughout.

This is the honest version of the marketing ask: a buyer can see exactly what
they would receive, in full fidelity, without being shown a single number that
pretends to be measured. `tests/test_landing.py` asserts the labelling so it
cannot be quietly dropped — if these ever become sourced data, that is a
deliberate change to those tests, not a silent edit to a JSON file.

### The $999 tier is not in PRICE_TABLE

CONTEXT.md offers three prices and the page shows three, but only two are
self-serve. `stripe_service.PRICE_TABLE` drives `/v1/billing/checkout`, so
adding a $999 "custom category feed" entry would have made it immediately
purchasable — taking real money for a bespoke feed that no code delivers.

`/v1/public/pricing` therefore composes its response: the two self-serve packs
are read from `PRICE_TABLE` (so the page can never quote a price checkout
would refuse) and the bespoke tier is appended from `CUSTOM_FEED_TIER` with
`self_serve: false`. The page renders "Contact us" rather than "Get started"
for it, and omits the "one-time" qualifier, because an ongoing feed is not a
one-time purchase.

### The static mount is registered last

`app.mount("/", StaticFiles(...))` matches every path no earlier route claimed.
Registered before the routers it would shadow the entire API. The ordering in
`app/main.py` is load-bearing and
`tests/test_landing.py::test_static_mount_does_not_shadow_the_api` pins it.

### Each compose service builds its own image

`docker compose run --rm pytest` reported 411 passing while the host reported
435. The `pytest` service has its own image (`import_alpha_agent-pytest`), and
rebuilding only `fastapi` left it holding a tree with no `tests/test_landing.py`
in it. Rebuild every service that matters after adding files, or the test run
silently grades an older commit. This is the same stale-image trap recorded
under P2.3 fixes.

## P2.4 — x402 USDC payments on Base, testnet only (2026-09-23)

### USDC is ERC-20, so the payment is in the logs, not in `tx.value`

The brief described verifying `to == recipient` and `value == amount`. That is
how you verify an **ETH** transfer, and it is wrong for USDC. On a real USDC
payment:

    tx.value == 0                  # no native ETH moved
    tx.to    == the USDC contract  # NOT the seller

Implemented literally, that check would reject every genuine USDC payment —
and, worse, accept a zero-value call to the seller as if it were one. The
amount and the real recipient exist only in the
`Transfer(address,address,uint256)` event the token contract emits, so the
verifier decodes receipt logs.

The log must also come **from the USDC contract address**. Anyone can deploy a
worthless token that emits an identically-shaped Transfer event; without that
check, paying in a token you minted yourself would work.

### Confirmations, because a successful transaction can still be undone

A receipt reporting success is not final — a reorg can remove the block.
`X402_MIN_CONFIRMATIONS` (default 2) is the cheapest defence and costs seconds
on Base. It should be raised before real value is involved.

### A transaction hash is public: it proves payment, not identity

This is the structural weakness of hash-as-receipt, and it cannot be fully
fixed at this layer. Anyone reading a block explorer can copy a hash and
present it as their own.

Two mitigations, both departures from the brief:

**Per-payer accounts, not a shared `x402-agent`.** One shared account would
pool every agent's credits, so whoever called next would spend whatever the
last payer had left. The account id is derived from the paying address
(`x402:0x…`), which the Transfer log already provides.

**A re-used hash is refused with 402, not silently accepted.** A hash buys one
credit, once, and is consumed by the request presenting it. Allowing a replay
would let a stranger spend the real payer's balance — the exact theft the
public-hash problem enables.

That closes the theft path but not the race: whoever presents a fresh hash
first gets the credit. The real fix is the x402 spec's signed authorisation
(EIP-3009 `transferWithAuthorization`), where the caller proves control of the
paying key. Not built; recorded in docs/X402_SETUP.md.

### Mainnet is refused in code, not discouraged in prose

AGENTS.md: *no wallet custody, no mainnet payments, sandbox/testnet only.*
`assert_testnet()` raises `MainnetRefused` on `base-mainnet`, so enabling real
money is an edit to the guard plus an edit to AGENTS.md in the same commit —
never a config flag that silently starts accepting value.

This project holds no private key and signs nothing. The wallet address is a
receiving address only; every chain call is read-only.

### The x402 price is ~300–990x below fiat, and is left visible rather than fixed

`X402_CREDIT_PRICE_BASE_UNITS` defaults to 10 000 base units = **$0.01** per
credit, as specified. Stripe sells the same credit at **$2.99** (api_credits)
or **$9.90** (report_pack). An agent therefore pays two to three orders of
magnitude less than a human for identical work.

That is a pricing decision, not a bug in this code, so it is not silently
"corrected" here — but it is not hidden either: the constant carries the
comparison in a comment, `.env.example` repeats it with the aligned values
($2.99 = 2 990 000, $9.90 = 9 900 000), and X402_SETUP.md lists it as a
prerequisite for mainnet. On testnet it costs nothing. On mainnet it is a real
loss per call.

### `.lstrip("0x")` is not how you remove a hex prefix

Found while checking the log decoder: `"0x0df2…".lstrip("0x")` returns
`"df2…"` — lstrip removes *every* leading `0` and `x`, so any topic or hash
beginning with a zero nibble loses it. `HexBytes.hex()` also returns a
prefixed string in some web3 versions and a bare one in others, so the prefix
cannot be assumed present or absent.

Both are handled by `x402._hex()`, which strips an actual `0x` prefix and
nothing else. It is tested with a leading-zero value that the old code would
have mangled.

## P2.3 fixes — stale images, Stripe v12 objects, real landing pages (2026-09-23)

### The stale-image trap: `--force-recreate` does not rebuild

Three correct fixes were applied to the host and appeared to do nothing,
because the running container held code from the previous day:

    image created:             2026-09-22T17:58:35Z
    host webhooks.py modified: 2026-09-23 12:07:52

`docker compose up -d --force-recreate <svc>` recreates the **container** from
the **existing image**. The Dockerfile copies source at build time, so a host
edit reaches a container only after `docker compose build`. Recreating without
building reruns yesterday's code with complete confidence.

This is the second time this trap has cost us — the first was P1 Part 2, where
`docker compose run --rm pytest` reported 65 passing tests against code that
had 141, because profile-gated services are not rebuilt by `up --build`
either. The failure mode is the same both times: **the system reports success
for code that is not running.**

The habit that catches it, before changing anything else:

    docker compose exec <svc> grep -n "<the line you just changed>" <file>

If the old line is still there, the edit never shipped. Rebuild with
`docker compose build <svc> && docker compose up -d`, and remember
`docker compose build pytest cli` for the profile-gated services.

### stripe-python v12+: StripeObject is not a dict

`stripe.Event` and every nested `StripeObject` stopped subclassing `dict`.
`.get()` now raises:

    AttributeError: 'get' is a dict method, but a Event is not a dict.
    Use .to_dict() to convert it.

This applies to nested values too: `session.metadata` is itself a
StripeObject, so `metadata.get("account_id")` fails exactly as the outer call
did — which is why a first round of fixes to the top-level event was not
enough. Even `dict(session.metadata)` raises; it needs `.to_dict()`.

Fields are now read with `getattr(obj, name, None)`, which returns None for an
absent key rather than raising AttributeError.

### Dict fixtures let this ship green, so the fixtures changed

`tests/test_stripe.py` built events as plain dicts. Plain dicts support both
`.get()` and (via the handler's attribute access) nothing — so the suite
exercised a code path that could not fail the way production did. **25 tests
passed while the first real webhook 500'd.**

Fixtures are now built with `stripe.Event.construct_from(...)`, producing real
StripeObjects. Verified by reverting the handler to `event.get("id")`: **12
tests fail**, where previously all passed. `test_fixtures_are_real_stripe_objects_not_dicts`
asserts the fixtures are not dicts and that `.get()` raises on them, so a
future rewrite back to dicts fails loudly instead of silently restoring the
blind spot.

The general lesson: a mock that is more permissive than the real object tests
nothing about the boundary it stands in for.

### "Example Domain" was a placeholder, not a broken checkout

After paying, the browser landed on example.com and the checkout URL looked
broken — but the `evt_` in the tunnel proved payment had completed. The
`success_url` was `https://example.com/billing/success`, a placeholder from
P2.3. Stripe did exactly what it was told.

There are now real pages at `/billing/success` and `/billing/cancel`
(`app/billing_pages.py`), unauthenticated because a redirected browser carries
no API key, and `PUBLIC_BASE_URL` overrides the host for tunnels or
deployment. `success_url` carries `?session_id={CHECKOUT_SESSION_ID}`, which
Stripe substitutes.

The success page deliberately does **not** say the credits have arrived. They
are added by the webhook, a separate request that may land a moment after the
redirect; claiming otherwise would be the one lie that page could tell.

## P2.3 — Stripe Checkout, test mode (2026-09-22)

### The signature is the credential; metadata is never an amount

`POST /v1/webhooks/stripe` is the only unauthenticated route in the API, and
it grants credits. Stripe cannot send an API key, so the HMAC signature on the
payload is the entire trust boundary. Nothing in the body is read until
`stripe.Webhook.construct_event` has verified it — an unverified payload is
forged until proven otherwise, and is discarded before any field is touched.

It lives on its own router in `app/api/v1/webhooks.py` rather than on the v1
router, which authenticates everything by default. An unauthenticated route
has to be a visible, deliberate choice, not an exemption buried in a decorator.

After verification, metadata is still only *identifiers*: `account_id` (who to
credit) and `pack_id` (which pack). **The credit count is never read from the
payload.** It is re-derived from `PRICE_TABLE[pack_id]` server-side, so a
session created with tampered or stale metadata cannot mint credits.

There is a second check on top: the amount Stripe actually charged is compared
against the pack's price, and a mismatch is refused. The pack lookup fixes how
many credits a pack is worth; this fixes what that pack costs. Without it, a
session whose price was altered after creation would still grant a full pack.
`test_amount_mismatch_is_rejected` charges 1 cent for the $99 pack and asserts
nothing is credited.

### Idempotency needed a database constraint, not a check

The plan was "if a ledger row with this event id exists, do not credit". That
was implemented, tested — and then tested *concurrently*, where it failed:
six simultaneous deliveries of one event all passed the check and credited
four to six times.

The reason is SQLite's locking. A write lock is taken at the first *write*, so
several connections can each run the SELECT, each see nothing, and each then
insert. Check-then-insert inside a transaction is not atomic against a
concurrent writer.

The fix is a partial unique index on `credit_transactions.reference` where
`reason = 'purchase'`, added in migration `e8cad44c3fec`. `record_purchase`
catches the resulting `IntegrityError` and reports "already recorded", which
is the same outcome as the in-transaction check firing — that check is kept as
the fast path. The index is partial because only purchases carry a globally
unique reference; a topup's reference is a free-text note and may repeat.

Stripe retries a webhook until it gets a 2xx, and a retry after a slow but
successful first delivery is ordinary traffic. Double-crediting here is
double-paying a customer.

### 200 for events we ignore, 400 for events we cannot act on

An event type we do not handle returns 200 with `handled: false`. A 4xx would
make Stripe retry, with backoff, an event we will never act on.

A *verified* event we cannot act on — unknown pack, missing metadata, price
mismatch — returns 400, so it surfaces in the Stripe dashboard rather than
being silently dropped. That distinction matters: the first is noise, the
second is a bug or an attack and should be visible.

### A live key is refused at the call site

`assert_test_mode()` raises on any key starting `sk_live_`. This codebase has
no business charging a real customer, and "we were careful" is not a control.
It fails where the key is used, not in review.

### The $999 custom feed is not a pack

`PRICE_TABLE` holds `report_pack` ($99 / 10 credits) and `api_credits`
($299 / 100 credits). The custom category feed from CONTEXT.md is deliberately
absent: it is a bespoke engagement priced per customer, and making it
self-serve would sell something we have not agreed to deliver. It is granted
with `billing.adjust_balance()` after the scope is settled, and a test asserts
no pack costs 99900.

**Known mismatch with CONTEXT.md:** it describes api_credits as "$299/month".
This is implemented as a one-time 100-credit purchase, because P2.3 is
one-time payments only. Recurring billing is a separate piece of work, and
the pricing page should not promise a subscription until it exists.

## P2.2 — Append-only ledger and universal refund (2026-09-22)

### The ledger landed before Stripe, deliberately

A bare integer balance cannot answer "why is my balance 3". While credits are
granted by hand that is an annoyance: we can reconstruct what happened from
logs, and if we get it wrong we hand out a few free reports.

The moment money buys credits, the same question becomes a dispute. A customer
who paid and believes they were charged twice needs an answer that is not "our
integer says 3". A chargeback needs evidence. A refund needs to be
distinguishable from a top-up. None of that is reconstructable after the fact —
the history has to have been written at the time.

So the ledger is a prerequisite for Stripe rather than a follow-up to it, and
P2.2 took the slot Stripe originally had. Retrofitting history onto money that
has already moved means a permanent gap in the record.

### `_apply()` is the only writer of a balance

Every credit movement — charge, refund, topup, purchase, adjustment — goes
through one function that writes the `CreditTransaction` row and updates
`CreditAccount.balance` **in the same database transaction**. There is one
place to audit, and the cache cannot disagree with the ledger even if the
process dies mid-write.

`require_funds=True` keeps the conditional UPDATE from P2.1, so the
insufficient-funds check and the write remain a single atomic statement and
two concurrent spenders still cannot both take the last credit. A failed spend
writes no ledger row at all — a phantom entry would be worse than no entry.

### The balance column stays, as a cache

`SUM(delta)` is the truth; `balance` is a denormalised copy so the hot path
(402 checks on every report) is one indexed read rather than an aggregate.

`reconcile()` compares them. It should never return False — they are written
in one transaction — so if it ever does, that is evidence of something real,
and the response is to trust the ledger and write a compensating adjustment.
Editing the cache by hand would destroy the evidence of what went wrong.

`test_reconciles_after_a_randomized_mixed_sequence` runs 120 randomized mixed
operations and asserts the two agree after **every** step, not just at the end.

### Append-only is enforced in review, not by the database

Nothing in the service layer updates or deletes a ledger row; a correction is
a new compensating row, which is what `adjustment` exists for.
`test_no_production_path_updates_or_deletes_a_ledger_row` asserts this against
the module's own source.

That is a real limitation, stated plainly: SQLite has no row-level permissions
here, so anything holding a connection can still rewrite the table. The
invariant is a discipline the tests protect, not something the storage
enforces. Postgres later can enforce it properly with a revoked UPDATE/DELETE
grant and a trigger.

`clear_ledger()` is the single DELETE in the codebase and exists only so tests
start from an empty table. It is named to be unmistakable.

### Refunds key on the status, never on the reason

P2.1's gap was not that timeouts were unhandled — it was that the refund was
attached to *specific* failure reasons, so any failure mode nobody had
enumerated silently kept the customer's money.

The rule is now: whoever wins `settle_if_pending(... STATUS_FAILED ...)`
refunds, full stop. `reason` is recorded on the ledger row and branched on
nowhere. `test_any_failure_reason_refunds` drives reasons that appear nowhere
in the codebase (`malformed_artifact`, `downstream_unavailable`, and the empty
string) to prove a future failure mode is covered by default rather than by
remembering to add a branch.

### The history endpoint takes no account_id

`GET /v1/account/transactions` derives the account from the API key. Reading
someone else's ledger is not a permission check that could be implemented
wrongly — it is unrepresentable. A test passes `?account_id=` for another
account and asserts the scope does not move.

### The migration needed a backfill that autogenerate could not know about

Creating the table was clean autogenerated DDL. But existing accounts had
balances with no rows behind them, so `reconcile()` would have reported every
one of them as corrupt on day one: cached N against SUM 0.

The migration therefore seeds one `adjustment` row per non-zero account,
referenced `pre-ledger opening balance`. It is honest about what it is — we
cannot reconstruct the history that produced those credits, only state where
the ledger begins. Zero-balance accounts get nothing, because SUM over no rows
is already 0.

This is the second migration in a row where reading the generated file before
applying it was what caught the problem.

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
