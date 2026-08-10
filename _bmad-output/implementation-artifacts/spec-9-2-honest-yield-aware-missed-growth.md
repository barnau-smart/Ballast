---
title: 'Story 9.2: Honest, yield-aware missed-growth'
type: 'feature'
created: '2026-08-10'
status: 'done'
baseline_revision: '355dcd437595c4ec5179173cf3d2363c7388c180'
final_revision: 'd2bab487d5a644202e1dd1fcac3c9610f1789626'
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-9-context.md'
  - '{project-root}/_bmad-output/brainstorming/brainstorm-cash-readiness-idle-cash-2026-08-10/brainstorm-intent.md'
warnings:
  - oversized
---

<intent-contract>

## Intent

**Problem:** The Missed-Growth meter is dishonest about idle cash. It measures **only** settlement cash (`view.cash`), so it *under-counts* by ignoring money the user has parked in money-market funds — yet naively folding those funds in would *over-count*, because parked money already earns yield. It also ignores the user's declared reserve, so a "you're missing growth" figure can read as "invest everything," which frightens the exact beginner Ballast exists to calm. Story 9-1 already exposes `parked` and `reserved` on the portfolio read but deliberately left the market math untouched (this story owns it).

**Approach:** Make the missed-growth engine cash-state-aware and yield-aware. Compute the figure on the **investable base = settlement cash + parked − reserve** (clamped ≥ 0, reserve drawn parked-first), where settlement cash misses the full benchmark window return and parked money misses only `window_return − money-market_yield_over_window`. Always surface the protected reserve alongside the figure and disclose the yield assumption, so the number is honest by construction and can never read as FOMO. No new schema — everything derives from the existing `CashConfig` + holdings.

## Boundaries & Constraints

**Always:**
- Compute forgone growth on the **investable base only** (`cash + parked − reserve`, clamped ≥ 0). Draw reserve **parked-first**: `parked_investable = max(parked − reserve, 0)`, then remaining reserve reduces settlement cash.
- Settlement cash misses the **full** window return; parked money misses `window_return − yield_over_window`, where `yield_over_window = money_market_apy × (lookback_days / 252)`. The parked term stays **signed** (if the market underperformed money-market yield, parked reduces the figure — that is honest).
- **Always surface the resolved reserve** in the response and **disclose the money-market yield assumption** whenever parked > 0 (never lie by omission).
- All money is `Decimal` end-to-end, quantized to cents (`ROUND_HALF_UP`); returns fixed-point strings via `format_money` (no `E+`/`E-`). The engine stays **deterministic** (no wall-clock reads; `as_of` = the window-end market bar, as today).
- Calm/honest voice (NFR8): all new copy passes the `test_digest_compose.py` FORBIDDEN word list; negatives/parked never render red/pink (sky-blue only). No FOMO, urgency, or "invest everything" framing.
- Keep the engine **backward-compatible**: `parked`/`reserved` default to `0`, so a caller passing only `idle_cash` behaves exactly as today (existing engine/digest callers unaffected).

**Block If:**
- Delivering this appears to require an **ALTER/new column on an existing table** or any DB migration. 9-2 must be additive-only (new response fields + a named constant); if a schema change seems necessary, HALT — the design is wrong.
- The intent's "yield-aware" math appears to require a **live per-fund money-market yield data source** — none exists (market data covers only the 14 index ETFs). Do not fabricate one; use the disclosed default constant (see Never).

