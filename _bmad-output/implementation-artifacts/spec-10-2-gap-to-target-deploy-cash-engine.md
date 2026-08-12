---
title: 'Story 10.2 — Gap-to-target deploy-my-cash engine → action items (populate, don''t submit)'
type: 'feature'
created: '2026-08-12'
status: 'done'
baseline_revision: 2af3d0ae60467e70134274837bf6ae2f4324ccef
final_revision: b8a8db9ffea8098bc6b86eafd053e664b78bf524
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-10-context.md'
  - '{project-root}/_bmad-output/implementation-artifacts/10-1-target-allocation-model-pick-a-model-portfolio.md'
warnings: ['oversized']
---

<intent-contract>

## Intent

**Problem:** Story 10-1 lets a user pick a target model portfolio, but nothing yet answers the beginner's real freeze — *"I have $2k of idle cash, what do I actually buy?"* There is no engine that compares the account to the target and hands back a concrete, pre-filled move.

**Approach:** A pure, deterministic backend engine groups the user's holdings by asset class, compares them against their resolved target (10-1), computes investable cash (Epic 9: `ready_to_trade − reserve`, excluding parked), and produces a **cash-only rebalance plan** — concrete BUYs (canonical fund + dollar amount) that close the largest gaps toward target. A read-only `GET /api/allocation/plan` returns the plan; the coach console **populates** its order controls with the primary buy for the human to co-sign via the existing `/approve` spine. Never sells, never narrates (10-3), never auto-submits.

## Boundaries & Constraints

**Always:**
- Engine is **pure/deterministic** (no I/O, no wall-clock, no RNG in the math): same holdings + config + target → identical plan. All money is `Decimal`; on the wire, fixed-point strings via `format_money`/`WireMoney` (never float, never `E` notation).
- **Rebalance toward target, never chase.** Only BUY the canonical fund of an **underweight** asset class; never deploy past a class's target (leftover cash stays undeployed and is stated honestly).
- **"Nothing to do" is a valid, honest output** — when already at/above target where cash could help, or there is no investable cash, return a calm no-action status; never manufacture a trade.
- **Never invent a fact / honest undecided:** an undecided target (10-1 resolves to `None`) or a **never-decided reserve** (Epic 9 `resolve_reserve` → `None`) yields a calm prompt status, not a fabricated plan. Parked money-market is excluded from investable cash.
- **Populate, don't submit.** The engine/endpoint never places an order and never writes a `decision_record`; the human co-signs through the existing `/approve` execution spine, which independently re-validates.
- Per-user **scoped** reads only (AD-10): holdings/cash/config reached through the existing scoped helpers (`get_portfolio`, `cash.config`, `allocation.config`). Degraded-safe: the plan reads **cached** portfolio data and needs **no live broker session**.
- Asset-class classification uses 10-1's `SYMBOL_ASSET_CLASS` (single-class index-core funds only). Holdings that don't map to exactly one class (e.g. **VT** whole-world, non-index single stocks, other ETFs) are an **"unclassified" sleeve**: surfaced honestly, **excluded** from the rebalance math (concentration/trim is Story 10-4).
- Calm/no-FOMO voice on any new copy (same tone bar as the digest `FORBIDDEN` list); the full backend `pytest` + frontend `vitest` suites stay green.

**Block If:**
- The resolved-target contract from 10-1 (`allocation.config.resolve` / `strategy.target_allocation.resolve_target`) or the investable-cash contract from Epic 9 (`cash.config.resolve_reserve` / `parked_market_value`, `brokers.portfolio.get_portfolio`) is absent or shaped differently than the plan requires — HALT `blocked` (a real upstream-contract gap, not an unattended guess).

