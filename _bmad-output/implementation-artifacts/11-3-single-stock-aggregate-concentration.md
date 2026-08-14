---
baseline_commit: 308c16a
---
# Story 11.3: Single-stock aggregate concentration

Status: done

<!-- Epic 11 (Fiduciary-Grade Portfolio Review), story 3 of 4. INFORMATIONAL-ONLY
     (MasterB decision 2026-08-14) — proposes NO order → NON-money-path, like 11.1/11.4.
     No spec gate. Independent review before merge (it touches the review module). -->

## Story

As a beginner,
I want the review to flag when a lot of my money sits in individual stocks in aggregate — even if no single one is huge —
so that I understand my single-company risk, which the "one position over 40%" rule misses entirely.

## Context

Story 10.4's concentration bucket catches ONE holding over 40%. But the common beginner failure is *many* hand-picked stocks — 25 names at 2–9% each is 60%+ of the account with single-company risk, and none trips the 40% rule (the operator's real portfolio is exactly this). This story adds an **informational** aggregate check: when the individual-stock / unclassified sleeve exceeds `SINGLE_NAME_AGG_MAX` of the portfolio, surface a calm "this is a lot riding on individual companies" note. **MasterB decision 2026-08-14: informational-only (no order), threshold 0.25.** No order → non-money-path (same lane as 11.1).

It reuses Story 11.1's `Coverage` computation: the individual-stock sleeve IS the unclassified sleeve `compute_coverage` already measures, so 11.1 and 11.3 report the SAME dollars with **distinct framings** — 11.1 = coverage honesty ("I can only see X%", at <80% classified); 11.3 = concentration risk ("that's a lot in single companies", at >25% single-stock). They must read as complementary, not two ways of saying the same thing.

## Acceptance Criteria

1. **Aggregate detector, informational.** A pure function computes the individual-stock sleeve as `unclassified_value / total` (reuse `Coverage.unclassified_value` / `Coverage.total` from 11.1 — the same non-index, non-parked sleeve). When it **exceeds** `SINGLE_NAME_AGG_MAX` (a locked `Decimal("0.25")` strategy constant), produce an informational result carrying the aggregate value (money), the percent, and the de-duplicated symbols. **No `OrderIntent` is ever produced.**
2. **Honest label + calm framing.** Because Ballast can't perfectly tell an individual stock from a niche ETF in the unclassified sleeve, the copy names it "individual stocks and specialty funds" and frames it as single-company risk to *consider* diversifying — never a directive, never FOMO, no forecast. Distinct wording from the 11.1 coverage line so the two don't read as duplicates.
3. **Threshold + boundary.** Fires only when the sleeve fraction is strictly greater than `SINGLE_NAME_AGG_MAX`. At or below → no finding ("nothing to flag" is valid). `total <= 0` (empty/never-imported) → no finding, no crash. Reuse the 11.1 non-finite guard (drop unpriced rows) so a NaN can't distort the fraction.
4. **Additive wire.** A new `single_stock` object on `ReviewResponse` (mirror the 11.1 `coverage` object: `value` money string, `pct` percent string, `symbols` list, `message`). Present only when over threshold; `null` otherwise. No existing `ReviewFindingOut`/`coverage` field changed.
5. **UI renders it calmly.** `CoachConsult.jsx` shows the single-stock line when present (in ready AND empty states, like the coverage line), React-escaped, degrade-safe (absent/null → nothing).
6. **Contracts preserved.** Read-only; per-user AD-10 scoped; fixed-point `Decimal` money; never-invent (pure arithmetic); no XSS. Backend + frontend suites green on `ballast_test`. Independent review before merge.

## Tasks / Subtasks

- [ ] Task 1 — Detector + constant (AC: 1, 3)
  - [ ] `SINGLE_NAME_AGG_MAX = Decimal("0.25")` + `compute_single_stock(view, cash_config)` (or derive from a `Coverage`) in `allocation/review.py`; returns value/pct/symbols when `unclassified/total > 0.25`, else `None`; reuse the 11.1 finite-guard + `classify_holdings`.
- [ ] Task 2 — Message + scoped builder (AC: 2)
  - [ ] Deterministic `single_stock_message()` (calm, risk-framed, distinct from `coverage_message`); `build_single_stock(scope, session)` read-only/AD-10 (or fold into the existing review reads to avoid a third portfolio fetch).
- [ ] Task 3 — Wire (AC: 4, 6)
  - [ ] Additive `SingleStockOut` + `ReviewResponse.single_stock` in `api/allocation.py`, serialized in `read_review` (fixed-point strings).
- [ ] Task 4 — UI (AC: 5)
  - [ ] Render the single-stock line in `CoachConsult.jsx` (mirror the coverage line); extend `portfolio-review.test.jsx`.