**Never:**
- Never add a live/auto yield feed or a user-editable per-fund yield input in 9-2 (out of scope; the disclosed default constant is the v1 honest source — user-editable yield is a possible later refinement).
- Never touch `brokers/portfolio.py` / `PortfolioView` shape, `db/models.py:PortfolioCache`, or the `cash`/`holdings`/`as_of` fields of `PortfolioOut` (all fixed contracts).
- Never build the liquidation / deferred-buy / pending-buy flow (that is Story 9-3).
- Never nudge on the full (pre-reserve) amount, and never emit an unprompted "you're missing out" alert.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Cash + parked, reserve set, market rose | cash=5000, parked=5000, reserve=2000, window +14%, apy 4% | base=8000; parked drawn-first so parked_investable=3000, cash_investable=5000; forgone = 5000·0.14 + 3000·(0.14−0.04); `reserved`=2000 surfaced; statement discloses the 4% assumption | No error |
| Reserve covers all cash | cash=1000, parked=1000, reserve=5000 | base clamped to 0; calm reserve-aware statement ("your reserve covers all your cash right now — nothing is sitting idle"); `reason="fully_reserved"`; forgone=0.00 | No error |
| Reserve never-decided | cash=5000, parked=0, reserve never-decided | reserve treated as 0 for the calc; `reserved`=null + `reserve_decided`=false surfaced; figure computed on 5000; statement stays calm, no FOMO | No error |
| Parked only, no settlement cash | cash=0, parked=4000, reserve=0, window +10%, apy 4% | forgone = 4000·(0.10−0.04) = 2400.00; discloses yield assumption | No error |
| Market fell over window | cash=5000, parked=0, window −10% | forgone negative → "avoided loss" framing (never "cost"); sky-blue not red | No error |
| No cash anywhere | cash=0, parked=0, reserve=0 | `reason="no_idle_cash"`, forgone=0.00, calm statement, source cited | No error |
| Insufficient market history | < lookback+1 bars | `sufficient=false`, `reason="insufficient_history"`, forgone=0.00, calm statement | No error |
| Backward-compat call | `estimate_missed_growth(session, idle_cash=X)` (no parked/reserved) | identical output to pre-9-2 (base=X, no yield term) | No error |
| Unauthenticated | no session | 401, no figure leaks | Auth gate |

</intent-contract>

## Code Map

- `ballast/backend/precedent/missed_growth.py` -- the engine to extend: add cash-state/yield-aware params + formula, `DEFAULT_MONEY_MARKET_APY`, new result fields, reserve-aware statements; keep backward-compatible defaults.
- `ballast/backend/api/precedent.py` -- `GET /api/precedent/missed-growth`; load `CashConfig` (read-only) + holdings, derive parked total + resolved reserve, pass to engine; add additive response fields.
- `ballast/backend/cash/config.py` -- add `parked_market_value(holdings, config) -> Decimal` helper (single source of the parked-sum + `resolve_reserve` reuse) so the read-path rule can't drift between endpoints.
- `ballast/backend/api/portfolio.py` -- refactor the existing inline parked-sum to call the new helper (consistency; keep `cash_states` output byte-identical).
- `ballast/frontend/src/components/MissedGrowthMeter.jsx` -- render the protected-reserve line + disclosed yield assumption alongside the figure; preserve no-red / no-nudge rules.
- `ballast/backend/tests/test_missed_growth.py`, `test_missed_growth_endpoint.py`, `ballast/frontend/src/test/missed-growth.test.jsx` -- cover the new math, degraded states, and calm voice.

## Tasks & Acceptance

**Execution:**
- [x] `ballast/backend/precedent/missed_growth.py` -- Add `DEFAULT_MONEY_MARKET_APY = Decimal("0.04")` (named, documented as a tunable placeholder to tune against real data). Extend `estimate_missed_growth` with `parked: Decimal = Decimal("0")`, `reserved: Decimal = Decimal("0")`, `money_market_apy: Decimal = DEFAULT_MONEY_MARKET_APY`. Compute reserve-parked-first split + yield-aware `forgone_growth` (see Design Notes). Clamp base ≥ 0; add `reason="fully_reserved"` when reserve covers all cash. Add result fields `settlement_cash`, `parked`, `reserved` (Decimal|None), `money_market_apy`, `investable_base` (`reserve_decided` is added at the endpoint layer, not the engine); set `idle_cash` = investable_base for the computed figure. Make statements reserve-aware and disclose the yield assumption when parked > 0. Ensure `to_dict()` renders all money as fixed-point strings.
- [x] `ballast/backend/cash/config.py` -- Add `parked_market_value(holdings, config) -> Decimal`: sum `market_value` of holdings whose normalized symbol is in `config.parked_symbols` (reuse `normalize_symbols`; `Decimal("0")` when config is None). Rationale: single source of the parked rule.
- [x] `ballast/backend/api/portfolio.py` -- Replace the inline parked-sum in `_to_out` with `parked_market_value(...)`. Rationale: one rule, no drift. `cash_states` output unchanged.
- [x] `ballast/backend/api/precedent.py` -- In `missed_growth`, load the scoped `CashConfig` read-only (`get_config`), compute parked via `parked_market_value`, resolve reserve via `resolve_reserve` (None→treat as 0 for the calc), and call the engine with `idle_cash=view.cash, parked=..., reserved=...`. Add additive fields to `MissedGrowthOut` including `reserve_decided` (from config), `reserved`, `parked`, `settlement_cash`, `investable_base`, `money_market_apy`. Keep the 401 gate and never-a-dead-end behavior.
- [x] `ballast/frontend/src/components/MissedGrowthMeter.jsx` -- Render a calm protected-reserve line when a reserve is present (e.g. "$X stayed protected, as you set it") and a disclosed yield-assumption note when parked > 0. Render the engine `statement` verbatim (AD-1). Preserve fail-quiet, no-red, no-nudge. When `reserve_decided` is false, do not fabricate a reserve figure.
- [x] `ballast/backend/tests/test_missed_growth.py` -- Unit-test the I/O matrix engine rows: yield-aware split, reserve parked-first, fully_reserved, parked-only, negative window, backward-compat (no parked/reserved == today), determinism, fixed-point JSON.
- [x] `ballast/backend/tests/test_missed_growth_endpoint.py` -- Endpoint tests: reserve set / declined / never-decided produce the right `reserved`/`reserve_decided`; parked holdings raise the figure honestly; disclosed apy present; 401 unauth.
- [x] `ballast/frontend/src/test/missed-growth.test.jsx` -- Assert the protected-reserve line and yield-assumption note render, statement shown verbatim, no `brand-red|accent-pink|line-red`, no FOMO/nudge copy, reserve absent when never-decided.

