---
title: 'Story 3.5: Headline contextualizer'
type: 'feature'
created: '2026-07-27'
status: 'done'
baseline_revision: 26167c884cc12b819b1e9791ce0121e49f3979bc
final_revision: 0daa3f8b23a3cc373fc15ffbb8a4dfb7623e8c91
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-ai_practice_project-2026-07-22/ARCHITECTURE-SPINE.md'
  - '{project-root}/_bmad-output/implementation-artifacts/3-3-recovery-precedent-view.md'
warnings: ['oversized']
---

<intent-contract>

## Intent

**Problem:** A user rattled by a scary headline has no calm, data-backed way to react to what the market *actually did* in comparable drops instead of to fear (FR20). The Precedent Engine already produces drawdown-keyed evidence, and the recovery-precedent view (3.3) renders it passively — but nothing lets a user submit a headline on demand and get precedent back, and nothing structurally guarantees the response never interprets the news event itself.

**Approach:** Add one auth-gated, read-only endpoint that accepts a headline the user submits on demand, ignores the headline's content entirely (v1 matching is drawdown-band only — no event taxonomy), and returns the deterministic drawdown-keyed `EvidenceRecord` for the benchmark via the existing `find_precedent`. On the Coach surface, add a headline input + submit that renders that record as the same calm, honest precedent data-block — prefaced by copy that explicitly does NOT judge the news. Extract the shared record→DOM rendering so the two calming views cannot drift on the color/honesty rules.

## Boundaries & Constraints

**Always:**
- The endpoint reframes to drawdown-keyed precedent ONLY and NEVER classifies or interprets the news event (FR20): the submitted headline is accepted and length-bounded but is used for nothing — it is never passed to the engine, never parsed, never influences matching. Same market state + any two different headlines ⇒ byte-identical record (incl. identical `id`).
- The backend is the sole source of every number (AD-1); precedent is obtained ONLY through `precedent.find_precedent` (AD-3) — never a direct `market_daily`/vendor read in the API layer. The endpoint returns exactly the engine's `EvidenceRecord.to_dict()` 6-field shape `{id, kind, statement, stats, source, as_of}` verbatim (reuse the existing `RecoveryPrecedentOut` wire model).
- On demand / pull-only (FR20): nothing is fetched or rendered until the user submits; never pushed, never unprompted. Auth-gated to the active user (same `get_scope` dependency as `/recovery`); precedent is global reference data, so no `owner_id`/`Scope` filtering.
- Never a dead end: `event-precedent` → the matched-drops block; `strategy` → the strategy-default rationale; a fetch failure → a calm static fallback. Every state cites `source` + `as_of`. Never red/pink, never color alone (every ▲/▼ paired with a sign + real DOM text), respects `prefers-reduced-motion`. Framing copy is calm, non-alarmist, never a nudge/CTA/urgency, and never a take on the news.
- The `PrecedentEvidence` extraction is behavior-preserving: all existing 3.3 `data-testid`s and DOM structure are kept, and `recovery-precedent.test.jsx` stays green unchanged.

**Block If:**
- Surfacing the record would require adding/removing/renaming a field on the AD-12 evidence contract `{id, kind, statement, stats, source, as_of}` (a contract change ripples into Epic 4 — a product/architecture decision).
- Delivering the view would require a 7th SPA surface route (v1 is fixed at exactly six — see `App.jsx`); the view must live within the existing Coach surface.
- Satisfying "respond to the headline" would require classifying/interpreting the event or adding event-category/news-taxonomy matching (explicitly a later enrichment, and a product decision — not a v1 build detail).

