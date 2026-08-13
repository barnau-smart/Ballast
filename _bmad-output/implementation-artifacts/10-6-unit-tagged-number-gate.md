# Story 10.6: Unit-tagged (context-aware) never-invent number gate

Status: done
baseline_commit: 1cb73fa
followup_review_recommended: false  # human-requested independent review done 2026-08-13; 1 High fixed (money-axis laundering)

<!-- HARD GATE (docs/dev-loop-policy.md, adopted 2026-08-12): governing spec APPROVED by
     MasterB 2026-08-12. Bare-number strictness settled: option (a) STRICT — a bare token
     matches only BARE/fraction-tagged allowed values (rejects a bare integer equal to a
     percent/money value). -->

## Story

As a beginner reading the coach's narration,
I want the never-invent-a-fact gate to tell dollars, percentages, and bare counts apart,
so that a fabricated figure that merely equals a real weight-percent or amount (e.g. "30 companies" when 30% is my target) can't be laundered past the gate and shown to me as fact.

## Context & problem

The never-invent numeric gate (`allocation/narrate.py:check_no_invented_numbers`) is **value-based and unit-blind**. `_normalize_number_token` strips `$`, commas, and a trailing `%`, so `"$40"`, `"40"`, and `"40%"` all normalize to `Decimal("40")` and match ANY allowed value of 40. A fabricated integer that coincides with an admitted weight-percent or amount (e.g. an LLM writing *"a basket of 30 companies"* when `30` is a target-weight percent, or *"$40"* when `40` is the concentration ceiling percent) passes the gate and reaches a beginner as if it were engine-sourced. Surfaced by the Epic 10 Group-A review (F2, recorded in `spec-10-3`); a prior 10-3 pass explicitly accepted it as degrade-safe, and MasterB has now chosen to invest in stronger enforcement.

**This is a degrade-safe hardening, not a live exploit fix:** the bar is "strictly better, still degrade-safe," not "provably perfect." Spelled-out magnitudes and qualitative forecasts remain out of scope (unchanged 10-3 residuals).

## Design decision for approval

**Recommended approach — make the gate unit-aware:**
1. **Tag each extracted token by its unit signal:** `$`-prefixed → `MONEY`; `%`-suffixed → `PERCENT`; neither → `BARE`. Preserve the leading sign exactly as today (a sign-flipped value is never laundered to its magnitude).
2. **Tag the allow-set:** change the allow-set from `frozenset[Decimal]` to a set of `(Decimal, Unit)` pairs. In `allowed_facts` / `allowed_review_facts`: action-item amounts, cash (investable/undeployed), market values, `holding_value` → `MONEY`; the 0–100 percent weight form → `PERCENT`; the fraction weight form (e.g. `0.60`) → `BARE`.
3. **Match on `(value, unit)`:** a `MONEY` token matches only a `MONEY`-tagged allowed value; a `PERCENT` token only a `PERCENT`-tagged value; a `BARE` token only a `BARE`-tagged value. Any non-match → reject → deterministic template (unchanged degrade path).

**BARE-token strictness — DECIDED: option (a) STRICT.** A `BARE` token matches ONLY `BARE`/fraction-tagged allowed values. A bare integer equal to a percent/money value (the *"30 companies"* case) no longer matches → rejected → degrade to the deterministic template. This is the load-bearing choice that actually closes the laundering hole; the cost (a legitimate engine-absent bare number degrades to template) is acceptable and matches the already-accepted behavior for stray years/ordinals/share-counts. Rejected: (b) permissive bare-matches-any-unit — does not fix the laundering.

## Acceptance Criteria

