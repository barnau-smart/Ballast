---
title: 'Production database migration path'
type: 'chore'
created: '2026-08-01'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: []
baseline_revision: 'c64a81669af1f2475bb55ca659293d8d3e0b3a20'
final_revision: '29c06556f17dbd9981c0a6501363c9264248e137'
---

<intent-contract>

## Intent

**Problem:** Every Epic 6 schema addition (`decision_record.idempotency_key` + its unique index, `broker_ref` + its index, the `(owner_id, co_signed_at)` composite index, `reconciliation_snapshot`/`reconciled_at`, and the `portfolio_balance` table + `uq_portfolio_balance_owner`) was declared only in the ORM and provisioned by `Base.metadata.create_all`. `create_all` creates missing *tables* but never ALTERs an existing one, so on an already-deployed database these columns and indexes simply would not exist — the unique-index double-place backstop and the queryable columns the go-live correctness guarantees depend on are silently absent. This is the hard blocker gating the rest of Epic 7.

**Approach:** Add an idempotent startup migration (the AC-sanctioned equivalent of Alembic, matching the `ADD COLUMN / CREATE INDEX IF NOT EXISTS` pattern already proven in the test fixtures) that runs *after* `create_all` and patches every Epic 6 addition onto pre-existing tables, backfilling NULL `idempotency_key` on carried-over `proposed` rows with a unique, stable synthetic key. Re-running is a no-op.

## Boundaries & Constraints

**Always:**
- Provision every listed Epic 6 addition on an already-existing DB via idempotent DDL; a second run of the migration must be a clean no-op (no error, no data change).
- Run the migration strictly *after* `create_all` — `create_all` builds any brand-new table (e.g. `portfolio_balance`) in full; the migration only patches columns/indexes onto pre-existing tables, so index DDL can assume its table exists.
- Backfill NULL `idempotency_key` on `proposed` rows with a value unique per row and derived from the immutable primary key (`'migrated:' || id`), so the unique index can never be violated and the double-place backstop stays meaningful. Add the column, then backfill, then create the unique index — in that order.
- Use the exact identifier names from the ORM (`uq_decision_record_idempotency_key`, `ix_decision_record_broker_ref`, `ix_decision_record_owner_co_signed_at`, `uq_portfolio_balance_owner`) and the exact column types (`VARCHAR(64)`, `JSON`, `TIMESTAMPTZ`) so no divergent duplicate object is created.
- Wrap all statements in one transaction so any failure rolls back cleanly.

**Block If:**
- The deployed schema is found to diverge from the ORM in a way these additive statements don't cover (e.g. an existing column of a conflicting type) — HALT rather than guess a destructive ALTER.

**Never:**
- Never introduce a full migration framework (Alembic) — out of scope for this go-live blocker; the idempotent-startup path is the deliberately chosen equivalent.
- Never DROP or rewrite existing columns or data; the migration is additive only, plus the NULL-only backfill.
- Never create the identifier names divergently from the ORM model, and never source idle cash cleanup / re-link clearing / atomicity here (those are Stories 7.2/7.5).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Fresh DB | No tables | `create_all` builds full schema; migration's every `IF NOT EXISTS` statement is a no-op | No error expected |
| Pre-Epic-6 DB | Tables exist, new columns/indexes missing | Migration adds the four columns, four indexes, and backfills NULL `proposed` keys | No error expected |
| Already-migrated DB | Everything present | Migration re-run is a full no-op | No error expected |
| Carried-over proposed row | One `proposed` row, `idempotency_key IS NULL` | Key set to `migrated:<id>`; unique index holds | No error expected |
| Multiple proposed NULL keys | Several `proposed` rows, all NULL | Each gets a distinct key from its PK; no unique violation | No error expected |

</intent-contract>

## Code Map

- `ballast/backend/db/models.py` -- canonical ORM defs of all Epic 6 additions (`DecisionRecord` ~L271, `PortfolioBalance` ~L166); source of truth for column types & identifier names the migration must match.
- `ballast/backend/db/session.py` -- async `engine` + `create_db_and_tables()` (`create_all`); migration runs after this.
- `ballast/backend/api/app.py` (lifespan ~L68-74) -- FastAPI startup; wire the migration call after `create_db_and_tables()`.
- `ballast/backend/tests/test_coach_api.py` (autouse fixture ~L79-136) -- existing idempotent `ADD COLUMN`/`CREATE INDEX IF NOT EXISTS` pattern + decision-creation pattern to reuse in the backfill test.
- `ballast/backend/db/migrations.py` -- **NEW** the idempotent startup migration.
- `ballast/backend/tests/test_migrations.py` -- **NEW** provisioning + idempotency + backfill tests.

