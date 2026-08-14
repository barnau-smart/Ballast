---
title: 'Story 11.2 — Bond-floor / risk-capacity check'
type: 'feature'
created: '2026-08-14'
status: 'approved'
review_loop_iteration: 0
followup_review_recommended: false
money_path: true
context:
  - '{project-root}/_bmad-output/implementation-artifacts/11-2-bond-floor-risk-capacity.md'
  - '{project-root}/_bmad-output/implementation-artifacts/11-1-unclassified-coverage-gate.md'
  - '{project-root}/_bmad-output/implementation-artifacts/spec-10-4-analysis-buckets-concentration-cost.md'
---

<intent-contract>

## Intent

**Problem:** Changing risk tolerance does nothing today — the review never reads the chosen target (only the deploy engine does), and the deploy engine is BUY-only, so nothing ever tells a beginner "you hold far fewer bonds than your Conservative plan calls for." That downside gap (under-protected in a drop) is the single highest-value fiduciary check.

**Approach:** Add a third deterministic, read-only review bucket — **bond-floor / risk-capacity** — alongside the two 10-4 buckets. It fires ONLY when the bond weight of the CLASSIFIED sleeve is materially below the chosen model's bond target, and hands back a concrete rebalance-toward-target order the human co-signs (populate, never submit): **prefer a BUY** of the canonical bond fund (`CANONICAL_FUND[BONDS]` = BND) funded by investable cash; **fall back to a SELL** of an overweight equity class into bonds only when cash can't close the shortfall. It is hard-gated on Story 11.1 coverage and narrated by the 10-3 safeguards. It **supersedes Story 10.14's bond-shortfall scope**; 10.14's equity-overweight (both-directions) generalization is deferred.

## Boundaries & Constraints

**Locked decisions (MasterB-approved 2026-08-14):**
- **D1 — Denominator = classified sleeve.** `current_bond_pct = bonds_value / Σ(classified by-class values)` via `classify_holdings` — NOT ÷ (classified + investable cash). On a cash-heavy account the full-base version never fires; the classified-sleeve view answers "within what you've invested, are you under-bonded for your plan."
- **D2 — BUY-first, SELL-fallback.** Prefer a BUY of BND with investable cash (Epic 9 `ready_to_trade − reserve`, excl. parked); emit a SELL of an overweight equity class into bonds ONLY when investable cash cannot close the shortfall. Whole-share sized, dust dropped, never past target.
- **D3 — Defer the cash portion; SELL only the residual.** The deploy card owns the cash-funded bond BUY. Bond-floor computes the base-invariant rebalance need `bond_gap_dollars = target_bond_pct × classified_total − current_bond`, subtracts the deploy plan's actual BND buy amount (read `build_plan` read-only; 0 when no deploy), and emits a SELL-of-overweight-equity-into-bonds finding sized to the **residual** `max(0, bond_gap_dollars − deploy_bond_buy)`. Cash fully covers it → residual dust/zero → **no finding** (defer entirely). This is the **safe direction**: subtracting the deploy's bond buy can only UNDER-size the SELL (never oversell), because a cash BUY that grows the base makes the true residual slightly larger, not smaller. (MasterB decision 2026-08-14: "sell equity for the residual only.")
- **D4 — `BOND_SHORTFALL = Decimal("0.15")`** (15 percentage points). Fires only when `target_bond_pct − current_bond_pct > BOND_SHORTFALL` (measured on the classified sleeve, before any action).

