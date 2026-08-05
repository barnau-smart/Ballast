# Story 8.6: Suggestion Honesty — Fill-Likelihood + Falling-Market Floor + Data Freshness

Status: ready-for-dev

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

- [ ] **Task 1 — Fill-likelihood (backend) (AC: #1, #4)**
  - [ ] In `coach/suggest.py::suggest_resting_order`, after `limit_price` + `ask` are known, compute `pct_below_ask = (ask − limit_price) / ask` (Decimal, quantized) and a banded `fill_note` (pure helper `fill_likelihood(pct_below_ask) -> (band, note)`; deterministic bands + calm copy in the NFR8 voice).
  - [ ] Add `pct_below_ask: Decimal` and `fill_note: str` to `SuggestedOrder` (frozen dataclass).
  - [ ] Feed both into `narrate_suggestion` facts so the LLM weaves the honesty in; include the fill-likelihood sentence in `_fallback_reasoning` too (deterministic).
- [ ] **Task 2 — Falling-market floor (backend) (AC: #2, #4)**
  - [ ] Add constant `SUGGEST_MIN_DISCOUNT_FROM_ASK: Decimal` (propose `Decimal("0.02")`; named for one-line tuning like the existing 8.4 constants).
  - [ ] In `compute_suggested_price(recent_low, ask)` (keep it pure, no new args if possible — it already has `ask`): floor with `min(base, quantize_2dp_down(ask * (1 − SUGGEST_MIN_DISCOUNT_FROM_ASK)))`. Confirm it never rises above the old behavior in rising/flat markets (min preserves the deeper discount) and only bites the new-lows case.
  - [ ] Re-size whole shares off the (possibly-floored) `limit_price` — it already does (`whole_share_quantity(budget, limit_price)` runs after compute); just confirm ordering.
- [ ] **Task 3 — Data-freshness guard (backend) (AC: #3, #4)**
  - [ ] Extend `_recent_low` (or add `_recent_low_and_asof`) to also return the newest bar's `day`. Add an `as_of: date` param to `suggest_resting_order` (endpoint passes `date.today()`; tests pass a fixed date — keeps determinism).
  - [ ] If `as_of − newest_day > SUGGEST_STALE_AFTER_DAYS`: attach a calm `stale_data` note (keep the suggestion) — and refuse calmly (`OrderScopeError`) ONLY beyond a hard cutoff if chosen. Document the threshold + the note-vs-refuse decision in Dev Notes.
- [ ] **Task 4 — API surface (AC: #1, #3)**
  - [ ] Add `pct_below_ask: str` (fixed-point via `_money_str`/percent formatter), `fill_note: str`, and an optional `stale_note: str | None` to `SuggestOrderResponse` (`api/coach.py`). Wire the `_to_suggest_response`/endpoint mapping. Money/percent as strings, never float.
- [ ] **Task 5 — Frontend (AC: #1, #3)**
  - [ ] In `frontend/src/components/CoachConsult.jsx` suggest render path (the `suggested` phase, `suggestReasoning`/`pendingSuggestion` state), display the `fill_note` (and `stale_note` if present) as a calm, dismissable line beside the reasoning. Reuse the existing suggest UI; do not build a parallel surface. Thread `fill_note`/`stale_note` through `pendingSuggestion` so they survive the ask round-trip (mirror how `reasoning` is carried).
- [ ] **Task 6 — Tests (AC: all)**
  - [ ] Backend: deterministic tests for `pct_below_ask` + `fill_note` bands; the floor across rising/flat/**falling(new-lows)** fixtures; freshness (fresh → no note; stale → note/refuse) with a fixed `as_of`; real-vs-fake gateway number+note identity; existing 8.4 suggest tests still green.
  - [ ] Frontend: the suggest render shows the fill/stale note; component/interaction test alongside the existing 8.4 suggest tests.
  - [ ] Run `uv run pytest tests/test_suggest_order.py tests/test_coach_api.py -q` (now fast — Story 8.5) and the frontend suite; confirm green.

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

### Debug Log References

### Completion Notes List

### File List
