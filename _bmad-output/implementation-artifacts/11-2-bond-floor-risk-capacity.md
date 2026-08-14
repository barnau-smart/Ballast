# Story 11.2: Bond-floor / risk-capacity check

Status: ready-for-dev

<!-- Epic 11 (Fiduciary-Grade Portfolio Review), story 2 of 4. MONEY-PATH — proposes a real
     BUY/SELL order the human co-signs. HARD GATE (docs/dev-loop-policy.md): NOT ready-for-dev.
     Needs an approved bmad-spec + MasterB sign-off before dev, and independent review before
     merge. Do NOT let the loop pick this up (sprint-status stays out of ready-for-dev). -->

## Story

As a beginner who chose a risk level,
I want the review to warn me when I hold far fewer bonds than my chosen plan calls for, and offer the fix,
so that I'm not carrying more risk than I signed up for — right when a downturn would hurt most.

## Context

Changing risk tolerance does nothing to the review today (confirmed: `allocation/review.py` never reads the target model — only the deploy engine does, `allocation/engine.py:397-398`). And the deploy engine is **BUY-only** ("never touches an overweight class", `engine.py:263`), so nothing ever tells a user "you're too stock-heavy for Conservative." This story adds the **downside-only** risk-capacity check as a third review bucket: when bonds are materially **below** the chosen model's bond target, propose a rebalance toward that target. The fiduciary consultation ranked this the single highest-value check — it's the asymmetric harm (under-protected in a drop). It builds directly on Story 11.1's coverage gate: class-level math is only trustworthy when coverage is adequate.

**This is the honest, focused slice of the drafted Story 10.14 (target-drift, both directions).** 11.2 ships the bond-shortfall case only. See "Reconciliation" below.

## ⚠️ Open decisions for the spec (resolve BEFORE dev)

