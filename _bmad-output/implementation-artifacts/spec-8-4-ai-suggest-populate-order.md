---
title: 'Story 8.4: AI "Suggest & Populate the Order" Button (Order Interface Expansion — Story D, MasterB core vision)'
type: 'feature'
created: '2026-08-04'
baseline_revision: 'd37d0213bfcd9c51f4dca6a222fc456a28c6a8e5'
final_revision: 'c35ffd503295d7187b606b6ae509865eab7f1ee6'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: [oversized]
---

<intent-contract>

## Intent

**Problem:** Stories 8.1–8.3 built the full order backend (marketable + resting limits, GTC, cancel) and the human order-entry UI, but a beginner still has to invent the "buy near the low and wait" limit price by hand. MasterB's north-star is an optional button that computes a sensible resting-limit order and fills the form for them.

**Approach:** Add a per-user, live-session-gated `POST /api/coach/suggest-order` endpoint. The **backend deterministically computes** a resting BUY limit price ("a touch below the recent 20-day low, always strictly below the live ask"), sizes a whole-share dollar amount from the user's real idle cash, and asks the LLM gateway to **narrate that already-computed number in plain English** — the model never does money-math and never sets the price. A frontend "Suggest this order" button calls it and populates the frozen 8.3 controls (side, LIMIT, limit price, GTC, amount) plus the reasoning inline. Nothing executes: the human still runs the existing `/approve` → co-sign → place path.

## Boundaries & Constraints

**Always:**
- **The backend owns the number.** `limit_price` and `amount` are computed entirely in backend code with zero LLM involvement. Same symbol + same `MarketDaily` + same live ask ⇒ byte-identical price, regardless of which LLM adapter runs (assert this without the LLM).
- **Locked deterministic formula** (see Design Notes): `recent_low` = min `low` over the most recent `SUGGEST_LOOKBACK_DAYS = 20` `MarketDaily` bars for the symbol; `limit_price = quantize_2dp_down( min(recent_low, ask) * (1 - SUGGEST_DISCOUNT) )` with `SUGGEST_DISCOUNT = Decimal("0.01")`. Because the discount applies to `min(recent_low, ask)`, the result is always strictly below the live ask (a genuine resting buy).
- **Money is `Decimal`/fixed-point strings, never float.** Prices/amounts reach the wire via the existing `_money_str`/`format_money` fixed-point path; internal math uses `Decimal` with `ROUND_FLOOR`/`ROUND_DOWN`.
- Suggestion is a BUY LIMIT with `duration = GTC` (it rests until the price is reached or the user cancels).
- **Whole-share sizing, reuse don't reinvent:** budget = the request's optional `amount` if `> 0`, else the user's available cash, capped at available cash; `shares = floor(budget / limit_price)`; returned `amount = shares * limit_price` (exact whole-share cost). Refuse `< 1` share calmly.
- `is_index_core(symbol)` gates every suggestion; the endpoint mirrors the `/approve` DI exactly (`get_scope` + `require_live_broker_session` + `get_execution_broker` + `get_async_session`).
- Calm-envelope discipline (NFR8): every decline is a calm HTTP 422 `{error:{type,message}}` (reuse the existing `OrderScopeError`→422 mapping), never a 500, never a phantom order. A lapsed session is the existing 409 `RECONNECT_MESSAGE`.
- LLM narration is resilient: a gateway failure falls back to a deterministic templated reasoning string (mirror `build_default_plan` resilience) — never crashes, never blocks the suggestion.
- Frontend reads the calm reason via the real envelope: `data?.error?.message ?? data?.detail ?? data?.message` (the 8.3-hardened shape).

**Block If:**
- The frozen `OrderIntentIn`/`OrderIntentOut` field set, order enums, or `is_index_core` / cash-source / `MarketDaily` contracts differ from what 8.1/8.2/6.5/3.1 shipped (re-verify before building) — HALT `contract drift`.
- The recent-low window or discount would need to change to satisfy an unstated product goal — the formula is LOCKED here; do not silently re-tune. HALT `pricing-formula decision`.

