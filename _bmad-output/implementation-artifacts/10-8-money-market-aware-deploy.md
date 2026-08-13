# Story 10.8: Money-market-aware deploy (count parked cash-equivalents)

Status: review
baseline_commit: 80e53a7

<!-- HARD GATE (docs/dev-loop-policy.md): APPROVED by MasterB 2026-08-13. Core design
     settled: money-market counts as DEPLOYABLE during analysis/planning, but at
     EXECUTION the coach must distinguish money-market from real (settled) cash and
     ensure enough real cash on hand (liquidate MM first, let it settle, then buy) —
     never place a buy it can't actually cover. Money-path → mandatory independent
     review before merge. -->

## Story

As an investor who keeps my cash in a money-market fund (earning yield, not idle),
I want the deploy coach to recognize that money-market balance as deployable,
so that it doesn't falsely tell me "no cash to deploy" while tens of thousands sit in my money-market fund.

## Context & PROVEN problem

Surfaced 2026-08-13 by MasterB's real Schwab account. His setup: **$12,182.82 settlement cash** ("Cash & Cash Investments"), **$93,766.26 in SWVXX** (Schwab Prime Advantage money-market, tagged as his parked money-market), **reserve = $40,000**.

The deploy engine (`allocation/engine.py:build_plan`) computes:
```
ready_to_trade = max(0, view.cash)        = $12,182.82   (settlement cash ONLY)
investable     = ready_to_trade − reserve = 12,182.82 − 40,000 = −$27,817
→ status "no_cash"
```
So the coach says **"no cash to deploy"** while **$93,766 of deployable money-market sits uncounted.** Proven by a repro test (2026-08-13): status == `no_cash`. His mental model — "$105,949 cash+MM, reserved $40k, so ~$65,949 deployable" — is the correct one; the engine's is broken for the money-market case.

Two defects:
1. **Parked money-market is uncounted as deployable.** It's a cash-equivalent the user holds *instead of* idle settlement cash — exactly what Epic 9 was meant to make Ballast aware of (9-2 nags about idle cash vs MM yield; 9-3 liquidates MM to fund buys). The deploy engine ignores it.
2. **The reserve is subtracted from the wrong base** — from settlement cash alone, not from total available (settlement + parked MM).

## Approved design — two phases

**Phase 1 — ANALYSIS / PLANNING: money-market counts as deployable.**
`investable = max(0, settlement_cash + parked_money_market_value − reserve)`, where
`parked_money_market_value` = Σ `market_value` of holdings whose normalized symbol ∈
the user's `cash_config.parked_symbols` (declared funds, e.g. SWVXX — the user opts
in). For MasterB: `max(0, 12,182.82 + 93,766.26 − 40,000) = $65,949.08` deployable.
The reserve is a cushion out of TOTAL available (settlement + parked MM), never
settlement alone.

**Phase 2 — EXECUTION / PURCHASE: distinguish money-market from REAL cash; never buy
what we can't cover.** A deploy buy must only place against **real settled cash**. When
a co-signed buy exceeds `ready_to_trade` (settlement cash), the coach must FIRST
**liquidate the parked money-market** to raise real cash, let it **settle**, and only
THEN place the buy — reusing the Epic 9 Story 9-3 just-in-time liquidation + deferred
buy. **No margin, ever** — only the user's own settlement cash + their own money-market
holdings. The plan/UI must make the money-market-vs-cash distinction visible (e.g.
"$X comes from selling SWVXX; it settles, then the buys place").

### Decisions settled at approval (2026-08-13)
- **MM = deployable in analysis** (Phase 1) — the user's declared `parked_symbols`; user opts in. ✅
- **Reserve out of total** (settlement + parked MM). ✅
- **Funding via 9-3 liquidation** — sell MM → settle → buy; ensure sufficient REAL cash before any placement. ✅ Dev must confirm 9-3 composes with a MULTI-buy deploy plan; if not, add an explicit "sell $X of SWVXX first" step (still the same principle: real cash before buys).
- **Big-move framing** — deploying ~$65,949 is correct per config; the coach must clearly frame "this deploys your money-market cash and keeps your $40,000 reserve untouched" so the number isn't alarming.

