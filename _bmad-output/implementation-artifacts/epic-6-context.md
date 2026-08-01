# Epic 6 Context: Go Live — Real Broker & LLM Integration

<!-- Generated from planning artifacts. Regenerate with compile-epic-context if planning docs change. -->

## Goal

Epic 6 retires the deferred integration risk carried out of Epics 4–5: it first hardens the money and email seams against concurrency, then wires the real Anthropic (LLM) and Schwab (broker) adapters behind the gates and ports the earlier epics already built — so the Coach operates against live services instead of fakes. It matters because everything to date has been proven only against test doubles; going live is where phantom/duplicate orders, double-sends, race conditions, and unhardened external-API failure modes become real-money and real-trust hazards. Scope stays v1: buying/holding a small set of broad index funds/ETFs. All existing trust invariants — structural teeth on advice, single-writer state ownership, per-user isolation, never-a-dead-end, and the calm/honest/never-alarmist voice — must continue to hold unchanged. A hard sequencing rule governs the epic: **no real credential may reach a placement or send path until the concurrency-hardening story (6.1) lands**, and live Schwab placement (6.3) is explicitly gated on 6.1.

## Stories

- Story 6.1: Atomic decision claim & idempotency hardening (GATING)
- Story 6.2: Live LLM Gateway enablement & hardening
- Story 6.3: Live Schwab placement & reconciliation mapping
- Story 6.4: Fixed-point money serialization pass
- Story 6.5: Real Schwab balances & cash-only mapping (AD-14)
- Story 6.6: Decisions history scale hardening
- Story 6.7: Durable cross-request timeout reconciliation

## Requirements & Constraints

- **No trade without explicit approval; single execution path.** Every trade follows propose → user-approve → Coach Engine → Broker Port → reconcile → persist outcome. No module places orders out of band, and no phantom or duplicate orders may ever occur.
- **Execution must be reconciled and truthful.** Order rejection, partial fills, and timeouts are handled and clearly reported; after any order the user always sees the true resulting state as reconciled against Schwab — never an optimistic assumption.
- **Approval→placement integrity.** If the Schwab session/token expires between approval and placement, the system must not silently place a stale or partial order; it re-establishes a live session and re-confirms intent before placing. Execution requires a live session; reads/coaching may continue in a degraded mode.
- **Idempotent placement.** Every order carries a stable, persisted client idempotency key established at proposal time and reused across placement retries, so a timeout can never place a second order. A DB unique constraint backs the key.
- **Recommendations stay structurally trustworthy.** The live LLM must emit the validated Recommendation schema via structured output; reasoning, ≥1 real cited evidence record, and stated uncertainties are mandatory. Malformed, refused, or timed-out LLM responses degrade to the always-valid default plan — never a dead-end. The LLM may cite only evidence IDs it was handed; it never computes or recalls a statistic.
- **Money correctness on the wire.** All monetary values (amount, filled quantity, average price) are fixed-point decimals; no binary float and no exponent (`E+`) notation may cross the wire, and values must round-trip cleanly through the documented decimal consumer.
- **Security & isolation unchanged.** Brokerage OAuth tokens remain app-layer-encrypted secrets decrypted only in-memory at use; per-user data isolation via the fail-closed scoped repository holds; secrets and raw tokens are never logged.
- **Honest cash reality.** Idle cash must be sourced from a dedicated balances source (not inferred from a holdings row) so cash-only/cash-heavy accounts report truthfully.
- **Bounded history at scale.** Decision history reads must stay fast and bounded (pagination + supporting index) with a retention policy for never-co-signed proposed records; per-user isolation and verbatim replay stay intact.

## Technical Decisions

- **Hexagonal edges.** External dependencies (broker, LLM, market data) sit behind ports; the Coach Engine depends only on interfaces, and concrete adapters (SchwabAdapter, Anthropic client) are swappable and injectable — the same injection pattern used to mock the client for offline proof.
- **Single owner per concern.** Coach Engine is the sole writer of decision records; LLM Gateway is the sole caller of the Anthropic API; Broker Port is the sole path to brokerage state. No module reaches around an owner. Prompt assembly and citation-validity checks live in the Coach Engine, not the LLM Gateway.
- **Broker Port contract.** The port exposes a normalized `OrderOutcome {status: filled|partial|rejected|timeout|pending, filled_qty, avg_price, broker_ref}` plus a status-lookup by idempotency key. Adapters must map broker-native order-status JSON and status strings into this normalized shape and enum; transport/SDK errors map to an indeterminate `timeout` status with no raw exception leaking through the port and no phantom fill.
- **Schwab placement specifics (v1 decisions, locked 2026-08-01):**
  - *Dollar→share sizing = whole-share MARKET order.* Order intent amount is a dollar notional, but the Schwab SDK (schwab-py 1.5.1) exposes only share-quantity equity builders. At placement, fetch a quote and compute `quantity = floor(amount / ask)`, then place a whole-share market order. If the amount buys less than one whole share, refuse calmly with a clear message and place no order. No fractional shares, no notional/dollar orders.
  - *No-`order_id` timeout = surface `pending`, never guess.* On an in-request indeterminate placement, reconcile once via an in-instance idempotency-key→order-id cache plus a direct order lookup. On a true timeout with no order id, surface `pending` and never auto-search or attribute-match recent orders. `broker_ref` is persisted as a **queryable column** (not only inside the co-sign snapshot JSON) so a later explicit reconcile can find the order.
- **Authenticated client construction.** The Schwab trading client is built from the decrypted stored token with no disk or network access at construction, and the account hash is resolved before placing.
- **Model routing.** The LLM Gateway applies deterministic model routing (a stronger model for flagged hard-reasoning cases, the default model otherwise) and enforces structured output.
- **Immutable decision record.** On approval the blessed Recommendation (with its evidence + uncertainties snapshot) is persisted immutably with a schema version; co-sign is that record and replay is reading it back — no feature re-derives or mutates it.
- **Portfolio cache is a single-writer projection.** The broker is authoritative; on any conflict a fresh broker reconciliation wins over optimistic local writes.
- **Conventions.** UUID primary keys; ISO-8601 UTC timestamps; money as decimal/minor-units never binary float; structured logs that never contain secrets; market data only via the Precedent Engine (never yfinance in production).

## Cross-Story Dependencies

- **6.1 is gating for the whole epic and specifically blocks 6.3** — no real credential may reach a placement or send path until the atomic-claim + idempotency hardening lands. 6.1 also folds in the digest double-send guard (from Epic 5).
- **6.3 depends on 6.1** for the persisted idempotency key and includes a NULL-idempotency-key pre-flight guard carried over from 6.1's deferred work.
- **6.5 depends on 6.3** (real Schwab balances become available only after the live broker adapter is wired).
- **6.7 is carved out of 6.3**: 6.3 surfaces `pending` in-request and persists `broker_ref` as a queryable column; 6.7 makes an ambiguous placement recoverable *across* requests without a broker-honored idempotency key.
- **6.2 and 6.3** share the adapter-injection/mocked-client pattern for offline proof, with a one-time live paid call as a manual go-live step behind real credentials.
- Several stories close specific deferred-work items from Epics 4–5 (concurrency, double-send, money formatting, cash-only mapping, decisions pagination/index/retention).