1. **Unit-matched acceptance.** A `MONEY` token (`"$X"`) is accepted only against a `MONEY`-tagged allowed value; a `PERCENT` token (`"X%"`) only against a `PERCENT`-tagged value; a `BARE` token only against a `BARE`/fraction-tagged value. Otherwise the narration degrades to the deterministic template.
2. **Laundering closed (regression).** A narration stating a bare count equal to an admitted weight-percent (e.g. `"30 companies"` when `30` is a target-weight percent) OR a `$`-figure equal to a percent value (e.g. `"$40"` when `40` is the concentration-ceiling percent) is **rejected** — it no longer passes as fact.
3. **Legit citations still pass.** The deterministic fallback templates (which state `$X` amounts and `X%` / `XX.XX%` weights) and any LLM narration restating real engine figures with correct units continue to pass — existing accept-path tests stay green (both the narration and review fallbacks self-cite correctly).
4. **Single-sourced, both sides move together.** `check_no_invented_numbers` stays defined ONCE in `narrate.py` and imported verbatim by `review.py`. Both allow-set builders (`narrate.py:allowed_facts` and `review.py:allowed_review_facts`, each with its own `_add_weight_forms`) produce the unit-tagged set, so `GET /api/allocation/narration` (10-3) AND `GET /api/allocation/review` (10-4) both get the stronger gate.
5. **Invariants preserved.** Sign-awareness preserved (sign-flipped value never laundered); degrade-safe (any reject → honest template, never a surfaced fabricated fact); still best-effort (spelled-out magnitudes + qualitative forecasts remain out of scope, unchanged).
6. **No collateral change.** No change to the no-forecast gate or nothing-to-do path; no new endpoints; the pure functions stay pure (no I/O, deterministic).

## Tasks / Subtasks

- [x] Task 1 — Unit tag + token extraction (AC: 1, 5)
  - [x] Added `UNIT_MONEY`/`UNIT_PERCENT`/`UNIT_BARE` constants + `_classify_number_token` emitting `(Decimal value, unit)` from a token's `$`/`%` signal, sign preserved (replaced `_normalize_number_token`).
- [x] Task 2 — Unit-aware compare (AC: 1, 2)
  - [x] `check_no_invented_numbers` now takes `frozenset[tuple[Decimal, str]]` and rejects unless the `(value, unit)` PAIR is admitted (BARE strict).
- [x] Task 3 — Tagged allow-sets, both sides (AC: 4)
  - [x] `narrate.py:allowed_facts`/`_add_weight_forms` + `review.py:allowed_review_facts`/`_add_weight_forms` (own copy) emit tagged pairs; `review.py` imports the `UNIT_*` constants. Added `_add_money` (both files): a money amount is admitted as BOTH MONEY and BARE — the deterministic fallback renders amounts via `format_money` (bare, no `$`) while the LLM is told to use `$`. This does NOT reopen the laundering (weight percents stay PERCENT-only, never BARE).
- [x] Task 4 — Tests (AC: all)
  - [x] +4 tests (3 narrate, 1 review): reject bare-count == weight-percent and `$X` == weight-percent; accept the percent cited with its unit; both narrate + review sides. Updated the two allow-set membership tests to the tagged shape. Full backend suite 835 passed.

## Dev Notes

### Reuse map / exact touch points
- **`allocation/narrate.py`:** `_NUMBER_TOKEN_RE` (:180), `_normalize_number_token` (:341), `check_no_invented_numbers` (:359), `allowed_facts` (:302), `_add_weight_forms` (:295). The gate + tokenizer live here and are the single source.
- **`allocation/review.py`:** imports `check_no_invented_numbers` VERBATIM (:56) — do NOT fork it. It has its OWN `_add_weight_forms` (:462) and `allowed_review_facts` (:473) / `build_review_facts` (:417) — these must be updated to the tagged shape in lockstep with `narrate.py`, or the review side will pass a bare `frozenset[Decimal]` into a gate expecting tagged pairs (type/behavior break).
- The gate is called on `reasoning + action_label + *uncertainties` in both `narrate_plan`/`narrate_finding`; any raise is caught → deterministic fallback.

### What must be preserved (regressions to avoid)
- **Degrade-safe by construction:** a false reject is SAFE (falls to the honest template) — never raise past the gate. The fallback templates must still pass their own gate (they self-cite `$X`/`X%`); verify after the unit change.
- **Sign-awareness** from the prior 10-3 pass (`-30%` compared as `-30`, not `30`).
- **Single-sourcing** — the gate stays defined once; `review.py` keeps importing it.
- Out of scope (unchanged 10-3 residuals, do NOT attempt here): spelled-out numbers ("thirty"), `"3k"`-scale suffixes, qualitative/number-free forecasts. The 10-3 system-prompt instruction ("write every quantity in digits as given") stays and complements this.

### Testing standards
- Backend: `cd ballast/backend && DATABASE_URL=postgresql://ballast:ballast@localhost:5432/ballast_test BALLAST_ALLOW_DIRTY_BROKERAGE_DB=1 uv run pytest -q` (⚠️ `ballast_test` DB ONLY). Existing gate tests: `test_allocation_narrate.py` (`check_no_invented_numbers` accept/reject, sign-flip) and `test_allocation_review.py` — extend, keep green.
- Pure-unit story: no DB/migration/frontend changes expected.