**Acceptance Criteria:**
- Given a user with settlement cash and parked money-market funds and a set reserve, when they read the missed-growth meter, then the forgone figure is computed on `cash + parked − reserve` (reserve drawn parked-first) with the parked portion offset by the disclosed money-market yield, and the protected reserve is shown alongside.
- Given any missed-growth response with parked > 0, when it is returned, then it discloses the money-market yield assumption used and never presents the pre-reserve amount as the figure.
- Given a reserve that is never-decided, when the meter renders, then no reserve figure is fabricated, the calc treats reserve as 0, and `reserve_decided=false` is surfaced — with calm, non-alarmist copy.
- Given the change set, when the backend `pytest` and frontend `vitest` suites run, then all pass (including the pre-existing engine/endpoint/meter tests, unchanged in the pure-settlement case), no DB migration is introduced, and all new copy passes the calm/no-red bars.

## Review Triage Log

### 2026-08-10 — Follow-up review pass
- intent_gap: 0
- bad_spec: 0
- patch: 1: (high 0, medium 0, low 1)
- defer: 0
- reject: 12
- addressed_findings:
  - `[low]` `[patch]` The endpoint's never-decided-reserve remap was a confusing self-assignment (`payload["reserved"] = None if resolved_reserve is None else payload["reserved"]`) on the honesty-critical path — reads as if it does something in both branches when the `else` reassigns the value to itself. Fixed: replaced with a plain `if resolved_reserve is None: payload["reserved"] = None` (behavior-identical, clarifies that this override is the ONLY place the honesty-crux null lives). Verified: 27 missed-growth tests + full backend suite (675 passed) + frontend meter tests (10 passed) all green.
- rejected/verified-safe (grounded against source, not consumer-impacting):
  - Negative `reserved` inflating the base / an unclamped negative-reserve path: NOT reachable — `cash/config.py::_validate_reserve` rejects negative, `NaN`, and `Infinity` reserve amounts at write time (calm 422), so the engine is never handed a negative reserve.
  - "`resolve_reserve` returns a value while `reserve_decided` is false" (frontend would render a reserve line contradicting the flag): impossible — both derive from the same `config.reserve_decided`; `resolve_reserve` returns `None` iff `reserve_decided` is false.
  - Frontend `reserveLine` renders "$NaN" for a non-numeric `reserved`: already handled — `formatUsd` returns `null` on `!Number.isFinite`, so `reserveLine`'s `amount == null` guard omits the line.
  - float `market_value` → `TypeError` now also on the portfolio read: no new blast radius — `portfolio.py::_to_out` already summed `market_value` identically before this change, the DB column is `Mapped[Decimal]`, and the prior pass already flagged it verified-safe; not confidently reachable → reject (not re-deferred).
  - `money_market_apy` carried on the wire in degraded (`no_idle_cash`/`insufficient_history`) states: harmless additive contract; the frontend gates the note on `parked > 0` so it never renders there.
  - `_TRADING_DAYS_PER_YEAR` (252) vs `LOOKBACK_TRADING_DAYS` (252) "duplication": correct by design — the annualization denominator must stay 252 even if a caller passes a custom `lookback_days`; a constant, not drift.
  - fully_reserved "covers all of your cash" when only parked money existed: money-market funds are cash-equivalents, so the phrasing is honest.
  - Speculative future-coupling (fully_reserved reachable with a null reserve; negative/zero APY; `lookback_days=0`): no current caller produces these; APY is the fixed `0.04` constant.
  - `reserveLine` collapses "declined $0" vs "never-decided" to no line: by design (a $0 reserve has nothing to show protected); the engine statement already omits the reserve clause in both cases.
