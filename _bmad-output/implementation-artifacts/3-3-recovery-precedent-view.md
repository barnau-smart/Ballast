---
title: 'Story 3.3: Recovery-precedent view'
type: 'feature'
created: '2026-07-27'
baseline_revision: ad11c3e45c9c818883b534c0be18e50c7f41ff1a
final_revision: b8f4f8ca98f2b40c322d82c3c64ea204d6e72071
status: 'done'
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-ai_practice_project-2026-07-22/ARCHITECTURE-SPINE.md'
  - '{project-root}/_bmad-output/implementation-artifacts/3-2-drawdown-matching-evidence-record-contract.md'
warnings: ['oversized']
---

<intent-contract>

## Intent

**Problem:** The Precedent Engine (Story 3.2) produces a deterministic `EvidenceRecord` proving that drops like the current one have recovered, but nothing surfaces it. An anxious user in a scary moment has no calm, data-backed view to reassure them, and no way to reach the engine's evidence over HTTP.

**Approach:** Add one auth-gated read endpoint that calls the existing `find_precedent` and returns its record as JSON, then render that record on the Coach surface as a calm, expandable data-block — matched drops + recoveries shown with sky-blue ▼ / green ▲ (never red, never color alone), citing source + as-of. When the record is the `strategy` fallback, show its rationale instead of an empty state.

## Boundaries & Constraints

**Always:**
- The backend is the sole source of the numbers (AD-1); the endpoint returns exactly the engine's `EvidenceRecord.to_dict()` shape `{id, kind, statement, stats, source, as_of}` unchanged — the frontend only formats/renders it, computes no market figure, and invents no statistic.
- The endpoint reads precedent ONLY via `precedent.find_precedent` (AD-3) — never re-derives from `market_daily` or any vendor source directly.
- The endpoint is gated to the authenticated active user (same dependency pattern as `/api/portfolio`); precedent itself is global reference data, so no `owner_id`/`Scope` filtering is applied to the query.
- The view always cites `source` and the `as_of` date, and is never a dead end: `event-precedent` → the matched-drops block; `strategy` → the strategy-default rationale; fetch failure → a calm static fallback message. It never shows an error screen, never uses red/pink, never uses color alone (every ▲/▼ is paired with a sign/label + real DOM text), and respects `prefers-reduced-motion`.
- Losses/drawdowns render sky-blue ▼; recoveries/forward-gains render green ▲ (reuse `MarketIndicator` + `--ballast-color-market-up/down` tokens).

**Block If:**
- Surfacing the record would require adding/removing/renaming a field on the AD-12 evidence contract `{id, kind, statement, stats, source, as_of}` (a contract change ripples into Epic 4 — a product/architecture decision, not a build detail).
- Delivering the view would require a 7th SPA surface route (v1 is fixed at exactly six — see `App.jsx`); the view must live within an existing surface.

**Never:**
- No LLM/coach pipeline, no Recommendation object, no execution wiring — that is Epic 4. This story is read-only: one endpoint + one view.
- No push/notification/unprompted delivery — the view is pull-only (rendered when the user opens the surface).
- No new market-data fetch, no persistence/snapshotting of records (Epic 4 owns snapshotting), no change to `precedent/engine.py` matching logic.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Qualifying precedent | Authed GET `/api/precedent/recovery` (default symbol `VTI`); engine returns an `event-precedent` record | 200 with `{id, kind:"event-precedent", statement, stats{...windows[]}, source, as_of}`; view renders the calm block: statement, drawdown ▼ sky-blue, median forward-return ▲ green, source + as-of, expandable to per-window instances | No error expected |
| No / degraded precedent | Engine returns a `strategy` record (no band match, all-time high, or insufficient data) | 200 with `{kind:"strategy", stats.reason, windows:[]}`; view renders the strategy-default rationale (statement + source/as-of), never an empty block | No error expected |
| Unauthenticated | GET `/api/precedent/recovery` with no/invalid session | 401 (FastAPI-Users active-user dependency), no record body | Standard auth rejection |
| Frontend fetch fails | Backend unreachable / non-2xx | View shows a calm static fallback rationale — no error screen, no dead end | Fail-quiet (mirror `Dashboard` pattern) |

