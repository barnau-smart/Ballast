# Deferred Work

Incidental, real issues surfaced during autonomous review passes but out of scope for the story that found them. Each entry is appended by the dev-auto review step; do not edit existing entries.

- source_spec: `_bmad-output/implementation-artifacts/3-1-market-data-ingestion-market-daily.md`
  summary: Harden the TiingoAdapter response parser against real-vendor payload shapes (missing/None OHLC fields, non-list error payloads, robust ISO-8601 timezone→calendar-day handling) when real Tiingo network wiring lands.
  evidence: The adapter's `fetch_eod` assumes well-formed rows (`row["date"][:10]`, `Decimal(str(row[field]))`, `row.get("volume")`); a missing/null field or a non-list payload would raise inside the per-symbol try/except and silently skip that symbol. Deliberately out of scope for 3.1 (credential-gated stub, "do NOT wire real network here"), but it is real code that will run once `MARKETDATA_ADAPTER=tiingo` with a live key — revisit alongside the first real Tiingo integration.

- source_spec: `_bmad-output/implementation-artifacts/3-2-drawdown-matching-evidence-record-contract.md`
  summary: The Precedent Engine's recovery-day and 252-"trading-day" forward-return math offset by ROW INDEX, so both stats silently assume `market_daily` holds exactly one contiguous, gapless row per trading day; the engine neither owns nor verifies that invariant.
  evidence: `precedent/engine.py` computes `fwd_index = trough_index + FORWARD_RETURN_DAYS` and `recovery_days = recovery_index - trough_index` on row positions. If ingestion (Story 3.1 / a real Tiingo feed) ever leaves a gap or inserts a non-trading-day row, "252 trading days" spans the wrong horizon and recovery counts drift. Out of scope for 3.2 (fake/crafted data is dense by construction); revisit when real Tiingo history lands (alongside the vendor-hardening item above), e.g. a contiguity assertion at ingest or a calendar-aware offset in the engine.

- source_spec: `_bmad-output/implementation-artifacts/3-2-drawdown-matching-evidence-record-contract.md`
  summary: The Precedent Engine does not validate that `adj_close` values are positive; degenerate zero/negative vendor data could produce a drawdown magnitude > 100% (division guards prevent a crash but not a nonsensical stat).
  evidence: `historical_episodes`/`current_drawdown` guard `peak_close > 0` and `base > 0` against divide-by-zero, but a positive peak with a non-positive trough yields `magnitude = (peak - trough)/peak > 1`, which would flow into band-matching and a "~>100% below peak" statement. Cannot occur with the deterministic fake/crafted series (all positive); real bad-data handling belongs with the Tiingo vendor-hardening pass. Consider skipping/flagging non-positive `adj_close` in `_load_series` or clamping magnitude to ≤ 1 then.

- source_spec: `_bmad-output/implementation-artifacts/3-2-drawdown-matching-evidence-record-contract.md`
  summary: The Precedent Engine's ±2.5pp magnitude band has no lower floor, so a trivially small historical wiggle can qualify as a cited "precedent" for a small current drop.
  evidence: `_match_and_rank` filters episodes by `abs(episode.magnitude - current_magnitude) <= MAGNITUDE_BAND` (0.025) with no minimum episode magnitude. A ~0.1–0.5% historical dip that still forms a peak→trough→recovery episode falls in the band around a small current drawdown and is surfaced as an `event-precedent` window — a statistically meaningless "precedent" that would read as noise to a beginner. Cannot surface with the deterministic crafted test series (episodes are clean 8% drops); revisit when tuning band parameters against real Tiingo history (e.g. add a minimum-episode-magnitude floor or a relative band).
