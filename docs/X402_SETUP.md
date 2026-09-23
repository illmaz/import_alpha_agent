# x402 — paying for API calls with USDC on Base

Agents that have no account with us can pay per call: send USDC on Base, put
the transaction hash in a header, get a response. No signup, no card, no human.

> **Testnet only.** AGENTS.md: *no wallet custody, no mainnet payments,
> sandbox/testnet only.* `base-mainnet` is **refused at the call site**
> (`x402.assert_testnet`), not merely discouraged. Everything below uses Base
> Sepolia and fake USDC. Switching to mainnet is a human decision — see
> [Going to mainnet](#going-to-mainnet-later), which is deliberately not a
> config change alone.

---

## 1. A Base wallet

You need an address to *receive* USDC. This project never holds a private key
and never signs anything — it only reads the chain to confirm a transfer
happened, so the receiving wallet can live anywhere you like.

**MetaMask**
1. Install the extension, create a wallet, save the recovery phrase offline.
2. Networks → *Add network* → search "Base Sepolia", or add manually:
   - Network name: `Base Sepolia`
   - RPC URL: `https://sepolia.base.org`
   - Chain ID: `84532`
   - Currency: `ETH`
   - Explorer: `https://sepolia.basescan.org`
3. Copy the account address (`0x…`).

**Coinbase Wallet** — create a wallet, enable testnets in Settings, switch to
Base Sepolia, copy the address.

Put it in `.env`:

```
X402_WALLET_ADDRESS=0xYourAddressHere
X402_NETWORK=base-sepolia
```

---

## 2. Testnet ETH and USDC

Two different things are needed, and it is easy to get only the first:

- **Testnet ETH** pays gas. Without it a transfer cannot be sent at all.
- **Testnet USDC** is what actually gets transferred.

| What | Where |
|---|---|
| Base Sepolia ETH | https://www.coinbase.com/faucets/base-sepolia-faucet |
| Base Sepolia ETH (alt) | https://www.alchemy.com/faucets/base-sepolia |
| Base Sepolia USDC | https://faucet.circle.com (choose *Base Sepolia*) |

Circle's faucet is the authoritative source for test USDC, because Circle
issues USDC. Add the token to your wallet if it does not appear:

```
Base Sepolia USDC: 0x036CbD53842c5426634e7929541eC2315fA19Ef0   (6 decimals)
```

Neither faucet costs anything. None of this is real money.

---

## 3. Paying for a call

One credit currently costs **10 000 base units = $0.01 USDC**
(`X402_CREDIT_PRICE_BASE_UNITS`). USDC has 6 decimals, so:

```
$0.01  =    10_000
$1.00  = 1_000_000
```

Send that amount of USDC to `X402_WALLET_ADDRESS` on Base Sepolia, copy the
transaction hash, then:

```bash
curl -i http://localhost:8000/v1/opportunities \
  -H "X-Payment-Hash: 0xYOUR_TRANSACTION_HASH"
```

A first call with a valid, confirmed transfer returns `200` and writes a
purchase row to the ledger. The same hash cannot be used twice.

### Comparing the two ways in

```bash
# Existing customer — API key, credits already bought
curl http://localhost:8000/v1/opportunities \
  -H "Authorization: Bearer ia_your_key_here"

# Agent with no account — pay per call
curl http://localhost:8000/v1/opportunities \
  -H "X-Payment-Hash: 0xYOUR_TRANSACTION_HASH"

# Both? The API key wins and the payment is left untouched.
```

### What a refusal looks like

An unpaid or unusable request gets `402` with everything needed to pay and
retry — no human in the loop:

```json
{
  "error": "payment_required",
  "detail": "no USDC transfer to 0x1111… found in this transaction",
  "accepts": [{
    "scheme": "x402-usdc-transfer",
    "amount_base_units": 10000,
    "asset": "USDC",
    "decimals": 6,
    "network": "base-sepolia",
    "asset_contract": "0x036CbD53842c5426634E7929541eC2315fA19EF0",
    "pay_to": "0x1111111111111111111111111111111111111111",
    "header": "X-Payment-Hash"
  }]
}
```

---

## 4. What is actually verified

`app/services/x402.py` checks, in order:

1. the transaction exists and its receipt reports **success**;
2. it has at least `X402_MIN_CONFIRMATIONS` behind it — a reorg can undo a
   transaction that already looked successful;
3. it contains a `Transfer` log **emitted by the USDC contract**;
4. that transfer's recipient is `X402_WALLET_ADDRESS`;
5. the amount is at least the credit price. Overpaying is fine.

**USDC is an ERC-20 token, so the payment is a contract call, not a value
transfer.** On a real USDC payment `tx.value` is `0` and `tx.to` is the *USDC
contract*, not the seller. Checking those two fields — the obvious approach,
and the one you would use for ETH — would reject every genuine USDC payment
and accept a zero-value call to the seller as if it were payment. The amount
and the real recipient are only in the event log.

Step 3 matters as much as the rest: anyone can deploy a worthless token that
emits an identically-shaped `Transfer` event. Only a log from the real USDC
contract means USDC moved.

---

## 5. Known limitation: a hash is not proof of identity

A transaction hash is **public**. Anyone reading a block explorer can see it.
It proves *a payment happened*; it does **not** prove the caller is the one
who made it.

The mitigation here is that a hash buys exactly **one** credit, **once**, and
is consumed by the request presenting it. A replay is refused with `402`
rather than being allowed to spend down the real payer's balance, and each
payer gets their own account (`x402:0x…`) so one agent's credits are never
pooled with another's.

That closes the theft path but not the race: whoever presents a fresh hash
first gets the credit. The real fix is the x402 spec's signed payment
authorisation (EIP-3009 `transferWithAuthorization`), where the caller proves
control of the paying key rather than merely quoting a public hash. That is
the upgrade path, and it is not built.

---

## Going to mainnet later

**Not a config change.** `base-mainnet` raises `MainnetRefused`, by design.
Turning it on means accepting real money into a wallet this codebase can see,
and AGENTS.md forbids that today. When it is genuinely wanted, all of the
following need a decision from a human, not a default:

1. **Remove the guard deliberately.** Change `assert_testnet` in
   `app/services/x402.py` and update AGENTS.md in the same commit, so the rule
   and the code never disagree.
2. **Fix the price.** The default `$0.01` per credit is ~300–990x below fiat
   pricing (Stripe sells credits at `$2.99`–`$9.90` each). On mainnet that is
   a real loss per call, not a rounding error.
3. **Raise `X402_MIN_CONFIRMATIONS`.** Two blocks is fine for testing. Real
   value warrants more.
4. **Replace hash-as-receipt with EIP-3009**, per section 5.
5. **Decide who holds the receiving key**, and where. This project does not
   and should not.

Mainnet config, for when that day comes:

```
X402_NETWORK=base-mainnet
X402_USDC_CONTRACT=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
# RPC: https://mainnet.base.org
```

Bridging real USDC to Base (Coinbase → *Send* → choose the **Base** network,
or https://bridge.base.org) is only relevant after all of the above.