- deferred: none new. The one genuine forward-looking item both reviewers re-surfaced — backend/frontend APY percentage formatted independently (would diverge only for a future non-round tuned APY) — is ALREADY recorded in `deferred-work.md` from the prior 9-2 pass; not duplicated.

### 2026-08-10 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 3: (high 1, medium 1, low 1)
- defer: 1: (high 0, medium 0, low 1)
- reject: 7
- addressed_findings:
  - `[high]` `[patch]` Statement authoring keyed market-direction wording ("the market fell" / "roughly flat") on the dollar `forgone_growth` sign — with parked money in a modestly rising market (0 < window_return < yield-over-window) the yield offset makes forgone negative while the market rose, so the copy claimed "the market fell" when it did not. Fixed: market-direction wording now keys on `window_return`; parked-outpaces-a-rising-market and net-$0-but-market-moved get honest "kept pace / came out ahead" copy. Regression test `test_modestly_rising_market_parked_outpaces_never_says_market_fell` added.
  - `[medium]` `[patch]` `reason="fully_reserved"` (new enum value) fell through to the figure component on the frontend — "Idle cash, over the past year" eyebrow, empty window line, and a yield note beneath a $0.00 figure. Fixed: dedicated calm `missed-growth-fully-reserved` block (statement + protected-reserve line, no figure, no yield note); frontend test added.
  - `[low]` `[patch]` `parked_market_value` matched holdings via inline `symbol.strip().upper()` while the stored set used `normalize_symbols` — two spellings of the "single source" rule. Fixed: holding side now normalizes through `normalize_symbols`.
  - Also added (Blind-Hunter coverage gap): a falling-window + reserve + parked engine test locking the composed "avoided loss" + reserve-protected + yield-disclosure copy.
- rejected/verified-safe (not consumer-impacting): the 5 new `MissedGrowthEstimate` fields are non-breaking (verified only the engine + its tests construct it; full suite green); reserve "just as you set it" clause traced honest in all reachable figure cases; float `market_value` TypeError is a pre-existing assumption (no regression); the spec I/O-matrix "Parked only" example says `2400.00` but the correct value is `240.00` — the CODE is correct and tested at `240.00`; the example lives inside the read-only `<intent-contract>`, so it is left as-is and flagged here for a manual one-line fix.
- deferred: APY is rendered independently on backend (`quantize(0.1)`) and frontend (`toLocaleString maxFrac:1`) — identical while APY is the round `0.04`, but a future non-round tuned APY could make the two disagree; recorded in `deferred-work.md`.

## Design Notes

**The yield-aware formula (deterministic, reserve-parked-first):**
```python
parked_investable = max(parked - reserved, Decimal("0"))
reserve_left      = max(reserved - parked, Decimal("0"))
cash_investable   = max(settlement_cash - reserve_left, Decimal("0"))
investable_base   = cash_investable + parked_investable          # == max(cash+parked-reserve, 0)
yield_over_window = _q(money_market_apy * Decimal(lookback_days) / Decimal(252))
forgone_growth    = _cents(cash_investable * window_return
                           + parked_investable * (window_return - yield_over_window))
```
`_q` (4-dp) and `_cents` (cents, ROUND_HALF_UP) already exist. When `parked == 0` and `reserved == 0`, `investable_base == settlement_cash` and the yield term vanishes → identical to today (backward-compat).