**Never:**
- No selling/trimming, no concentration or cost/fee buckets (Story 10-4). No LLM narration, advisor persona, evidence records, or coach validation-gate/`Recommendation` object (Story 10-3). No new target models or user-editable weights. No live-broker call, no order placement, no `decision_record` write. No splitting VT across classes (no invented ratio).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Deploy | target chosen; reserve decided; investable cash > 0; ≥1 underweight class | `status:"deploy"`; `action_items` = per-class BUYs (canonical fund + amount) sorted by amount desc; `primary_order` = largest-gap buy `{symbol, side:"buy", amount, order_type:"market"}`; `Σ deployed ≤ investable cash` and never past target | none |
| Cash exceeds gaps | investable cash > total positive gap | deploy only up to total positive gap; leftover cash left undeployed and reported (`undeployed_cash`); still `status:"deploy"` | none |
| At/above target | no underweight class the cash could add to | `status:"at_target"`, empty `action_items`, calm `reason` (the rest would require selling — not done here) | none |
| No investable cash | investable cash ≤ 0 after reserve | `status:"no_cash"`, calm `reason` | none |
| Undecided target | 10-1 resolves to `None` | `status:"no_target"`, calm prompt (pick a target mix) | none |
| Reserve never decided | Epic 9 `resolve_reserve` → `None` | `status:"decide_reserve"`, calm prompt (set your cushion first) | none |
| Unclassified holdings | holds VT / single stock / non-index | those excluded from rebalance math; surfaced in `unclassified` (market value + symbols); do NOT suppress a valid deploy plan on the classified sleeve + cash | none |
| Isolation | user A requests plan | only A's holdings/cash/config used | scoped repo fail-closed |

</intent-contract>

## Code Map

- `ballast/backend/strategy/target_allocation.py` — CONSUME: `SYMBOL_ASSET_CLASS` (symbol→class), `CANONICAL_FUND` (class→fund), `resolve_target(key)`. Asset-class string consts `US_EQUITY/INTL_EQUITY/BONDS`.
- `ballast/backend/allocation/config.py` — CONSUME: `get_config(scope, session)` (read-only), `resolve(config)` → `{"model","weights","funds"}|None`.
- `ballast/backend/allocation/engine.py` — **NEW**: the deterministic gap-to-target engine (pure math + one scoped orchestrator).
- `ballast/backend/cash/config.py` — CONSUME: `get_config`, `resolve_reserve(config)` (→ `Decimal|None`), `parked_market_value(holdings, config)`.
- `ballast/backend/brokers/portfolio.py` — CONSUME: `get_portfolio(scope, session)` → `PortfolioView(holdings, cash, as_of)`; holding fields `symbol`, `market_value` (Decimal).
- `ballast/backend/api/allocation.py` — **NEW**: `GET /api/allocation/plan` router (mirror `api/cash.py` DI).
- `ballast/backend/api/app.py` — MODIFY: register the new router next to `cash_router`/`target_allocation_router`.
- `ballast/backend/money.py` — CONSUME: `format_money`/`WireMoney` for wire serialization.
- `ballast/frontend/src/components/CoachConsult.jsx` — MODIFY: add a "Deploy your cash toward your target" affordance mirroring the 8-4 `onSuggest` populate (`setSymbol/setSide('buy')/setAmount/setOptions(market)`).
- `ballast/frontend/src/lib/orderOptions.js` — CONSUME: `DEFAULT_OPTIONS` (market defaults).
- `ballast/backend/tests/test_allocation_engine.py` — **NEW**. `ballast/frontend/src/test/deploy-cash.test.jsx` — **NEW**.

## Tasks & Acceptance

