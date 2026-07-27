---
title: 'Story 3.2: Drawdown matching & Evidence Record Contract'
type: 'feature'
created: '2026-07-27'
baseline_revision: df1d714e4e4b227f58804c91802443e9a5a47809
final_revision: 1da88c1ff2e01cc7670e02d08f995b354b99c0be
status: 'done'
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/_bmad-output/planning-artifacts/architecture/architecture-ai_practice_project-2026-07-22/ARCHITECTURE-SPINE.md'
  - '{project-root}/_bmad-output/implementation-artifacts/3-1-market-data-ingestion-market-daily.md'
warnings: ['oversized']
---

<intent-contract>

## Intent

**Problem:** Ballast's coach may never fabricate a market fact (FR13); every precedent claim must come from a deterministic engine over real history. Epic 4 (recommendation validator, immutable decision snapshot) depends on a fixed evidence shape that must never drift. Neither the engine nor its output contract exists yet — only the `market_daily` store (Story 3.1) it reads from.

**Approach:** Build a deterministic, LLM-free Precedent Engine in `precedent/` that, given a current drawdown (magnitude + velocity), finds historically similar drawdown episodes in `market_daily` and returns them as an **Evidence Record** of the fixed AD-12 shape `{id, kind, statement, stats, source, as_of}`. When no episode qualifies, it returns the always-available `strategy` fallback record instead of an empty result.

## Boundaries & Constraints

**Always:**
- Every returned record has EXACTLY the fixed shape `{id, kind, statement, stats, source, as_of}`; `kind` ∈ {`event-precedent`, `strategy`}; `as_of` is an ISO-8601 date; all prices/percentages are `Decimal` (never float); day counts are `int`.
- Computation is fully deterministic: no LLM, no network, no randomness, no wall-clock reads in the matching path (`as_of` is passed in / defaults to the latest `day` present in data). Same `(symbol, as_of)` + same `market_daily` rows → byte-identical records including identical `id`.
- `id` is a deterministic content hash of the record (symbol, kind, as_of, stats), so a record snapshotted into an immutable decision (Epic 4 / AD-5) is reproducible and never re-derived.
- Reads ONLY the global `market_daily` table directly (no `owner_id`, not via `ScopedRepository` — it is global reference data, AD-10). Never `yfinance` or any live vendor call.
- The engine never dead-ends: every call returns a non-empty `list[EvidenceRecord]` (≥1 record).

**Block If:**
- The AD-12 evidence shape would need a field added/removed/renamed beyond `{id, kind, statement, stats, source, as_of}` to satisfy this story (a contract change ripples into Epic 4 — a product/architecture decision, not a build detail).