- [ ] Task 5 — Tests (AC: all)
  - [ ] Backend: over/at/under threshold, empty→None, non-finite ignored, parked-not-counted, message calm + distinct from coverage, wire serialization, per-user scope. Frontend: renders when present, hidden when absent. `ballast_test` DB only.

## Dev Notes

### Reuse / exact touch points
- **`allocation/review.py`** — reuse `compute_coverage` (its `unclassified_value` + `total` ARE the individual-stock sleeve); add `SINGLE_NAME_AGG_MAX`, `compute_single_stock`, `single_stock_message`, `build_single_stock`. To avoid a third portfolio read, consider computing single-stock from the SAME `Coverage` already built in `build_coverage`/`read_review`.
- **`api/allocation.py`** — `ReviewResponse` (add `single_stock`), `read_review` (call the builder), `_coverage_out`-style serializer. `money.format_money` for fixed-point.
- **`ballast/frontend/src/components/CoachConsult.jsx`** — the coverage line block (added in 11.1) is the exact pattern to mirror; `portfolio-review.test.jsx` for the test.

### What must be preserved
- 11.1 coverage behavior + its non-finite guard (drop unpriced rows before classifying).
- Read-only, AD-10 scoped, additive wire, calm-copy FORBIDDEN bar, fixed-point money, React-escaped UI.
- Distinct framing from 11.1 so the two informational lines complement (not duplicate).

### Testing standards
- Backend: `cd ballast/backend && DATABASE_URL=postgresql://ballast:ballast@localhost:5432/ballast_test BALLAST_ALLOW_DIRTY_BROKERAGE_DB=1 uv run pytest -q` (⚠️ **ballast_test DB only**).
- Frontend: `cd ballast/frontend && npm test`.

### References
- [Source: _bmad-output/planning-artifacts/epics.md#Epic 11] — story 11.3 definition.
- [Source: _bmad-output/implementation-artifacts/11-1-unclassified-coverage-gate.md] — the `Coverage` computation + coverage-line UI pattern this reuses/mirrors.
- [Source: ballast/backend/allocation/review.py] · [Source: ballast/backend/api/allocation.py].

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (bmad-dev-story, in-chat)

### Debug Log References

- Backend `test_allocation_review.py`: 69 passed. Full backend: **917 passed**. Frontend `npm test`: **208 passed** (ballast_test DB). No regressions.

### Completion Notes List

- **Detector (`allocation/review.py`):** `SINGLE_NAME_AGG_MAX=Decimal("0.25")`, `SingleStock` dataclass, `single_stock_from_coverage(cov)` derives the individual-stock sleeve fraction from the SAME 11.1 `Coverage` (unclassified_value/total — inherits its finite-guard; no third portfolio read); `over = fraction > 0.25` (strictly greater). `single_stock_message()` is deterministic, calm, risk-framed, DISTINCT from `coverage_message` (a test asserts `!=`).
- **Wire (`api/allocation.py`):** additive `ReviewResponse.single_stock: SingleStockOut | None`; `_single_stock_out` surfaces ONLY when `over` (else null); fixed-point percent/money strings. `read_review` reuses the one `build_coverage` call for both coverage + single_stock.
- **UI (`CoachConsult.jsx`):** `reviewSingleStock` state + a note in ready AND empty states when the sleeve is over the band; React-escaped, degrade-safe.
- **Informational-only / non-money-path:** independent review confirmed ZERO OrderIntent/writes/mutations — cannot place or submit any order. Read-only, AD-10 scoped, never-invent (pure arithmetic).

### File List

- `ballast/backend/allocation/review.py` — `SINGLE_NAME_AGG_MAX`, `SingleStock`, `single_stock_from_coverage`, `single_stock_message`.
- `ballast/backend/api/allocation.py` — `SingleStockOut`, `ReviewResponse.single_stock`, `_single_stock_out`, `read_review` wiring.
- `ballast/backend/tests/test_allocation_review.py` — 5 single-stock tests; updated 2 exact-shape assertions for the additive field.
- `ballast/frontend/src/components/CoachConsult.jsx` — `reviewSingleStock` state + note render.
- `ballast/frontend/src/test/portfolio-review.test.jsx` — `stubFetch` carries `single_stock`; 2 render tests.

## Change Log

- 2026-08-14 — Story created via bmad-create-story (Epic 11, story 3/4). Informational-only / non-money-path (MasterB decision); `SINGLE_NAME_AGG_MAX=0.25`; reuses 11.1 Coverage.
- 2026-08-14 — Implemented via bmad-dev-story + independent review: **APPROVE WITH NITS** — confirmed cannot place/submit any order (zero OrderIntent/writes); reuse of 11.1 `Coverage` correct (no divergence); strict-`>25%` boundary clean. Fixed 1 LOW (frontend test fixture message drifted from backend copy). Backend 917 + frontend 208 green. Status → done. NOT merged — MasterB's call.
