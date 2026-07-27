---
title: 'Story 3.4: Missed-growth meter'
type: 'feature'
created: '2026-07-27'
status: 'done'
baseline_revision: 8950df0d3779b9c7d648f45b3c4f9825f87ff5e6
final_revision: 542998a24c3078d5191070b71b96e7a581788a7a
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-ai_practice_project-2026-07-22/ARCHITECTURE-SPINE.md'
  - '{project-root}/_bmad-output/implementation-artifacts/3-3-recovery-precedent-view.md'
warnings: ['oversized']
---

<intent-contract>

## Intent

**Problem:** A user prone to sitting in cash has no calm, data-backed way to see what staying out has actually cost, so inaction feels free (FR19). The Precedent Engine owns `market_daily`, but nothing computes or surfaces the growth idle cash has forgone.

**Approach:** Add a deterministic engine function (in `precedent/`) that estimates forgone growth as `idle_cash × benchmark total return over a trailing window`, read the user's idle cash from the existing Epic-2 portfolio cache, expose it over one auth-gated read endpoint, and render a quiet always-available meter on the Dashboard — framed strictly as information, honest in both directions, never a nudge.

## Boundaries & Constraints

**Always:**
- The backend/engine is the sole source of every number (AD-1, AD-3): the figure is computed deterministically from `market_daily` — same market data + same idle cash always yield the same estimate; no LLM computes or recalls any market figure, and the frontend only formats/renders what the endpoint returns.
- Idle cash is read from the existing per-user portfolio cache via the scoped read (reuse the Epic-2 portfolio service / `ScopedRepository(PortfolioCache, scope, session)`); the endpoint is gated to the authenticated active user (same `get_scope` dependency as `/api/portfolio`).
- **Honest in both directions:** when the benchmark *rose* over the window, show the growth idle cash missed (green ▲); when it *fell*, say so calmly — idle cash avoided a loss (sky-blue ▼). Never present a market decline as a "cost" of holding cash, and never invent a figure.
- Framed strictly as information stated once, calmly (FR19): never pressure, never FOMO, never a nudge or call to action; pull-only (rendered when the user opens the surface, never pushed).
- Always cites `source` and the window (`as_of` end date + the trailing period); never a dead end — no idle cash, insufficient history, and fetch failure each render a calm informational state, never an empty/error screen. Never uses red/pink, never color alone (every ▲/▼ paired with a sign + real DOM text), respects `prefers-reduced-motion`.

**Block If:**
- Surfacing the figure would require adding/removing/renaming a field on, or adding a third `kind` to, the AD-12 evidence contract `{id, kind:event-precedent|strategy, statement, stats, source, as_of}` (a contract change ripples into Epic 4 — a product/architecture decision). The meter must use its own standalone DTO, not the shared `EvidenceRecord`.
- Delivering the view would require a 7th SPA surface route (v1 is fixed at exactly six — see `App.jsx`); the meter must live within an existing surface.

**Never:**
- No LLM/coach pipeline, no `EvidenceRecord`/Recommendation object, no execution wiring — Epic 4. This story is read-only: one engine function + one endpoint + one view.
- No push/notification/unprompted delivery; no nudge/FOMO/urgency copy.
- No new market-data fetch, no persistence/snapshotting, no change to `precedent/engine.py` matching logic, no attempt to fix the deferred AD-14 cash-only gap (the meter reads cash as the cache reports it today).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Idle cash + rising market | Authed GET `/api/precedent/missed-growth`; cache has `cash>0`; `market_daily` (VTI) has ≥ lookback bars, positive trailing return | 200 `{idle_cash, benchmark, window_return, window_start, window_end, forgone_growth>0, trading_days, statement, source, as_of, sufficient:true, reason:null}`; view renders forgone growth as green ▲ with statement + source + window | No error expected |
| Idle cash + falling market | Same, but trailing return is negative | 200 with `forgone_growth<0` / `window_return<0`; view renders sky-blue ▼ and honest calm copy (cash avoided ~$X of loss) — never framed as a cost | No error expected |
| No idle cash | `cash == 0` or no portfolio cache rows (never imported / all-cash gap) | 200 `{forgone_growth:"0.00", sufficient:true, reason:"no_idle_cash", ...}`; view renders a calm "nothing sitting idle right now" state, still cites source/as-of | No error expected |
| Insufficient history | Fewer than lookback+1 bars for the benchmark | 200 `{sufficient:false, reason:"insufficient_history", window_return:null, forgone_growth:"0.00"}`; view renders a calm "not enough market history yet" rationale, not an empty state | No error expected |
| Unauthenticated | GET with no/invalid session | 401, no body | Standard FastAPI-Users active-user rejection |
| Frontend fetch fails | Backend unreachable / non-2xx | Calm static fallback rationale — no error screen, no dead end | Fail-quiet (mirror `Dashboard`/`RecoveryPrecedent`) |

