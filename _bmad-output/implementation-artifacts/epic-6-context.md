# Epic 6 Context: Go Live — Real Broker & LLM Integration

<!-- Generated from planning artifacts. Regenerate with compile-epic-context if planning docs change. -->

## Goal

Epics 1–5 built the full Coach loop against fakes; Epic 6 retires the deferred integration risk and turns on real services. It first hardens the money and email seams against concurrency, then wires the real Anthropic (LLM) and Schwab (broker) adapters behind the existing trust gates so the Coach proposes, executes, and reconciles against live APIs. This is the "go live" step: no real credential may reach a placement or send path until the concurrency-hardening story lands. Scope stays v1 (broad index funds/ETFs), and every trust invariant established earlier — structural validation teeth, sole-writer decision records, fail-closed per-user isolation, never-a-dead-end fallback, and the calm/honest/never-red voice — must still hold unchanged.

## Stories

- Story 6.1: Atomic decision claim & idempotency hardening (GATING)
- Story 6.2: Live LLM Gateway enablement & hardening
- Story 6.3: Live Schwab placement & reconciliation mapping
- Story 6.4: Fixed-point money serialization pass
- Story 6.5: Real Schwab balances & cash-only mapping
- Story 6.6: Decisions history scale hardening

## Requirements & Constraints

- **Concurrency safety is gating.** A double-approval or overlapping digest run must be structurally impossible, not merely unlikely: exactly one caller wins an atomic decision-status claim (proposed→cosigning→cosigned), and the digest send marker advances exactly once per week. Prove this with tests that exercise the true in-flight overlap window, not just sequential re-runs.
- **Idempotency across placements.** A stable per-decision idempotency key must be persisted at proposal time and reused across every placement attempt, backed by a DB unique constraint, so a timeout/retry can never double-place an order.
- **Real adapters behind existing gates only.** The live LLM and broker adapters must pass through the same validation gate, provider/evidence-match checks, placement-time integrity check, and v1-scope enforcement already built — turning on real services changes the adapter, never the gate.
- **Never a dead-end.** Live-LLM failure modes (timeouts, malformed output, refusals) must degrade gracefully to the strategy-backed default plan; they must never surface an unbacked recommendation or a hard error to the user.
- **Truthful reconciliation.** Live Schwab responses map to the normalized order outcome (status ∈ filled/partial/rejected/timeout/pending, filled_qty, avg_price, broker_ref); indeterminate placements reconcile exactly once via the persisted idempotency key; no phantom or duplicate order is ever possible.
- **Money on the wire reads as plain decimals.** All serialized money values (amount, filled_qty, avg_price) go through a shared fixed-point formatter so no exponent (E+) notation can cross the wire, and values round-trip cleanly through the documented decimal-from-string consumer.
- **Real idle cash.** For all-cash / cash-heavy accounts, idle cash must come from a dedicated balances source (not inferred from a holdings row), so the missed-growth meter and the oversized-lump warning operate on true portfolio value.
- **Decisions history must scale.** The decisions list endpoint must be paginated and index-backed by (owner_id, co_signed_at); never-co-signed proposed records need a retention/pruning policy — with per-user isolation and verbatim replay unchanged.

## Technical Decisions

- **Atomic claim mechanism:** conditional `UPDATE … WHERE status='proposed'` gated on `rowcount==1` (or `SELECT … FOR UPDATE`); digest marker via conditional `UPDATE … WHERE last_sent_week IS DISTINCT FROM :week` gated on `rowcount==1`. A unique index on `idempotency_key` enforces single placement.
- **Ports & adapters:** external LLM, broker, and market-data dependencies sit behind ports with swappable adapters. Epic 6 flips selection via config (`LLM_ADAPTER=anthropic`, `BROKER_ADAPTER=schwab`) rather than editing call sites.
- **LLM Gateway is the sole Anthropic caller,** enforcing structured output and deterministic model routing (Opus 4.8 for flagged hard-reasoning, Sonnet 4.6 otherwise). The Anthropic SDK is an optional dependency enabled only when the real adapter is selected.
- **Single execution path** remains `propose → approve → Coach Engine → Broker Port → reconcile → persist`; the broker is authoritative and reconcile-wins. Decision records stay immutable and carry a `schema_version` for replay durability; the Coach Engine remains their sole writer.
- **Portfolio cache** is a single-writer, broker-authoritative projection; the cash-only fix sources idle cash from broker balances rather than a synthesized holdings row.
- **Data access is fail-closed and per-user scoped;** background/system jobs (digest, ingestion) run under an explicit SYSTEM scope.

## Cross-Story Dependencies

- **Story 6.1 is gating and blocks 6.3.** No real credential may reach a placement or send path until 6.1 lands; the persisted idempotency key it introduces is what 6.3 relies on for exactly-once reconciliation.
- **Story 6.5 depends on 6.3** (real Schwab balances are only available once the live broker path works).
- **Story 6.2** is independent of the broker stories and can proceed in parallel once 6.1's shared hardening is in place.
- Epic 6 closes several Epic 4/5 deferred-work items: 4.9 concurrency + 5.1 double-send (6.1), 4.1 real-adapter hardening (6.2), 4.6/4.7 money-format (6.4), AD-14 cash-only gap (6.5), and 4.9/4.10 pagination/index/retention (6.6).
