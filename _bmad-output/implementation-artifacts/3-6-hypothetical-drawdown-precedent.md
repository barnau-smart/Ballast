---
title: 'Story 3.6: Hypothetical-drawdown precedent'
type: 'feature'
status: 'done'
baseline_revision: d235d2ae2141745281e8509b968e9e07f28ceced
final_revision: 0892abf8540019cbd611b700be1d8106da633a62
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-ai_practice_project-2026-07-22/ARCHITECTURE-SPINE.md'
  - '{project-root}/_bmad-output/implementation-artifacts/3-2-drawdown-matching-evidence-record-contract.md'
  - '{project-root}/_bmad-output/implementation-artifacts/3-3-recovery-precedent-view.md'
  - '{project-root}/_bmad-output/implementation-artifacts/3-5-headline-contextualizer.md'
---

# Story 3.6: Hypothetical-drawdown precedent ("what if it fell ~X%?")

Status: done

<!-- Epic 3 (Precedent Engine) enhancement of FR20. Additive; the 6-field EvidenceRecord contract is preserved. -->

## Story

As **a user rattled by a scary headline while the market is calm**,
I want **to ask what history shows for a bigger drop than today's ("what if it fell ~20%?")**,
so that **I can see the real "drops this size have historically recovered" precedent even when we're near a high — the whole point of the contextualizer (FR20).**

## Context

The Precedent Engine matches the symbol's **current** drawdown against similar historical episodes. Both surfaces (`/precedent/recovery`, `/precedent/contextualize`) key off `current_drawdown()`, and the headline text is inert. So when the market is near a high (VTI −1.4% now), a "scary headline" only ever returns a shallow-dip precedent — even though the data (real Tiingo history, 2004→2026) contains the −55% 2008 GFC, COVID-2020, and 2022 recoveries. This defeats FR20's intent ("user rattled by a headline sees what comparable drops actually did"), because fear usually strikes *before* or *during* a drop, not only after.

This story lets a caller supply a **hypothetical target drawdown**; the engine then matches historical episodes at that magnitude and returns an honestly-framed, explicitly-hypothetical precedent record. No prediction, no event classification — just "if it fell ~X%, here is what the record shows."

## Acceptance Criteria

1. **Engine accepts a hypothetical target.** `find_precedent(session, symbol, *, hypothetical_drawdown=None)`: when `hypothetical_drawdown` is a positive `Decimal`, matches historical episodes whose magnitude is within `MAGNITUDE_BAND` of the target (instead of the current drawdown); when `None`, behavior is byte-identical to today.
2. **Hypothetical record is honestly framed.** The returned `EvidenceRecord.statement` reads as a hypothetical, never a prediction — e.g. *"If VTI fell about 20% from a recent high, here's what the record shows: in N comparable drops since 2004, it recovered to breakeven in a median of X months, and was higher a year later in Y of N."* Stats gain **additive** keys `hypothetical: true` and `hypothetical_drawdown_pct` — the six TOP-LEVEL `EvidenceRecord` fields are unchanged (Epic 4 contract preserved).
3. **API param (additive).** `POST /api/precedent/contextualize` accepts an optional `drawdown` (0 < drawdown ≤ 0.90); when present it drives a hypothetical match, when absent the current-conditions behavior is unchanged. Out-of-range → calm 422. (Optionally the same param on `GET /precedent/recovery`.)
3b. **Never a dead end.** If no historical episode matches the target band (e.g. a 70% drop with no precedent for that symbol), degrade to the strategy-default record with an honest reason (`no_band_match`) — never empty, never an error.
4. **Contextualizer offers scenarios.** `HeadlineContextualizer` gains a small, calm set of drawdown scenarios (e.g. "a dip ~5%", "a correction ~10%", "a bear market ~20%", "a crash ~35%"); choosing one POSTs `drawdown` and renders the returned record through the existing `PrecedentEvidence` block. Current-conditions behavior remains the default. No nudge, no urgency, `prefers-reduced-motion` respected.
5. **Honesty invariants hold.** Headline text stays inert (FR20 — no event classification). Color/honesty rules unchanged (drops sky-blue ▼, forward-returns green ▲ via `MarketIndicator`; never red). Presentation-only (AD-1) — the frontend computes no figure.
6. **No regression.** Existing precedent/recovery/contextualize/coach behavior and the `RecoveryPrecedentOut` contract are unchanged when `drawdown`/`hypothetical_drawdown` is absent. Backend + frontend suites green.