### Project Structure Notes
- Backend Python only (`ballast/backend/allocation/`). No new files strictly required; a tiny `Unit` enum can live in `narrate.py` beside the gate. No migration, no endpoint, no frontend.

### References
- [Source: _bmad-output/implementation-artifacts/spec-10-3-fiduciary-advisor-narration-never-invent-safeguard.md#Review Findings — Independent (2026-08-12)] — the numeric-gate decision (Decision→Story 10-6)
- [Source: _bmad-output/implementation-artifacts/spec-10-4-analysis-buckets-concentration-cost.md] — the gate is reused VERBATIM by the review side (both must move together)
- [Source: ballast/backend/allocation/narrate.py#check_no_invented_numbers] · [Source: ballast/backend/allocation/narrate.py#allowed_facts] · [Source: ballast/backend/allocation/review.py#allowed_review_facts]
- [Source: docs/dev-loop-policy.md] — per-story spec-approval hard gate governing this spec.

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (dev-story)

### Debug Log References

- RED confirmed: the 2 new reject tests failed on the value-only gate (bare `30` and `$30` were accepted).
- GREEN in one iteration after one correction: the deterministic fallback renders amounts via `format_money` (bare `"3000.00"`, no `$`), so money amounts must be admitted as BOTH MONEY and BARE (`_add_money`) — 5 fallback self-citation tests caught the MONEY-only tagging; fixed → 835 passed.

### Completion Notes List

- Made `check_no_invented_numbers` unit-aware: tokens classified `$`→MONEY / `%`→PERCENT / bare→BARE, matched on the `(value, unit)` pair. Bare-token strictness = option (a): a bare integer equal to a weight-percent (the "30 companies" case) no longer matches the PERCENT entry → degrades to the honest template.
- Single-sourced: the gate stays in `narrate.py`; `review.py` imports it + the `UNIT_*` constants. Both allow-set builders (+ their own `_add_weight_forms`) moved to tagged pairs in lockstep.
- Key nuance discovered in dev: amounts are cited bare by the fallback (`format_money`) but `$`-prefixed by the LLM, so `_add_money` admits both units. Weight percents remain PERCENT-only (never BARE), so the laundering fix holds.
- Degrade-safe + sign-awareness preserved; no endpoints/DB/frontend touched. Backend 835 passed; frontend unaffected (backend-only).
- Out of scope (unchanged 10-3 residuals): spelled-out numbers, `3k`-scale suffixes, number-free forecasts.

### File List

- `ballast/backend/allocation/narrate.py` — UNIT_* constants, `_add_money`, tagged `_add_weight_forms`/`allowed_facts`, `_classify_number_token` (replaces `_normalize_number_token`), unit-aware `check_no_invented_numbers`.
- `ballast/backend/allocation/review.py` — import UNIT_*, `_add_money`, tagged `_add_weight_forms`/`allowed_review_facts`.
- `ballast/backend/tests/test_allocation_narrate.py` — +3 tests; updated 2 membership tests to tagged shape.
- `ballast/backend/tests/test_allocation_review.py` — +1 test; updated 1 membership test; import UNIT_*.

## Change Log

- 2026-08-13 — Story 10.6 implemented: unit-tagged (context-aware) never-invent number gate. `check_no_invented_numbers` now matches on `(value, unit)`; closes the value-only laundering where a fabricated bare count / `$` figure equal to a real weight-percent passed. +4 tests, backend 835 passed.
- 2026-08-13 — Independent review (3-layer) — 1 HIGH fixed. The first pass admitted amounts as BOTH MONEY and BARE (so the `format_money` fallback would self-cite), which REOPENED the laundering on the money axis: a fabricated bare count equal to a real dollar amount (e.g. "3000 companies" when the plan deploys $3,000) passed. Fix (both hunters' recommendation): amounts are now MONEY-only, and the deterministic fallback + LLM request + evidence statements cite amounts WITH a `$` (`format_money` → `${format_money(...)}`). Bare integers ≥1 now match only fraction weights (0–1), fully closing both laundering axes. The `/plan` wire serialization (`"3000.00"`, no `$`) is untouched. +1 regression test; backend 836 + frontend 196 green.
