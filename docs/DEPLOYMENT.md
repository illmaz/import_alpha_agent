# Deployment

One VPS, Docker Compose, nginx. No Kubernetes, no orchestrator, no CI.

Everything below has been validated by running `deploy/docker-compose.prod.yml`
locally behind the real nginx config with a self-signed certificate. **No VPS
has been provisioned** — see "What is not verified" at the end before trusting
any of it in production.

---

## 1. Provision the droplet

DigitalOcean → Create → Droplet.

| Setting | Value |
|---|---|
| Image | Ubuntu 22.04 LTS |
| Plan | Basic / Regular. The **$12/mo** tier was 2 GB RAM, 1 vCPU, 50 GB at time of writing — confirm current specs, DigitalOcean changes them |
| Region | Nearest your buyers |
| Authentication | **SSH key**, not a password |
| Hostname | `importalpha-prod` |

2 GB is enough. The full stack measured **767 MiB idle** across all eleven
containers on the identical compose file — Kafka's JVM is 449 MiB of that, and
`KAFKA_HEAP_OPTS` caps it. The headroom goes on `docker compose build`, which
is the memory-hungriest thing that happens on the box; `setup.sh` adds 2 GB of
swap for exactly that spike.

Add a DigitalOcean weekly backup ($2.40/mo on this tier) or you are one
`rm -rf` away from losing the ledger. The SQLite file lives in `data/`.

### The same thing from the CLI

Creating a droplet spends money on your account, so it is a human action
regardless of whether it is a click or a command. `doctl` is not installed in
this checkout and **none of these commands have been run** — they are transcribed
from the `doctl` reference, and the slugs in particular must be confirmed against
your account before you paste them.

```bash
brew install doctl            # or https://docs.digitalocean.com/reference/doctl/
doctl auth init               # PAT from cloud.digitalocean.com/account/api/tokens

# Confirm before assuming. DigitalOcean renames and reprices these, and the
# $12/mo tier is not guaranteed to map to any particular slug.
doctl compute size list
doctl compute image list --distribution | grep 22.04
doctl compute ssh-key list    # you need the fingerprint below

doctl compute droplet create importalpha-prod \
  --image ubuntu-22-04-x64 \
  --size s-2vcpu-2gb \
  --region nyc3 \
  --ssh-keys <fingerprint> \
  --enable-backups \
  --enable-monitoring \
  --wait

DROPLET_IP=$(doctl compute droplet get importalpha-prod --format IP --no-header)
echo "$DROPLET_IP"
ssh "root@$DROPLET_IP"
```

`--enable-backups` adds the backup fee to the monthly cost; drop it if you plan
to rely on `deploy.sh`'s pre-deploy copies, which live on the same disk and so
do **not** protect against a lost droplet.

## 2. Point DNS at it

Create an **A record** before running `setup.sh` — certbot validates over HTTP
against the name, so the name has to resolve first.

| Type | Host | Value | TTL |
|---|---|---|---|
| A | `api` (or `@` for the apex) | the droplet's IPv4 | 300 |

Wait for it to propagate, and check from somewhere that is not your laptop:

```bash
dig +short api.example.com     # must print the droplet IP
```

A 300 s TTL keeps a mistake cheap. Raise it once things are stable.

Or from the CLI, once you have `$DROPLET_IP` from step 1:

```bash
doctl compute domain records create example.com \
  --record-type A --record-name api \
  --record-data "$DROPLET_IP" --record-ttl 300
```

`--record-name api` is the subdomain label only — pass `@` for the apex.
Confirm it resolved before moving on: `dig +short api.example.com`.

## 3. Run setup.sh on the server

```bash
ssh root@<droplet-ip>
git clone <your-repo-url> /opt/importalpha
cd /opt/importalpha
./deploy/setup.sh api.example.com ops@example.com
```

It installs Docker, creates a `deploy` user in the `docker` group, adds 2 GB of
swap, enables `ufw` (22, 80, 443 only), installs unattended security upgrades,
obtains the first Let's Encrypt certificate, and substitutes your domain into
`deploy/nginx.conf`.

It is idempotent — re-running it is safe.

> `ufw` is enabled **after** OpenSSH is allowed. If you edit that ordering, you
> will lock yourself out of the droplet and the only way back in is the
> DigitalOcean web console.

## 4. Put the production `.env` on the server

Never committed, never copied by these scripts. Create it by hand at
`/opt/importalpha/.env`, starting from `.env.example`:

```bash
ssh deploy@api.example.com
cd /opt/importalpha
cp .env.example .env
nano .env          # fill in the values below
chmod 600 .env
```