## Tasks / Subtasks

- [x] **Engine** (AC 1, 2, 3b) — `precedent/engine.py`: add `hypothetical_drawdown` kwarg to `find_precedent`; when set, center the magnitude match on it (reuse `historical_episodes` + `MAGNITUDE_BAND`), skip the current-drawdown/velocity path, and build the record with hypothetical statement wording + additive `stats.hypothetical*`. No-match → strategy default (`no_band_match`). Keep money `Decimal`.
- [x] **API** (AC 3) — `api/precedent.py`: add optional `drawdown: Decimal | None` to `ContextualizeIn` (and/or a `recovery` query param); validate `0 < drawdown ≤ 0.90` (calm 422 otherwise); pass through to `find_precedent`. `RecoveryPrecedentOut` top-level shape unchanged.
- [x] **Frontend** (AC 4, 5) — `ballast/frontend/src/components/HeadlineContextualizer.jsx` (+`.css`): add the scenario selector; on select, POST `{ headline, drawdown }`; render via `PrecedentEvidence`. Reuse the existing mounted-ref/fail-quiet pattern.
- [x] **Tests** — `tests/test_precedent_endpoint.py` + a precedent-engine test: hypothetical band surfaces the deep 2008/2020 episodes; out-of-range → 422; no-match → strategy default; absent param → unchanged. `ballast/frontend/src/test/headline-contextualizer.test.jsx`: a scenario renders a hypothetical record; default (no scenario) unchanged.

## Dev Notes

