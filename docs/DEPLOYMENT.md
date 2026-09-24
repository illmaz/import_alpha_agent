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

It refuses to run with uncommitted changes, then on the server: fetches and
hard-resets to `origin/main`, backs up `data/reports.db` (keeping the last 10),
builds, runs `alembic upgrade head`, starts the stack, prunes dangling images,
and polls `/health` — exiting non-zero and dumping API logs if it does not come
up.

Then check it from outside:

```bash
curl https://api.example.com/health
open https://api.example.com/
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