</intent-contract>

## Code Map

- `ballast/backend/precedent/__init__.py` -- source of `find_precedent`, `EvidenceRecord`, `EvidenceKind`, `DEFAULT_BENCHMARK` ("VTI"); record `to_dict()` yields the JSON-safe 6-field shape.
- `ballast/backend/api/precedent.py` -- NEW: `APIRouter(prefix="/api/precedent")` + Pydantic `RecoveryPrecedentOut` + the read endpoint.
- `ballast/backend/api/app.py` -- register the new router (mirror the `portfolio_router`/`brokerage_router` includes, ~L157-161).
- `ballast/backend/api/deps.py` -- `get_scope` auth dependency (the active-user gate used by other endpoints).
- `ballast/backend/db/session.py` -- `get_async_session` FastAPI dependency.
- `ballast/backend/tests/test_precedent_endpoint.py` -- NEW: endpoint tests; mirror `tests/test_session_status.py` (TestClient, register/login, seed `market_daily` via a real session) and `tests/test_precedent.py` (crafted deterministic bars).
- `ballast/frontend/src/routes/Coach.jsx` -- mount the recovery-precedent view here (replace the placeholder card); the Coach surface already exists — no new route.
- `ballast/frontend/src/components/RecoveryPrecedent.jsx` + `RecoveryPrecedent.css` -- NEW: fetch `/api/precedent/recovery`, render the calm data-block / strategy fallback / calm fetch-fail fallback.
- `ballast/frontend/src/lib/precedent.js` -- NEW: presentation-only helpers (percent/date formatting, recovery phrasing) — AD-1, no logic; mirror `lib/holdings.js`.
- `ballast/frontend/src/components/MarketIndicator.jsx` -- reuse for ▲/▼ (never color-alone).
- `ballast/frontend/src/hooks/useReducedMotion.js` -- reuse to gate any expand animation.
- `ballast/frontend/src/lib/session.js` -- `apiFetch` (Bearer auto-attach).
- `ballast/frontend/src/test/recovery-precedent.test.jsx` -- NEW: component/route tests; mirror `src/test/dashboard.test.jsx` (fetch stub, color-rule negative assertions).

## Tasks & Acceptance