**Never:**
- No LLM / coach pipeline / Recommendation object / execution wiring — that is Epic 4. No Anthropic SDK or any generative call.
- No event classification, sentiment, topic tagging, or NLP over the headline; no storage/persistence/logging of the headline content.
- No new market-data fetch, no snapshotting, no change to `precedent/engine.py` matching logic, no change to the AD-12 contract, no 7th route.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Headline + qualifying precedent | Authed `POST /api/precedent/contextualize` `{headline, symbol}`; engine returns an `event-precedent` record | 200 with the AD-12 6-field record (`kind:"event-precedent"`, `stats.windows[]`, `source`, `as_of`); view renders the neutral framing line + calm block (drawdown ▼ sky-blue, forward return ▲/▼ by sign), expandable to windows | No error expected |
| Headline + no qualifying precedent | Engine returns a `strategy` record (no band match / all-time high / insufficient data) | 200 `{kind:"strategy", stats.reason, windows:[]}`; view renders the strategy-default rationale, never an empty block | No error expected |
| Headline-independence (never classifies) | Two POSTs, different `headline` text, same seeded market state | Byte-identical record body incl. identical `id` — proves the event is never interpreted (FR20) | No error expected |
| Empty headline | POST `{headline:""}` (or the frontend has an empty/whitespace-only input) | Backend 422 (`min_length`); frontend keeps submit disabled until non-blank, so no request is sent; a stray 4xx renders the calm fallback, never an error screen | 422 / calm fallback |
| Unauthenticated | POST with no/invalid session | 401, no record body | Standard FastAPI-Users active-user rejection |
| Frontend fetch fails | Backend unreachable / non-2xx | Calm static fallback rationale — no error screen, no dead end | Fail-quiet (mirror `RecoveryPrecedent`) |

</intent-contract>

## Code Map

- `ballast/backend/api/precedent.py` -- add `ContextualizeIn(BaseModel)` request body + `POST /contextualize`; reuse the existing `precedent_router`, `RecoveryPrecedentOut`, `get_scope`, `get_async_session`, `find_precedent`, `DEFAULT_BENCHMARK`. The router is already registered in `app.py` (no change there).
- `ballast/backend/precedent/__init__.py` -- source of `find_precedent`, `EvidenceRecord` (reference only; no change).
- `ballast/backend/tests/test_headline_contextualizer_endpoint.py` -- NEW: endpoint tests; mirror `test_precedent_endpoint.py` (register/login, seed `market_daily` with TEST-prefixed symbols, per-test cleanup).
- `ballast/frontend/src/components/PrecedentEvidence.jsx` -- NEW: presentational record→DOM renderer extracted from `RecoveryPrecedent.jsx` (event-precedent block + windows disclosure + strategy rationale). Owns its own expand state. Preserves ALL existing `data-testid`s (`precedent-event`, `precedent-statement`, `precedent-drawdown`, `precedent-forward`, `precedent-source`, `precedent-toggle`, `precedent-windows`, `precedent-window-N`, `precedent-strategy`) and DOM/classes.
- `ballast/frontend/src/components/RecoveryPrecedent.jsx` -- MODIFY: replace its inline `EventPrecedent`/`PrecedentWindow`/strategy rendering with `<PrecedentEvidence record={record} />`; keep its loading / fetch / calm-fallback states. Behavior-preserving.
- `ballast/frontend/src/components/HeadlineContextualizer.jsx` + `.css` -- NEW: headline input + submit (pull-only); states idle / submitting / ready (framing + `<PrecedentEvidence>`) / calm fetch-fail. POSTs via `apiFetch`.
- `ballast/frontend/src/routes/Coach.jsx` -- MODIFY: mount `<HeadlineContextualizer />` alongside `<RecoveryPrecedent />` (existing six-surface set; no new route).
- `ballast/frontend/src/lib/precedent.js` -- reuse `directionAndMagnitude`, `formatPct`, `sourceLine`, `recoveryPhrase` unchanged (AD-1, presentation only).
- `ballast/frontend/src/components/MarketIndicator.jsx`, `hooks/useReducedMotion.js`, `lib/session.js` (`apiFetch`) -- reuse.
- `ballast/frontend/src/test/headline-contextualizer.test.jsx` -- NEW: component tests; mirror `recovery-precedent.test.jsx` (fetch stub, color-rule/no-nudge negatives).

## Tasks & Acceptance