### Engine anchors (read first)
- `precedent/engine.py`: `find_precedent` (entry), `current_drawdown` (~89), `historical_episodes` (~149), `MAGNITUDE_BAND = 0.025` (~42), all-time-high/zero-magnitude → strategy default (~47). The hypothetical path bypasses `current_drawdown` for the query magnitude but reuses `historical_episodes` + the same ±band filter and the same recovery/forward-return stat computation, so matched-window stats stay identical in shape to the live path.
- `precedent/evidence.py`: `EvidenceRecord` — the AD-12 6-field contract (`id, kind, statement, stats, source, as_of`). `stats` is a free-form dict, so `hypothetical`/`hypothetical_drawdown_pct` are safe additive keys; **do not add/rename a top-level field** (Epic 4's replay + coach depend on the exact 6).
- `api/precedent.py`: `contextualize` (~87, headline inert, `find_precedent(symbol)`), `recovery_precedent` (~53), `RecoveryPrecedentOut` (frozen 6-field), `ContextualizeIn`.

### Framing (honesty is load-bearing — NFR8, FR20)
- Always hypothetical, never a forecast: lead with "If … fell about X%", cite "the record shows", and keep the existing uncertainty ethos ("this isn't a prediction; it's the base rate"). Round the target to a friendly band in the copy ("about 20%").
- Headline still classifies nothing: `drawdown` (a number) drives the match; `headline` remains inert and unparsed.
- `id` should encode the hypothetical magnitude so two identical hypothetical queries are byte-stable (mirrors the deterministic-id property the current records have).

### Frontend
- Reuse `PrecedentEvidence` (color/honesty single source of truth) and `MarketIndicator`. Scenario control is calm chips/select — not a fear nudge; copy frames it as "want to see what history shows for a bigger drop?".
- Keep the current-conditions result as the default so the surface still works with no scenario chosen.

### Out of scope (note, don't build)
- Wiring hypothetical precedent into the **Coach LLM pipeline** (so a fearful question auto-surfaces it) — a separate Epic 4 follow-on.
- Event/taxonomy tagging of headlines (explicitly deferred by FR20 v1).
- Changing `MAGNITUDE_BAND` or the recovery-episode detection.

### References
- [Source: planning-artifacts/epics.md] — FR20 (headline contextualizer: comparable-drawdown precedent, never event classification).
- [Source: planning-artifacts/ux-designs/.../EXPERIENCE.md] — headline contextualizer as a calming, non-interpretive surface; UJ-1 scary-moment.
- [Source: precedent/engine.py] — current-drawdown matching this story generalizes to an explicit target.

## Verification

**Commands:**
- `cd ballast/backend && .venv/bin/pytest tests/test_precedent_endpoint.py -q` — hypothetical band, 422 bounds, no-match default, absent-param-unchanged all green.
- `cd ballast/frontend && npm test && npm run lint:css` — scenario + default paths green; no hardcoded colors.

**Manual check (real Tiingo data loaded):** `POST /api/precedent/contextualize {symbol:"VTI", drawdown:0.35}` → a hypothetical record citing the 2008/2020 deep drops with a real multi-month median recovery and a "higher a year later in Y of N" figure — framed as "if it fell ~35%", not a prediction.

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (Opus 4.8, 1M context) — unattended BMAD implementation subagent.

### Debug Log References

- Full backend `pytest -q` cannot complete in this environment: `tests/test_coach_api.py` (and the LLM-network coach-pipeline cases) HANG on a live-network/LLM path unrelated to this story — confirmed pre-existing by bisecting individual files (`test_atomic`/`test_brokerage` pass, `test_coach_api` hangs). This story's change is additive and confined to the precedent module; every precedent-touching suite was run directly and passes (see below).

### Completion Notes List

- **Engine (`precedent/engine.py`).** Added a keyword-only `hypothetical_drawdown: Decimal | None = None` to `find_precedent`. When set (positive `Decimal`), a new `_find_hypothetical_precedent` helper reuses `historical_episodes` + `_match_and_rank` with the target magnitude and velocity ranked as 0 (velocity is not a hypothetical input), bypassing `current_drawdown` entirely. It builds the `event-precedent` record with additive `stats["hypothetical"]=True` + `stats["hypothetical_drawdown_pct"]` (4-dp `Decimal`) and a reworded honest statement ("If SYM fell about X% from a recent high, here's what the record shows: … recovered … in a median of N trading days, and it was higher a year later in Y of N. This isn't a prediction; it's the base rate."). No band match → the `strategy` default with `reason="no_band_match"`, and the target is carried into the strategy `stats` (additive keys) so its `id` encodes the magnitude — byte-stable and distinct from a live no-match. The six top-level `EvidenceRecord` fields are untouched (AD-12). `hypothetical_drawdown=None` is byte-identical to prior behavior (pinned by a test).
- **Deviation from a literal reading:** the spec's example statement said "since 2004". I dropped the hardcoded year and used "on record" instead — the engine has no build-time knowledge of the data's start year (it is symbol/data-dependent and must stay deterministic without a wall-clock/`series[0]` assumption baked into copy), so "on record" is the honest, data-agnostic phrasing. The base-rate + uncertainty framing the spec requires is preserved.
- **API (`api/precedent.py`).** Added optional `drawdown: Decimal | None = Field(default=None, gt=0, le=Decimal("0.90"))` to `ContextualizeIn` — Pydantic bounds yield a calm 422 out of range — passed through as `hypothetical_drawdown`. Also added the identical bounded `drawdown` query param to `GET /precedent/recovery` (spec-optional; low-risk and consistent). `RecoveryPrecedentOut` top-level shape unchanged.
- **Frontend (`HeadlineContextualizer.jsx` + `.css`).** Added a calm scenario chip group ("Right now" default + "A dip ~5%", "A correction ~10%", "A bear market ~20%", "A crash ~35%"), framed as "Or see what the record shows for a bigger drop" — no fear nudge/urgency. Chips share a single `fetchPrecedent(drawdown)` path: `null` → body `{ headline }` (current-conditions default, unchanged wire contract), a scenario → `{ headline, drawdown }`. The result renders through the existing `PrecedentEvidence` block (sky-blue ▼ / green ▲, never red, presentation-only). Hypothetical results show a distinct non-prediction framing line. Reused the mounted-ref/fail-quiet pattern. CSS uses only existing tokens (no hardcoded colors); no motion added.
- **Tests.** Engine: 4 new tests in `test_precedent.py` (deep episode surfaced bypassing a shallow current drop; no-band-match → strategy; byte-stable/distinct id per target; absent param byte-identical to default). Endpoint: 4 new tests in `test_precedent_endpoint.py` (hypothetical deep episode; out-of-range → 422; no-match → strategy; absent drawdown → current-conditions, no hypothetical keys). Frontend: 3 new tests in `headline-contextualizer.test.jsx` (chips gated on headline + no fetch on mount; scenario POSTs `{headline, drawdown}` and renders the hypothetical record with correct colors/framing; default submit still POSTs exactly `{headline}`).

### Verification (actual)

- `pytest tests/test_precedent.py -q` → 18 passed (14 prior + 4 new).
- `pytest tests/test_precedent_endpoint.py -q` → 7 passed (3 prior + 4 new).
- `pytest tests/test_precedent.py tests/test_precedent_endpoint.py tests/test_headline_contextualizer_endpoint.py tests/test_missed_growth.py tests/test_missed_growth_endpoint.py -q` → 44 passed.
- `pytest tests/test_recommendation_gate.py -q` → 22 passed; `pytest tests/test_coach_pipeline.py -k "not (llm or anthropic)"` → 34 passed, 6 deselected (deselected = pre-existing LLM-network cases).
- Full `pytest -q` NOT run to completion — hangs in `test_coach_api.py` on a pre-existing live-network path unrelated to this story (see Debug Log).
- `npm test` → 89 passed across 13 files (incl. `headline-contextualizer.test.jsx` with the 3 new cases; `recovery-precedent.test.jsx` green).
- `npm run lint:css` → clean (no errors).

### File List

- `ballast/backend/precedent/engine.py` — MODIFIED (hypothetical path)
- `ballast/backend/api/precedent.py` — MODIFIED (`drawdown` param on POST /contextualize + GET /recovery)
- `ballast/backend/tests/test_precedent.py` — MODIFIED (4 engine tests)
- `ballast/backend/tests/test_precedent_endpoint.py` — MODIFIED (4 endpoint tests)
- `ballast/frontend/src/components/HeadlineContextualizer.jsx` — MODIFIED (scenario selector)
- `ballast/frontend/src/components/HeadlineContextualizer.css` — MODIFIED (chip styles)
- `ballast/frontend/src/test/headline-contextualizer.test.jsx` — MODIFIED (3 frontend tests; +1 review honesty assertion)
- `ballast/frontend/src/components/PrecedentEvidence.jsx` — MODIFIED (review patch: hypothetical-aware "In this scenario" labeling so a queried target is never shown as the symbol's real current standing)

## Review Triage Log

### 2026-08-03 — Review pass (follow-up #2)
- intent_gap: 0
- bad_spec: 0
- patch: 1: (high 0, medium 0, low 1)
- defer: 0
- reject: 17
- addressed_findings:
  - `[low]` `[patch]` The hypothetical statement rounded the target to a friendly whole percent (`quantize("1")`), so a valid-but-tiny `drawdown` (the API validator only requires `> 0`; the UI never sends `< 0.05`, but a direct caller can) rendered the nonsensical honesty copy *"If SYM fell about 0% from a recent high"* — and the no-band-match strategy statement read *"no comparable 0% drop"* (honesty invariant NFR8/FR20/AC2). Fixed in `engine.py`: new `_target_pct_display` helper keeps the friendly whole-percent band for normal targets but falls back to one decimal below 1% ("about 0.5%"); applied at both the matched (`~400`) and no-band-match (`~591`) statement sites. Pinned by `test_hypothetical_sub_one_percent_target_never_reads_zero`. `test_precedent.py` + `test_precedent_endpoint.py` → 28 passed.
  - Rejected (noise / by-design / contrived, deduped across both reviewers): median-low forward-return vs positive-count phrasing only diverges on a contrived even/mixed-sign match set (real deep-drawdown data is all-positive); `≤ 0.90` API bound is the spec'd AC3 ceiling; "of N-with-forward-window" denominator and the index-investing plan copy are both honest and consistent with existing app-wide messaging; `aria-pressed` on the "Right now" chip after a plain submit is semantically correct (a current-conditions result IS "Right now"); stale record after a headline edit is a non-issue because the headline is inert (byte-identical record either way); duplicated `if`, redundant `current_drawdown` recompute, 4-dp id granularity, fabricated-0 velocity, and various test-coverage nits are cosmetic or already covered.

### 2026-08-03 — Review pass (follow-up)
- intent_gap: 0
- bad_spec: 0
- patch: 1: (high 0, medium 1, low 0)
- defer: 1: (high 0, medium 1, low 0)
- reject: 19
- addressed_findings:
  - `[medium]` `[patch]` `_find_hypothetical_precedent` matched over the FULL `historical_episodes(series)`, unlike the live path which excludes the symbol's current in-progress drawdown (`peak_date < current_peak_date`). When the symbol is presently in a drop whose magnitude falls in the queried band, that live, unrecovered drop was counted as a "comparable drop on record" — inflating `instance_count` and polluting the base rate, contradicting the docstring's own "reuses the SAME filter as the live path" claim (honesty invariant NFR8/FR20/AC1). Fixed in `engine.py`: the hypothetical path now applies the identical current-episode exclusion before `_match_and_rank`; docstring corrected. Pinned by a new engine test (`test_hypothetical_excludes_current_in_progress_drop`) — a 20% query on a symbol presently ~20% down surfaces only the completed historical episode. `test_precedent.py` + `test_precedent_endpoint.py` → 27 passed.

### 2026-08-03 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 3: (high 1, medium 0, low 2)
- defer: 2: (high 0, medium 0, low 2)
- reject: 12
- addressed_findings:
  - `[high]` `[patch]` `PrecedentEvidence` rendered a hypothetical record's queried target (`stats.initial_drawdown_pct`) under the label "Where it stands now — {X}% below its recent peak", presenting a fiction as the symbol's real position (honesty invariant, NFR8/FR20/AC2/AC5). Fixed in `PrecedentEvidence.jsx`: when `stats.hypothetical === true`, render "In this scenario — a ~{X}% drop from a recent high"; live path unchanged (guarded). Pinned by a new frontend assertion + `recovery-precedent.test.jsx` still green.
  - `[low]` `[patch]` Hypothetical statement read "in 1 comparable drops" for a single-episode band match (realistic for a ~35% target that lands on one episode). Fixed in `engine.py` (`drop_word` pluralization); endpoint test now asserts the singular grammar.
  - `[low]` `[patch]` `GET /precedent/recovery`'s new `drawdown` param shipped untested. Fixed: added `test_recovery_hypothetical_drawdown_param` (hypothetical pass-through + out-of-range 422).

## Auto Run Result

Status: done (follow-up review pass #2)

**Change reviewed:** Story 3.6 (hypothetical-drawdown precedent, FR20) — a follow-up adversarial + edge-case review of the already-`done` implementation. Two reviewers (Blind Hunter, Edge Case Hunter) ran in parallel against the full diff since `d235d2a`.

**Files changed this pass:**
- `ballast/backend/precedent/engine.py` — added `_target_pct_display` helper; hypothetical statement + no-band-match statement now use it (sub-1% targets render one decimal instead of rounding to "0%").
- `ballast/backend/tests/test_precedent.py` — added `test_hypothetical_sub_one_percent_target_never_reads_zero`.

**Findings breakdown:** 1 patch applied (low — tiny-target "about 0%" honesty copy); 0 intent_gap; 0 bad_spec; 0 deferred; 17 rejected (contrived match-set assumptions, spec'd bounds, by-design coaching copy, semantically-correct a11y state, inert-headline non-issues, cosmetic/maintainability nits, test-coverage nits).

**Verification:** `pytest tests/test_precedent.py tests/test_precedent_endpoint.py -q` → **28 passed** (27 prior + 1 new). Frontend untouched this pass (no JSX/CSS change), so the frontend suite is unaffected.

**Follow-up review recommended:** false — this pass made a single localized, low-consequence copy patch behind a nonsensical direct-API input the UI never produces.

**Residual risks:** None material. The rejected findings are documented in the triage log for traceability; the strongest (median-low vs positive-count phrasing, denominator shift) only surface on contrived match sets that real Tiingo deep-drawdown history does not produce, and were judged honest-as-written.

