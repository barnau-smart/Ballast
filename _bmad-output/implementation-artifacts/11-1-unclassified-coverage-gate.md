# Story 11.1: Unclassified-holdings coverage gate

Status: ready-for-dev

<!-- Epic 11 (Fiduciary-Grade Portfolio Review), story 1 of 4 — the FOUNDATION.
     INFORMATIONAL, NOT money-path (proposes NO order) → no bmad-spec/independent-review
     money-gate required (unlike 11.2/11.3). Still: per-story scope stays approved by MasterB. -->

## Story

As a beginner,
I want the review to tell me how much of my portfolio it can actually categorize,
so that I know when its allocation findings cover my whole account versus just part of it — and it never pretends to see money it can't.

## Context

Epic 10.4 gave the review two checks (single-name >40% concentration; high-fee-fund switch). Neither states how much of the portfolio Ballast can actually reason about. On a real account a large chunk is often **unclassified** — individual stocks / niche ETFs not in the 3-class index-core map (the operator's real account is ~40% unclassified single stocks + ~60% SWVXX money-market). Without a stated coverage figure, every *class-level* finding (this epic's 11.2 bond-floor, any target-drift) is quietly measured over a partial base and can mislead. This story adds the **coverage meta-check**: it computes and honestly surfaces the classifiable share, and exposes an `adequate` signal the later class-level checks hard-gate on. It is **informational** — pure arithmetic, proposes no order, never framed as a problem to "fix".

