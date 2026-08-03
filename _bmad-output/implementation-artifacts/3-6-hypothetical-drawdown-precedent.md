# Story 3.6: Hypothetical-drawdown precedent ("what if it fell ~X%?")

Status: ready-for-dev

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

- [ ] **Engine** (AC 1, 2, 3b) — `precedent/engine.py`: add `hypothetical_drawdown` kwarg to `find_precedent`; when set, center the magnitude match on it (reuse `historical_episodes` + `MAGNITUDE_BAND`), skip the current-drawdown/velocity path, and build the record with hypothetical statement wording + additive `stats.hypothetical*`. No-match → strategy default (`no_band_match`). Keep money `Decimal`.
- [ ] **API** (AC 3) — `api/precedent.py`: add optional `drawdown: Decimal | None` to `ContextualizeIn` (and/or a `recovery` query param); validate `0 < drawdown ≤ 0.90` (calm 422 otherwise); pass through to `find_precedent`. `RecoveryPrecedentOut` top-level shape unchanged.
- [ ] **Frontend** (AC 4, 5) — `ballast/frontend/src/components/HeadlineContextualizer.jsx` (+`.css`): add the scenario selector; on select, POST `{ headline, drawdown }`; render via `PrecedentEvidence`. Reuse the existing mounted-ref/fail-quiet pattern.
- [ ] **Tests** — `tests/test_precedent_endpoint.py` + a precedent-engine test: hypothetical band surfaces the deep 2008/2020 episodes; out-of-range → 422; no-match → strategy default; absent param → unchanged. `ballast/frontend/src/test/headline-contextualizer.test.jsx`: a scenario renders a hypothetical record; default (no scenario) unchanged.

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

### Debug Log References

### Completion Notes List

### File List