### Open sub-question for the dev (design, not blocking approval)
- **Settlement timing.** Schwab money-market funds (SWVXX) generally liquidate same-day; confirm the 9-3 deferred-buy resume behaves acceptably for a MM sale (vs. an equity sale's T+1). The buy must not place until proceeds are genuinely available.

## Acceptance Criteria (draft — finalize at approval)

1. `investable` counts parked money-market value: `max(0, settlement_cash + Σ parked_mm market_value − reserve)`. MasterB's repro flips from `no_cash` to `deploy` with ~$65,949 investable.
2. Reserve is subtracted from total available (settlement + parked MM), never settlement alone.
3. **A buy NEVER places without sufficient REAL settled cash.** A deploy buy exceeding `ready_to_trade` (settlement cash) is funded by first liquidating parked MM and letting it settle (reused 9-3 just-in-time liquidation + deferred buy); the buy places only once real cash covers it. Never margin. Whole-share sizing + populate-don't-submit + never-past-target preserved. (This is the load-bearing safety AC — money-market is deployable *on paper* in Phase 1, but Phase 2 must always have real cash before any order.)
4. A user with NO parked money-market behaves exactly as today (pure additive for the MM case).
5. Honest framing: the plan/narration makes clear it's deploying money-market cash and the reserve stays untouched (10-3 guardrails apply).
6. Per-user scoped (AD-10); Decimal money; the Epic 9 cash-state model stays coherent (ready-to-trade / parked / reserved).

## Tasks / Subtasks

- [x] Task 1 — Phase 1: engine counts parked money-market as deployable (AC: 1, 2, 4)
  - [x] `allocation/engine.py:build_plan` re-imports `parked_market_value`; investable = `max(0, view.cash) + parked_market_value(view.holdings, cash_config) − reserve` (reserve out of TOTAL); added a finiteness guard on the final `investable`. Docstring + comment rewritten (ADDS parked MM). No-parked users unchanged.
- [x] Task 2 — Phase 2: verify the deploy→liquidate composition (AC: 3)
  - [x] Confirmed NO new Phase-2 code needed: the existing 9-3 `plan_liquidation` already triggers for ANY buy > `ready_to_trade`, sets `available_parked = max(parked − reserve, 0)`, sells parked MM, never touches the reserve. Integration test proves a deploy plan's primary buy (> settlement) composes: `needs_liquidation=True`, `coverable=True`, sells SWVXX ≤ (parked − reserve). Phase-1 investable ($65,949) == settlement + available_parked (reserve counted once, no double-count, no buy without real cash).
- [x] Task 3 — Tests (AC: all)
  - [x] +3 tests: the flipped repro (→ `deploy`, `investable_cash == "65949.08"`), untagged-MM-not-deployable (→ `no_cash`), and the Phase-2 liquidation-composition. Updated the superseded Group-B parked test (parked now ADDS → investable $8,000). Full backend suite 841 passed.

## Dev Notes (reuse / touch points)

- `allocation/engine.py:build_plan` — the investable computation (`ready_to_trade`/`reserve`/`investable`, ~:369-395). This is where the parked-MM count + reserve-from-total change lands.
- `cash/config.py:parked_market_value` — already sums Σ market_value of parked holdings (the value to add). (NB: the Group-B review removed its use from the engine as a *subtraction*; this story re-introduces it as an *addition* to deployable — opposite direction, correct this time.)
- `cash/liquidation.py` + `api/cash.py` (Story 9-3 just-in-time liquidation / `PendingBuy`) — the funding mechanism to reuse.
- `allocation/review.py` parked handling + Epic 9 cash states for consistency.
- Testing: `ballast_test` DB only. Flip the repro (`no_cash`→`deploy`, investable $65,949.08) into the AC-1 test.

### References
- [Source: docs/real-money-readiness.md] — the money-market/margin findings from the live account.
- [Source: _bmad-output/implementation-artifacts/epic-9 cash-intelligence] (9-1 cash states, 9-3 liquidation) — the model this composes with.

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (dev-story, in-chat)

### Debug Log References

- RED: the flipped repro failed on the old engine (returned `no_cash`); untagged-MM guard passed.
- Key finding: **Phase 2 required NO new code.** The Epic 9 `plan_liquidation` (9-3) already funds any buy beyond `ready_to_trade` by selling parked MM, drawing `max(parked − reserve, 0)` — exactly matching Phase-1's `investable = settlement + parked − reserve`. So counting parked MM in analysis + the existing liquidation-at-co-sign compose cleanly; the reserve is subtracted once, consistently.

### Completion Notes List

- **Phase 1 (analysis):** `build_plan` now counts the user's declared parked money-market as deployable: `investable = max(0, view.cash) + parked_market_value − reserve` (reserve out of total). MasterB's real setup flips from `no_cash` to `deploy` with $65,949.08 investable. Only `parked_symbols`-declared funds count (untagged SWVXX stays uncounted). Added a finiteness guard so a NaN parked/reserve can't slip past the `investable <= 0` check.
- **Phase 2 (execution):** unchanged code — a deploy buy > settlement cash triggers the existing 9-3 liquidation (sell parked MM → settle → buy), reserve-protected, never margin. Proven by an integration test end-to-end.
- **Money-safety preserved:** populate-don't-submit, whole-share, never-past-target, per-user scoping all intact; the engine stays read-only. Behind the fake broker.
- **Deferred (noted for review):** SWVXX now appears BOTH in the x-ray's unclassified sleeve (as a holding) AND in investable (as deployable cash) — a display nuance worth reconciling later (exclude parked MM from the unclassified sleeve, or label it). Not a correctness issue.

### File List

- `ballast/backend/allocation/engine.py` — `parked_market_value` import; `build_plan` investable = settlement + parked MM − reserve + finiteness guard; docstring/comment.
- `ballast/backend/tests/test_allocation_engine.py` — +3 tests (MM deployable / untagged-not-deployable / deploy→liquidate composition); updated the superseded Group-B parked test.

## Change Log

- 2026-08-13 — Story 10.8 implemented: money-market-aware deploy. Phase 1 counts parked money-market as deployable (`investable = settlement + parked_MM − reserve`); Phase 2 reuses the existing 9-3 liquidation (no new code) to fund buys beyond settlement with real settled cash, reserve-protected, never margin. Flips MasterB's real case from `no_cash` to `deploy` ($65,949.08). +3 tests, backend 841 passed. Money-path → awaiting mandatory independent review before merge.
