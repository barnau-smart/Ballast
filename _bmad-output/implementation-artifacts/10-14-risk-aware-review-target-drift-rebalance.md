# Story 10.14: Risk-aware review — flag target drift and propose a rebalance SELL

Status: backlog

<!-- HARD GATE (docs/dev-loop-policy.md): MONEY-PATH story (proposes SELLs) — needs an
     APPROVED spec + MasterB scope approval + independent review before merge. This file
     is the story shell; do NOT dev it until the open design decision below is resolved. -->

## Story

As a beginner who just set (or changed) my risk tolerance,
I want "Review my portfolio" to tell me when my invested mix is too aggressive or too conservative for my chosen target, and propose the SELL that rebalances toward it,
so that changing my risk level actually produces an action — not a silent "nothing to fix".

## Context — the gap this closes

Today the Review is **risk-tolerance-blind by design.** It runs exactly two SELL-side
checks (Story 10.4): single-name **concentration** (>40% ceiling) and **cost** (high-fee
fund → cheaper index twin). Neither reads the user's chosen model, so Conservative vs.
Growth yields a byte-identical review. Confirmed in code: `allocation/review.py` never
imports the target config; the target model is consumed in exactly **one** place —
`allocation/engine.py:397-398` (`build_plan`), which powers "Deploy your cash toward your
target".

That deploy path is **BUY-only** — a deliberate water-fill that "never touches an
overweight class" (`engine.py:263`). So there is **no feature anywhere** that says "your
mix is too stock-heavy for Conservative — sell some down." A user who dials risk *down*
reasonably expects the coach to react; it can't. This story adds the missing **SELL-side,
target-aware rebalance** finding as a **third review analysis bucket**, parallel to
concentration + cost, reusing the same 10-3/10-4 safeguards.

This is the intended home for "sell index-core to balance an asset class": the
concentration bucket explicitly **excludes** index-core because "a broad index position
over the ceiling is asset-class balance handled by the deploy path, never a single-name
trim" (`review.py:41`, `find_concentration_findings`). This story *is* that asset-class
balance, now on the SELL side the deploy path can't do.

## ⚠️ Open design decision (MUST resolve in the spec before dev)

