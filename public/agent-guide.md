# ImportAlpha Lite — agent guide

For autonomous agents buying sourcing intelligence. No signup form, no browser
step, no human in the loop on your side.

Everything here is fetchable. Start at `/llms.txt` for the map, read
`/v1/agent/info` for live payment parameters, and `/openapi.json` for the
exact request and response shapes.

---

## 1. Read before you spend

Every payload carries a `status`:

| `status`  | Meaning                                                        |
|-----------|----------------------------------------------------------------|
| `ok`      | Observed from a real source.                                   |
| `curated` | Hand-written for development. Never observed anywhere.         |
| `stub`    | Placeholder. Estimates are `null`.                             |

**This deployment currently returns `curated`.** The numbers are plausible and
internally consistent, and they are not measurements. Do not resell them,
quote them to a principal, or make a purchasing decision on them. Every
datapoint additionally carries `source_metadata` with `source_url`,
`observed_at` and `confidence` — a `curated://` URL means exactly what it says.

Free, unauthenticated specimens are at `/v1/public/sample-reports`. Learn the
response shape there before paying for anything.

## 2. Discover

```bash
curl -s https://HOST/llms.txt          # the map
curl -s https://HOST/v1/agent/info     # live payment params, JSON
curl -s https://HOST/openapi.json      # full contract, generated from code
curl -s https://HOST/health            # version, uptime, dependency state
```

`/v1/agent/info` is authoritative for anything that can change — wallet
address, price, which networks are accepted. Do not cache it across a payment.

## 3. Authenticate

Two independent methods. Pick one.

### API key

```bash
curl -s https://HOST/v1/opportunities \
  -H "Authorization: Bearer $IMPORTALPHA_KEY"
```

Keys are issued manually by the operator and draw on a prepaid balance bought
with a card. Check the balance and ledger with:

```bash
curl -s https://HOST/v1/account/transactions \
  -H "Authorization: Bearer $IMPORTALPHA_KEY"
```

### x402 — pay per call, no account

Send a USDC transfer on the accepted network, then present the transaction
hash. One verified transfer grants one credit.

```bash
curl -s -X POST https://HOST/v1/reports \
  -H "X-Payment-Hash: 0xYOUR_TX_HASH" \
  -H "Content-Type: application/json" \
  -d '{"category":"home_organization"}'
```

An `Authorization` header always wins. If you send both, the payment is
**ignored, not consumed** — you are not double-billed, but you also do not get
the credit, so send only one.

## 4. The payment loop

Call without credentials and the server tells you exactly how to pay:

```bash
curl -s -X POST https://HOST/v1/reports \
  -H "Content-Type: application/json" -d '{"category":"home_organization"}'
```

```http
HTTP/1.1 402 Payment Required
```

```json
{
  "error": "payment_required",
  "detail": "...",
  "accepts": [
    {
      "scheme": "x402-usdc-transfer",
      "amount_base_units": 10000,
      "asset": "USDC",
      "decimals": 6,
      "header": "X-Payment-Hash",
      "network": "base-sepolia",
      "asset_contract": "0x036CbD53842c5426634e7929541eC2315fA19Ef0",
      "pay_to": "0x..."
    }
  ]
}
```

The loop:

1. `POST /v1/reports` with no credentials.
2. Read `accepts[0]` from the 402 body — or `payment` from `/v1/agent/info`.
   They carry the same fields deliberately.
3. Transfer `amount_base_units` of USDC at `asset_contract` to `pay_to` on
   `network`.
4. Wait for `min_confirmations` (2 by default; Base blocks are ~2s). A reorg
   can undo a transaction that already reported success, which is why this
   wait exists.
5. Retry the call with `X-Payment-Hash: <tx hash>`.

Each transaction hash is redeemable **once**. Replaying it is rejected.

### What the server checks

- The transfer reached the seller wallet named in `pay_to`.
- The amount is at least `amount_base_units`.
- The asset is USDC at the expected contract — USDC is ERC-20, so the transfer
  is read from the log, not from `tx.value`, which is zero on a token transfer.
- The transaction has `min_confirmations`.
- The hash has not been redeemed before.

Failing any of these returns 402 again with the reason in `detail`.

## 5. Networks

| Network        | Accepted | USDC contract                                |
|----------------|----------|----------------------------------------------|
| `base-sepolia` | **yes**  | `0x036CbD53842c5426634e7929541eC2315fA19Ef0` |
| `base-mainnet` | **no**   | `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` |

This deployment is **testnet only**. A mainnet payment is refused and the funds
are not recoverable by us. Read `networks[].accepted` from `/v1/agent/info`
rather than assuming — it is the field that changes if this ever does.

## 6. Testnet USDC

Base Sepolia ETH for gas, then Base Sepolia USDC:

- Base Sepolia ETH: <https://www.alchemy.com/faucets/base-sepolia>
- Base Sepolia ETH (Coinbase): <https://portal.cdp.coinbase.com/products/faucet>
- Circle testnet USDC: <https://faucet.circle.com/> — select Base Sepolia

You need both: gas in ETH, payment in USDC.

## 7. Endpoints

| Method | Path                            | Auth             | Returns |
|--------|---------------------------------|------------------|---------|
| GET    | `/v1/opportunities`             | required         | Scored opportunities for a category |
| POST   | `/v1/landed-cost`               | required         | Landed cost for unit cost, weight, quantity |
| POST   | `/v1/reports`                   | required, 1 credit | 202 with a `report_id` |
| GET    | `/v1/reports/{report_id}`       | required         | Report status, then the report |
| GET    | `/v1/account/transactions`      | required         | Credit ledger |
| GET    | `/v1/public/sample-reports`     | none             | Three synthetic specimens |
| GET    | `/v1/public/pricing`            | none             | Card pricing tiers |
| GET    | `/v1/agent/info`                | none             | This guide, as JSON |
| GET    | `/health`                       | none             | Version, uptime, dependencies |

`POST /v1/reports` is asynchronous: it returns 202 and a `report_id`, and you
poll `GET /v1/reports/{report_id}` until `status` leaves `pending`. A report
that fails refunds its credit.

## 8. Response shape

An opportunity, trimmed:

```json
{
  "product_id": "HO-bamboo-drawer-organizer",
  "title": "Bamboo drawer organizer",
  "opportunity_score": 74,
  "estimated_unit_cost_usd": { "low_usd": 2.10, "high_usd": 3.50, "currency": "USD" },
  "estimated_landed_cost_usd": 4.35,
  "estimated_margin_pct": 66.5,
  "competition_signal": "moderate",
  "risk_flags": ["low_data"],
  "confidence_score": 0.25,
  "status": "curated",
  "source_metadata": [
    {
      "source_url": "curated://importalpha/home-organization/v1#bamboo-drawer-organizer",
      "observed_at": "2026-09-24T00:00:00+00:00",
      "confidence": 0.25,
      "note": "Curated synthetic value. Not an observation."
    }
  ]
}
```

`/openapi.json` carries the full schemas, including every `risk_flags` and
`competition_signal` value. Parse it rather than pattern-matching this example.

## 9. Errors

| Code | Meaning |
|------|---------|
| 401  | Missing or invalid API key. |
| 402  | Payment required, or the payment failed verification. Body says which. |
| 403  | Valid key, insufficient credits. |
| 404  | Unknown report id. |
| 422  | Request body failed validation. Body names the field. |
| 429  | Rate limited. Back off and retry. |
| 503  | A dependency is down. Check `/health`. |
