---
baseline_commit: 2a1a25e35ca7dd0d208a672a828c00ad875c548e
baseline_revision: 35f8d01299fd1bbe22f0c6d649057aa9b8990d5c
status: in-progress
---

# Story 3.1: Market-data ingestion → `market_daily`

Status: in-progress

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

- [ ] **Task 1: Market-data port + normalized bar** (AC: 1)
  - [ ] Add `marketdata/port.py`: a `DailyBar` frozen dataclass (`symbol`, `day: date`, `open/high/low/close/adj_close: Decimal`, `volume: int`) and a `MarketDataPort` ABC with `fetch_eod(symbol, start, end) -> list[DailyBar]`. Money is `Decimal`. Document it as the sole boundary to any market-data vendor (AD-8) — the Precedent Engine (Epic 3) depends only on `market_daily`, never on a vendor SDK.
- [ ] **Task 2: Fake + Tiingo adapters + factory** (AC: 1, 3)
  - [ ] `marketdata/fake_adapter.py`: `FakeMarketDataAdapter` — deterministic synthetic daily bars for a small symbol set (a generated, reproducible series spanning many days; NO wall-clock, NO network) so ingestion + later precedent are fully testable with zero creds. Determinism is load-bearing (precedent tests will assert exact stats later).
  - [ ] `marketdata/tiingo_adapter.py`: `TiingoAdapter` — credential-gated, lazy-imports the Tiingo client, raises a clear `TiingoNotConfiguredError` without `TIINGO_API_KEY` (mirrors `SchwabAdapter`'s fail-loud posture). Do NOT wire real network here. Optionally note the Stooq backup as a future adapter.
  - [ ] `marketdata/factory.py`: `get_market_data()` selects fake vs tiingo from config (`MARKETDATA_ADAPTER`, default `fake`); lazy-imports the Tiingo adapter so the default path never loads its SDK. Add the config keys to `api/config.py` + `.env.example`.
- [ ] **Task 3: `market_daily` model (global reference)** (AC: 1, 2)
  - [ ] Add `MarketDaily(Base)` to `db/models.py` — `__tablename__ = "market_daily"`, a UNIQUE constraint on (`symbol`, `day`), `Numeric` OHLC + adj_close, `volume` BigInteger, plus `source` and `ingested_at` (tz-aware UTC). It is deliberately NOT an `OwnedEntityMixin` (no `owner_id`) — it is global, not per-user; document why (it is not routed through `ScopedRepository`, which is for owned per-user entities).
- [ ] **Task 4: The SYSTEM-scope ingestion job** (AC: 1, 2, 3)
  - [ ] Add `marketdata/ingest.py`: `ingest_market_daily(session, source, symbols, start, end) -> IngestResult`. For each symbol: fetch bars via the port, UPSERT into `market_daily` on (`symbol`, `day`) (Postgres `ON CONFLICT` or select-then-update) so re-runs never duplicate (AC2). Wrap each symbol in try/except so one symbol's failure logs a structured warning and the run continues (AC3); return a small result summarizing rows written / symbols failed.
  - [ ] Store DERIVED analytics fields (OHLC/adjusted close), not a redistribution of raw vendor payloads (Data-sourcing rule). Runs as the non-user SYSTEM context — market_daily is global; do NOT attach a user scope.
  - [ ] Scheduling: build the job as a runnable, idempotent function (a thin CLI/entrypoint is fine). A real daily scheduler/cron wiring is a deployment concern — out of scope here; note how it would be invoked.
- [ ] **Task 5: Tests (real DB)** (AC: 1, 2, 3)
  - [ ] Fake adapter returns a deterministic bar series (assert exact values; `Decimal` money).
  - [ ] Ingest writes `market_daily` rows (one per symbol/day); values round-trip as `Decimal`.
  - [ ] **Idempotent:** running ingest twice over the same range yields the SAME row count (upsert, no dupes); a changed bar updates in place.
  - [ ] **Source-hiccup tolerance:** with an adapter that raises for one symbol, the other symbols are still ingested and the failure is reported (run not aborted).
  - [ ] `market_daily` is global (no `owner_id` column); a `MarketDataPort` selection via the factory returns the fake by default. No regressions.
- [ ] **Task 6: Verify** — full backend suite; run the fake ingestion end-to-end (job → `market_daily` populated → re-run is a no-op count-wise → a symbol failure doesn't abort).

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

### Debug Log References

### Completion Notes List

Ultimate context engine analysis completed — comprehensive developer guide created.

### File List

## Change Log