**Execution:**
- [x] `ballast/backend/api/precedent.py` -- Define `RecoveryPrecedentOut(BaseModel)` with fields `id:str, kind:str, statement:str, stats:dict, source:str, as_of:str` and a `GET /recovery` endpoint `async def recovery_precedent(symbol: str = Query(default=DEFAULT_BENCHMARK), scope=Depends(get_scope), session=Depends(get_async_session))` that calls `records = await find_precedent(session, symbol=symbol)` and returns `RecoveryPrecedentOut(**records[0].to_dict())`. -- Read-only surface over the engine; returns the AD-12 shape verbatim, auth-gated, engine-only (AD-3).
- [x] `ballast/backend/api/app.py` -- Import and `app.include_router(precedent_router)` alongside the existing includes. -- Wires the endpoint into the app.
- [x] `ballast/backend/tests/test_precedent_endpoint.py` -- Cover the I/O matrix: authed request with seeded qualifying history → 200 `event-precedent` with `windows[]` and `source`/`as_of` present; degraded/no-match seed → 200 `strategy` with `stats.reason`; unauthenticated → 401. Seed `market_daily` with crafted deterministic bars (reuse `test_precedent.py` conventions, TEST-prefixed symbols, per-test cleanup). -- Proves the read contract, the fallback, and the auth gate.
- [x] `ballast/frontend/src/lib/precedent.js` -- Presentation-only helpers: `formatPct(decimalStr)` (e.g. `"0.0801"`→`"8.0%"`), `formatSignedPct` for forward returns (`"0.1140"`→`"+11.4%"`), `sourceLine(record)` (`"{source} · as of {as_of}"`), and window phrasing (recovered vs "not yet recovered back to breakeven"). No market computation (AD-1). -- Keeps copy/format out of the component, mirrors `holdings.js`.
- [x] `ballast/frontend/src/components/RecoveryPrecedent.jsx` + `RecoveryPrecedent.css` -- On mount, `apiFetch('/api/precedent/recovery')`; render states: loading (calm note), `event-precedent` (statement headline; drawdown ▼ sky-blue via `MarketIndicator direction="down"`; median forward-return ▲ green; `sourceLine`; an expandable "See the matched drops" disclosure listing each `stats.windows[]` entry with peak/trough/recovery dates, drawdown ▼, recovery-days, forward-return ▲/"not yet recovered"), `strategy` (render `statement` + `sourceLine`, no windows), and fetch-fail (calm static fallback rationale). Use `<button aria-expanded>` for the disclosure; gate any transition on `useReducedMotion`; give every ▲/▼ a real text label; add `data-testid`s. NEVER red/pink, never color-alone, never an error screen. -- The calm, honest, accessible data-block (FR15, UX-DR5).
- [x] `ballast/frontend/src/routes/Coach.jsx` -- Render `<RecoveryPrecedent />` in place of the placeholder card (keep the calm eyebrow/title/prose). -- Surfaces the view within the existing six-surface set; pull-only.
- [x] `ballast/frontend/src/test/recovery-precedent.test.jsx` -- Stub `fetch`; assert: event-precedent renders the statement, sky-blue ▼ (`ballast-market-indicator--down`) and green ▲ present, source + as-of shown, expand reveals windows; strategy record renders the rationale not an empty state; fetch failure renders the calm fallback (no error text, no dead end); and `container.innerHTML` never matches `brand-red|accent-pink|line-red`. -- Locks the calm/color/fallback invariants.

**Acceptance Criteria:**
- Given a user opens the recovery-precedent view when the engine has a qualifying `event-precedent` record, when it renders, then it shows the real matched drops + recoveries in a calm data-block (down sky-blue ▼, up green ▲, never red, never color alone) and cites `source` + `as_of` (FR15, UX-DR5).
- Given the engine returns a `strategy` fallback (no qualifying precedent, all-time high, or insufficient data), when the view renders, then it shows the strategy-default rationale — never an empty state and never a dead end.
- Given the endpoint is called, when it responds, then the body is exactly the engine's `EvidenceRecord.to_dict()` 6-field shape with no field added/removed, and the precedent is obtained only through `find_precedent` (no direct `market_daily`/vendor read in the API layer).
- Given an unauthenticated request to `/api/precedent/recovery`, when it is handled, then it is rejected with 401 and returns no record.
- Given a beginner using assistive tech or `prefers-reduced-motion`, when the block renders, then all stats have real DOM text equivalents, every direction glyph is paired with a sign/label, and no motion plays under reduced-motion.

## Design Notes

**Placement:** The view lives on the existing `/coach` surface (currently a placeholder), not a new route — `App.jsx` fixes v1 at exactly six surfaces. It is pull-only: rendered when the user navigates there, never pushed.

**Fail-quiet fallback:** Mirror `Dashboard.jsx` — on any non-2xx or network error, render a calm static strategy-style rationale rather than an error screen. The engine already guarantees ≥1 record, so a real backend response is always renderable; the static fallback only covers transport failure.