**Never:**
- No REST endpoint, no UI, no coach wiring — those are Story 3.3+ / Epic 4. This story is the engine + contract + tests only.
- No event-category / news taxonomy matching (explicitly a later enrichment; v1 is drawdown-based only).
- No persistence of evidence records (they are computed on demand; snapshotting is Epic 4's job).
- No LLM Gateway, Anthropic SDK, or any generative call.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Qualifying precedent | Symbol currently ~8% below peak; history contains ≥1 past episode with magnitude in the band | ONE `event-precedent` record; `stats` carries `initial_drawdown_pct`, `current_velocity`, `instance_count`, `recovery_days_median`, `recovery_days_range {min,max}`, `forward_return_1yr_median`, and `windows[]` (per-episode peak/trough/recovery dates + stats) | No error expected |
| No qualifying precedent | Current magnitude has zero matching historical episodes (band empty) | ONE `strategy` record (default-plan statement, empty `stats.windows`) — never empty list | No error expected |
| Not in a drawdown | Symbol at/near all-time high (magnitude ≈ 0) | ONE `strategy` record ("no comparable drop; stay the course") | No error expected |
| Insufficient data | Symbol absent from `market_daily`, or < 2 bars | ONE `strategy` record; structured warning logged | No crash; degrade to fallback |
| Determinism | Same `(symbol, as_of)` run twice | Identical records incl. identical `id` | No error expected |

</intent-contract>

## Code Map

- `ballast/backend/precedent/__init__.py` -- existing placeholder; will export public API (`find_precedent`, `EvidenceRecord`, `EvidenceKind`).
- `ballast/backend/precedent/evidence.py` -- NEW: the AD-12 contract type + deterministic id.
- `ballast/backend/precedent/engine.py` -- NEW: drawdown computation, episode detection, band matching, aggregate stats, strategy fallback, async `market_daily` read, public entry point.
- `ballast/backend/db/models.py` -- `MarketDaily` (read source): cols `symbol`, `day`, `open/high/low/close/adj_close` (`Numeric(20,8)` → `Decimal`), `volume`, `source`, `ingested_at`; UNIQUE(`symbol`,`day`); no `owner_id`.
- `ballast/backend/db/session.py` -- `async_session_maker` for reads.
- `ballast/backend/marketdata/port.py` -- `DailyBar` shape reference (adj_close is the series used for drawdown math).
- `ballast/backend/tests/test_market_ingest.py` -- real-DB test pattern to mirror (autouse table fixture, per-test cleanup with prefixed test symbols, exact `Decimal` assertions).

## Tasks & Acceptance

**Execution:**
- [x] `ballast/backend/precedent/evidence.py` -- Define `EvidenceKind` (`str`, `Enum`: `EVENT_PRECEDENT="event-precedent"`, `STRATEGY="strategy"`) and a frozen `EvidenceRecord` dataclass with exactly `id: str`, `kind: EvidenceKind`, `statement: str`, `stats: dict`, `source: str`, `as_of: date`. Provide `to_dict()` (JSON-safe: `Decimal`→str, `date`→ISO) and a module-level `make_id(kind, symbol, as_of, stats) -> str` (e.g. `sha256` of canonical JSON, hex-truncated, prefixed `ep-`/`strat-`). -- Fixed AD-12 shape that Epic 4 cites and snapshots; stable id enables immutable replay.
- [x] `ballast/backend/precedent/engine.py` -- Implement, over `market_daily`: (1) async `_load_series(session, symbol) -> list[(day, adj_close: Decimal)]` ordered by `day`; (2) `current_drawdown(series, as_of)` → running-peak (high-water-mark) magnitude `(peak-close)/peak` as `Decimal` + `velocity` = magnitude / trading-days-from-peak-to-`as_of`; (3) `historical_episodes(series)` → each past peak→trough→recovery-to-peak window with magnitude, velocity, `recovery_days`, `forward_return_1yr`; (4) band match episodes to the current magnitude (default ±0.025), rank by |Δmagnitude| then |Δvelocity|; (5) build ONE aggregate `event-precedent` record (aggregate `stats` + `windows[]`) when ≥1 match, else the `strategy` fallback record; (6) public `async def find_precedent(session, symbol=DEFAULT_BENCHMARK, as_of=None) -> list[EvidenceRecord]` (returns len-1 list in v1). Use `statistics.median_low` for integer day medians; quantize `Decimal` returns. Log a structured warning on insufficient data and return the strategy record. -- The deterministic matching engine; single citable evidence record + drill-down windows in `stats`.
- [x] `ballast/backend/precedent/__init__.py` -- Add module docstring (engine is the sole source of market statistics, AD-3; deterministic, no LLM) and export `find_precedent`, `EvidenceRecord`, `EvidenceKind`. -- Clean public surface for Epic 4 callers.
- [x] `ballast/backend/tests/test_precedent.py` -- Real-Postgres tests mirroring `test_market_ingest.py` conventions (autouse table fixture, per-test cleanup, TEST-prefixed symbols). Insert crafted deterministic bar series with a KNOWN peak→trough→recovery, then cover the I/O matrix: qualifying match (assert exact `Decimal` stats, `windows[]`, and fixed shape), no-match → `strategy` fallback, not-in-drawdown → `strategy`, insufficient data → `strategy` (no crash), determinism (two runs → identical records incl. identical `id`), and shape/`kind`/`Decimal` assertions. -- Proves determinism, the contract shape, and the fallback.

**Acceptance Criteria:**
- Given `market_daily` holds a symbol's history with at least one past drawdown episode of comparable magnitude, when `find_precedent(session, symbol, as_of)` runs, then it returns exactly one `event-precedent` `EvidenceRecord` of the fixed shape whose `stats` include `instance_count`, `recovery_days_median`, `recovery_days_range`, and `forward_return_1yr_median` computed from the matched windows.
- Given a current state with no qualifying historical episode (empty band, all-time high, or insufficient data), when the engine runs, then it returns exactly one `strategy` `EvidenceRecord` and never an empty list.
- Given identical `market_daily` rows and identical `(symbol, as_of)`, when the engine runs twice, then both runs produce byte-identical records including an identical `id`, and no LLM/network/random/wall-clock call occurs in the matching path.
- Given any returned record, when its fields are inspected, then they are exactly `{id, kind, statement, stats, source, as_of}` with `kind` ∈ {`event-precedent`, `strategy`}, prices/percentages as `Decimal`, and `as_of` an ISO-8601 date.
- Given the engine reads market data, when it loads the series, then it reads only the global `market_daily` table directly (no `owner_id`, no `ScopedRepository`) and performs no live vendor call.

## Design Notes

**Build-time parameters (spine-deferred; module constants, tunable):**
- `DEFAULT_BENCHMARK = "VTI"` (broad index-core proxy; overridable per call).
- Magnitude band = current ± `0.025` (2.5 pp) absolute; primary hard filter.
- Velocity = magnitude ÷ trading-days from running peak to trough (current: to `as_of`); computed for current + each window; used as secondary rank key, not a hard cut in v1.
- Recovery = trading days from trough back to the prior peak (breakeven). Episodes not recovered by end of data are excluded from `recovery_days_*` but still counted in `instance_count` (flag them in their `windows[]` entry).
- Forward return = close-to-close return over 252 trading days from the trough; windows with < 252 following bars omit `forward_return_1yr` (excluded from median).
- Lookback = all available history for the symbol. Min match to qualify = ≥ 1 episode.

**Why one aggregate record + windows-in-stats:** Epic 4 cites evidence by a single stable `id`; an aggregate `event-precedent` record ("N similar drops recovered in a median of X trading days") is the natural citable, calming unit, while `stats.windows[]` preserves the individual matched windows for the Story 3.3 drill-down and travels inside the record so it snapshots immutably for replay. Keeping windows inside `stats` (an object) honors the fixed 6-field shape.

**Golden `EvidenceRecord` (event-precedent), JSON-safe:**
```
id: "ep-9f2a1c7b3d40"
kind: "event-precedent"
statement: "VTI is ~8.0% below its recent peak. In 5 similar drops, it recovered to breakeven in a median of 34 trading days."
stats: { initial_drawdown_pct: "0.0801", current_velocity: "0.0021",
         instance_count: 5, recovery_days_median: 34, recovery_days_range: {min: 12, max: 71},
         forward_return_1yr_median: "0.1140",
         windows: [ {peak_date: "2018-09-20", trough_date: "2018-12-24", recovery_date: "2019-04-23",
                     drawdown_pct: "0.0795", recovery_days: 34, forward_return_1yr: "0.1502"} ] }
source: "VTI daily close (market_daily, Tiingo EOD)"
as_of: "2026-07-27"
```

## Verification

**Commands:**
- `cd /Users/blainearnau/repos/ai_practice_project/ballast/backend && docker compose up -d db && .venv/bin/python -m pytest tests/test_precedent.py -v` -- expected: all new precedent tests pass.
- `cd /Users/blainearnau/repos/ai_practice_project/ballast/backend && .venv/bin/python -m pytest -q` -- expected: full suite green (no regressions), test count increased over the current baseline.

## Review Triage Log

### 2026-07-27 — Review pass (follow-up)
- intent_gap: 0
- bad_spec: 0
- patch: 4 (high 0, medium 0, low 4)
- defer: 1 (low 1)
- reject: 13
- addressed_findings:
  - `[low]` `[patch]` The coach-facing `statement` rendered the drop as `~8.0000%` (percent quantized at the 4-dp `stats` grain) instead of the spec golden example's `~8.0%`. Added a display-only `_PCT_DISPLAY_Q` (1-dp) for the statement percentage; leaves `stats` and the `id` hash untouched (statement is not part of `make_id`).
  - `[low]` `[patch]` The insufficient-data branch stamps `as_of=date.min` (`0001-01-01`) when the series is empty AND no `as_of` was passed, which reads as a bug. It is in fact the correct deterministic behavior — using `date.today()` would break the no-wall-clock invariant, and `date.min` is a valid ISO-8601 date so the AD-12 shape holds. Added a comment documenting the deliberate choice to prevent a future "fix" from reintroducing a wall-clock read.
  - `[low]` `[patch]` The two-run determinism test used byte-identical inserts, so it could not catch raw `Numeric(20,8)` round-trip precision leaking into `make_id`. Added `test_id_is_stable_across_input_decimal_precision` (same values written with extra trailing-zero precision → identical `id` and `to_dict()`), pinning the AD-5 replay invariant.
  - `[low]` `[patch]` The forward-return median was only ever exercised with a single forward value (qualify test) or all-`None` (multi-episode test) — the multi-value `median_low` over `Decimal`s was uncovered. Added `test_forward_return_median_over_multiple_windows_is_decimal` (two 8% episodes, each with a full 252-bar forward window → two distinct `Decimal` forward returns), asserting the aggregate median stays `Decimal` (never `None`/float) and equals the lower value.
- deferred:
  - `[low]` The ±2.5pp magnitude band has no lower floor, so a trivially small historical wiggle (e.g. a 0.1–0.5% dip that still forms a peak→trough→recovery episode) can band-match a small current drop and be cited as "precedent" — appended to `deferred-work.md` (revisit when tuning bands against real Tiingo history).
- rejected (13): NULL/`adj_close` crash & duplicate-row skew (impossible — `adj_close` is `NOT NULL` and `UNIQUE(symbol,day)` is enforced at the DB); `source` omits the vendor (AD-12 requires only a `source` field, present; the example text is illustrative and current data is the "fake" adapter); tests require a live Postgres (spec explicitly mandates mirroring `test_market_ingest.py`'s real-DB convention); raw `select` bypasses `ScopedRepository` (by-design AD-10 — `market_daily` is ownerless global reference data); self-exclusion rests on the plateau-tie invariant (both paths already aligned to `>=` in the prior pass and `(symbol,day)` is UNIQUE so peak dates never tie); strategy `statement` copy unasserted (low product concern, not in `id`, no coach wiring this story); `stats` dict is shallow-mutable despite `frozen=True` (Epic 4 snapshots via `to_dict()`/JSON; `frozen` blocks attribute rebinding as intended); forward median computed on raw then quantized vs quantized windows (deterministic; `median_low` returns an input element and ordering is preserved); `recovery_days=0` V-bottom (impossible — recovery index is always > trough index); recovery-bar reused as next peak start double-counts (conventional drawdown-from-running-peak definition; sub-pullback counting was already rejected); 48-bit `id` truncation collision (negligible for one record per `(symbol, as_of)`; Epic 4 freezes the whole record); flat-plateau velocity denominator (consistent between current and episode paths by construction); gapless-index recovery/forward math & non-positive `adj_close` (both ALREADY tracked in `deferred-work.md` from the prior pass — not re-deferred).

### 2026-07-27 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 7 (high 0, medium 2, low 5)
- defer: 2 (low 2)
- reject: 6
- addressed_findings:
  - `[medium]` `[patch]` No test exercised the multi-window aggregation path (medians/ranges over >1 matched episode) — the headline "N drops recovered in median X days" stat was only covered at N=1. Added `test_multi_episode_aggregation_median_and_range` (4 matched 8% episodes, recovery_days [2,4,6,8]) exercising EVEN-count `median_low` (=4), range {2,8}, `instance_count`, all-`None` forward-return median, and per-window detail.
  - `[medium]` `[patch]` Peak-tie handling was asymmetric — `current_drawdown` picked the FIRST bar of a high-water plateau (strict `>`) while `historical_episodes` extended to the LAST (`>=`), so velocity and the peak_date exclusion filter were computed by inconsistent rules on flat plateaus. Aligned `current_drawdown` to `>=` (also subsumes the "exclude current drawdown by peak_date vs index" finding — peaks are now chosen identically in both paths).
  - `[low]` `[patch]` The four strategy-fallback situations (insufficient data / all-time high / no-band-match) produced identical `stats={"windows":[]}`, indistinguishable without parsing English and colliding on the same `id` at a given `(symbol, as_of)`. Added a machine-readable `stats.reason` (`insufficient_data` | `all_time_high` | `no_band_match`) that also gives each reason a distinct deterministic id.
  - `[low]` `[patch]` The insufficient-data branch used `as_of or (...)` while the other branch used `as_of if as_of is not None else (...)` — a latent inconsistency. Unified on the explicit `is not None` form.
  - `[low]` `[patch]` `_NEAR_ZERO` was named as if it were a tolerance band but held exactly `Decimal("0")`. Renamed to `_NO_DRAWDOWN` and corrected the docstring (exact-zero = at the running peak; magnitude is floored at 0).
  - `[low]` `[patch]` The determinism test only compared two same-day runs, which could not detect a wall-clock read. Added `test_default_as_of_is_latest_bar_not_today` asserting an `as_of=None` call is stamped with the latest DATA day (a 2015 date), never `date.today()`.
  - `[low]` `[patch]` No test guarded the "never a Python float" and "directly json.dumps-able" contract claims. Added `test_record_is_json_safe_and_never_float` (recursive no-`float` assertion over `stats` + a `json.dumps(to_dict())` round-trip).
- deferred:
  - `[low]` Recovery-day / 252-"trading-day" forward-return math offsets by row index, silently assuming gapless one-row-per-trading-day `market_daily` — appended to `deferred-work.md` (revisit with real Tiingo history).
  - `[low]` Engine does not validate positive `adj_close`; degenerate zero/negative vendor data could yield a >100% magnitude (divide guards prevent crashes only) — appended to `deferred-work.md`.
- rejected (6): `id` excludes `statement`/`source` (both are deterministic functions of the hashed `(symbol, as_of, stats)` inputs — they cannot vary independently for identical inputs; spec defines the id basis explicitly); velocity semantics differ current(peak→as_of) vs episode(peak→trough) (spec-sanctioned, intended); episode scan restarts at recovery so nested pullbacks aren't separate episodes (this IS the conventional drawdown-from-running-peak definition; counting sub-pullbacks would double-count); no guard for `as_of` beyond the series range (normal path is `as_of=None`→latest; callers pass valid dates); no schema-version in the id hash (Epic 4 replay freezes the whole record, so cross-code-version id reproducibility is not required); event-precedent with all-unrecovered/no-forward matches is "numberless" (windows still carry dates + magnitudes and the statement is honest — degrading to strategy would hide real precedent).


## Auto Run Result

Status: done (follow-up review pass — the prior run recommended one)

**Summary:** Ran a fresh independent adversarial + edge-case review over the full Story 3.2 diff (the deterministic, LLM-free Precedent Engine and the AD-12 Evidence Record contract) since baseline `df1d714`. No intent gaps and no spec deviations were found — the engine, the fixed 6-field contract, and its determinism/`Decimal`/global-read invariants all hold. Four low-consequence patches were applied (one consumer-facing cosmetic fix, one clarifying comment, two invariant-hardening tests); one new lower-priority item was deferred.

**Files changed this pass:**
- `ballast/backend/precedent/engine.py` — statement percentage now renders at a 1-dp display grain (`~8.0%`, matching the spec golden example) via a display-only `_PCT_DISPLAY_Q`; added a comment documenting that the empty-series `as_of=date.min` fallback is a deliberate deterministic sentinel (using `date.today()` would break the no-wall-clock invariant).
- `ballast/backend/tests/test_precedent.py` — added `test_id_is_stable_across_input_decimal_precision` (id/`to_dict` invariant vs `Numeric(20,8)` round-trip precision) and `test_forward_return_median_over_multiple_windows_is_decimal` (multi-value `median_low` over `Decimal` forward returns stays `Decimal`, never `None`/float).

**Review findings breakdown:** 0 intent_gap · 0 bad_spec · 4 patch (all low) applied · 1 defer (low, appended to `deferred-work.md` as a new entry) · 13 reject. See the follow-up entry in the Review Triage Log for the per-finding rationale.

**Verification:**
- `pytest tests/test_precedent.py -v` → 13 passed (11 prior + 2 new).
- `pytest -q` (full backend suite) → 127 passed, 1 pre-existing unrelated deprecation warning. No regressions.

**Follow-up review recommended:** false — this pass made only a handful of localized, low-consequence hardening changes with no behavior, API, security, or data impact.

**Residual risks:** All tracked in `deferred-work.md` — gapless-index recovery/forward-return math and non-positive `adj_close` handling (revisit with real Tiingo history), and the newly deferred lack of a lower magnitude-band floor. None block this story's scope (engine + contract + tests over deterministic crafted data).
