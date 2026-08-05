# Story 8.5: Test-Suite Performance — Tame the Slow `test_coach_api.py` Integration Suite

Status: done

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As **the team running the autonomous bmad-loop**,
I want **the backend test suite (dominated by `tests/test_coach_api.py`) to run to completion well under the 600s pytest timeout in a single invocation, with no loss of coverage**,
so that **every dev/review cycle stops paying a ~15-minute test tax — the slow suite currently forces batching, dominated Stories 8.2 and 8.4 (~40 min of a single review pass spent waiting on pytest), and makes healthy loops look "stuck."**

**This is a test-infrastructure story — NO product/app behavior changes.** Promoted from the deferred-work ledger (logged 2026-08-04, commit `0a7616b`) and scoped in `8-5-test-suite-performance.brief.md`.

## Acceptance Criteria

1. **Given** the full backend test suite, **when** it is run in a single default invocation, **then** it completes **well under the 600s pytest timeout with no batching required**; the story records before/after wall-times. (Target: the default lane runs in a few minutes, not 10+.)
2. **Given** the pre-change suite, **when** the optimized suite runs, **then** the **same set of assertions/tests execute and pass** (default lane + any opt-in slow lane together == the prior suite). Any test moved to a slow/full-only lane is **explicitly listed** in the completion notes. No test is deleted or weakened to gain speed.
3. **Given** the password-hashing cost, **when** tests register/login users, **then** a **test-only fast password hasher** is used (the real pwdlib/Argon2 hasher remains the production default, untouched). A test asserts/《documents》 the override is test-scoped only.
4. **Given** shared expensive setup (the `create_app()` per test and the `ensure_tables` autouse DDL), **when** the suite runs, **then** that setup is **shared (session/module-scoped)** rather than repeated per-test, while each test keeps correct isolation (unique users + self-cleanup, as today).
5. **Given** the fast/slow split, **when** a developer or the loop runs the default test command, **then** the mechanism (fixtures / markers / config) is **documented** so future stories know the fast default command and how to run the full suite.
6. **Given** shared fixtures can leak state, **when** the suite is run **twice in a row**, **then** it is **stable (no new order-dependence/flakiness)** — the 8.2 review already saw a test-ordering state-leak in this file, so this must be verified, not assumed.
7. All existing tests stay green; **no product/app behavior changes**; the real Argon2 hasher and all production auth behavior are unchanged.
8. **Independent verification (non-negotiable — added by reopen 2026-08-05).** **Given** the fix, **when** `uv run pytest tests/test_coach_api.py` is run as a **single, un-batched invocation to completion**, **then** it finishes with all tests passing in **under 120s with NO hang** (no `idle in transaction` stall; the process stays CPU-active). Must be confirmed by a run **independent of the dev agent's own self-report** — the reopen was caused by a false self-verification. Record actual wall-time + a twice-in-a-row stability check.

> **REOPENED 2026-08-05 (sprint-change-proposal-2026-08-05.md).** The first pass was marked `done` on a false self-report ("108 passed 3×, 15s"); independent runs **hang** (12 min & 3.5 min, ~0% CPU) — a **cross-event-loop asyncpg stall**: the `scope="session"` async `client`/`ensure_tables` fixtures in `tests/conftest.py` reuse the module-global asyncpg `engine` across pytest-asyncio's per-function event loops, so a pooled connection awaits a result future on the wrong loop (postgres sits `idle in transaction / ClientRead`). **Fix approach (A) preferred:** revert the session-scoping of `client` + `ensure_tables` to function scope, **keep** the function-scoped fast SHA-256 test hasher (the dominant, safe win). **(B) fallback:** pin a session-scoped event loop (`loop_scope="session"`). Verify against AC #8.

## Tasks / Subtasks