**Reference record shape (from Story 3.2, JSON-safe):**
```
{ id:"ep-9f2a1c7b3d40", kind:"event-precedent",
  statement:"VTI is ~8.0% below its recent peak. In 5 similar drops, it recovered to breakeven in a median of 34 trading days.",
  stats:{ initial_drawdown_pct:"0.0801", current_velocity:"0.0021", instance_count:5,
          recovery_days_median:34, recovery_days_range:{min:12,max:71}, forward_return_1yr_median:"0.1140",
          windows:[{peak_date:"2018-09-20", trough_date:"2018-12-24", recovery_date:"2019-04-23",
                    drawdown_pct:"0.0795", velocity:"0.0019", recovery_days:34, recovered:true, forward_return_1yr:"0.1502"}] },
  source:"VTI daily close (market_daily)", as_of:"2026-07-27" }
strategy record: kind:"strategy", stats:{ reason:"no_band_match"|"all_time_high"|"insufficient_data", windows:[] }
```
Windows may have `recovery_date:null` / `recovery_days:null` / `forward_return_1yr:null` (episode not recovered or <252 forward bars) — render "not yet recovered back to breakeven" / omit the forward-return line rather than showing a blank.

## Verification

**Commands:**
- `cd ballast/backend && docker compose up -d db && .venv/bin/python -m pytest tests/test_precedent_endpoint.py -v` -- expected: new endpoint tests pass (event-precedent, strategy, 401).
- `cd ballast/backend && .venv/bin/python -m pytest -q` -- expected: full backend suite green, no regressions.
- `cd ballast/frontend && npm test -- --run` -- expected: `recovery-precedent.test.jsx` passes; no color-rule regressions.

## Review Triage Log

### 2026-07-27 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 7: (high 0, medium 1, low 6)
- defer: 0
- reject: 12
- addressed_findings:
  - `[medium]` `[patch]` The forward-return `MarketIndicator` hard-coded `direction="up"`, so a NEGATIVE forward return (a matched drop that kept falling a year on — the engine can emit these per-window and in the median) would render as a green ▲ gain, violating the hard "losses render sky-blue ▼, never a gain" color invariant and misleading a beginner. Added a `directionAndMagnitude` helper that derives direction from the value's sign; both the event-precedent median and each per-window forward return now render sky-blue ▼ when negative.
  - `[low]` `[patch]` Double sign glyph: the label was built with a signed formatter (`formatSignedPct`, "+11.4%") while `MarketIndicator` also emits its own sign, producing "▲ + +11.4%". Labels now carry the UNSIGNED magnitude and `MarketIndicator` owns the single sign glyph (matches the `PortfolioPanel` convention); removed the now-unused `formatSignedPct`.
  - `[low]` `[patch]` The forward-return test asserted too loosely (`/\+11\.4%/`, only positive data) and masked the two findings above. Tightened the positive-case assertion and added `renders a NEGATIVE forward return as a sky-blue ▼ loss` — a regression test with a negative median + negative window forward return asserting `--down`/▼ and never `--up`/▲, plus a no-doubled-sign check.
  - `[low]` `[patch]` The reveal animation relied solely on the JS `useReducedMotion` hook; added a CSS `@media (prefers-reduced-motion: reduce) { animation: none }` guard as defense-in-depth, matching the app-wide convention (`theme/global.css`, `ReauthBanner.css`).
  - `[low]` `[patch]` `RecoveryPrecedent.css` referenced an undefined `--ballast-duration` token (worked only via the `160ms` fallback); replaced with the literal `160ms` to avoid a phantom-token smell.
  - `[low]` `[patch]` The backend `symbol` query param was unbounded free text; added `min_length=1, max_length=32` so an empty `?symbol=` returns 422 (instead of surfacing a `0001-01-01` `date.min` insufficient-data record) and bounds the reflected value.
  - `[low]` `[patch]` The matched-windows list used the array index as its React key; switched to the stable `${peak_date}-${trough_date}`.