</intent-contract>

## Code Map

- `ballast/backend/precedent/engine.py` -- reference for `_load_series`, the 252-`FORWARD_RETURN_DAYS` index convention, `DEFAULT_BENCHMARK` ("VTI"), and Decimal quantization helpers (`_PCT_Q`, `_q`). Do not modify matching logic.
- `ballast/backend/precedent/missed_growth.py` -- NEW: `MissedGrowthEstimate` frozen dataclass (+ `to_dict()`) and `async def estimate_missed_growth(session, idle_cash, symbol=DEFAULT_BENCHMARK, lookback_days=LOOKBACK_TRADING_DAYS, as_of=None)`; deterministic trailing-window total return × idle cash over `market_daily`.
- `ballast/backend/precedent/__init__.py` -- export `estimate_missed_growth`, `MissedGrowthEstimate`.
- `ballast/backend/api/precedent.py` -- add `MissedGrowthOut(BaseModel)` + auth-gated `GET /missed-growth` (reuses existing `precedent_router`, `get_scope`, `get_async_session`).
- `ballast/backend/brokers/portfolio.py` -- reuse the scoped portfolio read (`get_portfolio(scope, session) -> PortfolioView` with `.cash`, `.as_of`) as the idle-cash source; do not re-query raw rows.
- `ballast/backend/db/models.py` -- `PortfolioCache.cash` (account-level Decimal), `MarketDaily` (symbol/day/adj_close). Reference only.
- `ballast/backend/tests/test_missed_growth.py` -- NEW: unit tests for `estimate_missed_growth` (crafted deterministic bars, TEST-prefixed symbols, per-test cleanup — mirror `test_precedent.py`).
- `ballast/backend/tests/test_missed_growth_endpoint.py` -- NEW: endpoint tests (mirror `test_precedent_endpoint.py`: register/login, seed `market_daily` + a `PortfolioCache` row).
- `ballast/frontend/src/lib/missedGrowth.js` -- NEW: presentation-only helpers (`formatUsd`, calm phrasing incl. negative/no-cash/insufficient); reuse `formatPct`/`directionAndMagnitude` from `lib/precedent.js`. AD-1, no computation.
- `ballast/frontend/src/components/MissedGrowthMeter.jsx` + `.css` -- NEW: fetch `/api/precedent/missed-growth`; render loading / figure / no-idle-cash / insufficient-history / calm fetch-fail; ▲/▼ via `MarketIndicator` by sign; never red, never color-alone.
- `ballast/frontend/src/routes/Dashboard.jsx` -- mount `<MissedGrowthMeter />` (existing surface; pull-only).
- `ballast/frontend/src/components/MarketIndicator.jsx` -- reuse for sign-derived ▲/▼ (`--ballast-color-market-up/down`).
- `ballast/frontend/src/lib/session.js` (`apiFetch`), `ballast/frontend/src/hooks/useReducedMotion.js` -- reuse.
- `ballast/frontend/src/test/missed-growth.test.jsx` -- NEW: component tests; mirror `recovery-precedent.test.jsx` (fetch stub, color-rule negatives, no-nudge copy).

## Tasks & Acceptance