**Execution:**
- [x] `ballast/backend/api/precedent.py` -- Add `class ContextualizeIn(BaseModel)` with `headline: str = Field(min_length=1, max_length=500)` and `symbol: str = Field(default=DEFAULT_BENCHMARK, min_length=1, max_length=32)`, and `@router.post("/contextualize", response_model=RecoveryPrecedentOut) async def contextualize(body: ContextualizeIn, scope=Depends(get_scope), session=Depends(get_async_session))` that calls `records = await find_precedent(session, symbol=body.symbol)` and returns `RecoveryPrecedentOut(**records[0].to_dict())`. The `headline` is accepted/bounded but deliberately NEVER read, parsed, or passed anywhere. Docstring states: read-only, engine-only (AD-3), never classifies the event (FR20), headline is inert. -- On-demand read surface that returns drawdown-keyed precedent verbatim and structurally cannot interpret the news.
- [x] `ballast/backend/tests/test_headline_contextualizer_endpoint.py` -- Cover the matrix over the wire (mirror `test_precedent_endpoint.py`, TEST-prefixed symbols, `try/finally` cleanup): authed POST with a seeded qualifying history → 200 `event-precedent` (6-field shape, `windows[]`, `source`/`as_of`); degraded seed → 200 `strategy` with `stats.reason`; **two POSTs with different `headline` values against the same seeded symbol → identical record `id` and identical body** (headline-independence / never-classifies); `headline:""` → 422; unauthenticated POST → 401 with no record body. -- Proves the read contract, the fallback, the auth gate, and the FR20 never-classify invariant.
- [x] `ballast/frontend/src/components/PrecedentEvidence.jsx` -- Extract the record→DOM rendering currently inline in `RecoveryPrecedent.jsx`: given a `record` prop, render the `event-precedent` block (statement headline; drawdown ▼ via `MarketIndicator direction="down"`; median forward return via `directionAndMagnitude` sign; `sourceLine`; expandable "See the matched drops" disclosure over `stats.windows[]` with `useReducedMotion`-gated animation), or the `strategy` rationale (`statement` + `sourceLine`). Own the expand state internally. Preserve every existing `data-testid` and class name verbatim. -- Single source of truth for the calm, color-honest evidence block, shared by both calming views so they cannot drift.
- [x] `ballast/frontend/src/components/RecoveryPrecedent.jsx` -- Replace the inline `EventPrecedent`/`PrecedentWindow`/strategy JSX with `<PrecedentEvidence record={record} />`; keep the loading note, the `apiFetch('/api/precedent/recovery')` effect, and the `precedent-fallback` calm state. No behavior change. -- De-duplicates the render; `recovery-precedent.test.jsx` must still pass unchanged.
- [x] `ballast/frontend/src/components/HeadlineContextualizer.jsx` + `.css` -- A calm on-demand widget: a labeled headline `<input>`/`<textarea>` (bounded `maxLength=500`) + a submit `<button>` disabled while the trimmed value is empty or a request is in flight. On submit, `apiFetch('/api/precedent/contextualize', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ headline }) })`; on `!res.ok` or network error render the calm static fallback; on success render a neutral framing line that explicitly does NOT judge the news (e.g. "Ballast doesn't weigh in on the news itself — here's what the market has actually done in drops like today's.") followed by `<PrecedentEvidence record={data} />`. Idle state before first submit shows only the prompt + input (pull-only; no fetch on mount). Add `data-testid`s; NEVER red/pink, never color-alone, never nudge/urgency/CTA copy, never event-classification copy; reduced-motion respected. -- The on-demand headline → precedent view (FR20), calm and non-interpretive.
- [x] `ballast/frontend/src/routes/Coach.jsx` -- Render `<HeadlineContextualizer />` alongside the existing `<RecoveryPrecedent />` (keep the eyebrow/title/prose; still six surfaces). -- Surfaces the view within the existing Coach surface; pull-only.
- [x] `ballast/frontend/src/test/headline-contextualizer.test.jsx` -- Stub `fetch`; assert: idle renders the input with submit initially disabled and does NOT fetch on mount (pull-only); after typing + submit, an `event-precedent` payload renders the neutral non-judging framing line + the shared evidence block (sky-blue ▼ drawdown, sign-correct forward return, source + as-of); a `strategy` payload renders the rationale, never an empty state; network + non-2xx each render the calm fallback (no "error"/"failed" text); `container.innerHTML` never matches `brand-red|accent-pink|line-red`; and copy never matches a nudge/classification pattern (e.g. never `/invest now|you should|don'?t wait|buy the dip|this (crash|selloff|news) (means|is)/i`). -- Locks the calm / color / pull-only / no-nudge / never-classify invariants at the UI.

