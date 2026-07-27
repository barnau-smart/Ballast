---
baseline_commit: 2a1a25e35ca7dd0d208a672a828c00ad875c548e
baseline_revision: 35f8d01299fd1bbe22f0c6d649057aa9b8990d5c
status: in-review
followup_review_recommended: true
---

# Story 3.1: Market-data ingestion → `market_daily`

Status: done

## Story

As the system,
I want a local store of decades of daily market data,
so that all precedent is computed from real history I control.

## Acceptance Criteria

1. **SYSTEM-scope daily ingestion into `market_daily`.** Given a SYSTEM-scope job, when it runs, then it ingests EOD (end-of-day) market data into a `market_daily` store — global reference data (keyed by symbol + day), not per-user — computed/derived analytics, NOT redistributed raw vendor data. [Source: epics.md#Story-3.1 (AD-3), ARCHITECTURE-SPINE.md#AD-8, #Consistency-Conventions (Data sourcing)]
2. **Idempotent + re-runnable.** Running the job repeatedly (or for overlapping date ranges) does not duplicate rows — a symbol/day is upserted, so the store converges to one row per (symbol, day). [Source: ARCHITECTURE-SPINE.md#AD-14 analogue (single source of truth), FR13/FR15 (precedent computed over market_daily)]
3. **Tolerates source hiccups.** When the data source fails for one symbol (or transiently), the job logs it and continues with the other symbols rather than aborting the whole run; a later run backfills the gap. [Source: epics.md#Story-3.1 ("tolerates source hiccups"), NFR reliability]

**Cross-cutting:** market data lives behind a **port** (AD-8) — a `TiingoAdapter` is the production source (Stooq backup), swappable without touching callers; **fake-first**, so the whole ingestion path runs with ZERO credentials/network via a deterministic fake adapter. `market_daily` is GLOBAL reference data (no `owner_id`, not routed through the per-user `ScopedRepository`) — the ingestion runs as the non-user SYSTEM context (AD-10). Money is `Decimal`/`Numeric`, never float; `day` is a date, timestamps ISO-8601 UTC; never `yfinance` in production; never log secrets. [Source: ARCHITECTURE-SPINE.md#AD-8, #AD-10, #Consistency-Conventions]

## Tasks / Subtasks

- [x] **Task 1: Market-data port + normalized bar** (AC: 1)
  - [x] Add `marketdata/port.py`: a `DailyBar` frozen dataclass (`symbol`, `day: date`, `open/high/low/close/adj_close: Decimal`, `volume: int`) and a `MarketDataPort` ABC with `fetch_eod(symbol, start, end) -> list[DailyBar]`. Money is `Decimal`. Document it as the sole boundary to any market-data vendor (AD-8) — the Precedent Engine (Epic 3) depends only on `market_daily`, never on a vendor SDK.
- [x] **Task 2: Fake + Tiingo adapters + factory** (AC: 1, 3)
  - [x] `marketdata/fake_adapter.py`: `FakeMarketDataAdapter` — deterministic synthetic daily bars for a small symbol set (a generated, reproducible series spanning many days; NO wall-clock, NO network) so ingestion + later precedent are fully testable with zero creds. Determinism is load-bearing (precedent tests will assert exact stats later).
  - [x] `marketdata/tiingo_adapter.py`: `TiingoAdapter` — credential-gated, lazy-imports the Tiingo client, raises a clear `TiingoNotConfiguredError` without `TIINGO_API_KEY` (mirrors `SchwabAdapter`'s fail-loud posture). Do NOT wire real network here. Optionally note the Stooq backup as a future adapter.
  - [x] `marketdata/factory.py`: `get_market_data()` selects fake vs tiingo from config (`MARKETDATA_ADAPTER`, default `fake`); lazy-imports the Tiingo adapter so the default path never loads its SDK. Add the config keys to `api/config.py` + `.env.example`.
- [x] **Task 3: `market_daily` model (global reference)** (AC: 1, 2)
  - [x] Add `MarketDaily(Base)` to `db/models.py` — `__tablename__ = "market_daily"`, a UNIQUE constraint on (`symbol`, `day`), `Numeric` OHLC + adj_close, `volume` BigInteger, plus `source` and `ingested_at` (tz-aware UTC). It is deliberately NOT an `OwnedEntityMixin` (no `owner_id`) — it is global, not per-user; document why (it is not routed through `ScopedRepository`, which is for owned per-user entities).
- [x] **Task 4: The SYSTEM-scope ingestion job** (AC: 1, 2, 3)
  - [x] Add `marketdata/ingest.py`: `ingest_market_daily(session, source, symbols, start, end) -> IngestResult`. For each symbol: fetch bars via the port, UPSERT into `market_daily` on (`symbol`, `day`) (Postgres `ON CONFLICT` or select-then-update) so re-runs never duplicate (AC2). Wrap each symbol in try/except so one symbol's failure logs a structured warning and the run continues (AC3); return a small result summarizing rows written / symbols failed.
  - [x] Store DERIVED analytics fields (OHLC/adjusted close), not a redistribution of raw vendor payloads (Data-sourcing rule). Runs as the non-user SYSTEM context — market_daily is global; do NOT attach a user scope.
  - [x] Scheduling: build the job as a runnable, idempotent function (a thin CLI/entrypoint is fine). A real daily scheduler/cron wiring is a deployment concern — out of scope here; note how it would be invoked.
- [x] **Task 5: Tests (real DB)** (AC: 1, 2, 3)
  - [x] Fake adapter returns a deterministic bar series (assert exact values; `Decimal` money).
  - [x] Ingest writes `market_daily` rows (one per symbol/day); values round-trip as `Decimal`.
  - [x] **Idempotent:** running ingest twice over the same range yields the SAME row count (upsert, no dupes); a changed bar updates in place.
  - [x] **Source-hiccup tolerance:** with an adapter that raises for one symbol, the other symbols are still ingested and the failure is reported (run not aborted).
  - [x] `market_daily` is global (no `owner_id` column); a `MarketDataPort` selection via the factory returns the fake by default. No regressions.
- [x] **Task 6: Verify** — full backend suite; run the fake ingestion end-to-end (job → `market_daily` populated → re-run is a no-op count-wise → a symbol failure doesn't abort).

## Dev Notes

### Fake-first (no creds, no network) — same posture as Epic 2
The `SchwabAdapter` pattern (Story 2.1) is the template: a real adapter that is credential-gated + lazy-imported, and a deterministic fake that is the default/tested path. Build the entire ingestion + `market_daily` store against the fake; the `TiingoAdapter` is code-shaped but fails loud without `TIINGO_API_KEY`. This keeps 3.1 (and the precedent stories 3.2–3.4 that compute over `market_daily`) fully testable with zero credentials. When a Tiingo key is available, flip `MARKETDATA_ADAPTER=tiingo` — no caller changes (AD-8).

### `market_daily` is GLOBAL, not per-user [Source: ARCHITECTURE-SPINE.md#AD-10, #Consistency-Conventions]
Unlike `brokerage_token`/`portfolio_cache` (owned, per-user, via `ScopedRepository`), `market_daily` is shared reference data with NO owner. It is therefore NOT an `OwnedEntityMixin` and NOT routed through the fail-closed scoped repo (which exists specifically for owned per-user entities). The ingestion is the "non-user SYSTEM context" AD-10 anticipates — global by construction. Document this explicitly so it is not mistaken for a scoping bypass.

### Data sourcing rule [Source: ARCHITECTURE-SPINE.md#Consistency-Conventions]
Market data flows ONLY via this ingestion into `market_daily`, and the Precedent Engine reads only `market_daily`. Never `yfinance` in production. Store derived analytics (OHLC/adjusted close needed for drawdown + forward-return math), not a raw redistribution of the vendor feed.

### Builds on Epic 1/2 patterns — reuse
- `db/models.py` `Base` + `Numeric`/`DateTime(timezone=True)` conventions (from `PortfolioCache`), `create_db_and_tables`, `get_async_session`, `db/connection.get_connection` (for sync test inserts). [Source: db/models.py, db/session.py, db/connection.py]
- Adapter/factory/config pattern from `brokers/` (`get_broker` + `BROKER_ADAPTER` + lazy import + `*NotConfiguredError`). Mirror it for `marketdata/`. [Source: brokers/factory.py, brokers/schwab_adapter/adapter.py, api/config.py]
- Structured logging, never log secrets. [Source: api/logging_config.py]

### Scope guardrails
- **In scope:** the market-data port + fake/tiingo adapters + factory, the `market_daily` global model, the idempotent SYSTEM-scope ingestion job with source-hiccup tolerance, config keys, and tests.
- **Out of scope:** drawdown matching / evidence records (Story 3.2), the precedent VIEWS (3.3–3.5), a production scheduler/cron (deployment concern — the job is runnable + idempotent, that's enough), any real Tiingo network wiring (credential-gated stub only), the Stooq backup adapter (note it, don't build it).

### Testing standards
Real Postgres (docker), matching Epic 1/2 style: create the `market_daily` table (autouse fixture), assert idempotent re-runs by row count, and simulate a source hiccup with a raising fake to prove the run continues. Assert `Decimal` money round-trips exactly. No network, no creds.

### Project Structure Notes
- New: `marketdata/port.py`, `marketdata/fake_adapter.py`, `marketdata/tiingo_adapter.py`, `marketdata/factory.py`, `marketdata/ingest.py`, `tests/test_market_ingest.py`. `MarketDaily` in `db/models.py`. Config keys in `api/config.py` + `.env.example`.
- `marketdata/__init__.py` already exists (placeholder). Aligns with the domain-named package layout; adapters suffixed `Adapter`, port suffixed `Port`.

### References
- [Source: _bmad-output/planning-artifacts/epics.md#Story-3.1, #FR13, #FR15]
- [Source: ARCHITECTURE-SPINE.md#AD-8 (ports/adapters), #AD-10 (SYSTEM vs user scope), #AD-3, #Consistency-Conventions (money/data-sourcing), Stack (Tiingo EOD; Stooq backup), Source tree (marketdata/)]
- [Source: implementation-artifacts/2-1-… (SchwabAdapter credential-gated pattern), 2-3-… (Numeric money + real-DB tests)]

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (bmad-dev-auto, general-purpose implementation subagent)

### Debug Log References

- Full backend suite: `.venv/bin/python -m pytest -q` (from `ballast/backend`, docker Postgres via `docker compose up -d db`) → 114 passed, 1 pre-existing warning (+9 new tests over the 105 baseline).

### Completion Notes List

Ultimate context engine analysis completed — comprehensive developer guide created.

- All 6 tasks implemented; all 3 ACs satisfied and independently re-verified (114 passed).
- AC1: `ingest_market_daily` writes the global `market_daily` table (no `owner_id`, no `ScopedRepository`, non-user SYSTEM context); stores derived OHLC+adj_close, not raw vendor payloads.
- AC2: Postgres `ON CONFLICT (symbol, day) DO UPDATE` upsert — re-runs and overlapping ranges converge to one row per (symbol, day); a changed bar updates in place. Verified end-to-end via CLI (93 rows → re-run stayed 93).
- AC3: per-symbol try/except with per-symbol commit; a failing symbol logs a structured warning and is reported in `IngestResult.symbols_failed` while other symbols still ingest (run not aborted).
- Deviation: ingestion commits per successful symbol (not one end-of-run commit) so AC3 durability holds — a later symbol's rollback would otherwise discard already-ingested rows. Idempotency (AC2) makes per-symbol commit safe on retry.
- `TiingoAdapter.fetch_eod` uses `client.get_ticker_price(...)`; credential-gated and never exercised in tests (real network wiring out of scope per spec).

### File List

Created:
- `ballast/backend/marketdata/port.py`
- `ballast/backend/marketdata/fake_adapter.py`
- `ballast/backend/marketdata/tiingo_adapter.py`
- `ballast/backend/marketdata/factory.py`
- `ballast/backend/marketdata/ingest.py`
- `ballast/backend/tests/test_market_ingest.py`

Modified:
- `ballast/backend/db/models.py` (added `MarketDaily`)
- `ballast/backend/api/config.py` (added `MARKETDATA_ADAPTER`, `TIINGO_API_KEY`)
- `ballast/backend/.env.example` (documented new keys)

## Change Log

- 2026-07-26: Story 3.1 implemented (bmad-dev-auto). Market-data port + fake/Tiingo adapters + factory, global `market_daily` model, idempotent SYSTEM-scope ingestion job with source-hiccup tolerance, config keys, and 9 real-DB tests. Full suite green (114 passed). Status → in-review.

## Review Triage Log

### 2026-07-26 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 17 (high 0, medium 2, low 15)
- defer: 1 (low 1)
- reject: 5
- addressed_findings:
  - `[medium]` `[patch]` Fake bar generator could emit `adj_close < low` (impossible bar; 25/2193 bars) — reworked `_bar_for` so `low <= open,close,adj_close <= high` holds for every seed while staying fully deterministic; recomputed the pinned exact values and added a full-series invariant assertion. (verified: 0 violations across ~6yr × 4 symbols)
  - `[medium]` `[patch]` `ingest_market_daily` docstring said "Commits once at the end", contradicting the load-bearing per-symbol commit (AC3) — corrected the docstring.
  - `[low]` `[patch]` Fake `open` always equalled `low` (no realistic intraday shape) — folded into the generator rework (open now a distinct bounded offset).
  - `[low]` `[patch]` `symbols_failed` stored only the exception type, discarding the message — now stores `"{type}: {message}"` and logs it (AC3 forensics).
  - `[low]` `[patch]` `session.rollback()` in the per-symbol handler could itself raise on a dead connection and abort the whole run — guarded so the loop always continues (AC3).
  - `[low]` `[patch]` `provider=None` attribute on an adapter would cause a NOT NULL insert failure — `getattr(...) or __class__.__name__` guard.
  - `[low]` `[patch]` A repeated symbol in the input list double-counted/re-ingested — dedupe preserving order.
  - `[low]` `[patch]` CLI accepted invalid/reversed dates and empty symbol lists silently — added `parser.error` validation (empty symbols, bad date, start>end) and a one-line stdout summary printed regardless of log level.
  - `[low]` `[patch]` `MarketDataPort.fetch_eod` contract was silent on reversed ranges — documented `end < start` → empty list.
  - `[low]` `[patch]` Tiingo lazy import raised a bare `ModuleNotFoundError` instead of the adapter's fail-loud error — wrapped to raise `TiingoNotConfiguredError`; volume now parsed via `int(Decimal(str(...)))`.
  - `[low]` `[patch]` App-startup table-creation path was not asserted — test now asserts `"market_daily" in Base.metadata.tables`.
  - `[low]` `[patch]` DB-writing tests used the real universe symbols (VTI/VXUS/BND) on the shared GLOBAL table (flaky vs a real ingest run) — switched to test-only prefixed symbols; pure fake-adapter determinism tests keep VTI.
  - `[low]` `[patch]` `_row(...)` unpacked without a None guard — added clear assertions before unpacking.
- deferred:
  - `[low]` TiingoAdapter real-vendor response hardening (missing/null fields, non-list payloads, tz date parsing) — appended to `deferred-work.md`; out of scope for the credential-gated 3.1 stub.
- rejected (5): per-run `ingested_at` (defensible design); duplicate bars within one fetch (improbable — fake and Tiingo return one bar/day); async `fetch_eod` in a real adapter (contradicts the sync port contract); factory tiingo+missing-key (already handled by `TiingoNotConfiguredError`); test schema drop/reconcile (matches existing suite conventions).

## Auto Run Result

Status: done
Follow-up review recommended: true

### Summary of implemented change
Story 3.1 adds a fake-first market-data ingestion path feeding a GLOBAL `market_daily` store — the foundation the Precedent Engine (3.2–3.4) computes over. Market data lives behind a `MarketDataPort` (AD-8) with a deterministic `FakeMarketDataAdapter` (default, zero creds/network) and a credential-gated `TiingoAdapter`, chosen by config via a factory. The `market_daily` table is global reference data (no `owner_id`, not routed through the per-user `ScopedRepository` — the non-user SYSTEM context of AD-10). The idempotent `ingest_market_daily` job UPSERTs on `(symbol, day)` and tolerates per-symbol source hiccups by committing per symbol and continuing the run.

### Files changed
- `ballast/backend/marketdata/port.py` (new) — `DailyBar` frozen dataclass + `MarketDataPort` ABC (`fetch_eod`), the sole vendor boundary.
- `ballast/backend/marketdata/fake_adapter.py` (new) — deterministic, offline `FakeMarketDataAdapter`; OHLC invariant `low ≤ open,close,adj_close ≤ high` guaranteed for every seed.
- `ballast/backend/marketdata/tiingo_adapter.py` (new) — credential-gated `TiingoAdapter` + `TiingoNotConfiguredError`, lazy SDK import, fail-loud.
- `ballast/backend/marketdata/factory.py` (new) — `get_market_data()` selects fake (default) vs tiingo from `MARKETDATA_ADAPTER`.
- `ballast/backend/marketdata/ingest.py` (new) — `ingest_market_daily(...) -> IngestResult` (idempotent UPSERT, per-symbol commit + hiccup tolerance) and a thin CLI entrypoint.
- `ballast/backend/db/models.py` — added global `MarketDaily` model (UNIQUE(symbol, day), Numeric OHLC, BigInteger volume, tz-aware `ingested_at`; no `owner_id`).
- `ballast/backend/api/config.py` — `MARKETDATA_ADAPTER` (default `fake`), `TIINGO_API_KEY`.
- `ballast/backend/.env.example` — documented the two new keys.
- `ballast/backend/tests/test_market_ingest.py` (new) — real-DB tests: deterministic fake bars, one row per symbol/day, idempotent re-run, source-hiccup tolerance, global no-`owner_id` shape, factory default; test-only symbols on the shared global table.

### Review findings breakdown
- Patches applied: 17 (2 medium, 15 low) — see Review Triage Log. Highlights: fake generator reworked to guarantee the OHLC invariant (was emitting `adj_close < low`), corrected an AC3-critical docstring, hardened the ingest job (rollback guard, provider guard, symbol dedupe, richer failure records), CLI validation + summary, and de-flaked the real-DB tests off the shared global universe symbols.
- Deferred: 1 — TiingoAdapter real-vendor response hardening (`deferred-work.md`).
- Rejected: 5 (noise / out-of-contract / already-handled).

### Verification performed
- `docker compose up -d db` + `.venv/bin/python -m pytest -q` (from `ballast/backend`) → **114 passed, 1 pre-existing warning** (+9 new tests over the 105 baseline). Re-run independently after patches: still 114 passed.
- Independent check: fake OHLC invariant `low ≤ open,close,adj_close ≤ high` holds with **0 violations** across ~6 years × 4 symbols.
- All 3 acceptance criteria satisfied (AC1 global SYSTEM-scope ingestion; AC2 idempotent UPSERT; AC3 source-hiccup tolerance) — each covered by a dedicated real-DB test.

### Residual risks
- `TiingoAdapter` is a credential-gated stub whose real-vendor response parsing is unexercised (deferred). No production impact while `MARKETDATA_ADAPTER=fake`.
- Real daily scheduling/cron is intentionally out of scope — the job is runnable + idempotent; a scheduler would invoke `python -m marketdata.ingest`.
- Follow-up review recommended: the review pass reworked the load-bearing deterministic fixture (which stories 3.2–3.4 will pin statistics over) and AC3 durability handlers — breadth and consequence warrant an independent follow-up pass.