1. **Denominator (recommended: classified sleeve).** Measure `current_bond_pct` as bonds ÷ **classified holdings** (invested stocks+bonds), NOT ÷ (classified + investable cash). On a cash-heavy account (operator: 60% SWVXX) the full-base version reads everything underweight and this check would never fire — defeating its purpose. The story text assumes the **classified-sleeve** denominator; spec to confirm and state the exact formula + interaction with 11.1 coverage.
2. **BUY vs SELL to close the gap.** Two honest ways to add bonds: (a) BUY a bond index fund with **investable cash** (Epic 9 ready-to-trade − reserve, excl. parked), or (b) **SELL** an overweight equity class into bonds. Recommended: prefer the BUY when investable cash covers the shortfall (less disruptive, no realized gains); fall back to a SELL only when cash is insufficient. Spec to lock the rule and the sizing (whole-share, dust-dropped, never past target).
3. **Non-contradiction with the deploy plan.** If the deploy path would BUY bonds with the same cash, this finding and the deploy plan must not double-spend or contradict. Spec to define the shared guard (e.g. this check defers to the deploy plan when a deploy is already the answer, and only fires a SELL-to-bonds when cash can't close the gap).
4. **`BOND_SHORTFALL` band.** Locked pp constant (~15pp) — small enough to catch a real gap (Conservative 60% bonds held at 25%), large enough not to nag on normal drift. Confirm value in spec.

## Acceptance Criteria (draft — spec finalizes)

1. **New `bond_floor` bucket (directional).** `allocation/review.py` gains a pure detector firing ONLY when `target_bond_pct − current_bond_pct > BOND_SHORTFALL` on the classified sleeve; over-bonded (or within band) never fires. Kind key: `KIND_BOND_FLOOR = "bond_floor"`.
2. **Requires a chosen target.** No target model set (`model_key is None`) → no finding, no LLM call. Resolve the target via `allocation.config` (`get_config` + `resolve`) — the SAME resolver the deploy engine uses (never a cross-model guess).
3. **Hard-gated on 11.1 coverage.** When coverage is inadequate (`Coverage.adequate is False` / `None`), this check does NOT fire (class weights aren't trustworthy). Reuse `compute_coverage` / `build_coverage`.
4. **Proposes a rebalance toward the user's OWN target, populate-don't-submit.** Per the locked decision (2): BUY a bond index fund (`CANONICAL_FUND[BONDS]` = BND) with investable cash, or SELL an overweight equity class into bonds. Whole-share sized (reuse `_sell_intent` / `whole_share_quantity`), dust dropped, never past target. Carries a typed `OrderIntent` the human co-signs through `/approve`; PLACES NOTHING, writes no `decision_record`.
5. **Never-invent / no-forecast.** Every number narrated (current bond %, target bond %, the shortfall, the order amount) is detector-computed and in the finding's allow-set; narration passes `validate_recommendation` + `check_no_invented_numbers` + `check_no_forecast`, degrading to a deterministic fallback. Framed as meeting the user's chosen risk level — never a market prediction.
6. **No double-count / non-contradiction.** Dedup vs the concentration/cost buckets (a symbol can't surface as two overlapping orders); and honor decision (3) so this never contradicts or double-spends against the deploy plan.
7. **Additive wire + UI.** `ReviewFindingOut` carries the new kind + the two weights (current/target bond %) additively; `CoachConsult.jsx` renders it with the same populate-don't-submit control + narration card as the other kinds. `switch_to` null (or the bond fund for a SELL-into-bonds, per spec).
8. **Money-safety preserved + independent review.** Populate-don't-submit, per-user scoping (AD-10), fixed-point money, whole-share sizing, no XSS. Backend + frontend suites green on `ballast_test`. Independent adversarial review before merge (money-path).

## Reconciliation with Story 10.14 (target-drift)

10.14 proposed a general both-directions asset-class drift SELL. 11.2 is its **downside-only bond slice** — the piece with the clearest fiduciary value and the cleanest guardrail fit. Decision for the spec: **11.2 supersedes 10.14's bond-shortfall scope.** 10.14's remaining generalization (trimming an *over*-weight equity class purely for drift, absent a bond shortfall) is **deferred** — revisit only if a concrete need appears; do NOT build both. Mark 10.14 accordingly at spec time.

## Dev Notes

### Reuse / exact touch points
- **`allocation/config.py`** — `get_config` + `resolve` (fraction weights); the target resolver (same as `engine.py:397`).
- **`allocation/engine.py`** — `classify_holdings` for the class split; `Plan`/`plan_deployment` for the deploy-plan non-contradiction; `ASSET_CLASSES`, `MIN_DEPLOY`, `whole_share_quantity` patterns.
- **`allocation/review.py`** — add `KIND_BOND_FLOOR`, `BOND_SHORTFALL`, the detector, evidence/allow-set/narration branch; reuse `_sell_intent`, `_total_portfolio_value`, `compute_coverage`, the honesty gates, `_fallback_review_narration`, and the `find_review` dedup. Wire into `build_review` (thread target + coverage).
- **`strategy/target_allocation.py`** — `CANONICAL_FUND[BONDS]` (BND), `resolve_target`, `ASSET_CLASS_LABEL`, `BONDS`.
- **`api/allocation.py`** — `ReviewFindingOut` (+ additive weight fields) / `_review_finding_out`.
- **`ballast/frontend/src/components/CoachConsult.jsx`** (:833 review fetch/render) + `portfolio-review.test.jsx`.

### What must be preserved
- The four locked Epic-10 guardrails (opinion-not-forecast, never-invent, nothing-to-fix-valid, rebalance-not-chase — this is the canonical rebalance case).
- Populate-don't-submit; AD-10 scoping; fixed-point money; whole-share sizing; dust dropped.
- 11.1's honest-coverage gate — never emit a class-level finding over an inadequate coverage base.

### Testing standards
- Backend: `cd ballast/backend && DATABASE_URL=postgresql://ballast:ballast@localhost:5432/ballast_test BALLAST_ALLOW_DIRTY_BROKERAGE_DB=1 uv run pytest -q` (⚠️ **ballast_test DB only** — the suite wipes `brokerage_token`).
- Frontend: `cd ballast/frontend && npm test`.

### References
- [Source: _bmad-output/planning-artifacts/epics.md#Epic 11] — story 11.2 definition + guardrails.
- [Source: _bmad-output/implementation-artifacts/11-1-unclassified-coverage-gate.md] — the coverage gate this depends on (`Coverage` / `build_coverage` / `adequate`).
- [Source: _bmad-output/implementation-artifacts/10-14-risk-aware-review-target-drift-rebalance.md] — the both-directions draft this supersedes (bond slice).
- [Source: ballast/backend/allocation/engine.py#plan_deployment] · [Source: ballast/backend/allocation/config.py] · [Source: ballast/backend/allocation/review.py].
- [Source: docs/dev-loop-policy.md] — money-path spec-approval + independent-review hard gate.

## Dev Agent Record

### Agent Model Used

### Debug Log References

### Completion Notes List

### File List

## Change Log

- 2026-08-14 — Story created via bmad-create-story (Epic 11, story 2/4). MONEY-PATH → status ready-for-spec (NOT ready-for-dev); needs approved bmad-spec + MasterB sign-off before dev. Downside-only bond-floor slice; supersedes 10.14's bond-shortfall scope. 4 open decisions flagged for the spec (denominator, BUY-vs-SELL, deploy non-contradiction, BOND_SHORTFALL band).