**Always:**
- **Downside-only.** Fire ONLY on a bond *shortfall* past D4. Over-bonded or within-band → no finding (being more conservative than target is not a defect).
- **Requires a chosen target.** Resolve via `allocation.config.get_config` + `resolve` — the SAME resolver the deploy engine uses (`engine.py:397-398`), never a cross-model guess. `model_key is None` (undecided) → no finding, no LLM call.
- **Hard-gated on 11.1 coverage.** Reuse `compute_coverage`/`build_coverage`; when `Coverage.adequate` is `False` or `None`, this check does NOT fire (class weights aren't trustworthy over an unclassified base).
- **Never invent a fact.** Every number narrated (current bond %, target bond %, the shortfall, the order amount) is detector-computed and in the finding's allow-set. Reuse the 10-3 gates verbatim: `validate_recommendation` + `check_no_invented_numbers` + `check_no_forecast` over `reasoning + action_label + *uncertainties`; ANY failure (and fake-LLM mode) degrades to a deterministic `_fallback_review_narration` bond-floor branch.
- **Opinion, not forecast.** Framed as meeting the user's OWN chosen risk level (settled principle: bonds cushion drawdowns) — never a market prediction.
- **Populate, don't submit.** Reads the cached portfolio read-only; places nothing, writes no `decision_record`. Carries a typed `OrderIntent` (BUY or SELL, MARKET) co-signed through `/approve`. Whole-share sizing (`whole_share_quantity`); sub-one-share → dust, dropped.
- **Rebalance-toward-target, never chase; calm/no-FOMO; suite green.** Money is `Decimal` → fixed-point wire strings (`format_money`, no exponent); per-user scoped (AD-10); new copy passes the digest FORBIDDEN bar; the fallback passes the 5 good-lesson tests. Backend `pytest` (`ballast_test` DB) + frontend `vitest` green.

**Block If:**
- Any consumed upstream contract is absent/shaped differently — HALT `blocked`: `allocation.config.get_config`/`resolve`; `allocation.engine.classify_holdings` + `Classification`; `allocation.review.compute_coverage`/`Coverage`/`build_coverage`; `strategy.target_allocation.CANONICAL_FUND`/`BONDS`/`resolve_target`/`ASSET_CLASS_LABEL`; the 10-3 gate helpers + `_sell_intent`/`whole_share_quantity`; `coach.recommendation.OrderIntent`/`OrderSide`/`OrderType`; `brokers.portfolio.get_portfolio`/`PortfolioView`; `cash.config` parked/investable helpers; `db.scope.Scope`.
- The deploy-plan non-contradiction rule (D3) cannot be implemented without reading a shape the deploy engine doesn't expose — surface it, don't guess.

**Never:**
- No change to `allocation/engine.py`, `allocation/narrate.py`, `/plan`, `/narration`, or the frozen 10-2/10-3/10-4 behaviors — import public helpers only. No order type beyond MARKET. No live-broker call, no placement, no `decision_record` write, no auto-submit. No firing over-bonded (both-directions drift is 10.14, deferred). No firing when coverage is inadequate or no target is set. No LLM computing/altering a number. No tax computation. No selling past target; no selling index-core when a BUY with cash would do (D2/D3).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Under-bonded, cash covers it | Conservative (60% bonds), classified bonds 25%, investable cash ≥ shortfall | DEFER to deploy (D3) → **no** bond-floor finding (deploy path buys bonds) | none |
| Under-bonded, cash short | shortfall > investable cash after a BUY | one `kind="bond_floor"` finding: SELL MARKET of an overweight equity class into bonds (whole-share floored), sized to close the residual, never past target | fallback on any gate/gateway failure |
| Under-bonded, no cash at all | investable cash = 0, bonds far below target | SELL-to-bonds finding (whole-share floored) | fallback |
| Within band | `target_bond − current_bond ≤ 0.15` | no finding | none |
| Over-bonded | current bond % ≥ target | no finding (never fires on the upside) | none |
| No target chosen | `model_key is None` | no finding, NO LLM call | none |
| Low coverage | `Coverage.adequate` False/None | no finding (hard-gated on 11.1) | none |
| Dust | rebalance sizes to < 1 whole share | finding dropped (no order) | none |
| Invented number / forecast | LLM states an unlisted figure or predicts the market (incl. in an uncertainty) | degrade to `_fallback_review_narration` bond-floor branch | graceful fallback |
| Dedup | the SELL symbol also qualifies for concentration/cost | one finding only (defined precedence); never two overlapping orders on one symbol | none |
| Endpoint no-writes | `GET /api/allocation/review` | finding returned; no `decision_record`, no order placed | none |
| Isolation | user A requests review | only A's scoped holdings/cash/config/target used | scoped repo fail-closed |

</intent-contract>

## Code Map

- `ballast/backend/allocation/review.py` — **MODIFY (NEW additions)**: `KIND_BOND_FLOOR = "bond_floor"`; `BOND_SHORTFALL = Decimal("0.15")`; a pure detector `find_bond_floor_findings(view, target_weights, coverage, cash_config, investable_cash)` implementing D1–D4 (classified-sleeve bond %, downside-only, coverage-gated, BUY-first/SELL-fallback, whole-share, dust-drop, never past target); extend `ReviewFinding` (or a sibling) to carry `current_weight`/`target_weight` + BUY-or-SELL side; evidence/allow-set/narration bond-floor branch mirroring the 10-4 pattern; thread target + coverage + investable cash through `find_review`/`build_review`; extend the dedup in `find_review`.
- `ballast/backend/allocation/config.py` — CONSUME: `get_config`, `resolve` (target weights).
- `ballast/backend/allocation/engine.py` — CONSUME ONLY: `classify_holdings`, `Classification`, the investable-cash / deploy-plan shape needed for D3 (read, do not modify). If D3 needs a not-yet-exposed value, add a read-only helper WITHOUT changing existing behavior (flag in review if unavoidable).
- `ballast/backend/strategy/target_allocation.py` — CONSUME: `CANONICAL_FUND[BONDS]` (BND), `BONDS`, `resolve_target`, `ASSET_CLASS_LABEL`.
- `ballast/backend/coach/execution.py` / `coach/recommendation.py` — CONSUME: `whole_share_quantity`; `OrderIntent`/`OrderSide` (BUY + SELL)/`OrderType`.
- `ballast/backend/api/allocation.py` — MODIFY: `ReviewFindingOut` additive fields (`current_weight`, `target_weight`; `order.side` may now be `"buy"` for a bond-floor BUY); `_review_finding_out` maps the new kind. `/plan` + `/narration` untouched.
- `ballast/frontend/src/components/CoachConsult.jsx` — MODIFY: render the `bond_floor` finding with the same CoachCard + "Fill in this order" populate control; the control must correctly populate a BUY (not only SELL) side. `portfolio-review.test.jsx` — extend.
- `ballast/backend/tests/test_allocation_review.py` — extend: every matrix row above.

## Tasks & Acceptance

**Execution:**
- [ ] `allocation/review.py` — `KIND_BOND_FLOOR`, `BOND_SHORTFALL=Decimal("0.15")`, `find_bond_floor_findings` (D1 classified-sleeve %, D2 BUY-first/SELL-fallback sizing, D3 defer-to-deploy, D4 band; downside-only; coverage-gated; whole-share; dust-drop; never past target); evidence + allow-set (current %, target %, shortfall, amount — fraction+percent forms) + `_fallback_review_narration` bond-floor branch (principle: bonds cushion drawdowns; tradeoff: less upside for less risk; best practice: hold your plan's bonds; uncertainty: markets move / not a prediction) + `compose_review_request` bond-floor branch; thread target + coverage + investable cash through `find_review`/`build_review`; extend dedup.
- [ ] `api/allocation.py` — additive `ReviewFindingOut` fields + `order.side ∈ {buy,sell}`; map the new kind in `_review_finding_out`. No `/plan`/`/narration` change.
- [ ] `CoachConsult.jsx` — render bond_floor finding; ensure "Fill in this order" populates a BUY side correctly (not SELL-only). Extend `portfolio-review.test.jsx`.
- [ ] `tests/test_allocation_review.py` — cover all matrix rows: cash-covers→defer (no finding), cash-short→SELL-to-bonds, no-cash→SELL, within-band→none, over-bonded→none, no-target→none/no-LLM, low-coverage→none, dust-drop, invented-number/forecast→fallback (incl. inside an uncertainty), dedup, no-writes/no-order, per-user isolation; the 5 good-lesson tests as named assertions on the bond-floor fallback; calm-copy FORBIDDEN bar. `ballast_test` DB only.
- [ ] Independent adversarial review (money-path) BEFORE merge. Reconcile 10.14 status (mark bond scope superseded; equity-overweight deferred).

**Acceptance Criteria:**
- Given a chosen target with adequate coverage and classified bonds more than `BOND_SHORTFALL` below the target bond %, when review is requested AND investable cash cannot close the shortfall, then a `bond_floor` finding returns whose MARKET order rebalances toward the target bond % (BUY BND if any cash, else SELL an overweight equity class into bonds; whole-share sized, never past target), narrating only engine-provided numbers, no forecast, ≥1 cited evidence id, ≥1 uncertainty.
- Given the same shortfall but investable cash that WOULD close it via the deploy path, when review is requested, then this check DEFERS (no bond_floor finding) — the deploy plan owns that BUY (no double-spend).
- Given bonds within `BOND_SHORTFALL` of target, or over target, or no target chosen, or inadequate coverage, or a rebalance that sizes below one whole share, when review is requested, then no bond_floor finding is produced (no fabricated trade), and the no-target/coverage-gated paths make no LLM call.
- Given any bond_floor narration that states an unlisted number, predicts the market, or fails/degrades (incl. fake mode / a violation in an uncertainty), then the deterministic bond-floor fallback is returned — never a surfaced unvalidated fact — and it passes the 5 good-lesson tests.
- Given the coach console, when the user runs "Review my portfolio", then a bond_floor finding renders as an advisor CoachCard and "Fill in this order" populates the shared controls with its MARKET order (correct side, BUY or SELL) with nothing submitted; the human co-signs via the approve spine.
- Given user B's data, when user A requests review, then only A's scoped holdings/cash/config/target are used; no `decision_record` written, no order placed; `/plan`+`/narration` unchanged; full backend `pytest` + frontend `vitest` pass.

## Design Notes

**Additive by design — don't touch the deploy/narrate engines.** Like 10-4, this is a parallel SELL/BUY-side review path in `allocation/review.py`, importing 10-3 gate helpers + `classify_holdings` + the target resolver. The frozen 10-2/10-3/10-4 surfaces are import-only.

**Why downside-only (11.2 vs 10.14).** The asymmetric beginner harm is being UNDER-bonded for a chosen conservative plan (crushed in a drawdown). Over-bonded is merely conservative, not a defect. 11.2 ships the shortfall case; 10.14's both-directions equity-overweight trim is deferred (revisit only on concrete need) — so we don't build both.

**D3 non-contradiction is the subtle part.** The deploy plan already buys underweight classes (including bonds) with investable cash. If we ALSO emit a bond-floor BUY, the human could co-sign both and double-spend. Rule: when investable cash > 0 and the deploy path's answer includes buying bonds, bond-floor DEFERS (the deploy card is the right surface); bond-floor only fires when cash is insufficient and the honest move is to SELL equity into bonds — something the BUY-only deploy path can never do. The dev must read the deploy plan's bond action (or investable cash vs shortfall) read-only to decide; if that requires exposing a value the engine doesn't, add a read-only helper without changing deploy behavior.

**Never-invent mirrors 10-4 exactly.** One STRATEGY `EvidenceRecord` per finding (statement names the shortfall + the order amount; `stats` the raw `Decimal`s; `make_id`). Allow-set admits current/target bond % (fraction + percent), the shortfall, and the order amount. Fake-LLM → unbacked id → `validate_recommendation` raises → fallback.

## Verification

**Commands:**
- Backend: `cd ballast/backend && DATABASE_URL=postgresql://ballast:ballast@localhost:5432/ballast_test BALLAST_ALLOW_DIRTY_BROKERAGE_DB=1 uv run pytest -q`
- Frontend: `cd ballast/frontend && npm test`

## Auto Run Result

_(pending dev + independent review)_
