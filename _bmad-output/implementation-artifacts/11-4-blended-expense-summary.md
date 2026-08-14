---
baseline_commit: 8eadb2e
---
# Story 11.4: Whole-portfolio blended expense summary

Status: done

<!-- Epic 11 (Fiduciary-Grade Portfolio Review), story 4 of 4. INFORMATIONAL-ONLY —
     proposes NO order → NON-money-path (like 11.1/11.3). No spec gate. Independent
     review before merge (it touches the review module). -->

## Story

As a beginner,
I want one plain dollar figure for what I pay in fund fees across my whole account each year,
so that abstract percentages become a real recurring cost I can act on.

## Context

Story 10.4's cost bucket fires per-fund on a switchable near-duplicate. But beginners don't feel fees until they see **one dollar number for the whole account, every year**. "0.55% blended" is abstract; "$240 this year, and again next year" changes behavior — and it catches death-by-a-thousand-cuts (several mediocre-fee funds, none individually switch-worthy). This story adds an **informational** blended-cost summary. It reuses the Story 10.4 expense-ratio table (`strategy/expense_ratio.fund_cost`) — the same real published net ERs that already back the never-invent gate. **Informational-only (no order) → non-money-path (same lane as 11.1/11.3).**

## Acceptance Criteria

1. **Blended-cost detector, informational.** A pure function computes, over holdings whose symbol has a known ER (`fund_cost` — funds only; individual stocks have no entry): the **dollar-weighted blended ER** = `Σ(mv_i × er_i) / Σ(mv_i)`, the **annual $ cost** = `Σ(mv_i × er_i/100)`, and the **fee coverage** = `Σ(priced-fund mv) / total portfolio value`. Produces an informational result; **no `OrderIntent` ever**.
2. **Honest coverage — never imply free.** The copy states the blended ER + annual $ "across the funds I can price" AND the coverage % (what share of the portfolio had a known ER), so unknown-ER holdings (individual stocks, unlisted funds) are never implied to be fee-free. Never-invent: every ER comes ONLY from the table; every number is detector-computed.
3. **Threshold + boundaries.** Fires only when the blended ER exceeds `BLENDED_ER_INFO` (a locked `Decimal("0.30")` percentage-point strategy constant). No priced funds (`Σ priced mv <= 0`), or blended ER at/below the threshold → no finding (nothing to flag is valid). `total <= 0` → no finding, no crash. Reuse the non-finite guard (skip unpriced/NaN rows).
4. **Additive wire.** A new `fees` object on `ReviewResponse` (mirror the 11.1/11.3 informational objects): `blended_er` (percent string), `annual_cost` (money string), `coverage` (percent string), `message`. Present only when over threshold; `null` otherwise. No existing field changed.
5. **UI renders it calmly.** `CoachConsult.jsx` shows the fees line when present (ready AND empty states, like the coverage/single-stock lines), React-escaped, degrade-safe.
6. **Contracts preserved.** Read-only; per-user AD-10 scoped; fixed-point `Decimal` money; calm/no-FOMO/no-forecast; no XSS. Backend + frontend suites green on `ballast_test`. Independent review before merge.

## Tasks / Subtasks

- [ ] Task 1 — Detector + constant (AC: 1, 2, 3)
  - [ ] `BLENDED_ER_INFO = Decimal("0.30")` + `compute_fees(view, cash_config)` in `allocation/review.py`: iterate holdings, look up `fund_cost(symbol)`, weight ER by market_value; compute blended ER, annual $, coverage vs `_total_portfolio_value`; skip non-finite; return `Fees | None` (None when no priced funds / total<=0). Reuse `_aggregate_by_symbol` so a split fund is one position.
- [ ] Task 2 — Message + surface (AC: 2)
  - [ ] Deterministic `fees_message()` (calm, states blended ER + annual $ + coverage %); surface only when blended ER > threshold.
- [ ] Task 3 — Wire (AC: 4, 6)
  - [ ] Additive `FeesOut` + `ReviewResponse.fees` in `api/allocation.py`, serialized in `read_review` (fixed-point strings). Present only over threshold.
- [ ] Task 4 — UI (AC: 5)
  - [ ] Render the fees line in `CoachConsult.jsx` (mirror coverage/single-stock); extend `portfolio-review.test.jsx`.
- [ ] Task 5 — Tests (AC: all)
  - [ ] Backend: blended math (mixed cheap+pricey funds), annual-$ + coverage correctness, over/at/under threshold, no-priced-funds→None, empty→None, non-finite skipped, individual-stocks-excluded (coverage < 100%), message calm + never-invent, wire serialization, per-user scope. Frontend: renders when present, hidden when absent. `ballast_test` DB only.

## Dev Notes