Minimum for a real deployment:

```ini
# --- model ---------------------------------------------------------------
LLM_PROVIDER=openai              # spends money on every goal
LLM_API_KEY=sk-...
LLM_MODEL=gpt-4o-mini
LLM_BASE_URL=https://api.openai.com/v1

# --- payments: TEST MODE ---------------------------------------------------
STRIPE_SECRET_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...

# --- agent payments: TESTNET ------------------------------------------------
X402_NETWORK=base-sepolia
X402_WALLET_ADDRESS=0xYourBaseSepoliaAddress

# --- public URLs -------------------------------------------------------------
PUBLIC_BASE_URL=https://api.example.com
```

`LLM_PROVIDER=openai` means the agent lane spends real money per goal. Leave it
`fake` until you want that; `./scripts/toggle_llm.sh status` says which is set.

## 5. Deploy

From your laptop, against a clean commit:

```bash
DEPLOY_HOST=deploy@api.example.com ./deploy/deploy.sh
```

It refuses to run with uncommitted changes **and with a `HEAD` that is not on
`origin/main`** — the server hard-resets to the *remote* branch, so a commit
that exists only on your laptop does not get deployed, it gets silently left
behind. Then on the server: fetches and hard-resets to `origin/main`, backs up
`data/reports.db` (keeping the last 10), builds, runs `alembic upgrade head`,
starts the stack, prunes dangling images, and polls `/health` — exiting
non-zero and dumping API logs if it does not come up.

Then check it from outside:

```bash
curl https://api.example.com/health
open https://api.example.com/
```

### Confirm the agent discovery layer is actually public

This is the point of the deployment: an agent has to be able to find us with no
key and no signup. Check all four routes, with GET **and** HEAD — agents and
crawlers probe with HEAD to see whether a document exists before fetching it,
and a HEAD that 404s tells the machine the manifest is not there:

```bash
for p in /llms.txt /agent-guide /openapi.json /v1/agent/info /health; do
  printf '%-18s GET %-3s HEAD %s  %s\n' "$p" \
    "$(curl -s -o /dev/null -w '%{http_code}'  https://api.example.com$p)" \
    "$(curl -s -o /dev/null -w '%{http_code}' -I https://api.example.com$p)" \
    "$(curl -s -o /dev/null -w '%{content_type}' https://api.example.com$p)"
done
```

Expect `200 200` on every line, and these content types: `text/plain`,
`text/markdown`, `application/json`, `application/json`, `application/json`.
Then read the manifest the way an agent does and confirm the payment terms it
advertises match what the server will actually charge:

```bash
curl -s https://api.example.com/llms.txt | head -20
curl -s https://api.example.com/v1/agent/info | jq '.payment, .networks'
```

`/v1/agent/info` is the live source of truth for wallet, price and network; the
static documents deliberately carry no wallet address, so a stale checked-in
address can never misdirect an agent's funds. Confirm `base-mainnet` still
reports `"accepted": false` — that is the guard that keeps us from taking real
USDC, and it is enforced by a test.

`/health` must report `version`, `uptime_seconds` and per-service state:

```json
{"status":"ok","version":"0.1.0","uptime_seconds":84.9,
 "services":{"api":"ok","database":"ok"}}
```

## 6. Going live with payments — read before you do it

This codebase **refuses live credentials at the call site**, deliberately:

- `stripe_service.assert_test_mode()` raises `LiveKeyRefused` on any
  `sk_live_` key.
- `x402.assert_testnet()` raises `MainnetRefused` on any mainnet network.

Both refusals are enforced by tests. They are not configuration — you cannot
switch them off with an environment variable, and that is the point: taking
real money is a decision someone makes on purpose, not one a stray `.env` line
makes for you.

`AGENTS.md` currently states **"No wallet custody. No mainnet payments.
Sandbox/testnet only."** Going live therefore means amending that rule first.
It is a project decision, not a deployment step, and nothing in `deploy/`
performs it.

When you do decide to go live, in this order:

1. **Complete Stripe KYC** and confirm the account can accept live charges.
2. **Amend `AGENTS.md`** to record that live payments are now permitted, and
   why. Without this the codebase and its rules disagree.
3. **Change the guard deliberately**, in its own commit, with its own test
   change — `assert_test_mode` is where a reviewer will look.
4. **Rotate the webhook secret**: the live-mode signing secret differs from the
   test one. Register the endpoint at `https://api.example.com/v1/webhooks/stripe`
   in the live dashboard and copy the new `whsec_...` into `.env`.