- rejected (12): Pydantic `response_model` silently drops a hypothetical new engine field (standard FastAPI behavior; the AD-12 `to_dict()` shape is pinned by Story 3.2 tests, so drift fails there first); loading-state has no test (transient state; source/as-of don't exist until data loads, so the "every state cites source" rule doesn't apply); strategy test fixture copy differs from real engine wording (the component renders `statement` verbatim; representative text is sufficient and the engine's fallback copy is covered by `test_precedent.py`); bare `−` before "8.0% below its recent peak" on the drawdown indicator (correct direction; `MarketIndicator`'s always-on sign is its established contract, shared with `PortfolioPanel`); and eight not-reachable edge cases — `recovery_days=0` (engine guarantees recovery index > trough index), `recovery_days` as a numeric string (`to_dict` keeps ints as ints), all-null / null-date windows (engine always sets `peak_date`/`trough_date`/`drawdown_pct`), unknown `kind` fall-through (enum-constrained to two values), null/empty strategy `statement` (engine always sets it), 2xx-with-null-body (endpoint always returns the model), and `formatPct` on hex/exponential input (engine emits plain decimal strings only).

## Auto Run Result

Status: done

**Summary:** Implemented Story 3.3 (Recovery-precedent view): a single auth-gated, read-only endpoint `GET /api/precedent/recovery` that surfaces the existing Precedent Engine's `EvidenceRecord` in the fixed AD-12 shape verbatim (engine-only per AD-3, no direct `market_daily`/vendor read), and a calm React view mounted on the existing Coach surface (no 7th route). The view renders matched drops + recoveries with sky-blue ▼ / green ▲ (never red, never color alone), always cites source + as-of, is expandable to the per-window instances, and never dead-ends — strategy-default rationale when no precedent qualifies, and a calm static fallback on transport failure. One adversarial review pass found a genuine color-correctness bug (negative forward returns would have rendered as green gains) which was patched and pinned with a regression test.

**Files changed:**
- `ballast/backend/api/precedent.py` (new) — `APIRouter(/api/precedent)` + `RecoveryPrecedentOut` (AD-12 shape) + auth-gated `GET /recovery` delegating to `find_precedent`; `symbol` bounded `min_length=1, max_length=32`.
- `ballast/backend/api/app.py` — registers the precedent router.
- `ballast/backend/tests/test_precedent_endpoint.py` (new) — real-DB endpoint tests: qualifying → 200 event-precedent, no-match → 200 strategy, unauthenticated → 401.
- `ballast/frontend/src/lib/precedent.js` (new) — presentation-only helpers (`formatPct`, `directionAndMagnitude`, `sourceLine`, `recoveryPhrase`); AD-1, no market computation.
- `ballast/frontend/src/components/RecoveryPrecedent.jsx` + `RecoveryPrecedent.css` (new) — the calm data-block: loading / event-precedent / strategy / calm fetch-fail states; ▼/▲ via `MarketIndicator` with sign derived from value; reduced-motion-guarded expandable disclosure; never red/pink.
- `ballast/frontend/src/routes/Coach.jsx` — mounts `<RecoveryPrecedent />` in place of the placeholder (still six surfaces).
- `ballast/frontend/src/test/recovery-precedent.test.jsx` (new) — event block + color/source assertions, expand-to-windows (incl. not-yet-recovered), strategy rationale, calm fallback on network + non-2xx, and the negative-forward-return regression.

**Review findings breakdown:** 0 intent_gap · 0 bad_spec · 7 patch (1 medium, 6 low) applied · 0 defer · 12 reject. See the Review Triage Log entry for per-finding rationale.

**Verification (actual):**
- `pytest tests/test_precedent_endpoint.py -q` → 3 passed.
- `pytest -q` (full backend) → 130 passed, 1 pre-existing httpx deprecation warning; no regressions (was 127 before this story's 3 new tests).
- `npm test -- --run` (frontend) → 46 passed across 8 files (`recovery-precedent.test.jsx` = 6 passed, incl. the new negative-forward regression).

**Follow-up review recommended:** false — the review fixes were localized to presentation (color/sign derivation), CSS, one query bound, and a React key; the one medium-severity item is a self-contained rendering fix now pinned by a dedicated passing regression test, with no API/data/security breadth.

**Residual risks:** None new. The pre-existing engine-layer items (gapless-index recovery/forward math, non-positive `adj_close`, magnitude-band floor) remain tracked in `deferred-work.md` for the real-Tiingo-history pass; they are upstream of this read-only view and unchanged by it.