## Tasks & Acceptance

**Execution:**
- [x] `ballast/backend/db/migrations.py` -- add `run_startup_migrations(engine: AsyncEngine)`: in a single `engine.begin()` transaction, execute (1) four `ALTER TABLE decision_record ADD COLUMN IF NOT EXISTS` for `idempotency_key VARCHAR(64)`, `broker_ref VARCHAR(64)`, `reconciliation_snapshot JSON`, `reconciled_at TIMESTAMPTZ`; (2) the backfill `UPDATE decision_record SET idempotency_key = 'migrated:' || id::text WHERE idempotency_key IS NULL AND status = 'proposed'`; (3) four index statements — `CREATE UNIQUE INDEX IF NOT EXISTS uq_decision_record_idempotency_key ON decision_record (idempotency_key)`, `CREATE INDEX IF NOT EXISTS ix_decision_record_broker_ref ON decision_record (broker_ref)`, `CREATE INDEX IF NOT EXISTS ix_decision_record_owner_co_signed_at ON decision_record (owner_id, co_signed_at)`, `CREATE UNIQUE INDEX IF NOT EXISTS uq_portfolio_balance_owner ON portfolio_balance (owner_id)`. Keep the statements in named, ordered lists for traceability. -- provisions every Epic 6 addition idempotently on an existing DB.
- [x] `ballast/backend/api/app.py` -- in the lifespan hook, call `await run_startup_migrations(engine)` immediately after `await create_db_and_tables()` (import `engine` + the new function). -- ensures a real deployment self-provisions on startup.
- [x] `ballast/backend/tests/test_migrations.py` -- unit-test the I/O matrix: (a) **provisions-missing-schema** — drop representative additions (`DROP INDEX IF EXISTS ix_decision_record_owner_co_signed_at`, `DROP INDEX IF EXISTS ix_decision_record_broker_ref`, `ALTER TABLE decision_record DROP COLUMN IF EXISTS broker_ref`), run the migration, then assert via `information_schema.columns` / `pg_class` that all four columns and all four indexes exist; (b) **idempotent** — run the migration twice with no exception and schema still intact; (c) **backfill** — persist a `proposed` `DecisionRecord` with NULL `idempotency_key` (reuse the decision-creation pattern from `test_coach_api.py`), run the migration, assert the key is now `migrated:<id>` and non-NULL. -- proves the AC's provisioning, idempotency, and backfill guarantees.

**Acceptance Criteria:**
- Given a database that already exists from an earlier release with the `decision_record` table but none of the Epic 6 additions, when the app starts (lifespan runs `create_all` then the migration), then all four columns, all four named indexes, and the `portfolio_balance` unique index exist with the exact ORM identifier names.
- Given the migration has already run, when it runs again, then it completes without error and changes no data (proven idempotent).
- Given carried-over `proposed` rows with NULL `idempotency_key`, when the migration runs, then each is backfilled to a unique `migrated:<id>` value and the `uq_decision_record_idempotency_key` unique index is created without violation.

## Spec Change Log

_No bad_spec loopbacks — the intent contract and spec held through review; all review findings were localized patches or defers._

## Review Triage Log

### 2026-08-01 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 5: (high 0, medium 3, low 2)
- defer: 3: (high 0, medium 0, low 3)
- reject: 8
- addressed_findings:
  - `[medium]` `[patch]` Concurrent-boot DDL race — added a `pg_advisory_xact_lock` as the first statement in the migration transaction so two instances booting at once serialize instead of racing `CREATE INDEX`/`ALTER`.
  - `[medium]` `[patch]` Opaque startup-failure diagnosis — wrapped each statement so a failure logs its `phase`/`step` name (previously dead labels) then re-raises; fail-fast preserved (never serve the money path without the backstop).
  - `[medium]` `[patch]` Coverage gap: the unique-index *create* path was only no-op-tested against `create_all`'s constraints — the provisions-missing-schema test now also drops both `uq_*` objects (constraint + index form) so the migration genuinely creates them from absent.
  - `[low]` `[patch]` Undocumented backfill scoping — added a comment explaining why only `proposed` rows are backfilled (only rows still claimable; terminal rows never re-placed; Postgres NULLs distinct in a unique index).
  - `[low]` `[patch]` Load-bearing ordering only asserted by construction — added a two-row test proving both NULL-key `proposed` rows get distinct `migrated:<id>` keys and the unique index builds over the backfilled data.