**Execution:**
- [x] `ballast/backend/allocation/engine.py` -- add pure helpers + one scoped orchestrator: `classify_holdings(holdings)` → per-class dollar totals + an `unclassified` sleeve (symbol→class via `SYMBOL_ASSET_CLASS`; unmapped incl. VT → unclassified); `plan_deployment(current_by_class, target_weights, funds, investable_cash)` → deterministic cash-only rebalance (base = classified total + investable cash; per-class gap = `weight*base − current`; deploy `min(investable_cash, Σ positive_gap)` split proportionally to positive gaps, quantized to cents with the residual cent added to the largest gap, never past target; drop sub-`MIN_DEPLOY` dust); and `build_plan(scope, session)` orchestrator that resolves target (10-1), investable cash (Epic 9), and holdings, then returns a plan object with `status`/`action_items`/`primary_order`/`current`/`unclassified`/`investable_cash`/`undeployed_cash`/`as_of`/`reason`. -- the deterministic core; keep math pure so tests need no DB.
- [x] `ballast/backend/api/allocation.py` -- add `GET /api/allocation/plan` (prefix `/api/allocation`) mirroring `api/cash.py`: `Depends(get_scope)` + `Depends(get_async_session)`, call `build_plan`, serialize all money/weights as fixed-point strings (`format_money`), `primary_order` as `{symbol, side, amount, order_type}` or `null`. Register the router in `api/app.py`. -- read-only, degraded-safe, per-user scoped.
- [x] `ballast/frontend/src/components/CoachConsult.jsx` -- add a calm "Deploy your cash toward your target" control that `apiFetch('/api/allocation/plan')`; on `status:"deploy"` populate the local controls with `primary_order` (`setSide('buy')`, `setAmount(primary_order.amount)`, `setSymbol(primary_order.symbol)`, `setOptions({...DEFAULT_OPTIONS, order_type:'market'})`) so the human reviews & co-signs via the existing approve spine; on any no-action status show the calm `reason` (no populate). Mirror the 8-4 `onSuggest` pattern; do not auto-submit. -- populate, don't submit.
- [x] `ballast/backend/tests/test_allocation_engine.py` -- unit-test every I/O Matrix row against `plan_deployment`/`classify_holdings`/`build_plan`: deploy split + amount-desc `primary_order`, cash-exceeds-gaps leftover, at_target, no_cash, no_target, decide_reserve, unclassified (VT excluded from math but surfaced), determinism (same input→equal output), Σ deployed ≤ investable & ≤ Σ gaps (never past target), fixed-point wire strings, and **scoped isolation** (A cannot see B). Use the `ballast_test` DB per the live-link guard. -- covers the deterministic contract.
- [x] `ballast/frontend/src/test/deploy-cash.test.jsx` -- test the deploy affordance: `status:"deploy"` populates side/amount/symbol/market order controls (assert setters land, no submit); a no-action status shows the calm `reason` and does not populate; calm copy passes the digest FORBIDDEN-word bar. Mock `fetch` by URL substring per the existing convention. -- covers populate-don't-submit on the UI.

**Acceptance Criteria:**
- Given a user with a chosen target, a decided reserve, investable cash > 0, and at least one underweight asset class, when `GET /api/allocation/plan` is called, then it returns `status:"deploy"` with `action_items` (canonical fund + dollar amount per underweight class, sorted by amount descending) and a `primary_order` for the largest gap as a MARKET BUY, with every money value a fixed-point string and total deployed ≤ investable cash and never past any class's target.
- Given the same user in the coach console, when they trigger "Deploy your cash toward your target", then the order controls are populated with the primary BUY (side=buy, the fund symbol, the dollar amount, market order) and nothing is submitted — the human still co-signs through the existing approve spine.
- Given a user already at/above target where cash could help, or with no investable cash, when the plan is requested, then it returns a calm no-action status (`at_target`/`no_cash`) with a plain reason and no order is manufactured.
- Given a user who has not chosen a target, or who has never decided a reserve, when the plan is requested, then it returns `no_target` / `decide_reserve` respectively (a calm prompt, never a fabricated plan).
- Given a user holding VT or a non-index single stock, when the plan is computed, then those holdings are surfaced in an `unclassified` sleeve and excluded from the rebalance math (no invented VT split), while a valid deploy plan is still produced on the classified sleeve plus investable cash.
- Given user B's data, when user A requests the plan, then only A's scoped holdings/cash/config are used; the full backend `pytest` and frontend `vitest` suites pass.

## Design Notes

**Cash-only rebalance (buy-only water-fill), worked example.** Target = growth (US 0.60 / Intl 0.30 / Bonds 0.10). Holdings: VTI $6,000 (us), BND $0, VXUS $0. Investable cash $4,000. `base = 6,000 + 4,000 = 10,000`. Desired: US 6,000 / Intl 3,000 / Bonds 1,000. Gaps: US `0`, Intl `+3,000`, Bonds `+1,000` (Σ positive = 4,000). `to_deploy = min(4,000, 4,000) = 4,000` → Intl `3,000` (VXUS), Bonds `1,000` (BND). `primary_order` = BUY $3,000 VXUS (market).