**Execution:**
- [x] `ballast/backend/precedent/missed_growth.py` -- Define `LOOKBACK_TRADING_DAYS = 252`, a frozen `MissedGrowthEstimate` dataclass with `to_dict()` yielding `{idle_cash, benchmark, window_return, window_start, window_end, forgone_growth, trading_days, statement, source, as_of, sufficient, reason}` (all Decimals as strings, dates as ISO), and `async def estimate_missed_growth(...)`. Load the benchmark series via the engine's `_load_series` convention; pick `end` = last bar at/before `as_of` and `start` = the bar `lookback_days` rows earlier; `window_return = (end_close - start_close)/start_close`; `forgone_growth = (idle_cash * window_return)` quantized to cents. Handle: `idle_cash<=0` → `reason:"no_idle_cash"`, forgone `0.00`; `< lookback_days+1` bars → `sufficient:false, reason:"insufficient_history"`, `window_return:null`. Compose a calm honest `statement` (positive: growth missed; negative: loss avoided; no-cash/insufficient: informational). -- Engine owns the market stat (AD-1/AD-3), deterministic, honest both directions.
- [x] `ballast/backend/precedent/__init__.py` -- Export `estimate_missed_growth`, `MissedGrowthEstimate`. -- Public engine API.
- [x] `ballast/backend/api/precedent.py` -- Add `MissedGrowthOut(BaseModel)` matching `to_dict()` and `GET /missed-growth` `async def missed_growth(scope=Depends(get_scope), session=Depends(get_async_session))` that reads idle cash via the scoped portfolio read, calls `estimate_missed_growth(session, idle_cash=<cash>)`, and returns `MissedGrowthOut(**estimate.to_dict())`. -- Read-only, auth-gated, engine-only; standalone DTO (never the AD-12 `EvidenceRecord`).
- [x] `ballast/backend/tests/test_missed_growth.py` -- Unit-test the I/O matrix on the engine function with crafted deterministic bars (TEST-prefixed symbol, `try/finally` cleanup): rising window → positive `forgone_growth` matching `cash*return`; falling window → negative; `< lookback` bars → `insufficient_history`; `idle_cash=0` → `no_idle_cash`, forgone `0.00`. Assert exact Decimal values. -- Proves the deterministic math and every fallback.
- [x] `ballast/backend/tests/test_missed_growth_endpoint.py` -- Authed request with seeded `market_daily` + a `PortfolioCache` row (`cash>0`) → 200 with `forgone_growth` present and `source`/`as_of` set; no cache row → 200 `no_idle_cash`; unauthenticated → 401. -- Proves the read contract, cash wiring, and auth gate.
- [x] `ballast/frontend/src/lib/missedGrowth.js` -- Presentation-only helpers: `formatUsd(decimalStr)` (`"1234.50"`→`"$1,234.50"`, `null` if unparseable), and phrase builders for each state (missed-growth / loss-avoided / no-idle-cash / insufficient). Reuse `formatPct`/`directionAndMagnitude`/`sourceLine` from `lib/precedent.js`. No market computation (AD-1). -- Keeps copy/format out of the component.
- [x] `ballast/frontend/src/components/MissedGrowthMeter.jsx` + `.css` -- On mount `apiFetch('/api/precedent/missed-growth')`; render: loading (calm note); figure (`sufficient && reason==null`) — statement headline + `forgone_growth` via `MarketIndicator` with direction derived from the value's sign (positive→green ▲, negative→sky-blue ▼) + `window_return` context + source/window line; `reason:"no_idle_cash"` — calm "nothing idle" info; `reason:"insufficient_history"` — calm rationale; fetch-fail — calm static fallback. Give every ▲/▼ a real text label, add `data-testid`s, gate any transition on `useReducedMotion`. NEVER red/pink, never color-alone, never an error screen, never nudge/urgency copy. -- The quiet, honest, accessible meter (FR19, UX-DR5).
- [x] `ballast/frontend/src/routes/Dashboard.jsx` -- Render `<MissedGrowthMeter />` on the Dashboard (calm placement alongside the portfolio panel; keep existing structure). -- Surfaces within the existing six-surface set; pull-only.
- [x] `ballast/frontend/src/test/missed-growth.test.jsx` -- Stub `fetch`; assert: rising figure renders green ▲ + statement + source; falling figure renders sky-blue ▼ and honest "avoided loss" copy (never a "cost" frame); `no_idle_cash` renders the calm info state not an empty block; `insufficient_history` renders the rationale; fetch failure renders the calm fallback; `container.innerHTML` never matches `brand-red|accent-pink|line-red`; and no nudge/CTA copy (e.g. never `/invest now|you should|don'?t wait|move your cash/i`). -- Locks the calm/honest/color/no-nudge invariants.