**Why a default APY constant, not a live feed or a user field:** money-market yields are not in the market-data set (Tiingo carries only the 14 index ETFs), and 9-1 stores no yield. A single **disclosed** default (`DEFAULT_MONEY_MARKET_APY = 0.04`) is honest — the copy always states the assumption ("counting your parked cash as already earning about 4% a year") — and needs no schema change. It is a named, tunable placeholder; add a retro/action item to tune it against real data and to consider a user-editable yield later (mirrors the Epic 8 heuristic-tuning action item).

**Statement framing (engine owns it, UI renders verbatim — AD-1):** reserve-aware and calm. Example (rising, with parked + reserve): "Over the past year, about $8,000 of investable cash sat out roughly $700 of growth — and your $2,000 reserve stayed protected, just as you set it (counting your parked money-market cash as already earning about 4% a year)." Falling → "avoided" framing (never "cost"). `fully_reserved` → "Your reserve covers all of your cash right now — nothing is sitting idle to invest." Never-decided reserve → no reserve clause; stays calm.

## Verification

**Commands:**
- `cd ballast/backend && DATABASE_URL=<disposable ballast_test> BALLAST_ALLOW_DIRTY_BROKERAGE_DB=1 uv run pytest -q tests/test_missed_growth.py tests/test_missed_growth_endpoint.py` -- expected: all pass, including new yield-aware/reserve rows.
- `cd ballast/backend && DATABASE_URL=<disposable ballast_test> BALLAST_ALLOW_DIRTY_BROKERAGE_DB=1 uv run pytest -q` -- expected: full suite green (no regressions; backward-compat holds).
- `cd ballast/frontend && npm test` -- expected: `missed-growth.test.jsx` and the rest pass; no red/FOMO.

**Manual checks:**
- Confirm no `db/migrations.py` entry and no new column were added (9-2 is additive-only).
- Confirm the missed-growth response includes `reserved`, `reserve_decided`, `parked`, `settlement_cash`, `investable_base`, `money_market_apy`, all money as fixed-point strings.

## Auto Run Result

Status: done (follow-up review pass on an already-`done` spec; the prior pass had set `followup_review_recommended: true`).

**Summary of change (this pass):** A fresh adversarial + edge-case review of the full Story 9.2 diff since baseline `355dcd4`. Two reviewers (Blind Hunter, Edge Case Hunter) ran in parallel with no prior context. Findings were grounded against the actual source before triage. Result: 1 low-severity readability patch applied, 0 defers (the one genuine forward-looking item was already in the ledger), 12 findings rejected as non-reachable / already-handled / cosmetic. No intent gaps, no spec repairs.

**Files changed (this pass):**
- `ballast/backend/api/precedent.py` — replaced a confusing self-assigning ternary on the honesty-critical never-decided-reserve remap with a plain guarded `if resolved_reserve is None: payload["reserved"] = None` (behavior-identical).

**Review findings breakdown:**
- Patches applied: 1 (`[low]`) — endpoint reserve-remap clarity.
- Deferred: 0 new (APY backend/frontend formatting divergence is already recorded in `deferred-work.md` from the prior 9-2 pass; not duplicated).
- Rejected: 12 — most notably: negative `reserved` is impossible (validated at write); `resolve_reserve`↔`reserve_decided` are the same flag; `$NaN` already guarded by `formatUsd`; float `market_value` has no new blast radius (portfolio.py already summed it) and is unreachable given `Mapped[Decimal]`; the 252-day annualization constant is correct-by-design, not drift.

**Verification performed:**
- `pytest tests/test_missed_growth.py tests/test_missed_growth_endpoint.py` (DATABASE_URL=ballast_test, BALLAST_ALLOW_DIRTY_BROKERAGE_DB=1) → **27 passed**.
- Full backend `pytest -q` → **675 passed** (no regressions).
- Frontend `vitest run src/test/missed-growth.test.jsx` → **10 passed**.

**Follow-up review recommendation:** `false` — this pass made one localized, behavior-preserving, low-consequence readability fix; nothing touching behavior, API shape, security, or data. Per the workflow's judgment guidance, a few localized low-consequence fixes do not warrant another independent review.

**Residual risks:** None new. The already-deferred APY-formatting divergence remains a latent cosmetic concern only if `DEFAULT_MONEY_MARKET_APY` is ever tuned to a non-round value; it is tracked in `deferred-work.md`.