**On leftover cash (corrected during review).** Because `base = classified + investable_cash`, the gaps over ALL classes sum to exactly the investable cash, so `Σ positive gap ≥ cash` whenever any class is underweight — i.e. all investable cash normally deploys. A genuine `undeployed_cash > 0` therefore arises only from (a) dropped sub-`MIN_DEPLOY` **dust** allocations, or (b) the sub-cent tail the per-class target caps can't absorb. Whatever isn't deployed is always reported in `undeployed_cash` (`= investable_cash − Σ deployed`) — honest, never chased past target. (An earlier draft's "$6,000 cash → $2,000 undeployed" example was arithmetically wrong: enlarging the base makes US underweight too, so all $6,000 deploys.)

**Why buy-only + market dollars.** Overweight classes can't be fixed by buying (that's selling → 10-4), so the plan only ever adds to underweight classes. Amounts are **dollars**; whole-share flooring happens later at execution (`whole_share_quantity`), so the engine needs no live ask and stays pure — matching the existing coach MARKET-buy path, not the 8-4 LIMIT suggest path. `MIN_DEPLOY = Decimal("1.00")` drops dust allocations; if all drop, fall through to `at_target`/`no_cash`.

**Undecided honesty (Epic 9 parity).** `resolve_reserve → None` (never-decided) must NOT be silently treated as 0 — it becomes `decide_reserve`. A **declined** reserve resolves to `Decimal("0")` (investable = ready_to_trade) and proceeds normally. Parked money-market is excluded via `parked_market_value` before computing investable cash.

**Quantization.** Split each positive gap's share of `to_deploy`, `quantize(Decimal("0.01"))`, capped per-class at the gap rounded DOWN to cents (never past target); assign any residual cent to the largest-gap class. Surviving `action_item` amounts sum to `to_deploy` minus any dropped sub-`MIN_DEPLOY` dust and any sub-cent tail the caps can't absorb — that remainder is returned to `undeployed_cash` (`= investable_cash − Σ deployed`), never a phantom over-target cent.

## Verification

**Commands:**
- `cd ballast/backend && DATABASE_URL=postgresql://ballast:ballast@localhost:5432/ballast_test BALLAST_ALLOW_DIRTY_BROKERAGE_DB=1 uv run pytest -q` -- expected: all pass incl. new `test_allocation_engine.py` (⚠️ `ballast_test` DB only — never `ballast`, which holds the live Schwab link the suite deletes).
- `cd ballast/frontend && npm test` -- expected: all pass incl. new `deploy-cash.test.jsx`.

## Auto Run Result

Status: **done** (dev-auto: plan → implement → adversarial review → patch → green).

**Implemented change.** A pure, deterministic gap-to-target "deploy my cash" engine for Epic 10. It groups the user's cached holdings by asset class (10-1's `SYMBOL_ASSET_CLASS`), compares to their resolved 10-1 target, computes investable cash (Epic 9: `ready_to_trade − reserve`, excluding parked money-market), and runs a buy-only water-fill that closes the largest gaps *toward* target — concrete BUYs (canonical fund + dollar amount). The read-only, degraded-safe `GET /api/allocation/plan` returns the plan; the coach console's new "Deploy your cash toward your target" control populates the order controls with the primary MARKET BUY for the human to co-sign via the existing `/approve` spine. Five honest states (`deploy`/`at_target`/`no_cash`/`no_target`/`decide_reserve`); never sells, never narrates (10-3), never auto-submits, never places an order or writes a decision record. All money `Decimal` → fixed-point wire strings.