**Acceptance Criteria:**
- Given a user with idle cash over a window where the benchmark rose, when they view the meter, then it shows a data-backed estimate of forgone growth (green ▲), framed calmly as information with source + window cited, never as pressure or a nudge (FR19).
- Given a window where the benchmark fell, when the meter renders, then it honestly states idle cash avoided a loss (sky-blue ▼) and never presents the decline as a cost of holding cash.
- Given the estimate is produced, when it responds, then every figure was computed deterministically by the engine from `market_daily` (no LLM, no frontend computation) and the response uses a standalone DTO — the AD-12 `EvidenceRecord` contract is untouched (no field or `kind` change).
- Given a user with no idle cash or insufficient market history, when the meter renders, then it shows a calm informational state (never an empty state, never a dead end, never an error screen).
- Given an unauthenticated request to `/api/precedent/missed-growth`, when it is handled, then it is rejected with 401 and returns no figure.
- Given a beginner using assistive tech or `prefers-reduced-motion`, when the meter renders, then all figures have real DOM text equivalents, every direction glyph is paired with a sign/label, no motion plays under reduced-motion, and no red/pink is used.

## Design Notes

**Window choice:** A fixed trailing window of `LOOKBACK_TRADING_DAYS = 252` rows (≈1 year), matching the engine's existing `FORWARD_RETURN_DAYS` index convention, anchored to the latest `market_daily` bar (or the bar at/before `as_of`). This is deterministic and legible ("over the past year"). It inherits the same gapless-row assumption already tracked in `deferred-work.md` for the engine's index-based horizons — do NOT re-solve or duplicate that entry here.

**Idle cash & the AD-14 gap:** Idle cash is read from the Epic-2 portfolio cache exactly as it reports today. All-cash accounts currently yield zero cache rows (cash surfaces as 0) — the known deferred AD-14 cash-only gap. The meter does not fix it; when that gap closes upstream, the meter benefits automatically. A `cash==0` read is handled gracefully as `no_idle_cash`.

**Why not an `EvidenceRecord`:** The AD-12 contract is pinned to two kinds (`event-precedent`, `strategy`) that Epic 4's validator and decision snapshot depend on. The forgone-growth figure is a standalone calming view, not a coach-cited precedent, so it uses its own `MissedGrowthOut` DTO — keeping AD-3 (engine owns the number) without contract drift.

**Reference estimate shape (JSON-safe):**
```
{ idle_cash:"25000.00", benchmark:"VTI", window_return:"0.1140", window_start:"2025-07-28",
  window_end:"2026-07-27", forgone_growth:"2850.00", trading_days:252,
  statement:"Your ~$25,000 in idle cash has sat out ~$2,850 of growth over the past year.",
  source:"VTI daily close (market_daily)", as_of:"2026-07-27", sufficient:true, reason:null }
no-idle:  reason:"no_idle_cash", forgone_growth:"0.00"
insufficient: reason:"insufficient_history", sufficient:false, window_return:null, forgone_growth:"0.00"
```

## Verification

**Commands:**
- `cd ballast/backend && docker compose up -d db && .venv/bin/python -m pytest tests/test_missed_growth.py tests/test_missed_growth_endpoint.py -v` -- expected: new unit + endpoint tests pass (rising/falling/no-cash/insufficient, 200 wiring, 401).
- `cd ballast/backend && .venv/bin/python -m pytest -q` -- expected: full backend suite green, no regressions.
- `cd ballast/frontend && npm test -- --run` -- expected: `missed-growth.test.jsx` passes; no color-rule regressions.

## Review Triage Log