**Never:**
- The LLM never computes, sets, or adjusts `limit_price`/`amount`/`shares` — narration only, from numbers handed to it as facts.
- Nothing auto-executes; the endpoint places nothing and touches no `place_order`/`decision_record` path. The human executes via the unchanged `/approve` flow.
- No change to `RECOMMENDATION_OUTPUT_SCHEMA` or the `/recommend` path — the coach still proposes MARKET-only. Suggest-order is a separate surface.
- No STOP/STOP_LIMIT/AM-PM/extended-session support — those stay rejected by `validate_order_intent`.
- No new DB migration; no change to the frozen 8.3 frontend order-options module contract (populate it via existing setters, don't fork it).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Happy suggest | core symbol, ample cash, ≥20 bars, live session | 200 `{symbol, side:"buy", order_type:"limit", limit_price:"<str>", duration:"gtc", amount:"<str>", shares:int, reasoning:"<str>"}`; places nothing | No error |
| Determinism | same symbol + same bars + same ask, fake vs real LLM | identical `limit_price`/`amount`/`shares`; only `reasoning` differs | No error |
| Recent low ≥ ask | market at/below its 20-day low | `limit_price = ask*(1-0.01)` quantized down — still strictly < ask | No error |
| Optional target amount | `amount` in request `> 0` | budget = min(amount, cash); sizes shares off it | No error |
| Insufficient idle cash | cash (or budget) buys `< 1` share at price | calm 422 "not enough idle cash for a whole share …" | Calm refusal, no order |
| No price history | `< 1` `MarketDaily` bar for symbol | calm 422 "not enough recent price history to suggest a resting order." | Calm refusal |
| Non-core symbol | `is_index_core` false | calm 422 "outside the v1 scope …" verbatim from the engine | Calm refusal |
| Broker quote unavailable | `get_quote` refuses (missing/non-positive ask) | calm 422 "couldn't read a live price right now …" | Calm refusal, no 500 |
| Session lapsed | no live broker session | 409 `RECONNECT_MESSAGE` (existing dependency) | Calm reconnect |
| LLM narration fails | gateway raises | 200 with deterministic templated `reasoning`; number unchanged | Silent resilient fallback |
| Frontend populate | 200 response | side=buy, amount, order_type=limit, limit_price (exact string), duration=gtc set via existing setters; reasoning shown inline; nothing submitted | No error |
| Frontend calm refusal | 422/409 | surface `error.message` verbatim; populate nothing | Calm surface |

</intent-contract>

## Code Map

- `ballast/backend/brokers/port.py` — NEW `Quote` frozen dataclass (`bid: Decimal`, `ask: Decimal`) + abstract `async def get_quote(self, symbol: str) -> Quote` on `BrokerPort` (beside `cancel_order`, ~L264). A read seam, no placement.
- `ballast/backend/brokers/fake_adapter.py` — implement `get_quote`: return `Quote(bid=FAKE_FILL_PRICE, ask=FAKE_FILL_PRICE)` (`Decimal("100.00")`) — deterministic, zero-network.
- `ballast/backend/brokers/schwab_adapter/adapter.py` — implement `get_quote`: reuse the existing internal `_read_quote`/`_usable_price` to read `askPrice`+`bidPrice` as `Decimal`; refuse unusable via the existing `OrderNotPlaceableError` (mapped to calm 422 by the endpoint).
- `ballast/backend/coach/suggest.py` — NEW engine module (money-math lives here, out of the API layer). `SUGGEST_LOOKBACK_DAYS=20`, `SUGGEST_DISCOUNT=Decimal("0.01")`, `SUGGEST_NARRATION_SCHEMA` (`{reasoning:string}`). Pure `compute_suggested_price(recent_low, ask) -> Decimal` (unit-pinnable). `async def suggest_resting_order(scope, session, *, broker, broker_session, gateway, symbol, target_amount) -> SuggestedOrder`: `is_index_core` gate → load recent lows (MarketDaily) → `broker.get_quote` ask → compute price → read cash (`get_portfolio(...).cash`) → whole-share size → `narrate_suggestion`. Calm declines raise `OrderScopeError` with a plain-English message. `narrate_suggestion(gateway, facts) -> str` composes the LLMRequest (numbers as FACTS to explain, calm coach voice) and returns `output["reasoning"]`; on any exception returns a deterministic templated fallback.
- `ballast/backend/coach/execution.py` — add reusable `whole_share_quantity(amount: Decimal, price: Decimal) -> int` (`ROUND_FLOOR`) mirroring the adapters' inline flooring; used by `suggest_resting_order`. Do NOT refactor the adapters' own sizing (keep 8.1/8.2 green).
- `ballast/backend/api/coach.py` — NEW `POST /suggest-order` (full path `/api/coach/suggest-order`) mirroring the `/approve` DI (`get_scope`, `require_live_broker_session`, `get_execution_broker`, `get_async_session`). `SuggestOrderRequest{symbol:str, amount:Decimal|None}` → `SuggestOrderResponse{symbol, side, order_type, limit_price, duration, amount, shares, reasoning}` (money as fixed-point strings via `_money_str`). Map `OrderScopeError`/`OrderNotSupportedError`/broker `OrderNotPlaceableError` → calm 422; `SessionIntegrityError`/lapsed → existing 409.
- `ballast/frontend/src/components/CoachConsult.jsx` — add a "Suggest this order" button on the order-entry surface. On click → `apiFetch('/api/coach/suggest-order', {POST, body:{symbol, amount: amount||null}})`. On 200 → `setSide('buy')`, `setAmount(<amount>)`, `setOptions(prev => ({...prev, order_type:'limit', limit_price:<str>, duration:'gtc'}))`, `setSuggestReasoning(reasoning)`; render reasoning in a calm inline panel. On 422/409 → surface `error.message` verbatim (read via `data?.error?.message ?? data?.detail ?? data?.message`), populate nothing. Reuse existing setters + `options` component state; do NOT fork `orderOptions.js`.
- `ballast/backend/tests/` (new `test_suggest_order.py` + extend `test_coach_api.py`) and `ballast/frontend/src/test/coach-consult.test.jsx` — see Acceptance.

## Tasks & Acceptance

**Execution:**
- [x] `ballast/backend/brokers/port.py` -- add `Quote` dataclass + abstract `async get_quote(symbol)` -- the clean read seam so the coach never touches the adapter directly.
- [x] `ballast/backend/brokers/fake_adapter.py` + `ballast/backend/brokers/schwab_adapter/adapter.py` -- implement `get_quote` (fake deterministic `100.00`; schwab reuse `_read_quote`/`_usable_price`) -- expose live ask for the clamp.
- [x] `ballast/backend/coach/execution.py` -- add `whole_share_quantity(amount, price)` (`ROUND_FLOOR`) -- shared sub-share sizing, reused not reinvented.
- [x] `ballast/backend/coach/suggest.py` -- NEW engine: locked formula constants, pure `compute_suggested_price`, `suggest_resting_order` orchestration, `narrate_suggestion` (LLM narration-only + resilient fallback) -- the deterministic money-math + narration owner.
- [x] `ballast/backend/api/coach.py` -- NEW `POST /suggest-order` mirroring `/approve` DI; request/response models; error→calm-envelope mapping -- the HTTP surface, places nothing.
- [x] `ballast/frontend/src/components/CoachConsult.jsx` -- "Suggest this order" button that populates side/amount/limit/GTC via existing setters and shows reasoning inline; calm 422/409 surface -- MasterB's populate-not-execute vision.
- [x] `ballast/backend/tests/test_suggest_order.py` (+ test-double `get_quote` in `test_coach_api.py`/`test_portfolio.py`/`test_brokerage.py`) -- pin the price against fixed `MarketDaily`+ask fixtures; prove real-vs-fake LLM price identity; cover every I/O-matrix decline (non-core, insufficient cash, no history, quote-unavailable, lapsed); assert nothing is placed.
- [x] `ballast/frontend/src/test/coach-consult.test.jsx` -- extend `stubFetch` with a `suggest` route; assert 200 populates the controls (limit_price exact string, GTC, amount, side=buy) + renders reasoning, and a 422 surfaces the envelope message and populates nothing.

**Acceptance Criteria:**
- Given a core symbol with ≥20 `MarketDaily` bars, ample idle cash, and a live session, when `POST /api/coach/suggest-order` is called, then it returns a deterministic BUY LIMIT GTC suggestion (`limit_price` strictly below the live ask, `amount` = whole-share cost, `shares`, and an LLM `reasoning`) and places nothing.
- Given identical `MarketDaily` + live ask, when the endpoint runs under the fake vs a real/stub LLM, then `limit_price`/`amount`/`shares` are byte-identical and only `reasoning` differs (the model never touches the number).
- Given idle cash (or the optional target amount) that buys `< 1` whole share at the computed price, or a non-core symbol, or a symbol with no `MarketDaily` history, or an unreadable live quote, when suggested, then the response is a calm HTTP 422 `{error:{type,message}}` (never a 500, never a phantom order); a lapsed session is the existing 409 reconnect.
- Given a successful suggestion, when the frontend button receives it, then it populates the 8.3 controls (side=buy, `order_type:"limit"`, `limit_price` as the exact string, `duration:"gtc"`, `amount`) via existing setters and shows the reasoning inline; nothing is submitted until the human runs the existing `/approve` co-sign path.
- Given the full suites, when run, then all existing 8.1/8.2/8.3 + prior tests stay green, `/recommend` stays MARKET-only, and STOP/STOP_LIMIT/AM-PM stay rejected.

## Spec Change Log

_No bad_spec loopbacks. The single review pass resolved every finding as an auto-fix patch, a defer, or a verified reject; the intent-contract and task shape held._

## Review Triage Log

### 2026-08-04 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 4: (high 1, medium 1, low 2)
- defer: 2: (high 0, medium 1, low 1)
- reject: 8: (high 0, medium 0, low 8)
- addressed_findings:
  - `[high]` `[patch]` P1 — **The AI-suggested resting LIMIT/GTC never reached approval (headline functional break).** The Suggest button populated the component `options` state, but reaching the co-sign/approve step requires an Ask, and `onAsk → resetResult()` blanks `options` back to MARKET — so after Suggest→Ask the user co-signed a MARKET order, contradicting AC-4. Fixed by holding the suggestion in a dedicated `pendingSuggestion` state (untouched by `resetResult`) and re-seeding it into the co-sign `options` in `onAsk`'s success branch when it still matches the asked symbol (one-shot). Also added a defensive type-guard so a malformed 200 body can't write literal `"undefined"` into a money field.
  - `[medium]` `[patch]` P4 — Schwab `get_quote` required a usable `bidPrice` even though suggest-order only consumes the ask, so a valid ask with a thin/absent bid leg would wrongly refuse a suggestion (live-path). Now reads the bid best-effort (falls back to the ask when unusable); only the ask is load-bearing.
  - `[low]` `[patch]` P2 — Added the missing Suggest→Ask→Approve round-trip test (the reviewers noted the existing tests validated `onSuggest` in isolation, concealing P1). It asserts the co-signed `order_intent` carries `order_type:"limit"` + the suggested `limit_price` + `duration:"gtc"`.
  - `[low]` `[patch]` P3 — `_recent_low` now skips non-finite / `<= 0` `low` bars so one bad ingestion row can't drag the min to a degenerate price and nuke a valid symbol; added a regression test proving a `low=0` bar is ignored.
  - Deferred (2): (1) `get_quote` has no calm timeout/retry envelope like the placement path (live-Schwab-gated, untested); (2) the recent-20-day-low has no data-freshness guard (a cross-cutting `MarketDaily` staleness concern shared with `precedent/engine.py`). Both logged to `deferred-work.md`.
  - Rejected (8, verified): fallback narration wording "a touch below its recent low" in the clamp branch (still directionally true — the price is below the recent low); non-finite `target_amount` (already handled at `suggest.py:262`); the "collapsed refusal message" for a degenerate price (moot — the `limit_price <= 0` guard at `suggest.py:251` fires first, so `shares < 1` genuinely means insufficient cash); the live-session gate being "too strict" (by-design — the clamp needs the live ask); "edited limit re-floors amount into more shares / increases spend" (misreads the dollar-denominated `amount` model — spend is bounded by the dollar budget); Suggest overwriting a typed amount (by-design populate); "ignores concentration/4.5 warnings" (the 4.5 detector still fires on the `/recommend` decision the user must obtain before approving); and the penny-level `:.2f` display vs 8-dp math (cosmetic).

### 2026-08-04 — Review pass (follow-up)
- intent_gap: 0
- bad_spec: 0
- patch: 3: (high 0, medium 0, low 3)
- defer: 1: (high 0, medium 1, low 0)
- reject: 7: (high 0, medium 0, low 7)
- addressed_findings:
  - `[low]` `[patch]` ECH2 — **The held suggestion could be spent into a dead end (a second, defensive hole in the P1 mechanism).** `onAsk` re-seeded the resting LIMIT and then unconditionally `setPendingSuggestion(null)` on every ask — so an ask that lands on a non-co-signable card (no `decision_id`) would consume the suggestion, and a later co-signable ask of the same symbol would silently downgrade to MARKET (the exact P1 class). Now the re-seed + spend only fire when the recommendation is genuinely co-signable (`data?.decision_id`); a non-co-signable ask of the same symbol KEEPS `pendingSuggestion` for the next co-signable ask; a different symbol discards it. The normal co-signable path is unchanged (still covered by the AC-4 round-trip test).
  - `[low]` `[patch]` ECH7 — **Suggest could race an in-flight Ask.** The "Suggest this order" button was not disabled while `phase === 'thinking'`, so a click during the recommend network window interleaved `onSuggest`'s `resetResult()`/`setPhase('idle')` with the pending `onAsk`'s `setRecommendation`/`setPhase('ready')`. Added a `phase === 'thinking'` early-return in `onSuggest` and to the button's `disabled` guard (mirrors `askDisabled`).
  - `[low]` `[patch]` ECH4 — **`onSuggest` populated options from a `...prev` spread** (unlike `onAsk`, which spreads a clean `DEFAULT_OPTIONS`), so any latent stale option key (a leftover session/stop) could ride into the suggested resting LIMIT. Now seeds from `{...DEFAULT_OPTIONS, order_type:'limit', limit_price, duration:'gtc'}`, consistent with `onAsk`.
  - Deferred (1): the suggested dollar `amount`/`shares` shown on the form + in the reasoning panel can diverge from the amount actually co-signed, because the co-sign base is `recommendation.order_intent ?? submitted.order` and 8.3 makes `order_intent.amount` (the coach's LLM-returned amount) authoritative — the P1 fix deliberately re-seeds only price + TIF, not amount. Reconciling "which sizing wins" is a product decision that touches the locked 8.3 amount-authority model; logged to `deferred-work.md`.
  - Rejected (7, verified): ECH1 negative `target_amount` falls through to full-cash sizing (unreachable from the UI — `DECIMAL_RE` rejects negatives — and harmless: capped at cash, places nothing, human still co-signs); ECH3 "stale limit_price after editing amount" (premise false — `limit_price = min(recent_low, ask)*0.99` is budget-independent, so editing the dollar amount never stales it); ECH5 `amount` re-quantize (self-noted no-op — `int * 2dp` is already ≤ 2dp); ECH6 schwab `Quote(bid=ask)` "false spread" (benign — only the ask is load-bearing, the P4 best-effort bid is by-design); BH2 `get_execution_broker` on a read-only endpoint (the intent-contract LOCKED "mirror the /approve DI"; a live session is already required, the token-bind message is an unreachable edge); BH3 no-balance user gets "not enough idle cash" (cosmetic — zero cash *is* not-enough cash, and a live session implies a fetched balance); BH4 `_recent_low` tie-break/determinism (moot — `MarketDaily` has `UniqueConstraint("symbol","day")`, so exactly one row per symbol/day and the 20-bar window is deterministic).

## Design Notes

**Locked pricing formula (this is the product decision the 8.3 spec deferred; MasterB locked compute-owner, this pass locks the params).** `recent_low = min(bar.low for the most recent 20 MarketDaily bars)`; `limit_price = quantize_2dp_down( min(recent_low, ask) * Decimal("0.99") )`. Two properties make it safe: (1) it is a pure function of stored data + the live ask — fully reproducible and testable without a network or an LLM; (2) discounting `min(recent_low, ask)` guarantees `limit_price < ask` in every branch, so the suggestion is always a genuine resting buy (never accidentally marketable). Quantize with `ROUND_DOWN` to 2 dp — a lower buy price only ever favors the user. Both constants live at the top of `coach/suggest.py` as named `Decimal`/`int` so a later real-data tuning pass is a one-line change, not a re-architecture.

**Narration is downstream of, and blind to, the math.** `compute_suggested_price` returns before the gateway is touched. `narrate_suggestion` receives the finished numbers as facts ("we chose $X because it's ~1% under VTI's recent 20-day low of $Y and below today's ask $Z; it rests until reached, or you cancel") and returns only the prose. This is why real-vs-fake LLM price identity is trivially true, and why a gateway outage degrades to a templated sentence instead of failing the suggestion. Own a tiny `SUGGEST_NARRATION_SCHEMA` (`{reasoning:string}`) — do NOT reuse `RECOMMENDATION_OUTPUT_SCHEMA` (that path stays MARKET-only).

**Quote seam.** Bid/ask lived only inside the adapters (verified: schwab `_quote_ask`/`_usable_price`; fake `FAKE_FILL_PRICE`). Rather than leak an adapter into the coach, add one clean `BrokerPort.get_quote` read method (the sibling of 8.2's `cancel_order` port addition) that both adapters implement; the fake's deterministic `100.00` keeps every test zero-network and seed-stable.

**The suggestion must survive the ask reset (review P1).** The 8.3 co-sign `options` (the fields the order actually composes from via `buildOrderIntent`) only exist after an Ask, and `onAsk → resetResult()` blanks `options` back to MARKET. So the Suggest button cannot simply `setOptions(...)` — that write is wiped before approval. The suggestion is held in a dedicated `pendingSuggestion` state (untouched by `resetResult`) and re-seeded into `options` in `onAsk`'s success branch when it still matches the asked (upper-cased) symbol, then spent one-shot. This is why the round-trip Suggest→Ask→Approve test is mandatory — a per-handler test passes while the end-to-end flow silently downgrades to MARKET.

## Verification

**Commands:**
- `cd ballast/backend && python -m pytest tests/test_suggest_order.py tests/test_coach_api.py -q` -- expected: new suggest tests green (price pinned, real-vs-fake identity, all declines calm-422, nothing placed).
- `cd ballast/backend && python -m pytest -q` -- expected: full backend suite green; `/recommend` MARKET-only and STOP/STOP_LIMIT/AM-PM rejections unchanged.
- `cd ballast/frontend && npm test` -- expected: full Vitest suite green incl. the extended coach-consult suggest-button test.
- `cd ballast/frontend && npm run build` -- expected: production build succeeds.

**Manual checks:**
- Confirm `limit_price` and `amount` in the JSON response are fixed-point strings (not `Number`), and that a `SuggestOrderRequest` with no `amount` sizes off available cash.


## Auto Run Result

Status: done (follow-up review pass — the story's `followup_review_recommended: true` carry-through)

**Summary:** A second, independent adversarial + edge-case review of the already-shipped Story 8.4 diff (baseline `d37d021` → HEAD). The deterministic pricing engine, calm 422/409 envelope mapping, LLM-narrates-only separation, and "places nothing" tripwires all held up. Three localized low-consequence frontend hardening patches were applied to the Suggest/Ask/co-sign interplay; one real amount-authority interaction was deferred; seven findings were verified as noise and rejected.

**Files changed this pass:**
- `ballast/frontend/src/components/CoachConsult.jsx` — (ECH2) gate the `onAsk` re-seed + spend of the held suggestion on a co-signable `decision_id` (keep it for a later co-signable ask of the same symbol instead of spending it into a dead end); (ECH7) guard `onSuggest` against racing an in-flight ask (early-return + `disabled` on `phase === 'thinking'`); (ECH4) seed suggested options from a clean `DEFAULT_OPTIONS` base, consistent with `onAsk`.
- `_bmad-output/implementation-artifacts/spec-8-4-ai-suggest-populate-order.md` — appended the follow-up Review Triage Log entry, this result, and set `followup_review_recommended: false`.
- `_bmad-output/implementation-artifacts/deferred-work.md` — one new defer (suggested amount vs. co-signed amount authority).

**Review findings breakdown:** patch 3 (all low) applied; defer 1 (medium) logged; reject 7 (all low/noise, each verified against the code).

**Verification:**
- `cd ballast/frontend && npx vitest run src/test/coach-consult.test.jsx` → 31/31 pass.
- `cd ballast/frontend && npx vitest run` → 14 files, 136/136 pass.
- `cd ballast/frontend && npm run build` → production build succeeds.
- Backend untouched this pass (all edits are in `CoachConsult.jsx`); the backend suite was green at the story's completion (`cf8d969`) and is unaffected.

**Residual risks:** the deferred amount-authority interaction (form/reasoning may show a different sizing than the amount co-signed when the coach returns its own `order_intent.amount`) needs a product decision before it can be reconciled; the two prior 8.4 defers (`get_quote` timeout envelope; `MarketDaily` staleness guard) remain open and are live-Schwab-gated. No follow-up review recommended — the fixes are localized, low-consequence, and covered by existing tests.