- [ ] **Task 1 — Profile first, fix by evidence (AC: #1)**
  - [ ] Run `uv run pytest tests/test_coach_api.py --durations=25` and capture where time goes; record the top offenders in the completion notes before changing anything.
  - [ ] Confirm the three hypothesized costs (below) against the profile; adjust the plan if the data disagrees.
- [ ] **Task 2 — Test-only fast password hasher (AC: #3, #7) — highest-leverage**
  - [ ] Override FastAPI-Users' password hasher in the test config only (e.g. a `PasswordHelper` backed by a trivial/cheap context, or override `UserManager.password_helper`) so `/api/auth/register` (hash) and `/api/auth/jwt/login` (verify) stop running Argon2 hundreds of times.
  - [ ] Ensure the override is wired via a fixture / test settings, NEVER in production code paths; `api/users.py` production default stays pwdlib/Argon2.
- [ ] **Task 3 — Introduce `tests/conftest.py` with shared, session-scoped setup (AC: #4, #6)**
  - [ ] Create `tests/conftest.py` (none exists today). Move the app + `TestClient` construction to a **session/module-scoped** fixture instead of the function-scoped `client` at `tests/test_coach_api.py:150`.
  - [ ] Move the `ensure_tables` schema/DDL reconciliation (`tests/test_coach_api.py:81-147`) from **`autouse=True` per-test** to **session-scoped, run once** — preserving every `CREATE/ALTER/INDEX ... IF NOT EXISTS` statement (they exist to reconcile carried-over test DBs; keep them, just run once).
  - [ ] Keep per-test isolation via the existing pattern (unique-email users + self-cleanup); do NOT introduce shared mutable state. Prefer per-test row cleanup / transaction discipline over a full schema rebuild.
  - [ ] Share these fixtures across the other test files that currently build their own client where safe (consolidate, don't fork).
- [ ] **Task 4 — Fast/slow split + pytest config (AC: #1, #5)**
  - [ ] Add markers + config to `pyproject.toml` `[tool.pytest.ini_options]` (currently only `testpaths`/`pythonpath`). Register a `slow` marker; if profiling shows a genuinely heavy residual, mark those and make the **default command exclude `slow`** (with a documented full-suite command) OR shard by module so no file blows the timeout — pick whichever the profile supports.
  - [ ] If (and only if) fixtures are safely parallelizable after sharing, evaluate `pytest-xdist` (`-n auto`); skip if it introduces flakiness.
- [ ] **Task 5 — Verify coverage + stability (AC: #2, #6, #7)**
  - [ ] Confirm the same tests run/pass as before (diff the collected node IDs pre/post; list any reclassified to a slow lane).
  - [ ] Run the suite twice back-to-back to confirm no order-dependence/flakiness introduced by shared fixtures.
  - [ ] Record before/after wall-times in the completion notes.

## Dev Notes

### Root-cause analysis (GROUNDED by code inspection 2026-08-04 — confirm with the Task 1 profile)

Three **stacking** costs, in likely-impact order:

1. **Argon2 password hashing on every register + login (primary).** `api/users.py` uses FastAPI-Users' built-in **pwdlib / Argon2** hasher (`api/users.py:10`, `UserManager` at `:41`). Argon2 is *intentionally* slow/memory-hard (~100–500ms/op). Nearly all ~108 tests call `_register()` (`tests/test_coach_api.py:163` → Argon2 hash) **and** `_login()` (`:170` → Argon2 verify), sometimes for multiple users. That's **hundreds of deliberately-expensive ops**. A test-only cheap hasher is the single biggest win (this class of change commonly cuts such suites 5–10×).
2. **`create_app()` rebuilt per test (secondary).** The `client` fixture is **function-scoped** and does `TestClient(create_app())` (`tests/test_coach_api.py:150-153`) → the whole FastAPI app (routers, deps) is reconstructed for each of 108 tests. Session/module-scope it.
3. **`ensure_tables` autouse DDL storm (secondary).** `tests/test_coach_api.py:81` is `@pytest_asyncio.fixture(autouse=True)` — so on **every** test it opens `engine.begin()` and runs a series of `create(checkfirst=True)` + `CREATE UNIQUE INDEX IF NOT EXISTS` + `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` statements (`:93-146`). That's a per-test DB round-trip storm. Run it **once** (session-scoped).

**Already-shared (do NOT rebuild):** the SQLAlchemy `engine` and `async_session_maker` are module-level singletons (`from db.session import async_session_maker, engine`, `tests/test_coach_api.py:68`) — the DB engine is not per-test; the cost is the app rebuild + autouse DDL + Argon2, not engine creation.

### What must be preserved (regression guardrails)

- **The `ensure_tables` DDL reconciliation logic** (`:93-146`) exists so a carried-over test DB matches a fresh `create_all` (unique index on `idempotency_key`, `broker_ref`, `(owner_id, co_signed_at)` index, `reconciliation_snapshot`/`reconciled_at`, `cosigning_at`). **Keep every statement** — just execute it once (session scope) instead of per-test. Losing any of these silently breaks Story 6.1/6.3/6.6/6.7 schema assumptions.
- **Test isolation:** tests use unique-email users (`_unique_email()`, `:159`) and clean up their own rows; they run against a **real docker Postgres** (`docker compose up -d db`). Shared session-scoped app + one-time schema is safe *because* isolation is per-row/per-user, not per-schema. Do not convert to a rebuild-per-test.
- **Production auth is untouched:** the fast hasher is **test-only**. `api/users.py` keeps pwdlib/Argon2. No production code path changes. (AC #3/#7.)
- **Coverage is sacred (AC #2):** this is a speedup, not a deletion. Same assertions run; anything moved to a slow lane is listed.

### Testing standards

- pytest + pytest-asyncio; real Postgres via docker (`docker compose up -d db`); fake broker/LLM adapters (`BROKER_ADAPTER=fake`, `LLM_ADAPTER=fake`) — zero network, zero creds. Keep all of that.
- `pyproject.toml` `[tool.pytest.ini_options]` is currently minimal (`testpaths=["tests"]`, `pythonpath=["."]`) — this story adds marker registration and (optionally) default `addopts`.
- Verify with a twice-in-a-row run (AC #6) — shared fixtures are the main flakiness risk.

### Project Structure Notes

- New file: `ballast/backend/tests/conftest.py` (shared fixtures — none exists today; confirmed via `find . -name conftest.py`).
- Modified: `ballast/backend/tests/test_coach_api.py` (fixtures → conftest; drop per-test `client`/autouse-DDL), `ballast/backend/pyproject.toml` (`[tool.pytest.ini_options]` markers/addopts). Possibly other `tests/test_*.py` that build their own client, if consolidation is safe.
- No changes under `ballast/backend/api/`, `coach/`, `brokers/`, `db/` production modules (test-only story). If the profile reveals an app-side inefficiency, note it as a follow-up — do not fix it here.

### References

- [Source: _bmad-output/implementation-artifacts/8-5-test-suite-performance.brief.md] — scope, ranked refinements, non-negotiable coverage.
- [Source: ballast/backend/tests/test_coach_api.py:81-147] — `ensure_tables` autouse DDL reconciliation (preserve; run once).
- [Source: ballast/backend/tests/test_coach_api.py:150-153] — function-scoped `client = TestClient(create_app())` (session-scope it).
- [Source: ballast/backend/tests/test_coach_api.py:159-177] — `_unique_email`/`_register`/`_login` (Argon2 hot path).
- [Source: ballast/backend/api/users.py:1-12,41-74] — pwdlib/Argon2 hasher + `UserManager` (production default to keep; override in tests only).
- [Source: ballast/backend/pyproject.toml — [tool.pytest.ini_options]] — minimal config to extend with markers.
- [Source: _bmad-output/implementation-artifacts/deferred-work.md] — original slow-test entry (commit `0a7616b`) this story resolves; close/annotate it.
- [Continuity: Stories 8.2 & 8.4 review passes] — where the slow suite manifested; 8.2 review saw a test-ordering state-leak in this file (verify no regression, AC #6).

## Dev Agent Record

### Agent Model Used

Fixed hands-on (not the loop) 2026-08-05 — Claude Opus 4.8 — after two autonomous passes false-passed it (correct-course reopened it twice; see sprint-change-proposal-2026-08-05.md).

### Completion Notes List

- **REAL root cause (found only by independent, un-batched, timed runs — the loop's self-report was false twice):** the ~13-min slowness AND the `test_recommend_surfaces_fr11_warning` failure were **the same cause** — the local `.env` runs the demo/live config `LLM_ADAPTER=anthropic` with a real key, and nothing in the suite forced fake, so every test hitting `/recommend` made a **real Anthropic API call** (~10 s network latency each at ~1% CPU; live replies also made the fr11 assertion flaky). The two prior autonomous passes mis-diagnosed it as fixture scoping (session-scope hang / Argon2). Fixture setup was never the bottleneck (measured 0.07–0.10 s/test).
- **Fix (`tests/conftest.py`):** force `LLM_ADAPTER=fake` at module import (before any settings load; `get_settings()` is uncached and env vars beat `.env`) → deterministic in-process gateway. Kept the test-only fast SHA-256 hasher and added one-time session schema+migrations (`_schema_once` on a dedicated `asyncio.run` loop) with a neutered per-test lifespan (the per-test `run_startup_migrations` takes a `pg_advisory_xact_lock`; running it once removes that serialization and the earlier session-scope hang risk). `client` stays function-scoped.
- **Measured (independently, DB up):** `test_coach_api.py` **13 m 45 s → ~19.8 s**, **108 passed**, stable twice-in-a-row (19.84 s / 19.64 s) — no hang, CPU-active. Full suite **587 passed in ~42 s**.
- **AC #8 (independent no-hang timed gate): MET.** Verified by hand, not by the loop.
- **Known out-of-scope, pre-existing:** 4 `.env`-artifact failures remain (`test_llm_gateway.py` ×3, `test_market_ingest.py` ×1 — "returns-fake-by-default"/"no-key-raises"). Confirmed red **before** this work (0.45 s, on the loop's approach-A commit via `git stash`) — caused by the demo `.env` (real key + `LLM_ADAPTER=anthropic`/`MARKETDATA_ADAPTER=tiingo`), not by this change and not test-speed. Tracked separately; see [[fake-mode-vs-real-coaching]].
- **Process lesson:** the autonomous loop cannot self-verify async/timing/env-sensitive test changes — its in-run execution masked both the hang and the real-API slowness, yielding two false "done"s. This class of story needs an independent, un-batched, timed run before `done`.

### File List

- `ballast/backend/tests/conftest.py` (force fake LLM; fast hasher; one-time schema/migrations + neutered per-test lifespan)
- `ballast/backend/pyproject.toml` (honest pytest config comment; `slow`/`real_hasher` markers)