### 2026-07-27 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 6: (high 0, medium 2, low 4)
- defer: 1
- reject: 14
- addressed_findings:
  - `[medium]` `[patch]` A flat window (0% return) — or a sub-cent move that rounds to `$0.00` — rendered as a green ▲ `+$0.00` "growth missed", overstating a non-event and violating the story's honesty invariant. The engine now frames the statement on the **dollar figure's sign** (positive → growth missed, negative → loss avoided, zero → a neutral "roughly flat … has not missed measurable growth"), and the meter suppresses the ▲/▼ `MarketIndicator` entirely when the figure is `$0.00`. Pinned by a new backend `test_flat_window_is_neutral_not_missed_growth` and a frontend flat-window test.
  - `[medium]` `[patch]` The endpoint test seeded and then `DELETE`d **all real `VTI`** rows from the shared global `market_daily` table in its `finally`, risking clobbering a real ingest and racing concurrent tests. Added a bounded `symbol` query param to `GET /missed-growth` (mirrors `/recovery`), and switched the figure test to a throwaway `TEST_MG_ENDPOINT` symbol so it never touches real `VTI` data. This also gives the previously-untested engine `symbol` parameterization coverage at the API boundary.
  - `[low]` `[patch]` The `no_idle_cash` state (and any pre-load path) carries no `as_of`, so the shared `sourceLine` rendered a phantom "· as of an unknown date". Gave the meter a local `sourceLine` that still cites `source` but appends the "as of" clause only when a real date is present — the state still cites its source (never a dead end) without a fake date.
  - `[low]` `[patch]` The frontend re-authored the figure sentence (`figurePhrase`) instead of rendering the engine's own `statement`, creating two copies of the honest copy to keep in sync and contradicting the AD-1 "render what the engine returns" claim. The figure branch now renders `estimate.statement` verbatim (like `RecoveryPrecedent`); removed the redundant `figurePhrase`.
  - `[low]` `[patch]` The two insufficient-history branches carried duplicated verbatim statement strings that could drift; extracted a single `_INSUFFICIENT_STATEMENT` constant.
  - `[low]` `[patch]` Test hardening tied to the fixes above: added a backend sufficiency-boundary test (`exactly lookback + 1` bars → sufficient, pinning the off-by-one) alongside the existing one-fewer case, plus the flat-window backend + frontend tests.
