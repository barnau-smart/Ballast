# Story 10.13: Align the deploy engine's margin-debit clamp with the liquidator

Status: review
baseline_commit: f6c01b0

<!-- HARD GATE (docs/dev-loop-policy.md, per-story-spec-approval): APPROVED by MasterB 2026-08-13. MONEY-PATH (analysis honesty). Low-severity + near-zero blast radius (only
     margin-DEBIT accounts change; normal accounts identical). Independent review before merge. -->

## Story

As an investor whose account is temporarily in a margin *debit* (I owe a little margin),
I want the deploy plan's "investable" figure to account for that debt,
so that it doesn't over-promise a deploy amount the liquidation can't actually free after covering what I owe.

## Context — the clamp asymmetry (10-8 review MED, deferred)

The deploy engine and the 9-3 liquidation treat a NEGATIVE settlement balance (a margin debit)
differently:

- `allocation/engine.py:build_plan` — `ready_to_trade = max(0, view.cash)` → **clamps** a negative
  balance to 0 (ignores the debt).
- `cash/liquidation.py:plan_liquidation` — `ready_to_trade = view.cash` → uses the **raw** balance
  (the debt reduces spendable cash).

On a margin-DEBIT account (`view.cash < 0`) they diverge, and the engine's clamp is the wrong one —
it **overstates** deployable by the debt amount. Worked example (`view.cash = −$5,000`, parked MM
$50,000, reserve $10,000, single fund):

- Engine (clamped): `investable = max(0,−5000) + min(50000, 50000−10000) = 0 + 40000 = $40,000`.
- Reality: total assets = −5000 + 50000 = $45,000; keep $10,000 reserve → deployable = **$35,000**.
- The (already-correct, raw) liquidation, asked to fund $40,000: `shortfall = 40000 − (−5000) =
  $45,000` but available parked above reserve is only $40,000 → **NOT coverable**. So the plan
  over-promises by exactly the $5,000 debt.

Story 10-9 (execution refuses a buy beyond real settled cash) + 10-10 (margin warning) already
backstop the *money* safety; this is the remaining **analysis-honesty** fix so the plan figure
matches what the liquidation can actually free. Only margin-debit accounts are affected — a normal
account (`view.cash ≥ 0`) is byte-identical (raw == `max(0,·)`), so near-zero blast radius.

## Design — RECOMMENDED (engine-only; aligns TO the already-correct liquidation)

Use the **raw** `view.cash` as the investable base, so a margin-debit balance correctly reduces
investable (matching `plan_liquidation`). Keep the funding split honest and never-negative for
display:

```
investable      = view.cash + min(largest_parked, parked_total − reserve)   # raw settlement
settlement_cash = max(0, view.cash)                                          # never shown negative
from_money_market = investable − settlement_cash                             # split still sums
```

- **Normal account (`view.cash ≥ 0`): unchanged** — `raw == max(0,·)`, `settlement_cash == view.cash`,
  `from_money_market == min(largest_parked, parked_total − reserve)` exactly as today.
- **Margin-debit (`view.cash < 0`):** `investable` is reduced by the debt → the plan never promises
  more than the liquidation frees (`shortfall == min(largest_parked, parked_total − reserve) ≤
  available_parked`, always coverable). `settlement_cash` shows `0` (nothing spendable now) and
  `from_money_market == investable` (all deployable cash nets from selling money-market, after the
  proceeds cover the debt). A deeper debt drives `investable ≤ 0 → no_cash` (correct).