**Acceptance Criteria:**
- Given a user submits a headline on demand, when Ballast responds, then it returns drawdown-keyed precedent (what the market did in comparable drops) rendered as the calm data-block, and the response is computed deterministically by the engine from `market_daily` (no LLM, no frontend computation) in the verbatim AD-12 6-field shape (FR20).
- Given two different headlines submitted against the same market state, when each is handled, then both return a byte-identical evidence record including an identical `id` — the endpoint never classifies or interprets the news event itself.
- Given the headline contextualizer, when the Coach surface first renders, then nothing is fetched or shown until the user submits (pull-only, never pushed/unprompted), and the response framing never offers a take on the news, never pressures, and never nudges.
- Given the engine returns a `strategy` fallback, or the fetch fails, when the view renders, then it shows the strategy-default rationale / a calm static fallback — never an empty state, never an error screen — and every rendered state cites `source` + `as_of` where present.
- Given an unauthenticated `POST /api/precedent/contextualize`, when it is handled, then it is rejected with 401 and returns no record body.
- Given a beginner using assistive tech or `prefers-reduced-motion`, when the view renders, then all figures have real DOM text equivalents, every direction glyph is paired with a sign/label, no motion plays under reduced-motion, and no red/pink is used; and the extracted `PrecedentEvidence` keeps `recovery-precedent.test.jsx` green (behavior-preserving).

## Design Notes

**Why the headline is inert (the FR20 crux):** v1 precedent matching is drawdown-band based only; event-category/news-taxonomy tagging is explicitly a later enrichment (epic AD note). So "respond to a headline" is satisfied by returning the current drawdown-keyed precedent, and the headline must never be interpreted. Accepting it as a bounded-but-unused input (rather than not sending it at all) keeps the user story honest ("a headline I submit"), gives a clean seam for the future taxonomy enrichment, and makes the never-classify invariant directly testable: different headlines ⇒ identical record.

**Reuse `RecoveryPrecedentOut`:** the response is exactly the AD-12 `EvidenceRecord.to_dict()` shape, identical to `/recovery`. Reusing the existing wire model avoids a redundant DTO and keeps the contract pinned in one place; the AD-12 contract is untouched.

**Behavior-preserving extraction:** `PrecedentEvidence` lifts the record-rendering out of `RecoveryPrecedent` so both calming views share the color/sign logic that 3.3's review already had to fix once (a negative forward return rendering as a green gain). Keeping all `data-testid`s/DOM identical means 3.3's suite is the regression guard for the refactor.

**POST for a read:** the endpoint has no side effects but accepts a submitted text body, so `POST {headline, symbol}` fits better than a GET query. Auth is Bearer-token (no cookies), so no CSRF concern. `symbol` is an optional bounded param so tests can seed a throwaway symbol without touching real `VTI` (mirrors `/recovery` and `/missed-growth`); the frontend always uses the default.

## Verification

**Commands:**
- `cd ballast/backend && docker compose up -d db && .venv/bin/python -m pytest tests/test_headline_contextualizer_endpoint.py -v` -- expected: new endpoint tests pass (event-precedent, strategy, headline-independence, empty→422, 401).
- `cd ballast/backend && .venv/bin/python -m pytest -q` -- expected: full backend suite green, no regressions.
- `cd ballast/frontend && npm test -- --run` -- expected: `headline-contextualizer.test.jsx` passes AND `recovery-precedent.test.jsx` stays green (behavior-preserving extraction); no color-rule regressions.