5. **Make one real £1-equivalent purchase yourself**, confirm the credit lands,
   then refund it. Verify the ledger with
   `python scripts/manage_accounts.py reconcile`.
6. **Only then** advertise it.

For x402, mainnet additionally means the wallet in `X402_WALLET_ADDRESS` takes
custody of real USDC from this codebase. Nothing here is reviewed or insured
for that. Keep `base-sepolia` until that changes.

## 7. Monitoring

**Health.** `GET /health` answers GET and HEAD, returns 200 with:

```json
{"status":"ok","version":"0.1.0","uptime_seconds":84.9,
 "services":{"api":"ok","database":"ok"}}
```

`status` is `degraded` when a dependency is down, but the code stays **200**.
On a single box a 503 would take the only node out of service when a degraded
API can still serve the landing page and the public endpoints — so alert on the
body, not the status code:

```bash
curl -s https://api.example.com/health | jq -e '.status == "ok"'
```

Point any uptime service (UptimeRobot's free tier is enough) at
`https://api.example.com/health` and alert on non-200 or on the string
`degraded`.

**Logs.** Capped at 10 MB × 3 files per container, so they cannot fill the disk.

```bash
cd /opt/importalpha
docker compose -f deploy/docker-compose.prod.yml logs -f --tail 100 fastapi
docker compose -f deploy/docker-compose.prod.yml logs -f orchestrator
docker compose -f deploy/docker-compose.prod.yml ps
docker stats --no-stream
tail -f /var/log/nginx/importalpha.access.log
```

**Certificates.** The certbot package installs a systemd timer that renews
twice daily; the deploy hook reloads nginx afterwards.

```bash
systemctl list-timers | grep certbot
certbot renew --dry-run
```

**Disk.** `df -h`, and watch `data/` plus `backups/`.

## 8. Rate limits

nginx limits per client IP: 30 r/s for the site, 10 r/s for `/v1/` (both with a
burst of 20), returning **429** beyond that. `/health` is unmetered so a monitor
cannot throttle itself into a false alarm, and `/v1/webhooks/stripe` is exempt
because Stripe retries on 429 and a throttled webhook delays crediting a
payment that already succeeded.

Measured: 60 rapid requests to `/v1/public/pricing` gave 27×200 and 33×429.

## 9. Rollback

```bash
ssh deploy@api.example.com
cd /opt/importalpha
git log --oneline -10
git checkout <last-good-sha>
docker compose -f deploy/docker-compose.prod.yml up -d --build
```

Migrations are not auto-reversed. If the bad deploy migrated the schema, restore
the pre-deploy copy from `backups/` before starting the old code.

## 10. What is *not* verified

Being explicit, because everything above reads like it has been done:

- **No VPS has been provisioned and no domain exists.** `setup.sh` and
  `deploy.sh` have been syntax-checked and their logic reviewed, but neither
  has run against a real server. Expect to debug them on first use — most
  likely the certbot step, which depends on DNS having propagated.
- **Every `doctl` command in this document is unexecuted.** `doctl` is not
  installed here, so the flag spellings come from the CLI reference and the
  `ubuntu-22-04-x64` / `s-2vcpu-2gb` / `nyc3` values are plausible rather than
  observed against an account. Run the three `list` commands before the
  `create`.
- **The TLS config has only been tested with a self-signed certificate.** OCSP
  stapling is enabled but unverifiable without a real chain.
- **HSTS is commented out** in `nginx.conf`. Turn it on only after unattended
  renewal has succeeded once, because a browser that has seen the header will
  refuse plain HTTP for the whole `max-age` if the certificate later lapses.
- **Stripe was verified in test mode only**, including through this exact nginx
  config: a real `cs_test_...` Checkout Session was created end to end. Live
  mode is refused by the code, as above.
- **No backup restore has been rehearsed.** Taking backups you have never
  restored is a way of feeling safe, not being safe.
- **x402 has never settled a real on-chain transfer.** The 402 challenge and the
  RPC path work, but the ERC-20 log decoder is covered by fixtures only. See
  docs/X402_SETUP.md.

### Two memory numbers, on purpose

`docker stats` on the box will not agree with `docker stats` on a laptop. The
767 MiB figure is the **production** stack, where `KAFKA_HEAP_OPTS` caps the JVM
at 512 MB. The development compose file has no cap, and measured 1035 MiB with
Kafka alone at 722 MiB. Neither is wrong; only the first describes the droplet.
Re-measure on the server and correct both here.