**What denominator defines "overweight"?** Two honest options with opposite behavior on a
cash-heavy portfolio (e.g. the operator's real account: 60% SWVXX parked, ~$210k idle):

- **(A) Invested/classified sleeve only** — drift = class weight *within the currently
  invested stocks+bonds* vs target. Fires a SELL even when lots of cash is idle. Matches
  the user's mental model ("I'm conservative → sell my stocks"). **Recommended.**
- **(B) Full investable base (classified + deployable cash)** — mirrors the deploy engine's
  `base = classified + investable_cash`. On a cash-heavy account almost every class reads
  *underweight* (cash isn't invested yet), so a SELL rarely fires — the honest answer
  becomes "deploy your cash first". Consistent with deploy math, but makes this feature a
  no-op for exactly the user who asked for it.

The two features must not contradict each other (deploy says BUY class X while review says
SELL class X). Recommend **(A)** for the drift SELL, and a guard so a class flagged for a
rebalance SELL is never simultaneously a deploy BUY target. Spec to confirm + define the
**materiality band** (below).

## Acceptance Criteria

1. **New `target_drift` analysis bucket (SELL side).** `allocation/review.py` gains a third
   pure detector, `find_drift_findings(view, target_weights, cash_config)`, additive and
   parallel to `find_concentration_findings` / `find_cost_findings`. Fires when a broad
   **asset class** (US equity / international / bonds) is **materially overweight** vs the
   user's resolved target, and proposes a SELL of that class's index-core canonical holding
   back toward target. Kind string: `KIND_DRIFT = "target_drift"`.
2. **Only fires with a chosen target.** No target model set (`model_key is None`,
   undecided) → **zero** drift findings, no LLM call (mirrors "nothing to fix is valid").
   `build_review` resolves the target via `allocation.config` (the read the review does not
   do today) and passes the weights into `find_review`.
3. **Materiality band, locked as a strategy constant.** A class must exceed target by at
   least `TARGET_DRIFT_BAND` (percentage points, e.g. `Decimal("0.05")` = 5pp) to fire —
   tuned deliberately like `CONCENTRATION_CEILING` / `EXPENSE_RATIO_MATERIAL_DELTA`, so a
   1–2pp wobble never nags. Value confirmed in the spec.
4. **Sell the overweight class's index-core holding, sized to target, whole-share, dust
   dropped.** SELL amount = `current_class_value − target_weight × base` (denominator per
   the resolved design decision), quantized to cents, priced off the cached unit like
   `_sell_intent`, whole-share floored via `whole_share_quantity`; a sub-one-share trim is
   dust → dropped. Target the class's canonical/index-core holding (e.g. sell VTI for
   overweight US equity); never sell past the target (never chase in reverse).
5. **Never invent a fact / opinion not forecast.** Every number the narration states (drift
   amount, current class %, target %, the band) is detector-computed and in the finding's
   numeric allow-set; narration passes `validate_recommendation` + `check_no_invented_numbers`
   + `check_no_forecast` (reused verbatim from `allocation.narrate`), degrading to a
   deterministic `_fallback_review_narration`-style template on any failure. Narration
   frames it as rebalancing toward the user's OWN chosen risk level — not a market call.
6. **Populate, don't submit.** Each finding carries a typed SELL/MARKET `OrderIntent` the
   human co-signs through the existing `/approve` spine; this layer PLACES NOTHING and
   writes no `decision_record` (identical contract to 10.4).
7. **No double-count across buckets.** A single holding must never surface as two
   overlapping SELLs (a co-sign-into-oversell hazard). Define precedence when a symbol
   qualifies for drift AND (concentration or cost) — e.g. a cost switch of a fund that is
   also its class's overweight holding. Reuse/extend the `find_review` dedup that already
   drops the redundant concentration trim when a cost switch subsumes it (`review.py:408`).
8. **Deploy/Review non-contradiction.** With design (A), guard that a class proposed for a
   rebalance SELL here is not simultaneously offered as a BUY by the deploy plan for the
   same account state (spec to define the shared rule).
9. **Wire + UI additive.** `ReviewFindingOut` (`api/allocation.py`) carries the new kind +
   the drift fields (current %, target %) without changing existing fields; the coach
   console renders the drift finding with the same populate-don't-submit SELL control +
   narration card as the other two kinds. `switch_to` stays null for drift.
10. **Money-safety contracts preserved.** Populate-don't-submit, per-user scoping (AD-10),
    fixed-point money, whole-share sizing, no-XSS (React-escaped) — all unchanged and
    re-verified. Read-only + degrade-safe: any resolution failure yields fewer findings,
    never a crash or a fabricated trade.

## Tasks / Subtasks

- [ ] Task 1 — Resolve + thread the target into the review (AC: 2)
  - [ ] `build_review` reads `allocation.config` (resolve model → weights), passes to `find_review`; undecided → skip drift entirely.
- [ ] Task 2 — `find_drift_findings` pure detector + `TARGET_DRIFT_BAND` (AC: 1, 3, 4)
  - [ ] Classify holdings by class (reuse `engine.classify_holdings` or mirror it), compute per-class drift vs target on the agreed denominator, size the whole-share SELL of the class's index-core holding, drop dust.
- [ ] Task 3 — Evidence + allow-set + narration (AC: 5, 6)
  - [ ] `build_review_facts` / `allowed_review_facts` / narration branch for `target_drift` (reuse the honesty gates + fallback template).
- [ ] Task 4 — Dedup + deploy non-contradiction (AC: 7, 8)
  - [ ] Precedence rule across the three buckets; shared guard vs the deploy BUY set.
- [ ] Task 5 — Wire + UI (AC: 9)
  - [ ] `ReviewFindingOut` additive fields; render the drift card in the coach console.
- [ ] Task 6 — Tests (AC: all)
  - [ ] Detector unit tests (overweight fires / within-band doesn't / undecided skips / dust dropped / sized-to-target-not-past); narration honesty-gate tests; dedup + non-contradiction; wire serialization; frontend render. `ballast_test` DB only.

## Dev Notes

### Reuse / exact touch points
- **`allocation/review.py`** — add `KIND_DRIFT`, `find_drift_findings`, `TARGET_DRIFT_BAND`; extend `find_review` (thread `target_weights` + dedup) and `build_review` (resolve target). Reuse `_sell_intent`, `whole_share_quantity`, `_aggregate_by_symbol`, the honesty gates, `_fallback_review_narration`.
- **`allocation/config.py`** — `get_config` + `resolve` (fraction weights) — the read the review currently lacks. Same resolver the deploy engine uses (`engine.py:397`), so there's no cross-model guess.
- **`allocation/engine.py`** — `classify_holdings` (:158) for the class buckets; `Plan.target_weights` shape for consistency; the BUY-only water-fill (`plan_deployment`) is the non-contradiction counterpart.
- **`strategy/target_allocation.py`** — `resolve_target` / `CANONICAL_FUND` (which index-core fund to sell per class) / `ASSET_CLASS_LABEL`.
- **`api/allocation.py`** — `ReviewFindingOut` (:137) + `_review_finding_out` (:268), additive.

### What must be preserved
- The four locked Epic-10 guardrails: opinion-not-forecast, never-invent-a-fact, nothing-to-do-is-valid, **rebalance-not-chase** (this story is the canonical rebalance case).
- Populate-don't-submit; per-user fail-closed scoping (AD-10); fixed-point money; whole-share sizing; dust dropped.
- "Nothing to fix" stays valid: no target, or within-band, → empty, no LLM call.

### Testing standards
- Backend: `cd ballast/backend && DATABASE_URL=postgresql://ballast:ballast@localhost:5432/ballast_test BALLAST_ALLOW_DIRTY_BROKERAGE_DB=1 uv run pytest -q` (⚠️ ballast_test DB only — the suite wipes `brokerage_token`).
- Frontend: `cd ballast/frontend && npm test`.

### References
- [Source: ballast/backend/allocation/review.py] — the two existing buckets + the dedup this extends.
- [Source: ballast/backend/allocation/engine.py#plan_deployment] — the BUY-only water-fill that "never touches an overweight class" (why a SELL-side rebalance is needed).
- [Source: ballast/backend/allocation/config.py] · [Source: ballast/backend/strategy/target_allocation.py#MODEL_PORTFOLIOS] — the target models (Conservative 30/10/60, Balanced 45/20/35, Growth 60/30/10).
- [Source: _bmad-output/implementation-artifacts/spec-10-4-analysis-buckets-concentration-cost.md] — the bucket + narration pattern to mirror.
- [Source: docs/dev-loop-policy.md] — per-story spec-approval + independent-review hard gate (money-path).

## Dev Agent Record

### Agent Model Used

### Completion Notes List

### File List

## Change Log

- 2026-08-14 — Story drafted (backlog) from an in-chat product discussion: the Review is risk-tolerance-blind, so changing risk level produces no action. Proposes a third `target_drift` analysis bucket that SELLs an overweight class toward the chosen target — the rebalance-SELL the BUY-only deploy path can't do. One open design decision (drift denominator) flagged for the spec; recommended option (A) invested-sleeve. Not ready for dev until spec + scope approval.