## Design Notes

- **Ordering is load-bearing.** `create_all` runs first so `portfolio_balance` (a whole new Epic 6 table) exists before its unique-index statement; the migration then only patches pre-existing tables. Within the migration: add columns → backfill NULL keys → create indexes, so the unique index is built against already-backfilled data.
- **Why not CONCURRENTLY.** `CREATE INDEX IF NOT EXISTS` runs inside the startup transaction (normal build, brief table lock). `CONCURRENTLY` cannot run in a transaction and is unnecessary for v1's data volume; revisit if a table grows large (leave as a deferred-work note, do not implement here).
- **Constraint vs index equivalence.** On a fresh DB `create_all` makes `uq_*` names as unique *constraints* (backed by same-named indexes); `CREATE UNIQUE INDEX IF NOT EXISTS` finds that relation by name and no-ops — matching the proven test-fixture behavior. NULLs are distinct in Postgres unique indexes, so historical non-`proposed` NULL keys never collide.

## Verification

**Commands:**
- `cd ballast/backend && python -m pytest tests/test_migrations.py -v` -- expected: all provisioning, idempotency, and backfill tests pass (requires `docker compose up -d db`).
- `cd ballast/backend && python -m pytest` -- expected: full suite still green (no regression from the lifespan change).

## Auto Run Result

Status: **done**

**Summary:** Added an idempotent production DB startup migration (`db/migrations.py::run_startup_migrations`) that provisions every Epic 6 schema addition onto an already-deployed database — closing the hard go-live blocker that `create_all` never ALTERs an existing table, so on a carried-over prod DB the correctness-critical columns and the double-place unique-index backstop would silently not exist. It runs in the FastAPI lifespan strictly after `create_all` (which builds any brand-new table like `portfolio_balance` in full), in one transaction, ordered columns → NULL-key backfill → indexes, and re-running is a clean no-op.

**Files changed:**
- `ballast/backend/db/migrations.py` (new) -- `run_startup_migrations(engine)`: `pg_advisory_xact_lock` to serialize concurrent boots, then idempotent `ADD COLUMN IF NOT EXISTS` (idempotency_key, broker_ref, reconciliation_snapshot, reconciled_at) → backfill NULL `idempotency_key` on `proposed` rows with a unique `migrated:<id>` → `CREATE [UNIQUE] INDEX IF NOT EXISTS` (4 indexes); per-step named logging with fail-fast re-raise.
- `ballast/backend/api/app.py` -- wired `await run_startup_migrations(engine)` into the lifespan immediately after `create_db_and_tables()`.
- `ballast/backend/tests/test_migrations.py` (new) -- 4 async tests against real Postgres: provisions-missing-schema (now drops the two `uq_*` objects too, so the create path is genuinely exercised), idempotent re-run, single-row backfill, and multi-row distinct-key + index-build-over-backfill.
- `_bmad-output/implementation-artifacts/deferred-work.md` -- 3 defer entries (constraint-vs-index drift; non-CONCURRENTLY index build on large tables; no migration-version ledger).
- `_bmad-output/implementation-artifacts/sprint-status.yaml` -- epic-7 → in-progress, 7-1 → done.

**Review findings breakdown:** 5 patches applied (3 medium: advisory lock, per-step logging, unique-index create-path test coverage; 2 low: backfill-scoping comment, multi-row ordering test) · 3 deferred · 8 rejected · 0 intent_gap · 0 bad_spec (no repair loopbacks).

**Verification:** `tests/test_migrations.py` → 4 passed; full suite → 415 passed (was 414; +1 new test), 2 pre-existing unrelated deprecation warnings. App imports/constructs cleanly. Requires `docker compose up -d db`.

**Residual risks:** All low and captured as defers — a fresh vs. migrated DB carry the two unique backstops as different catalog objects (constraint vs plain index) under identical names (functionally equivalent enforcement; reconcile at Alembic adoption); non-CONCURRENTLY index builds lock large tables (negligible at v1 scale); the migration re-runs its full statement list every boot (no version ledger). Follow-up review not recommended — localized, low-consequence hardening on a small, fully-tested module.