**Files changed.**
- `ballast/backend/allocation/engine.py` (new) — the pure engine (`classify_holdings`, `plan_deployment`) + scoped `build_plan` orchestrator + plan dataclasses.
- `ballast/backend/api/allocation.py` (new) — `GET /api/allocation/plan` (fixed-point serialization; `primary_order` as `{symbol, side, amount, order_type}`).
- `ballast/backend/api/app.py` — register `allocation_router`.
- `ballast/frontend/src/components/CoachConsult.jsx` — the "Deploy your cash toward your target" affordance (`onDeploy`) mirroring the 8-4 populate; populate-don't-submit.
- `ballast/frontend/src/components/CoachConsult.css` — style the deploy button.
- `ballast/backend/tests/test_allocation_engine.py` (new) — 25 tests (pure engine + real-DB endpoint, every I/O row, isolation, no-writes guardrail).
- `ballast/frontend/src/test/deploy-cash.test.jsx` (new) — 7 tests (populate-don't-submit, no-action reason, malformed-primary rejection, calm copy).

**Review findings breakdown.** Two adversarial reviewers (Blind Hunter + Edge Case Hunter). 0 intent_gap, 0 bad_spec, **9 patches applied** (1 medium honesty-copy fix + 8 low: a `max(0,…)` cash clamp, docstring/spec accuracy, 2 new safety/accounting tests, 2 test renames, and 3 frontend hardening fixes — pre-request-reset, cross-concurrency guard + stale-panel clear, and positive-amount/non-empty-symbol validation), 0 deferred, **18 rejected** (unreachable given the typed ORM projection + 10-1 locked reference data + Epic 9 validation, established-model, or verified-safe incl. the reviewers' two "HIGH" items). See the Review Triage Log.

**Verification.** Backend `pytest` against the disposable `ballast_test` DB: **740 passed** (0 regressions). Frontend `vitest`: **179 passed** (0 regressions). The live-link guard was honored throughout — the suite never ran against the `ballast` DB.

**Residual risks.** Low. The engine is pure/deterministic and read-only; the endpoint places nothing and writes nothing (now asserted by a backend test). `at_target` is reachable in practice mostly via the dust path (documented honestly). This ran the automated dev+adversarial-review loop and committed to the feature branch `epic-10/allocation-coach`; per the Epic 9 lesson, a human should still review before merging money-path-adjacent work to `main`.

## Review Triage Log

### 2026-08-12 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 9: (high 0, medium 1, low 8)
- defer: 0
- reject: 18: (high 0, medium 4, low 14)
- addressed_findings:
  - `[medium]` `[patch]` (P-A) `at_target` reason copy asserted "your mix already lines up with your target" when the true cause is investable cash too small to place a buy — rewrote to be honest about both causes (calm-word-list clean).
  - `[low]` `[patch]` (P-B) `ready_to_trade = view.cash − parked` mixed two data sources and could go negative — clamped to `max(0, …)` so a source mismatch collapses honestly to `no_cash`.
  - `[low]` `[patch]` (P-C) `plan_deployment` docstring + spec Quantization/Design-Notes claimed allocations "sum EXACTLY to to_deploy" — corrected: surviving items sum to to_deploy minus dropped dust / sub-cent tail, all returned to `undeployed_cash`.
  - `[low]` `[patch]` (P-D) added a pure test for the mixed one-surviving-buy + one-dropped-dust case proving honest `undeployed` accounting and never-past-target.
  - `[low]` `[patch]` (P-E) added a real-DB test asserting `GET /api/allocation/plan` writes no `decision_record` and places no order (the load-bearing populate-don't-submit guardrail — was only frontend-tested).
  - `[low]` `[patch]` (P-F) renamed two misnamed "at_target" tests that actually assert `no_cash`/zero-cash no-op.
  - `[low]` `[patch]` (P-G) `onDeploy` destroyed a shown recommendation BEFORE the fetch — moved the reset into the successful populate branch only, so a no-action/failed deploy no longer wipes the card.
  - `[low]` `[patch]` (P-H) `onDeploy`/`onSuggest` didn't cross-guard each other's in-flight ref (racing the shared order-control setters) and a deploy populate left a stale 8-4 suggest panel visible — added cross-guards + clear the suggest panel on deploy.
  - `[low]` `[patch]` (P-I) deploy populate only checked `typeof === 'string'` — hardened to require a non-blank symbol and a positive-decimal amount (`DECIMAL_RE`), else fall through to a calm failed note (no un-co-signable order).
- rejected (not this story's problem / unreachable / verified-safe): H2 populated-amount-string (verified the field's `DECIMAL_RE` accepts `"3000.00"` — the same field shipped 8-4 `onSuggest` populates); malformed-holdings crash-to-500 cluster (unreachable — `portfolio_cache` is a typed non-null ORM projection; uncaught errors still return the app's calm global-handler envelope); weights-not-summing-to-1 / missing class key / 0-weight strands cash (unreachable — `resolve_target` is 10-1 locked reference data); negative stored reserve (Epic 9 validates ≥0 on write); parked-vs-cash model (established Epic 9); residual-loop non-termination (terminates on the `progressed` flag); `no_target`-before-`decide_reserve` ordering (defensible single-prompt); weight-through-`format_money` / loose `unclassified` typing / native `as_of` serialization (cosmetic schema rigor); redundant disabled/ref guards; negative per-holding market_value (long-only index-core universe); and other defensive-against-impossible-state notes.

### Review Findings — Independent (2026-08-12)

_First independent human-requested review (Group B = deploy engine + target math). Blind Hunter + Edge Case Hunter + Acceptance Auditor. NOTE: the loop's own self-review above explicitly REJECTED "parked-vs-cash model (established Epic 9)" and "negative stored reserve" — both were actually wrong; the independent pass caught the parked one as a real HIGH bug._

- [x] [Review][Patch] **HIGH — Investable cash wrongly subtracted parked money-market holdings from settlement cash.** `ready_to_trade = max(0, view.cash − parked_market_value)` [engine.py:369-376] double-removed a pool never in `view.cash` — Epic 9 canonically sets `ready_to_trade = view.cash` (parked is a separate holding pool it *liquidates to top up*, story 9-3). Any user of the park feature was under-deployed or falsely told `no_cash` (e.g. $8k settlement + $5k parked + $2k reserve → deployed $1k instead of $6k). **FIXED 2026-08-12:** `ready_to_trade = max(0, view.cash)`, dropped the parked subtraction + its import; flipped `test_parked_excluded_from_investable` → `test_parked_holding_does_not_reduce_settlement_investable` (now asserts `deploy`, investable $4,000). 816 backend green.
- [x] [Review][Patch] Engine trusted the stored `reserve` verbatim — a `NaN` slipped past the `investable <= 0` guard (NaN compares False) and a negative inflated investable. **FIXED (bundled) 2026-08-12:** re-validate at read time (`if not reserve.is_finite() or reserve < 0: reserve = 0`).
- [x] [Review][Defer] Display-weight quantize inconsistency — `_current_breakdown` weight is bare `_ZERO` (`"0"`) when total==0 but 4dp (`"0.0000"`) otherwise, and three independent 4dp weights need not sum to 1.0000 [engine.py:~434] — deferred, display honesty only (not trade math).
- [x] [Review][Defer] `target_weights` sum-to-1.0 is enforced only by a sibling test, not asserted at the pure-engine boundary [engine.py:~349] — deferred, latent (models are locked reference data).
- [x] [Review][Defer] `CANONICAL_FUND ⊆ index-core` (the pre-filled BUY must pass the `/approve` scope gate) is not pinned by a test [target_allocation.py] — deferred, coincidentally-safe today (VTI/VXUS/BND).
- [x] [Review][Defer] `MIN_DEPLOY` dust-drop can silently swallow a real small-gap sleeve while deploying its neighbors; the `deploy` reason never names the skipped class [engine.py:~411] — deferred, product decision.
- [x] [Review][Defer] `parked_market_value` sums `h.market_value` with no `None`-guard (asymmetric with `classify_holdings`) [cash/config.py] — deferred; now MOOT for the engine (no longer called after the HIGH fix), but still latent for other Epic 9 callers.
- [x] [Review][Process] Story 10-1 (`strategy/target_allocation.py`, `api/target_allocation.py`) has NO governing spec (traceability gap); `expense_ratio.py` is a 10-4 file bundled into these commits — for the epic-10 retrospective.
