---
baseline_commit: 2a1a25e35ca7dd0d208a672a828c00ad875c548e
---

# Story 2.3: Import & cache portfolio (single-writer projection)

Status: done

## Story

As a user,
I want my current holdings pulled in on connect,
so that Ballast reflects my real account.

## Acceptance Criteria

1. **Holdings imported on connect (broker authoritative).** Given a linked account, when the portfolio is fetched from the Broker Port, then the user's holdings, balances, and cash are imported and written to `portfolio_cache` as a derived read-model — the broker is the authoritative source. [Source: epics.md#Story-2.3 (FR4), ARCHITECTURE-SPINE.md#AD-14]
2. **Single writer — nothing else writes the cache.** `portfolio_cache` has exactly ONE writer (the portfolio projection/reconciler). No other module or endpoint writes it; every other consumer reads it read-only. [Source: ARCHITECTURE-SPINE.md#AD-14, #Consistency-Conventions (State mutation), review-adversarial.md#D4]
3. **Reconcile-wins on conflict (keyed on broker `as_of`).** On any conflict, a fresh broker reconciliation wins over existing local state: the projection replaces the cached holdings atomically, keyed on the broker's `as_of` timestamp — a newer reconcile supersedes older cached rows, and a stale reconcile (older `as_of` than what is cached) never clobbers newer truth. [Source: ARCHITECTURE-SPINE.md#AD-14, review-adversarial.md#Hole→AD (last-reconcile-wins keyed on broker as_of)]

**Cross-cutting:** money is `Decimal` or integer minor units, NEVER binary float; all timestamps ISO-8601 UTC; per-user isolation via the fail-closed `ScopedRepository` (a user's cache only ever reflects their own account); never log tokens or account secrets. [Source: ARCHITECTURE-SPINE.md#Consistency-Conventions, #AD-10]

## Tasks / Subtasks

- [x] **Task 1: Extend the Broker Port with a read-only holdings fetch** (AC: 1)
  - [x] Added `Holding` + `PortfolioSnapshot` frozen dataclasses to `brokers/port.py` (Decimal money, tz-aware UTC `as_of`).
  - [x] Added `fetch_portfolio(self) -> PortfolioSnapshot` abstractmethod on `BrokerPort`, documented as the broker-authoritative read (not execution).
  - [x] `FakeBrokerAdapter.fetch_portfolio` returns a deterministic snapshot (VTI/VXUS/BND + cash) with a fixed `FAKE_AS_OF_BASE` and an injectable `as_of_offset` so reconcile-wins tests drive older/newer snapshots without wall-clock.
  - [x] `SchwabAdapter.fetch_portfolio` is credential-gated — raises `SchwabNotConfiguredError` (fails loud, never silently reconciles the cache to empty). Mirrors the 2.1 fake-first posture.
- [x] **Task 2: `portfolio_cache` model (owned, read-model)** (AC: 1, 2)
  - [x] Added `PortfolioCache(OwnedEntityMixin, Base)` — `Numeric` money columns (never Float), tz-aware `as_of`, `cash`/`as_of` denormalized per row. Choice documented in the model docstring.
  - [x] `owner_id` from `OwnedEntityMixin` (AD-10), following the `BrokerageToken` precedent.
- [x] **Task 3: The single-writer projection/reconciler** (AC: 1, 2, 3)
  - [x] `brokers/portfolio.py` is the SOLE writer. `reconcile_portfolio(scope, session, broker, *, snapshot=None)` reads the authoritative snapshot, compares `as_of` to cache, atomically delete-then-adds when strictly newer (or cache empty), else leaves it untouched.
  - [x] Single-writer discipline stated in module + function docstrings, referencing AD-14 and reconcile-wins-keyed-on-`as_of`.
  - [x] All access via fail-closed `ScopedRepository` under the user `Scope` (AD-10).
- [x] **Task 4: Import on connect + a read endpoint** (AC: 1)
  - [x] `/api/brokerage/callback` reconciles after storing tokens; wrapped in try/except so an import failure never breaks the link (token commit precedes reconcile — link survives). Structured log, no tokens.
  - [x] `GET /api/portfolio` (read-only via scoped repo) + `POST /api/portfolio/refresh` (force reconcile) return plain JSON (holdings, cash, `as_of`). Raw cache read only — dashboard is 2.4.
  - [x] Router registered in `api/app.py`.
- [x] **Task 5: Tests (real DB)** (AC: 1, 2, 3)
  - [x] Fake snapshot deterministic; reconcile writes matching rows; exact `Decimal` values asserted (no float drift).
  - [x] Reconcile-wins asserted BOTH directions (newer replaces; older ignored; equal is a no-op).
  - [x] Per-user isolation (A never sees B's cache).
  - [x] Single-writer: GET endpoint read-only + idempotent (repeated GETs identical).
  - [x] Import-on-connect populates the cache; a fetch failure leaves the account linked (no 500).
  - [x] No regressions (2.1 + 2.2 suites green).
- [x] **Task 6: Verify** — full backend suite (83 passed) + compile/import check across all modules incl. the schwab path; app builds with `/api/portfolio` + `/api/portfolio/refresh`; reconcile-wins verified both directions. (No backend linter is configured in `pyproject.toml`; the suite is the bar.)

## Dev Notes

### Fake-first continues (no creds, no network)
Consistent with 2.1/2.2: the FakeBrokerAdapter makes the entire import path runnable and testable with ZERO credentials and ZERO network. Do NOT wire real Schwab position calls — the `SchwabAdapter` gets a config-gated stub (same posture as 2.1's link methods). Everything in scope here is testable against the fake + the real Postgres.

### This is AD-14, the whole point of the story [Source: ARCHITECTURE-SPINE.md#AD-14, review-adversarial.md#D4]
- **AD-14:** `PORTFOLIO_CACHE` is a **read model with one writer**; the broker is authoritative, and on any conflict a **fresh broker reconciliation wins** over optimistic local writes.
- The adversarial review (D4) flagged the exact race this story prevents: a post-trade write racing the periodic refresh corrupting portfolio truth. The fix, which this story implements: name a **single owner** (the portfolio projection/reconciler), declare the broker the authoritative source and the cache a **derived read-model**, and specify **last-reconcile-wins keyed on broker `as_of`**. Post-trade optimistic writes (Epic 4) are always superseded by the next broker reconcile. **Forbid direct cache writes outside the owner.**
- Post-trade writes and the execution path are **Epic 4** — do NOT build them here. Here we build the projection, the cache, and the reconcile discipline so Epic 4 plugs in. There are no trades yet, so the only writer in this story IS the reconciler.

### Builds on 2.1 + 2.2 + Epic 1 (done) — reuse, do not reinvent
- **`BrokerPort` / `FakeBrokerAdapter` / `get_broker` factory** (2.1): extend the port with a read-only `fetch_portfolio`; the fake implements it deterministically; the factory already selects the adapter. [Source: brokers/port.py, brokers/fake_adapter.py, brokers/factory.py]
- **`ScopedRepository`** (1.4): the ONLY sanctioned per-user persistence funnel — fail-closed, `list`/`get`/`add`. It has NO `delete`/`update`; the 2.1 callback does an atomic replace via `session.delete(row)` on existing rows then `repo.add(...)` — mirror that pattern for the reconcile replace. [Source: db/repository.py, api/brokerage.py#callback]
- **`OwnedEntityMixin`** (1.4): apply to `PortfolioCache` to get `owner_id` + AD-10 isolation for free — follow the `BrokerageToken` precedent. [Source: db/models.py]
- **`get_scope`** (1.4) for the endpoint's user scope; **`get_async_session`** for the session; the app error envelope + structured logging (never log tokens/secrets). [Source: api/deps.py, api/app.py]
- **2.2 note:** the degraded-mode gate (`require_live_broker_session`) gates EXECUTION only; portfolio import/read is a read/coach-class surface and continues in degraded mode — do NOT gate `GET /api/portfolio` behind a live session. [Source: api/deps.py, ARCHITECTURE-SPINE.md#AD-11]

### Money & time discipline [Source: ARCHITECTURE-SPINE.md#Consistency-Conventions]
- **Money:** SQLAlchemy `Numeric`/Python `Decimal` or integer minor units — **NEVER** `Float`/binary float. Assert exact `Decimal` values in tests.
- **Time:** all `as_of`/timestamps are tz-aware UTC (`DateTime(timezone=True)`), ISO-8601 on the wire — matches `BrokerageToken.expires_at`.
- **IDs:** UUID primary keys with `default=uuid.uuid4` (as `BrokerageToken`).

### Scope guardrails
- **In scope:** the read-only Broker Port holdings fetch (+ deterministic fake), the `portfolio_cache` model, the single-writer projection/reconciler with reconcile-wins-keyed-on-`as_of`, import-on-connect, a raw `GET /api/portfolio` read endpoint, and tests.
- **Out of scope:** the plain-English portfolio dashboard UI (2.4 — this story ships raw JSON only), index-core mapping (2.5), any order placement / post-trade write / execution path (Epic 4), a periodic background refresh scheduler (import-on-connect + on-demand refresh is enough for v1 here; a scheduler is a later concern). Do NOT add order/execution methods to the Broker Port.
- **Security/isolation:** all cache access through the fail-closed `ScopedRepository`; a user's cache only ever reflects their own account; never log tokens or account numbers/secrets.

### Testing standards
Real Postgres (docker `docker compose up -d db`), no mocks for the DB — matches Epic 1 / 2.1 / 2.2 style (`tests/test_brokerage.py`, `tests/test_session_status.py` are the templates: unique users per test, self-cleanup, scoped-repo inserts). The reconcile-wins invariant (AC3) and per-user isolation are acceptance criteria — assert them explicitly, both directions. Assert money stays `Decimal` with no float drift.

### Project Structure Notes
- New: `brokers/portfolio.py` (or `portfolio/` package) — the single-writer projection; `api/portfolio.py` — the read/refresh router; `PortfolioCache` in `db/models.py`; `tests/test_portfolio.py`.
- Touched (UPDATE): `brokers/port.py` (add `Holding`/`PortfolioSnapshot` + `fetch_portfolio`), `brokers/fake_adapter.py` + `brokers/schwab_adapter/` (implement/stub `fetch_portfolio`), `api/app.py` (register router), `api/brokerage.py` (reconcile-on-callback hook). Keep the port import-light so the default fake path never loads schwab-py (existing lazy-import discipline).
- Aligns with the existing domain-named package layout (`brokers`, `api`, `db`); interfaces `Port`, adapters `Adapter`.

### References
- [Source: _bmad-output/planning-artifacts/epics.md#Story-2.3]
- [Source: ARCHITECTURE-SPINE.md#AD-14 (single-writer projection, broker-authoritative, reconcile-wins), #AD-8 (ports), #AD-10 (fail-closed scoped repo), #AD-11 (degraded mode — reads continue), #Consistency-Conventions (money/time/state-mutation)]
- [Source: architecture/.../review-adversarial.md#D4 + Hole→AD (last-reconcile-wins keyed on broker as_of; forbid writes outside the owner)]
- [Source: implementation-artifacts/2-1-broker-port-schwab-oauth-link.md (BrokerPort, FakeBrokerAdapter, factory, atomic-replace callback pattern)]
- [Source: implementation-artifacts/2-2-session-status-graceful-re-auth-degraded-mode.md (degraded mode — reads/coach continue; execution-only gating)]

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (Claude Code, autonomous story loop)

### Debug Log References

- Backend real-DB tests require docker Postgres (`docker compose up -d db`). Full suite: 83 passed (73 prior + 10 new).
- No backend linter configured (`pyproject.toml` has pytest only); ran `py_compile` across all touched modules + an app-build/import check (the schwab path isn't exercised by tests) — all clean; `/api/portfolio` + `/api/portfolio/refresh` present.
- Fresh-context adversarial review: no high-confidence defects; all 3 ACs met and tested both directions. Applied the one cheap hardening it flagged (deterministic representative-row selection via max `as_of`).

### Completion Notes List

- **AD-14 single-writer projection.** `brokers/portfolio.py` is the SOLE writer of `portfolio_cache`; every reader (`GET /api/portfolio`, `get_portfolio`) is read-only through the fail-closed `ScopedRepository` (AD-10). Verified single-writer by grep + an idempotent-GET test.
- **Reconcile-wins keyed on broker `as_of`.** `reconcile_portfolio` replaces the cache atomically (delete-then-add, one commit) only when the incoming snapshot is *strictly newer*; equal/older snapshots are skipped so a stale/out-of-order reconcile never clobbers newer truth. Both directions + the equal-as_of no-op are tested. tz-naive/aware coerced defensively.
- **Broker Port extended with a READ.** `fetch_portfolio -> PortfolioSnapshot` (Holding/PortfolioSnapshot dataclasses, Decimal money). This is not execution — the "no order/execution methods" note (place_order/OrderOutcome, Epic 4) is respected. Fake returns a deterministic snapshot with an injectable `as_of_offset`; Schwab is credential-gated (fails loud, never reconciles-to-empty).
- **Import-on-connect is resilient.** The 2.1 callback commits tokens FIRST, then reconciles inside try/except — a fetch failure logs `error_type` only and leaves the account linked (`state=live`), cache empty for later retry (AD-11 degraded mode). Tested with a fetch-failing adapter override.
- **Money/time discipline.** All money is `Numeric`/`Decimal` (never Float); exact values asserted with no drift. `as_of` is tz-aware UTC.
- **Deferred (flag for the real-Schwab balances story):** a cash-only / zero-holdings snapshot writes zero rows, so cash is dropped and reconcile-wins isn't enforced for an all-cash account (as_of/cash are denormalized onto holding rows). Documented in `db/models.py`; harmless for the fake (always has holdings) but must be closed when the real Schwab positions/balances mapping lands — likely via a dedicated balances row.

### File List

- ballast/backend/brokers/port.py (Holding/PortfolioSnapshot + fetch_portfolio abstractmethod)
- ballast/backend/brokers/fake_adapter.py (deterministic fetch_portfolio + injectable as_of)
- ballast/backend/brokers/schwab_adapter/adapter.py (credential-gated fetch_portfolio stub)
- ballast/backend/brokers/portfolio.py (new — the single-writer projection/reconciler)
- ballast/backend/api/portfolio.py (new — GET /api/portfolio + POST /api/portfolio/refresh)
- ballast/backend/api/brokerage.py (import-on-connect reconcile hook)
- ballast/backend/api/app.py (register portfolio router)
- ballast/backend/db/models.py (PortfolioCache model)
- ballast/backend/tests/test_portfolio.py (new)

## Change Log

- 2026-07-26: Implemented the portfolio import + AD-14 single-writer projection (reconcile-wins keyed on `as_of`), import-on-connect, and the raw read/refresh endpoints. Backend suite 83 passed; adversarial review clean. Status → done.