### Reuse / exact touch points
- **`strategy/expense_ratio.py`** — `fund_cost(symbol) -> FundCost | None` (`.expense_ratio` is a percentage-point `Decimal`; unknown → None). The ONLY fee source (real published net ERs). Do NOT invent an ER.
- **`allocation/review.py`** — add `BLENDED_ER_INFO`, `Fees` dataclass, `compute_fees`, `fees_message`; reuse `_total_portfolio_value`, `_aggregate_by_symbol`, the non-finite pattern. Compute in `read_review` (or a small `build_fees`) alongside the existing reads — avoid a fourth portfolio fetch (fold into the shared read if practical).
- **`api/allocation.py`** — `ReviewResponse` (add `fees`), `read_review`, a `_fees_out` serializer. `money.format_money` for fixed-point.
- **`ballast/frontend/src/components/CoachConsult.jsx`** — the coverage/single-stock line blocks are the exact pattern to mirror; `portfolio-review.test.jsx` for the test.

### What must be preserved
- Never-invent: ERs only from the table; state coverage honestly (don't imply unknown-ER holdings are free).
- Read-only, AD-10 scoped, additive wire, calm-copy FORBIDDEN bar, fixed-point money, React-escaped UI.
- Distinct from the 10.4 per-fund cost switch (that owns the propose-SELL action; this is a whole-account informational summary).

### Testing standards
- Backend: `cd ballast/backend && DATABASE_URL=postgresql://ballast:ballast@localhost:5432/ballast_test BALLAST_ALLOW_DIRTY_BROKERAGE_DB=1 uv run pytest -q` (⚠️ **ballast_test DB only**).
- Frontend: `cd ballast/frontend && npm test`.

### References
- [Source: _bmad-output/planning-artifacts/epics.md#Epic 11] — story 11.4 definition.
- [Source: ballast/backend/strategy/expense_ratio.py] — the ER table (fee source).
- [Source: _bmad-output/implementation-artifacts/11-3-single-stock-aggregate-concentration.md] — the informational-object wire + UI pattern this mirrors.
- [Source: ballast/backend/allocation/review.py] · [Source: ballast/backend/api/allocation.py].

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (bmad-dev-story, in-chat)

### Debug Log References

- Backend `test_allocation_review.py`: 76 passed. Full backend: **924 passed**. Frontend `npm test`: **210 passed** (ballast_test DB). No regressions.

### Completion Notes List

- **Detector (`allocation/review.py`):** `BLENDED_ER_INFO=Decimal("0.30")`, `Fees` dataclass, `compute_fees(view)` — dollar-weighted `blended_er = Σ(mv·er)/Σ(mv)`, `annual_cost = Σ(mv·er)/100` ($), `coverage = Σ(priced mv)/total` (over TOTAL incl cash, so unpriced stocks lower coverage and are never implied fee-free). ERs ONLY from `fund_cost` (never invent); non-finite mv/er skipped; aggregates by symbol; `None` when no priced funds or empty; `over = blended_er > BLENDED_ER_INFO`. `fees_message()` deterministic/calm, turns % into a recurring $ + states coverage honestly.
- **Wire (`api/allocation.py`):** additive `ReviewResponse.fees: FeesOut | None`; `_fees_out` surfaces ONLY when over the band; fixed-point strings. `build_fees` scoped read-only (AD-10).
- **UI (`CoachConsult.jsx`):** `reviewFees` state + note in ready AND empty states when over band; React-escaped, degrade-safe.
- **Informational-only / non-money-path:** independent review CONFIRMED zero OrderIntent/writes — cannot place or submit any order; blended-ER + coverage math verified correct with a worked example.

### File List

- `ballast/backend/allocation/review.py` — `BLENDED_ER_INFO`, `Fees`, `compute_fees`, `fees_message`, `build_fees`.
- `ballast/backend/api/allocation.py` — `FeesOut`, `ReviewResponse.fees`, `_fees_out`, `read_review` wiring.
- `ballast/backend/tests/test_allocation_review.py` — 7 fees tests; updated 2 exact-shape assertions for the additive field.
- `ballast/frontend/src/components/CoachConsult.jsx` — `reviewFees` state + note render.
- `ballast/frontend/src/test/portfolio-review.test.jsx` — `stubFetch` carries `fees`; 2 render tests.

## Change Log

- 2026-08-14 — Story created via bmad-create-story (Epic 11, story 4/4). Informational-only / non-money-path; `BLENDED_ER_INFO=0.30`; reuses the 10.4 expense-ratio table.
- 2026-08-14 — Implemented via bmad-dev-story + independent review: **APPROVE WITH NITS** — cannot place/submit any order (pure read-only); blended-ER (dollar-weighted), annual-cost (Σmv·er/100), and coverage (over total incl cash) math verified correct with a worked example. 3 non-blocking nits (inline Decimal consts — consistent w/ sibling _out fns; 4th portfolio read not folded — perf-only; split-fund test — moot for linear fee blend) accepted/deferred. Backend 924 + frontend 210 green. Status → done. NOT merged — MasterB's call.