- rejected (14): rounding-order of `forgone_growth` from the 4-dp `window_return` (intentional and deterministic — the displayed dollars match the displayed return; `ROUND_HALF_UP` is standard for money); index-based 252-row window may not equal a calendar year on gappy data (the SAME gapless-row assumption already tracked in `deferred-work.md` for the engine's horizons — the spec's Design Notes explicitly say not to re-solve or duplicate it); `_load_series` loads full history for two bars (reuses the engine's sanctioned data-access convention; acceptable at v1 volumes per NFR7); `benchmark` field is the raw symbol string (correct — it *is* the symbol; the symbol-override concern is now moot since the param exists and is tested); the component collapses 401/500/network into one calm fallback (by-design fail-quiet, matching `Dashboard`/`RecoveryPrecedent`; session expiry is surfaced app-wide by the Epic-2 re-auth banner, not per-widget); 2xx-with-null-body (the endpoint always returns the model); unknown/new `reason` string with `sufficient:true` (the engine only ever emits `None`|`no_idle_cash`|`insufficient_history`); a "doubled sign" across the window line and the figure line (they sign two different quantities — a % and a $ — not one number); `trading_days:252` rendered in a degraded state (the degraded branches never render `windowLine`); no explicit read-only assertion on the endpoint (it only calls two read paths — read-only by construction); `lookback_days=0` (no caller passes it; the endpoint hardcodes the default); NaN/Inf idle cash (the `Numeric(20,2)` column cannot store them); and two low-value defensive test gaps (`as_of` before the earliest bar; the non-positive-`start_close` guard, which exists and cannot occur with real positive `adj_close`).

## Auto Run Result

Status: done

**Summary:** Implemented Story 3.4 (Missed-growth meter, FR19): a deterministic engine function `estimate_missed_growth` that computes `forgone_growth = idle_cash × benchmark trailing-window total return` over the global `market_daily` store (AD-1/AD-3 — no LLM, no wall-clock, no randomness), one auth-gated read endpoint `GET /api/precedent/missed-growth` that reads the user's idle cash from the Epic-2 portfolio cache and returns a standalone DTO (the AD-12 `EvidenceRecord` contract is untouched), and a quiet, calm meter mounted on the existing Dashboard surface (no 7th route, pull-only). The figure is honest in both directions — a rising window shows growth idle cash missed (green ▲), a falling window shows a loss it avoided (sky-blue ▼), and a flat window is a neutral non-event — never red, never color-alone, never a nudge; no idle cash, insufficient history, and fetch failure each render a calm informational state, never a dead end. One adversarial review pass found and fixed a genuine honesty bug (flat/zero rendering as a green +$0.00 gain) and a destructive test hazard (deleting real `VTI` rows), plus four low-severity cleanups; the AD-14 cash-only gap was deferred.

**Files changed:**
- `ballast/backend/precedent/missed_growth.py` (new) — `MissedGrowthEstimate` frozen DTO (+ JSON-safe `to_dict()`) and `estimate_missed_growth(...)`; deterministic trailing-252-row return × idle cash; `no_idle_cash` / `insufficient_history` / non-positive-base fallbacks; honest three-way (missed / avoided / flat) statement keyed on the dollar figure's sign.
- `ballast/backend/precedent/__init__.py` — export `estimate_missed_growth`, `MissedGrowthEstimate`.
- `ballast/backend/api/precedent.py` — standalone `MissedGrowthOut` DTO + auth-gated `GET /missed-growth` with a bounded `symbol` query param; reads idle cash via `get_portfolio`, delegates math to the engine. AD-12 `EvidenceRecord`/`RecoveryPrecedentOut` untouched.
- `ballast/backend/tests/test_missed_growth.py` (new) — engine unit tests: rising (+$3,500), falling (−$1,000 loss-avoided), flat (neutral), sufficiency boundary (exactly lookback+1), insufficient/absent, zero/negative cash, determinism + JSON-safety.
- `ballast/backend/tests/test_missed_growth_endpoint.py` (new) — real-DB endpoint tests: seeded `TEST_MG_ENDPOINT` history + cache row → 200 figure; no cache row → `no_idle_cash`; unauthenticated → 401 (non-destructive; never touches real `VTI`).
- `ballast/frontend/src/lib/missedGrowth.js` (new) — presentation-only helpers (`formatUsd`, `amountLabel`, `windowLine`, graceful local `sourceLine`); re-uses `directionAndMagnitude`/`formatPct`. AD-1, no computation.
- `ballast/frontend/src/components/MissedGrowthMeter.jsx` + `.css` (new) — the calm meter: loading / figure / no-idle-cash / insufficient-history / calm fetch-fail; renders the engine `statement` verbatim; ▲/▼ derived from the figure's sign, suppressed when $0.00; reduced-motion-guarded; never red/pink, never a nudge.
- `ballast/frontend/src/routes/Dashboard.jsx` — mounts `<MissedGrowthMeter />` below the portfolio panel (still six surfaces; pull-only).
- `ballast/frontend/src/test/missed-growth.test.jsx` (new) — rising ▲, falling ▼ "loss avoided" (no "cost" frame), flat neutral (no phantom +$0.00), no-idle-cash, insufficient-history, network + non-2xx fallbacks; hard color-rule and no-nudge negatives.
- `_bmad-output/implementation-artifacts/deferred-work.md` — appended the AD-14 cash-only-gap defer entry.

**Review findings breakdown:** 0 intent_gap · 0 bad_spec · 6 patch (2 medium, 4 low) applied · 1 defer (AD-14 cash-only gap) · 14 reject. See the Review Triage Log for per-finding rationale.

**Verification (actual):**
- `pytest tests/test_missed_growth.py tests/test_missed_growth_endpoint.py -q` → 12 passed, 1 pre-existing httpx deprecation warning.
- `pytest -q` (full backend) → 142 passed, 1 warning (was 130 pre-story; +12 new tests, no regressions).
- `npm test -- --run` (frontend) → 53 passed across 9 files (`missed-growth.test.jsx` = 7 passed, incl. the flat-window regression); no color-rule regressions.

**Follow-up review recommended:** false — the review fixes were localized presentation/honesty changes (statement-verbatim rendering, a zero-figure guard, a graceful citation line), one additive bounded query param the frontend never uses, and test hygiene, each pinned by a passing test. No data-model, security, or broad-API breadth.

**Residual risks:** The AD-14 cash-only gap (deferred) means all-cash accounts read as `no_idle_cash` until the real-Schwab-balances mapping lands upstream. The index-based 252-row window inherits the engine's existing gapless-row assumption already tracked in `deferred-work.md`. Both are documented, out of scope for this read-only view, and unchanged by it.