## Spec Change Log

(none — no bad_spec loopback)

## Review Triage Log

### 2026-07-27 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 4: (high 0, medium 2, low 2)
- defer: 1: (low 1)
- reject: 8
- addressed_findings:
  - `[medium]` `[patch]` The extracted shared renderer `PrecedentEvidence.jsx` declared no stylesheet of its own — it rendered `.ballast-precedent*` markup but relied on `RecoveryPrecedent.css` being pulled in as a side-effect of a sibling mount, so a future consumer on a surface without `RecoveryPrecedent` would render unstyled (undercutting the story's "the two views cannot drift" goal). Moved the shared `.ballast-precedent*` rules into a new `PrecedentEvidence.css`, imported by `PrecedentEvidence.jsx` and by the two consumers that render those classes in their loading/fallback states (`RecoveryPrecedent`, `HeadlineContextualizer`); deleted `RecoveryPrecedent.css`.
  - `[medium]` `[patch]` Both calming views render `PrecedentEvidence` on the same Coach surface, so once a headline is submitted the page held two elements with the same DOM `id="precedent-windows"` and two `aria-controls="precedent-windows"` — invalid HTML that breaks the screen-reader disclosure association (an explicit epic a11y invariant). Added an `idPrefix` prop (default `'precedent'`, preserving Story 3.3) and pass `"headline-precedent"` from the contextualizer so each disclosure's `id`/`aria-controls` is unique. data-testids are unchanged (unit tests render each component in isolation).
  - `[low]` `[patch]` `HeadlineContextualizer` dropped the sibling `RecoveryPrecedent`'s mounted-guard, so a contextualize response resolving after the user navigates away would `setState` on an unmounted component. Added a `mounted` ref with effect-cleanup and guarded both the success and failure `setState`s (mirrors the `active` flag pattern).
  - `[low]` `[patch]` The endpoint docstring claimed "the frontend always sends the default benchmark," but the widget omits `symbol` entirely and relies on the Pydantic default. Corrected the docstring to "the frontend omits `symbol` and relies on this server default" (matches the frontend test that locks the request body to `{headline}`).
- deferred:
  - `[low]` The shared `directionAndMagnitude` helper (Story 3.3, `lib/precedent.js`) paints an exactly-zero return as a green ▲ `+0.0%` gain rather than a neutral non-event — now surfaced through this second view. Not fixed here because it is shared 3.3 code and changing its zero handling would alter the recovery view's rendering (out of scope); appended to `deferred-work.md` for a focused honesty pass across both views.
- rejected (8): whitespace-only headline passes the server `min_length=1` while the client trims (harmless — the headline is inert by design, and the SPA client is the only caller); the silent `catch` logs nothing (matches the app-wide fail-quiet convention in `RecoveryPrecedent`/`Dashboard`); the frontend "no-nudge/no-classify" copy regex is non-exhaustive (the framing is a fixed, reviewed static constant — the regex is a reasonable smoke test); a backend spy-test asserting `find_precedent` never receives the headline (the wire-level headline-independence test already *behaviorally* guards FR20 — routing the headline into matching would make two different headlines diverge and fail the test — and the file is deliberately no-mocks); the duplicated `500` bound / confusing 422-on-overlong path (unreachable: `maxLength={500}` caps the input client-side); non-unique React keys when two windows share peak+trough dates (the engine guarantees distinct running-peak dates — settled in the 3.2 review); a 200 body of `null`/non-object (the `response_model` guarantees a valid object, which would degrade to a calm block regardless); and `find_precedent` returning an empty list (the engine contract guarantees ≥1 record, same as `/recovery`).

## Auto Run Result

Status: done

**Summary:** Implemented Story 3.5 (Headline contextualizer, FR20): a single auth-gated, read-only `POST /api/precedent/contextualize` endpoint that accepts a user-submitted headline on demand, deliberately ignores its content (v1 matching is drawdown-band only — event taxonomy is a later enrichment), and returns the deterministic drawdown-keyed `EvidenceRecord` for the benchmark via the existing `find_precedent` in the verbatim AD-12 6-field shape (reusing `RecoveryPrecedentOut`; the contract is untouched). On the existing Coach surface (no 7th route) a calm, pull-only `HeadlineContextualizer` renders the record via a newly-extracted shared `PrecedentEvidence` block, prefaced by framing copy that explicitly does NOT judge the news. The "never classifies the event" invariant is proven testably: two different headlines against the same market state return a byte-identical record (identical `id`). Never a dead end (event / strategy / calm fetch-fail), never red/pink, never color-alone, reduced-motion respected. One adversarial + edge-case review pass applied 4 patches (shared-CSS ownership, unique disclosure ids for a11y, an unmount guard, a docstring correction), deferred 1 pre-existing shared-helper honesty gap, and rejected 8.

**Files changed:**
- `ballast/backend/api/precedent.py` — added `ContextualizeIn` (bounded `headline` 1–500 + optional bounded `symbol`) and auth-gated `POST /contextualize` delegating to `find_precedent(symbol=body.symbol)`; the headline is inert (never read/parsed/passed). Reuses `RecoveryPrecedentOut`; AD-12 contract untouched.
- `ballast/backend/tests/test_headline_contextualizer_endpoint.py` (new) — real-DB matrix over `TEST_HEADLINE_*` symbols: qualifying → 200 event-precedent; no-match → 200 strategy; headline-independence (two headlines → identical `id` and byte-identical body); empty → 422; unauthenticated → 401.
- `ballast/frontend/src/components/PrecedentEvidence.jsx` + `.css` (new) — the shared, self-contained record→DOM renderer (event block + windows disclosure + strategy rationale); owns its expand state, its stylesheet, and a namespaced disclosure `id` via `idPrefix`.
- `ballast/frontend/src/components/RecoveryPrecedent.jsx` — renders through `<PrecedentEvidence>`; now imports the shared `PrecedentEvidence.css`. Behavior-preserving (3.3 suite unchanged and green).
- `ballast/frontend/src/components/HeadlineContextualizer.jsx` + `.css` (new) — pull-only headline input + submit; POSTs on submit; success → non-judging framing + `<PrecedentEvidence idPrefix="headline-precedent">`; non-2xx/network → calm fallback; mounted-guarded.
- `ballast/frontend/src/routes/Coach.jsx` — mounts `<HeadlineContextualizer />` alongside `<RecoveryPrecedent />` (still six surfaces).
- `ballast/frontend/src/test/headline-contextualizer.test.jsx` (new) — pull-only/no-fetch-on-mount, disabled submit, event + strategy render, network + non-2xx calm fallback, color-rule and no-nudge/no-classify negatives.
- `ballast/frontend/src/components/RecoveryPrecedent.css` — deleted (shared styles moved to `PrecedentEvidence.css`).
- `_bmad-output/implementation-artifacts/deferred-work.md` — appended the zero-return color-honesty defer entry.

**Review findings breakdown:** 0 intent_gap · 0 bad_spec · 4 patch (2 medium, 2 low) applied · 1 defer (low) · 8 reject. See the Review Triage Log for per-finding rationale.

**Verification (actual):**
- `pytest -q` (full backend) → 147 passed, 1 pre-existing httpx deprecation warning (was 142 pre-story; +5 new endpoint tests, no regressions).
- `npm test -- --run` (frontend) → 59 passed across 10 files (`headline-contextualizer.test.jsx` new; `recovery-precedent.test.jsx` stayed green through the extraction).

**Follow-up review recommended:** false — the review changes were four localized, low/medium-consequence fixes (CSS ownership, id namespacing for a11y, an unmount guard, a docstring), each covered by the already-passing suites, with no change to the endpoint logic, the evidence contract, security, or data.

**Residual risks:** The zero-return color-honesty gap in the shared `directionAndMagnitude` helper is deferred (documented). The design intentionally makes the submitted headline inert; when event-taxonomy enrichment lands (a later product decision), the endpoint's headline seam is where that would attach.
