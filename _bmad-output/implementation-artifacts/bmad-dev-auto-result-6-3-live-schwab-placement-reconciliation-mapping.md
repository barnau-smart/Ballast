---
status: blocked
story: 6-3-live-schwab-placement-reconciliation-mapping
blocking_condition: intent gaps
created: '2026-08-01'
---

# BMad Dev Auto Result — Story 6.3 (Live Schwab placement & reconciliation mapping)

Status: **blocked**
Blocking condition: **intent gaps** — two non-deferrable product / live-trade decisions must be made before a Ready-for-Development spec can be written. Planning did not fantasize them into the spec.

## Why this halted (not premature)

Deep investigation is complete: broker port + adapters, the propose→approve→place→reconcile pipeline, Story 6.1's persisted idempotency key + atomic claim, the schwab-py 1.5.1 SDK surface, architecture ADs (AD-7/AD-11/AD-13), PRD FR8/FR9/FR22/FR23, and the deferred-work ledger. Most of the story **is** implementable offline behind a mocked schwab-py client (mirroring Story 6.2's pattern). But two mechanisms sit on the story's critical path and are genuinely undecided in the codebase — inventing them would move real money on a policy no human approved.

## The two blockers

### 1. Dollar → share order sizing (PRODUCT DECISION — blocks the core `place_order`)
- **Evidence:** `OrderIntent.amount` is a `Decimal` **dollar notional** (`ballast/backend/coach/recommendation.py:41-51`). The fake proves this by dividing: `filled_qty = order_intent.amount / FAKE_FILL_PRICE` (`ballast/backend/brokers/fake_adapter.py:158`). schwab-py 1.5.1 exposes only **share-quantity** equity builders — `equity_buy_market(symbol, quantity)` / `equity_sell_market(...)` (verified via `schwab.orders.equities`); there is **no** notional builder.
- **Gap:** There is no quote→share conversion and no sizing/rounding policy anywhere in `coach/`, `brokers/`, `strategy/`, or `marketdata/`. A developer cannot construct the Schwab order without one, so this is not a deferrable robustness item — it is the story's central act.
- **Decision needed from MasterB:** How does a dollar `amount` become a Schwab order?
  - **(A, recommended for v1)** Whole-share **market** order: fetch a placement-time quote (Schwab `get_quote`), `quantity = floor(amount / ask)`, refuse with a calm message if `amount < one share`. Simple, safe, no fractional edge cases; leftover cash (< 1 share) stays uninvested — acceptable for broad ETFs. Requires wiring a quote read into `coach/execution.py`.
  - **(B)** Fractional shares — needs a different Schwab order type (not the plain equity builder) and raises min-fractional / rounding rules; more surface, more real-money edge cases.
  - **(C)** Notional/dollar order — **not supported** by schwab-py 1.5.1 equity builders; would require hand-rolling a raw order spec against an unverified Schwab capability. Not recommended.

### 2. Timeout reconciliation against a keyless broker API (LIVE-TRADE SAFETY DECISION)
- **Evidence:** AD-13 states "every order carries a client idempotency key; retries reuse it so a timeout never places a second order," and `BrokerPort.get_order_status(idempotency_key)` looks up **by that key** (`ballast/backend/brokers/port.py`). Schwab's REST order API accepts **no** client idempotency key and echoes none back (`place_order(account_hash, order_spec)` returns the id only via the `Location` header / `Utils.extract_order_id`). The reconcile path (`coach/execution.py:_reconcile`) calls `get_order_status(idempotency_key)` once on an indeterminate (timeout/pending) placement.
- **Gap:** In-request reconcile is fine — the adapter can cache `idempotency_key → order_id` in-instance (exactly as the fake's `self._orders` does) and re-read via `client.get_order(order_id, hash)`. The unresolved case is a **true placement timeout with no `order_id`**: to honor "no duplicate order" on real money, the adapter must decide whether the order landed. Schwab offers no key echo, so this means attribute-matching recent orders (`get_orders_for_account`) — a safety-critical, fuzzy decision. Durable **cross-request** reconciliation also needs `broker_ref` as a **queryable column**; today it lives only inside the `cosign_snapshot` JSON (`coach/decision_record.py:280`), not a column.
- **Decision needed from MasterB:** On a no-`order_id` timeout, do we (recommended) **surface `pending` and never auto-search/guess** — persisting `broker_ref` as a new queryable column and reconciling on an explicit user/status action — or authorize an attribute-based order-search matcher (higher double-place risk)? This likely warrants splitting durable reconciliation into its own story.

## What IS ready (offline-provable, no blockers) — for when the two decisions land
- Construct an authenticated schwab-py trading `Client` from the **decrypted** stored token via `schwab.auth.client_from_access_functions` (no disk, no network at construction); resolve the account hash via `client.get_account_numbers()`. Injectable so tests use a mocked client (the Story 6.2 pattern).
- Map Schwab order-status JSON → normalized `OrderOutcome{status, filled_qty, avg_price, broker_ref}`; map Schwab status strings → the `OrderStatus` enum; wrap httpx/SDK transport errors → `OrderStatus.TIMEOUT` (indeterminate) with **no raw exception leaking the port** and **no phantom fill**.
- Add the **NULL-`idempotency_key` pre-flight guard** — refuse to place a decision whose persisted `idempotency_key` is `None` (converts a post-fill crash into a pre-fill refusal). This is deferred-work item from Story 6.1 explicitly scheduled "before go-live / Story 6.3."
- The existing gates (placement-time session integrity + provider-match, `coach/execution.py:130-143`; v1 index-core scope, `strategy/index_core.py` / `execution.py:145-154`) are adapter-agnostic and already fire — unchanged.
- All ACs provable offline with a mocked schwab-py client; the one-time **live** paid placement against real Schwab is a manual go-live step behind real `SCHWAB_*` creds + a live session (a credentials + real-money decision), exactly as Story 6.2 treated its live LLM call.

## Recommendation
Resolve the two decisions above (recommended: **1(A) whole-share market via placement-time quote** + **2 surface `pending`, persist `broker_ref` column, no fuzzy auto-matching**), then re-run the loop. If sizing (1) is confirmed as **(A)**, the story is a clean single story. If durable cross-request timeout reconciliation (2) is wanted beyond in-request pending, consider carving it into a follow-up so 6.3 stays shippable. Use `/bmad-loop-resolve 6-3-live-schwab-placement-reconciliation-mapping` (or just answer the two questions) to unblock.

No source code, spec, or ledger files were modified by this run.