- The split invariant `settlement_cash + from_money_market == investable_cash` still holds; the
  narration's never-invent-a-number allow-set (Story 10.8 AC5) is unaffected (it already admits all
  three). No change to `cash/liquidation.py` (it's already raw/correct) and none to Story 10-9's
  execution gate (which reads the real cached cash independently).

**Rejected:** clamp the liquidation to `max(0, view.cash)` too — that would make BOTH ignore the
debt, re-introducing a small margin overstatement (the plan would deploy $40k when only $35k is
debt-free) that 10-9 would then have to refuse at execution. Aligning to the raw (correct) side is
the honest fix.

## Acceptance Criteria

1. On a margin-DEBIT account (`view.cash < 0`), `investable_cash = view.cash + min(largest_parked,
   parked_total − reserve)` — reduced by the debt; the resulting figure is fully coverable by the
   9-3 liquidation. **G/W/T:** Given `view.cash = −$5,000`, parked $50,000, reserve $10,000, When the
   plan is built, Then `investable_cash == $35,000` (not $40,000), and `plan_liquidation` for that
   amount is `coverable`.
2. A normal account (`view.cash ≥ 0`) is UNCHANGED — `investable_cash`, `settlement_cash`,
   `from_money_market`, and the deploy items are byte-identical to today (existing tests stay green).
3. The funding split still sums (`settlement_cash + from_money_market == investable_cash`);
   `settlement_cash == max(0, view.cash)` (never negative on the wire); `from_money_market ≥ 0`.
4. A margin debit large enough that `investable ≤ 0` yields `no_cash` (never a negative deploy).
5. Money is `Decimal`; per-user scoped; read-only (no placement/writes); 10-3 narration guards intact.

## Dev Notes

- `allocation/engine.py:build_plan` (~:472-482) — change `ready_to_trade = max(_ZERO, view.cash)` so
  the investable base is the raw `view.cash` (keep the finiteness guard). Update the `settlement_cash`
  / `from_money_market` split to `settlement_cash = max(_ZERO, view.cash)` and
  `from_money_market = investable − settlement_cash`. Update the stale "clamped ≥ 0 — a negative
  margin balance is not investable" comment (that was the bug).
- `cash/liquidation.py` — NO change (already raw/correct); the engine aligns to it.
- `coach/execution.py` (Story 10-9 gate) — NO change (reads the real cached cash independently).
- Tests: a margin-debit deploy (`view.cash < 0`) → `investable` reduced by the debt + coverable by
  `plan_liquidation`; the split sums with `settlement_cash == 0`; a deep debt → `no_cash`; a normal
  account is unchanged (regression). Seed a negative `portfolio_balance.cash` (Numeric allows it).

### References
- [Source: _bmad-output/implementation-artifacts/10-8-money-market-aware-deploy.md#Independent Review Findings] — the negative-settlement clamp-asymmetry MED (deferred there).
- [Source: ballast/backend/allocation/engine.py#build_plan] · [Source: ballast/backend/cash/liquidation.py#plan_liquidation]
- [Source: docs/dev-loop-policy.md] — per-story spec-approval + independent review before merge.

## Dev Agent Record

### Agent Model Used

_(to be filled by dev)_

### Completion Notes List

### File List

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (bmad-dev-story, in-chat)

### Completion Notes List

- **`allocation/engine.py:build_plan`** — the investable base is now the RAW `view.cash`
  (`investable = view.cash + min(largest_parked, parked_total − reserve)`), so a negative
  margin-debit balance correctly reduces investable (aligns with the raw-cash 9-3
  `plan_liquidation`; the engine's `max(0, …)` clamp was the overstatement bug). The funding
  split stays honest + never-negative: `settlement_cash = max(0, view.cash)`,
  `from_money_market = investable − settlement_cash` (invariant `settlement + from_mm ==
  investable` preserved). Stale "a negative margin balance is not investable" comment corrected.
- **No change** to `cash/liquidation.py` (already raw/correct) or `coach/execution.py` (10-9 gate
  reads the real cached cash independently).
- **Normal accounts (`view.cash ≥ 0`) are byte-identical** (raw == the clamp) — the entire
  existing suite is unaffected. Only margin-DEBIT accounts change. Backend 888 passed (885 + 3).
- `followup_review_recommended: true` — money-path (analysis honesty); mandatory independent review.

### File List

- `ballast/backend/allocation/engine.py` — raw-`view.cash` investable base; honest never-negative split; comment fix.
- `ballast/backend/tests/test_allocation_engine.py` — +3 margin-debit tests (investable reduced + honest split; coverable by liquidation; deep debit → no_cash).

## Change Log

- 2026-08-13 — Story 10.13 implemented: the deploy engine now uses the RAW settlement balance
  as the investable base (aligning with the raw-cash 9-3 liquidation), so a margin-DEBIT account's
  investable is reduced by the debt and never over-promises a deploy the liquidation can't cover.
  The funding split stays honest + never-negative (`settlement_cash = max(0, view.cash)`). Normal
  accounts unchanged (byte-identical). +3 tests, backend 888 passed. Money-path → awaiting
  mandatory independent review before merge.