This is the load-bearing honesty foundation for Epic 11; build it first (the fiduciary consultation ranked it #1, and 11.2 is unsafe without it).

## Acceptance Criteria

1. **Deterministic coverage detector.** A pure function computes, from the caller's cached portfolio + Epic-9 cash config: `unclassified_value` (Σ market_value of holdings that are NOT index-core and NOT a declared parked money-market symbol), `classified_plus_cash` (everything else — classified holdings + cash + parked money-market), `total` (holdings MV + cash), and `coverage = classified_plus_cash / total` (0..1). Reuse `allocation.engine.classify_holdings` (it already partitions by_class / unclassified / parked→cash) rather than re-deriving the split — same classification the deploy engine uses, so coverage and deploy never disagree.
2. **Locked threshold + `adequate` signal.** A single locked strategy constant `COVERAGE_MIN` (a bare `Decimal`, value confirmed here at `Decimal("0.80")`) determines `adequate = coverage >= COVERAGE_MIN`. Named/commented like `CONCENTRATION_CEILING` / `EXPENSE_RATIO_MATERIAL_DELTA` (auditable reference constant, never LLM-touched).
3. **Informational finding when coverage is low.** When `not adequate`, the review surfaces a calm informational result carrying `coverage` (as a percent), `unclassified_value` (money), and the de-duplicated `unclassified_symbols` list. When `adequate`, no finding is emitted for this check ("nothing to flag" is valid). **No `OrderIntent` is ever produced** — this check proposes nothing.
4. **Deterministic copy — no LLM required.** The informational message is a deterministic template (like `_fallback_review_narration`), stating only the computed numbers ("I can categorize about 60% of your portfolio…"). No forecast, no invented number, calm/no-FOMO (mirror the review test FORBIDDEN word list). An LLM call is NOT needed for this check; if one is used it must pass the 10.3 honesty gates, but the default path is templated.
5. **`adequate` exposed for downstream gating (contract for 11.2).** The coverage result (at minimum `coverage` + `adequate` + `unclassified_value`) is returned from the review layer and serialized on the wire so 11.2 (bond-floor) and any drift check can hard-gate on it. Whether it rides on `ReviewResponse` as a new top-level `coverage` object or as an order-less finding is the dev's call — but it must NOT force an `OrderIntent` onto an informational result (prefer a dedicated `coverage` field on `ReviewResponse`).
6. **Honest edge behavior.** `total <= 0` (never imported / empty) → no coverage finding, no crash (coverage undefined → treated as "nothing to say", degrade-safe). Everything classified → `coverage = 1.0`, `adequate = true`, no finding. Parked money-market (SWVXX) counts toward the *classified/known* side (it's cash-equivalent, not an unseen holding) — it must NOT inflate `unclassified_value`.
7. **Money-safety + scope contracts preserved.** Read-only; per-user fail-closed scoping (AD-10 — only the caller's holdings/config); fixed-point `Decimal` money on the wire; no XSS (React-escaped text) in the UI; additive wire change (no existing `ReviewFindingOut` field altered). Full backend + frontend suites stay green.
8. **UI renders it calmly.** The coach console review area shows the coverage line when present ("I can categorize about X% of your portfolio; the rest is in individual stocks and specialty funds I don't classify"), listing the unclassified symbols/value honestly. Degrade-safe: absent/undefined coverage renders nothing, never NaN/blank.

## Tasks / Subtasks

- [ ] Task 1 — Coverage detector (AC: 1, 2, 6)
  - [ ] Pure `find_coverage`/`compute_coverage(view, cash_config)` in `allocation/review.py` reusing `classify_holdings`; returns coverage fraction + unclassified value/symbols + `adequate`. `COVERAGE_MIN = Decimal("0.80")` constant with a doc-comment.
- [ ] Task 2 — Surface via the review layer (AC: 3, 4, 5, 6)
  - [ ] `build_review` computes coverage; emit the informational result (deterministic templated copy) when `not adequate`; return coverage alongside the findings without forcing an order.
- [ ] Task 3 — Wire + API (AC: 5, 7)
  - [ ] Add a `coverage` object to `ReviewResponse` (`coverage` percent string, `adequate` bool, `unclassified_value` money string, `unclassified_symbols` list) in `api/allocation.py`, serialized in `read_review`. Additive only.
- [ ] Task 4 — UI (AC: 8)
  - [ ] Render the coverage line in `CoachConsult.jsx`'s review block; calm copy; degrade-safe; React-escaped. Extend `portfolio-review.test.jsx`.
- [ ] Task 5 — Tests (AC: all)
  - [ ] Backend: coverage math (mixed / all-classified / all-unclassified / empty), parked-not-counted-as-unclassified, threshold boundary, no-order-ever, per-user scope, wire serialization, calm-copy. Frontend: renders coverage line from a stubbed response + degrades when absent. `ballast_test` DB only.

## Dev Notes

### Reuse / exact touch points
- **`allocation/engine.py:classify_holdings` (:158)** — already returns `by_class`, `unclassified_value`, `unclassified_symbols`, and routes declared-parked cash-equivalents to investable cash (NOT unclassified). Reuse it verbatim so coverage's "unclassified" is byte-identical to the deploy engine's, and SWVXX-style parked funds never inflate `unclassified_value` (AC 6).
- **`allocation/review.py`** — add the detector + `COVERAGE_MIN`; wire into `build_review` (which already resolves `get_portfolio` + `get_cash_config`). Mirror the pure-detector + deterministic-fallback style already there; DO NOT add an `OrderIntent` path.
- **`strategy/index_core.py:is_index_core` / `strategy/target_allocation.py:asset_class_for`** — the classification predicates (already used by `classify_holdings`).
- **`api/allocation.py`** — `ReviewResponse` (:152) + `read_review` (:284): add the additive `coverage` object; `money.format_money` for fixed-point strings.
- **`cash/config.py:get_config` + `normalize_symbols`** — the parked-symbols set feeding `classify_holdings`.
- **`ballast/frontend/src/components/CoachConsult.jsx`** — fetches `GET /api/allocation/review` (:833) and lists each finding; add the coverage line here beside the findings. Styles in `CoachConsult.css`. Review render tests live in `ballast/frontend/src/test/portfolio-review.test.jsx` (extend for the coverage line).

### What must be preserved (read before coding)
- The review is READ-ONLY and degrade-safe (`build_review` in `allocation/review.py`) — never writes, never places. Keep it so.
- Per-user fail-closed scoping (AD-10): coverage is computed only over the caller's own holdings/config.
- Additivity: do not alter `ReviewFindingOut`'s existing fields; `coverage` is a NEW `ReviewResponse` field.
- Calm-copy tone bar (mirror the review test FORBIDDEN list); fixed-point money; React-escaped text (no XSS).

### Testing standards
- Backend: `cd ballast/backend && DATABASE_URL=postgresql://ballast:ballast@localhost:5432/ballast_test BALLAST_ALLOW_DIRTY_BROKERAGE_DB=1 uv run pytest -q` (⚠️ **ballast_test DB only** — the suite DELETEs `brokerage_token` and would wipe a live Schwab link).
- Frontend: `cd ballast/frontend && npm test`.

### Project Structure Notes
- Lives entirely in the existing Epic-10 allocation module (`allocation/review.py`, `api/allocation.py`) + the coach console — no new module or table. No migration (read-only over existing cache + cash_config).

### References
- [Source: _bmad-output/planning-artifacts/epics.md#Epic 11] — story definition + guardrails + the honest coverage principle.
- [Source: ballast/backend/allocation/engine.py#classify_holdings] — the classification split to reuse.
- [Source: ballast/backend/allocation/review.py] — the review layer + pure-detector/fallback pattern.
- [Source: ballast/backend/api/allocation.py#ReviewResponse] — the wire shape to extend.
- [Source: _bmad-output/implementation-artifacts/spec-10-4-analysis-buckets-concentration-cost.md] — the bucket/guardrail pattern this mirrors.

## Dev Agent Record

### Agent Model Used

### Debug Log References

### Completion Notes List

### File List

## Change Log

- 2026-08-14 — Story created via bmad-create-story (Epic 11, story 1/4). Foundation coverage gate; informational/non-money-path; reuses `classify_holdings`; exposes `adequate` for 11.2's hard-gate. COVERAGE_MIN=0.80 (confirm at dev). ready-for-dev.
