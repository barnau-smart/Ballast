# Story 8.6: Suggestion Honesty — Fill-Likelihood + Falling-Market Floor + Data Freshness

Status: done

<!-- baseline_revision: 8cc736e07ad87ce2fd6f4e15ec213dcf4cad175b -->
<!-- review_loop_iteration: 0 -->
<!-- followup_review_recommended: false -->
<!-- final_revision: 88a97f7b8db36b040b81b88acfd9f1956d8f105c -->

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As **a beginner using the "Suggest this order" button (Story 8.4)**,
I want **the suggested resting-limit order to tell me honestly how likely it is to fill, never sit unreasonably close to the market in a sell-off, and never be priced off stale data**,
so that **I understand what will actually happen ("this may take a while, or never — you can cancel anytime") instead of placing an order and silently wondering why nothing happens.**

**Feature context.** This refines the Story 8.4 suggest path (`coach/suggest.py`) with three *honesty/robustness* additions surfaced by MasterB's design review of the 8.4 pricing heuristic. **These are NOT correctness bugs** — 8.4 is safe as shipped (always buys below market, rounds down, filters bad data, refuses when it can't compute). **The locked 8.4 architecture is unchanged: the backend owns every number deterministically; the LLM only narrates.** All new context (fill-likelihood, floor, freshness) is backend-computed and fed to the narration as facts.

## Acceptance Criteria

1. **Fill-likelihood (primary). Given** a computed suggestion, **when** it is returned, **then** the backend computes a deterministic **distance below the live ask** (`(ask − limit_price) / ask`) and a **calm, banded plain-English `fill_note`** (e.g. near-market / meaningfully-below / far-below → "this may fill soon" … "this is well below today's price; it may take a while, or may not fill — you can cancel anytime"). Both are returned in the API response and rendered in the UI next to the reasoning. Same inputs ⇒ identical `pct_below_ask` + `fill_note`, independent of the LLM.
2. **Falling-market floor (secondary). Given** a symbol making new lows so the base formula would sit only ~1% below the ask, **when** the price is computed, **then** a floor guarantees the suggestion is **never closer than `SUGGEST_MIN_DISCOUNT_FROM_ASK` below the live ask** (`limit_price = min(base_formula, quantize_2dp_down(ask * (1 − SUGGEST_MIN_DISCOUNT_FROM_ASK)))`), verified deterministically across rising / flat / falling-market fixtures (including the new-lows case that today collapses to ~1%). Whole-share sizing uses the floored price.
3. **Data-freshness (robustness). Given** the newest `MarketDaily` bar for the symbol is older than a freshness threshold, **when** a suggestion is requested, **then** the response honestly signals delayed data (a calm `stale_data` note; a hard refusal ONLY if extremely stale — decide + document the threshold), rather than silently pricing off old bars. Determinism preserved by injecting the reference date (`as_of`), NOT reading the wall clock inside the pure pricing path.
4. **Determinism + narration invariants preserved (from 8.4).** `compute_suggested_price` stays pure; the LLM still only narrates (receives the finished `pct_below_ask`/`fill_note` as facts, never computes them); a gateway outage still degrades to the deterministic templated fallback (now including the fill-likelihood sentence). A test proves the numbers + `fill_note` are identical with the real vs fake gateway.
5. **All 8.1–8.5 + existing tests stay green.** `/recommend` stays MARKET-only; STOP/STOP_LIMIT/AM-PM stay rejected; `is_index_core` still gates suggestions; nothing executes without the human `/approve`.

## Tasks / Subtasks

- [x] **Task 1 — Fill-likelihood (backend) (AC: #1, #4)**
  - [x] In `coach/suggest.py::suggest_resting_order`, after `limit_price` + `ask` are known, compute `pct_below_ask = (ask − limit_price) / ask` (Decimal, quantized) and a banded `fill_note` (pure helper `fill_likelihood(pct_below_ask) -> (band, note)`; deterministic bands + calm copy in the NFR8 voice).
  - [x] Add `pct_below_ask: Decimal` and `fill_note: str` to `SuggestedOrder` (frozen dataclass).
  - [x] Feed both into `narrate_suggestion` facts so the LLM weaves the honesty in; include the fill-likelihood sentence in `_fallback_reasoning` too (deterministic).
- [x] **Task 2 — Falling-market floor (backend) (AC: #2, #4)**
  - [x] Add constant `SUGGEST_MIN_DISCOUNT_FROM_ASK: Decimal` (`Decimal("0.02")`; named for one-line tuning like the existing 8.4 constants).
  - [x] In `compute_suggested_price(recent_low, ask)` (kept pure, no new args): floor with `min(base, quantize_2dp_down(ask * (1 − SUGGEST_MIN_DISCOUNT_FROM_ASK)))`. Confirmed via tests it never rises above the old behavior in rising/flat markets (min preserves the deeper discount) and only bites the new-lows case.
  - [x] Whole shares re-size off the (possibly-floored) `limit_price` — `whole_share_quantity(budget, limit_price)` already runs after compute; ordering confirmed.
- [x] **Task 3 — Data-freshness guard (backend) (AC: #3, #4)**
  - [x] Added `_recent_low_and_asof` to also return the newest bar's `day`. Added an `as_of: date` param to `suggest_resting_order` (endpoint passes `date.today()`; tests pass a fixed date — keeps determinism).
  - [x] If `as_of − newest_day > SUGGEST_STALE_AFTER_DAYS`: attach a calm `stale_note` (keep the suggestion); refuse calmly (`OrderScopeError`) beyond `SUGGEST_STALE_REFUSE_AFTER_DAYS`. Threshold + note-vs-refuse decision documented in Dev Notes.
- [x] **Task 4 — API surface (AC: #1, #3)**
  - [x] Added `pct_below_ask: str` (fixed-point via new `_pct_str`), `fill_note: str`, and optional `stale_note: str | None` to `SuggestOrderResponse` (`api/coach.py`). Wired the endpoint mapping. Money/percent as strings, never float.
- [x] **Task 5 — Frontend (AC: #1, #3)**
  - [x] In `frontend/src/components/CoachConsult.jsx` suggest render path (the `suggested` phase, `suggestReasoning`/`pendingSuggestion` state), displays the `fill_note` (and `stale_note` if present) as calm lines beside the reasoning. Reused the existing suggest UI. Threaded `fill_note`/`stale_note` through `pendingSuggestion` so they survive the ask round-trip (mirrors how `reasoning` is carried + re-seeded when the symbol still matches).
- [x] **Task 6 — Tests (AC: all)**
  - [x] Backend: deterministic tests for `pct_below_ask` + `fill_note` bands; the floor across rising/flat/**falling(new-lows)** fixtures; freshness (fresh → no note; stale → note; extreme → refuse) with a fixed `as_of`; real-vs-fake gateway number+note identity; existing 8.4 suggest tests still green.
  - [x] Frontend: the suggest render shows the fill/stale note; interaction tests (render, stale render, round-trip survival) alongside the existing 8.4 suggest tests.
  - [x] Ran `uv run pytest tests/test_suggest_order.py tests/test_coach_api.py -q` and the frontend suite; both green.

## Dev Notes

### Current state of the code being modified (READ THESE)

- **`ballast/backend/coach/suggest.py`** (Story 8.4, the primary file). Key shapes:
  - `SUGGEST_LOOKBACK_DAYS = 20`, `SUGGEST_DISCOUNT = Decimal("0.01")` (named constants; add `SUGGEST_MIN_DISCOUNT_FROM_ASK`, `SUGGEST_STALE_AFTER_DAYS` beside them).
  - `compute_suggested_price(recent_low, ask)` — pure: `quantize_2dp_down(min(recent_low, ask) * (1 − SUGGEST_DISCOUNT))`, always `< ask`. **Add the floor here; keep it pure.**
  - `_recent_low(session, symbol)` — min `low` over the newest 20 bars, skips non-finite/≤0, returns `None` if no bars. **Extend to also surface the newest `day` for freshness.**
  - `narrate_suggestion(gateway, facts)` — LLM narrates the finished numbers; **resilient fallback `_fallback_reasoning`** on ANY exception. Add fill-likelihood to both.
  - `suggest_resting_order(...)` — the orchestration owner (gate `is_index_core` → `_recent_low` → `broker.get_quote` ask → `compute_suggested_price` → `whole_share_quantity` off `min(target, cash)` → narrate). Calm declines via `OrderScopeError` / `OrderNotPlaceableError`. **Add `as_of`, fill-likelihood, freshness here.**
  - Money is `Decimal` end-to-end; the API serializes to fixed-point strings.
- **`ballast/backend/api/coach.py`** — `SuggestOrderRequest{symbol, amount?}`, `SuggestOrderResponse{symbol, side, order_type, limit_price(str), duration, amount(str), shares, reasoning}` (~line 388+). `_money_str` = fixed-point serializer. **Add `pct_below_ask`/`fill_note`/`stale_note` to the response.** The `/suggest-order` endpoint is per-user scoped; declines map to calm 422.
- **`ballast/frontend/src/components/CoachConsult.jsx`** — suggest state machine: `suggest` phase (`idle|suggesting|suggested|suggest-failed`), `suggestReasoning`, `pendingSuggestion` (held outside `options` so it survives an ask round-trip; re-seeded when symbol still matches — mirror this for the new notes). Populates the frozen 8.3 controls; places nothing.

### What must be preserved (regression guardrails)

- **Backend owns the number; LLM only narrates** — `pct_below_ask`/`fill_note`/floor/freshness are ALL backend-computed; the model receives them as facts. Determinism (same bars+ask+as_of ⇒ same numbers+notes) is the safety property — pin it with tests.
- `compute_suggested_price` stays **pure** (no wall clock, no LLM). Freshness uses an injected `as_of`, not `date.today()` inside the pure path.
- The floor uses `min(...)` so it **only tightens the new-lows case** and never weakens the deeper discount in rising/flat markets.
- Calm declines stay calm (422 envelope, never 500, never a phantom order). Nothing executes without `/approve`.
- Reuse the existing suggest UI + resilient-fallback pattern; do not fork surfaces (avoid the Epic 4 "centerpiece missed" pattern).

### Testing standards

- Backend: pytest + real docker Postgres; **fake LLM is now forced suite-wide** (Story 8.5 `tests/conftest.py` sets `LLM_ADAPTER=fake`) — so `narrate_suggestion` is deterministic in tests and the suite is fast (~20s). Seed `MarketDaily` fixtures for the rising/flat/falling(new-lows) + stale cases. Assert numbers/notes WITHOUT the LLM (pure functions) and the real-vs-fake identity.
- Frontend: component/interaction tests alongside the existing 8.4 suggest tests (`frontend/src/test/coach-consult.test.jsx`).

### Data-freshness thresholds — decision + reasoning (Story 8.6, AC #3)

Two named constants gate freshness against the injected `as_of` (never the wall
clock inside the pure path):

- `SUGGEST_STALE_AFTER_DAYS = 5` (calendar days) — **NOTE, keep the suggestion.**
  When `as_of − newest_bar_day > 5`, the suggestion is still returned but carries a
  calm `stale_note` ("the newest price data … is N days old … double-check today's
  price before you approve"). Chosen at 5 because a normal weekend plus a market
  holiday can legitimately leave the newest *daily* bar ~4 days old (e.g. asking on
  a Tuesday after a long weekend), so 5 is the first gap that reliably means "the
  feed is actually behind", not merely a closed market. Boundary is strict `>`, so
  exactly 5 days old is still fresh (no note).
- `SUGGEST_STALE_REFUSE_AFTER_DAYS = 30` (calendar days) — **HARD REFUSE
  (`OrderScopeError` → calm 422).** When `as_of − newest_bar_day > 30`, we refuse
  rather than anchor a real-money resting order on a stale low. Chosen at 30 because
  roughly a full trading month of missing bars is unambiguously a broken/paused feed
  — well past any holiday gap — and pricing a live order off a month-old low would
  be dishonest.

**Note-vs-refuse decision:** we chose a *two-tier* policy (note between 5 and 30
days, refuse beyond 30) rather than note-only. Rationale: a mild delay is honestly
surfaced but still useful (the user can sanity-check against today's price and
cancel anytime, per the fill-note voice); an extreme delay makes the recent-low
anchor meaningless, so silently pricing off it — even with a note — would violate
the "honest coach" contract. The refusal reuses the existing calm `OrderScopeError`
→ 422 envelope (never a 500, never a phantom order). Both thresholds are named
constants for one-line tuning, matching the 8.4 pattern.

### Project Structure Notes

- Modified: `coach/suggest.py`, `api/coach.py`, `frontend/src/components/CoachConsult.jsx` (+ CSS if needed), backend + frontend tests. No new files strictly required. No new broker/port surface. No DB/schema change (suggestion is computed, never persisted).

### References

- [Source: ballast/backend/coach/suggest.py] — the 8.4 engine (constants, `compute_suggested_price`, `_recent_low`, `narrate_suggestion`, `suggest_resting_order`).
- [Source: ballast/backend/api/coach.py:388+] — `SuggestOrderRequest`/`SuggestOrderResponse`, `_money_str`, the `/suggest-order` endpoint.
- [Source: ballast/frontend/src/components/CoachConsult.jsx] — suggest state machine + populate flow.
- [Source: _bmad-output/implementation-artifacts/8-6-suggestion-honesty-fill-likelihood.brief.md] — scope + the ranked refinements.
- [Continuity: Story 8.4 (spec-8-4-ai-suggest-populate-order.md)] — locked "backend computes, LLM narrates" + the pricing formula this extends.
- [Memory: [[order-interface-expansion-plan]] (8.4 pricing locked), [[fake-mode-vs-real-coaching]], [[loop-cannot-self-verify-test-perf]] (tests force fake LLM), [[masterb-working-style]] (calm/honest coach voice)].

## Dev Agent Record

### Agent Model Used

claude-opus-4-8 (bmad-dev-auto)

### Debug Log References

- `uv run pytest tests/test_suggest_order.py tests/test_coach_api.py -q` (from ballast/backend) → **149 passed, 1 warning in 22.77s**.
- `npm test` (vitest run, from ballast/frontend) → **14 files, 139 tests passed**; coach-consult isolated run → **34 passed**.

### Completion Notes List

- **Backend owns the number; LLM only narrates (invariant preserved).** `pct_below_ask`, `fill_note`, the floor, and freshness are ALL backend-computed. `compute_suggested_price` stays pure (no wall clock, no LLM). Freshness uses the injected `as_of`, never `date.today()` inside the pure path.
- **Fill-likelihood (Task 1):** added pure `fill_likelihood(pct_below_ask) -> (band, note)` with three deterministic, inclusive-low bands — near-market (`<2%`), meaningfully-below (`<5%`), far-below (`>=5%`) — in the calm/honest coach voice (never promises a fill, always "you can cancel anytime"). `pct_below_ask` = `(ask − limit_price)/ask` quantized to 4 dp. Both added to the frozen `SuggestedOrder` and fed into `narrate_suggestion` facts AND appended to `_fallback_reasoning`.
- **Falling-market floor (Task 2):** `SUGGEST_MIN_DISCOUNT_FROM_ASK = Decimal("0.02")`; `compute_suggested_price` returns `min(base, quantize_2dp_down(ask*(1−0.02)))`. `min()` means it only tightens the new-lows case and never weakens the deeper discount in rising/flat markets. One existing 8.4 unit test (`recent_low 120 ≥ ask 100`) legitimately changed from `99.00` → `98.00` — that ~1% clamped case is exactly what the floor is designed to tighten; test updated (still strictly `< ask`).
- **Freshness (Task 3):** `_recent_low_and_asof` returns `(min_low, newest_day)`; `suggest_resting_order` gained an `as_of: date` param. Note between 5 and 30 days, calm refuse (`OrderScopeError`) beyond 30. Thresholds + reasoning documented in Dev Notes.
- **API (Task 4):** `SuggestOrderResponse` gained `pct_below_ask: str` (via new `_pct_str` fixed-point serializer), `fill_note: str`, `stale_note: str | None`; endpoint injects `as_of=date.today()`. Declines stay calm 422.
- **Frontend (Task 5):** reused the existing suggest panel; added `suggestFillNote`/`suggestStaleNote` state, threaded both through `pendingSuggestion` (re-seeded on the ask round-trip when the symbol matches, mirroring `reasoning`), and rendered them as calm lines (`data-testid` `coach-suggest-fill-note` / `coach-suggest-stale-note`) plus CSS.
- **Regression guardrails preserved:** `/recommend` untouched (MARKET-only); STOP/STOP_LIMIT/AM-PM still rejected; `is_index_core` still gates; nothing executes without `/approve`; real-vs-fake gateway now proven identical for numbers AND notes.

### File List

- `ballast/backend/coach/suggest.py` (constants, `fill_likelihood`, floor in `compute_suggested_price`, `_pct_below_ask`, `_recent_low_and_asof`, freshness + facts in `suggest_resting_order`, `SuggestedOrder` fields, `narrate_suggestion`/`_fallback_reasoning`)
- `ballast/backend/api/coach.py` (`SuggestOrderResponse` fields, `_pct_str`, `as_of` injection + response mapping)
- `ballast/frontend/src/components/CoachConsult.jsx` (fill/stale note state, threading, render)
- `ballast/frontend/src/components/CoachConsult.css` (fill/stale note styling)
- `ballast/backend/tests/test_suggest_order.py` (floor / fill-band / freshness / note-identity tests; `as_of` + `_fresh_day0` helpers; updated one 8.4 clamp assertion)
- `ballast/frontend/src/test/coach-consult.test.jsx` (fill-note render, stale-note render, round-trip survival; `SUGGESTION` fixture extended)

## Review Triage Log

### 2026-08-05 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 4: (high 0, medium 0, low 4)
- defer: 0
- reject: 13: (high 0, medium 0, low 13)
- addressed_findings:
  - `[low]` `[patch]` Fill-likelihood band now chosen from the **exact unrounded** `(ask − limit_price)/ask` fraction, not the 4dp `ROUND_HALF_UP` wire value — display rounding can no longer nudge a distance across a band edge (`suggest_resting_order`). Wire `pct_below_ask` stays 4dp.
  - `[low]` `[patch]` `_recent_low_and_asof` now derives `newest_day` from the newest **usable** bar (max day among kept rows), not `rows[0]` — a filtered bad-low row can no longer set the freshness clock while contributing nothing to the price.
  - `[low]` `[patch]` `narrate_suggestion`: moved the pure `pct_display` math **out of** the gateway-failure `try` (an arithmetic bug now fails loudly instead of silently degrading to the fallback) and aligned its rounding to `ROUND_HALF_UP` so the narrated percent matches the serialized `pct_below_ask`.
  - `[low]` `[patch]` Freshness gap clamped to `max(..., 0)` — a bar dated after `as_of` (timezone/clock skew or a corrupt future-dated row) can no longer produce a negative staleness that reads as "fresh" by accident.

Rejected (dropped): 2%-floor/2%-near-market band collision (behavior is honest — a 2%-below resting order genuinely "may take a while"; the near-market copy is intentionally reserved for sub-floor distances); `date − datetime` TypeError (the 149-test suite exercises the real `as_of=date.today()` subtraction — `MarketDaily.day` is a `date`); 30-day refuse boundary `>` vs `>=` (documented, symmetric with the 5-day note boundary); pure-fn guards for an unusable `ask` reaching `compute_suggested_price`/`_pct_below_ask` (ask is validated upstream — defense-in-depth already present); new required response fields as an API break (the web client is the sole consumer); assorted frontend `??`/null-provenance and test-comment nits.

## Auto Run Result

Status: done

**Summary.** Refined the Story 8.4 suggest path with three honesty/robustness additions — a backend-computed fill-likelihood note, a falling-market price floor (never closer than 2% below the live ask), and a data-freshness gate (calm note after 5 days, calm refusal after 30) — all while preserving the locked "backend owns every number; the LLM only narrates" invariant and the pure, wall-clock-free pricing path.

**Files changed.**
- `ballast/backend/coach/suggest.py` — fill-likelihood bands + floor + freshness + `as_of` injection; new honesty fields on the frozen `SuggestedOrder`; review patches (exact-fraction banding, usable-bar freshness clock, pure display math outside the gateway try, non-negative staleness clamp).
- `ballast/backend/api/coach.py` — `pct_below_ask`/`fill_note`/`stale_note` on `SuggestOrderResponse`, `_pct_str` fixed-point serializer, `as_of=date.today()` injection at the endpoint boundary.
- `ballast/frontend/src/components/CoachConsult.jsx` (+ `.css`) — calm fill/stale note lines threaded through `pendingSuggestion` so they survive the ask round-trip; reused the existing suggest panel.
- `ballast/backend/tests/test_suggest_order.py`, `ballast/frontend/src/test/coach-consult.test.jsx` — deterministic band/floor/freshness/note-identity tests + frontend render/round-trip tests.

**Review findings breakdown.** 4 low-severity patches applied (all localized to `coach/suggest.py`, zero behavior change to the passing tests — determinism/robustness hardening). 13 findings rejected as noise/unreachable/single-consumer. 0 intent gaps, 0 bad-spec loopbacks, 0 deferrals.

**Verification.**
- `uv run pytest tests/test_suggest_order.py tests/test_coach_api.py -q` (from `ballast/backend`, after patches) → **149 passed, 1 warning in 24.51s**.
- Frontend (from `ballast/frontend`) → **14 files, 139 tests passed** (coach-consult isolated → 34 passed) from the implementation pass; frontend untouched by the review patches.

**Residual risks.** Low. The fill-likelihood band thresholds (2%/5%) and freshness thresholds (5d/30d) are heuristics chosen without live-data tuning — named constants for one-line adjustment once real fill/staleness data exists. The near-market band is currently unreachable given the 2% floor (intentional: the floor keeps every suggestion patiently below market); it remains as coherent defensive code if the floor is ever lowered.

**followup_review_recommended:** false — the review pass applied only 4 low-severity, single-file hardening patches with no behavior/API/data-shape change and no new test failures.

## Independent Code Review (2026-08-05, bmad-code-review — 3 adversarial layers)

Run out-of-loop because the loop wedged in `dev-verify` before its own review could finalize (see below); the work was independently test-verified green (149 passed) first, then adversarially reviewed. **No Critical/High findings** — core money-math, `min()` floor direction, determinism/purity, fixed-point serialization, calm 422 error mapping, and the real-vs-fake-LLM number identity are all verified correct.

### Review Findings

- [x] [Review][Patch] Band selected from an unguarded recomputed division; band vs displayed-percent could disagree at an edge [coach/suggest.py:467-469] — APPLIED: select the band from the same guarded `pct_below_ask` (removes the second, unguarded `(ask-limit)/ask` division — a latent divide-by-zero→500 if a future change weakened the ask guard — AND guarantees the banded copy and the wire/narrated percent always agree). Re-verified: `test_suggest_order.py` 41 passed.
- [x] [Review][Defer] Near-market "may fill soon" band is unreachable under the 2% floor [coach/suggest.py:151-186] — already documented + accepted in this story's Residual Risks (coherent defensive code if the floor is lowered). Not a defect.
- [x] [Review][Defer] Frontend drops the fill/stale note on the "answered-without-executable-order" ask branch [CoachConsult.jsx:206-224] — mirrors pre-existing 8.4 `reasoning` behavior, not a regression from 8.6.
- [x] [Review][Dismiss] `str(facts.get("fill_note"))` → "None" if the fact were None — unreachable: the sole caller always passes a non-empty banded string.
- [x] [Review][Dismiss] Freshness `>` boundary "off-by-one" at 5d/30d — deliberate (`older than`), pinned by tests.
- [x] [Review][Dismiss] `pct_below_ask` wired but not rendered by the UI — intentional API surface; a client may display it.

**Result:** 0 decision-needed, 1 patch (applied), 2 deferred (both pre-existing/accepted), 3 dismissed as noise.
