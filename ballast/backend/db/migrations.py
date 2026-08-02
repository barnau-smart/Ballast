"""Idempotent startup migrations for carried-over databases (Story 7.1).

``Base.metadata.create_all`` (``db.session.create_db_and_tables``) creates any
*missing table* in full, but it NEVER ALTERs an already-existing table. So on a
database first provisioned before Epic 6, the ``decision_record`` table exists
yet its Epic 6 additions — ``idempotency_key`` + its unique index, ``broker_ref``
+ its index, the ``(owner_id, co_signed_at)`` composite index, and
``reconciliation_snapshot``/``reconciled_at`` — are silently absent, because they
were only ever declared in the ORM. The unique-index double-place backstop and
the queryable columns the go-live correctness guarantees depend on would not be
there.

This module closes that gap with the AC-sanctioned equivalent of a migration
(the same ``ADD COLUMN / CREATE INDEX IF NOT EXISTS`` pattern already proven in
the test fixtures) — deliberately NOT a full migration framework (Alembic is out
of scope for this go-live blocker). :func:`run_startup_migrations` runs *after*
``create_all`` (so any brand-new Epic 6 table such as ``portfolio_balance``
already exists) and patches every Epic 6 addition onto the pre-existing
``decision_record`` table. Every statement is idempotent, so a second run — or a
run against a fresh, already-complete schema — is a clean no-op.

Ordering within the migration is load-bearing: add the columns, then backfill
NULL ``idempotency_key`` on carried-over ``proposed`` rows with a unique, stable
synthetic key (``'migrated:' || id``), then create the unique index — so the
unique index is built against already-backfilled data and can never be violated.
All statements run in a single transaction so any failure rolls back cleanly.

Identifier names and column types match ``db.models`` EXACTLY
(``uq_decision_record_idempotency_key``, ``ix_decision_record_broker_ref``,
``ix_decision_record_owner_co_signed_at``, ``uq_portfolio_balance_owner``;
``VARCHAR(64)``, ``JSON``, ``TIMESTAMPTZ``) so no divergent duplicate object is
ever created. The migration is additive only — it never DROPs or rewrites
existing columns or data, save the NULL-only backfill.
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger("ballast.db.migrations")

# Constant transaction-scoped advisory-lock key. If two app instances boot at
# once (a rolling restart), their migration transactions serialize on this lock
# instead of racing concurrent DDL on the same table — the second boot then sees
# every ``IF NOT EXISTS`` statement as a no-op. Auto-released on commit/rollback.
_MIGRATION_LOCK_KEY = 71001  # Story 7.1 startup migration

# (1) Additive columns on the pre-existing ``decision_record`` table. Types match
# the ORM exactly (VARCHAR(64) / JSON / TIMESTAMPTZ). ``IF NOT EXISTS`` makes each
# a no-op once the column is present.
COLUMN_STATEMENTS: list[tuple[str, str]] = [
    (
        "add_idempotency_key",
        "ALTER TABLE decision_record "
        "ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(64)",
    ),
    (
        "add_broker_ref",
        "ALTER TABLE decision_record "
        "ADD COLUMN IF NOT EXISTS broker_ref VARCHAR(64)",
    ),
    (
        "add_reconciliation_snapshot",
        "ALTER TABLE decision_record "
        "ADD COLUMN IF NOT EXISTS reconciliation_snapshot JSON",
    ),
    (
        "add_reconciled_at",
        "ALTER TABLE decision_record "
        "ADD COLUMN IF NOT EXISTS reconciled_at TIMESTAMPTZ",
    ),
]

# (2) Backfill NULL ``idempotency_key`` on carried-over ``proposed`` rows with a
# value unique per row and derived from the immutable primary key, so the unique
# index below can never be violated and the double-place backstop stays
# meaningful. Runs AFTER the column exists and BEFORE the unique index is built.
# Only ``proposed`` rows are backfilled — they are the only rows still claimable/
# placeable, so they are the only ones that need a key for the double-place
# backstop. Terminal rows (``cosigned`` etc.) are never re-placed, and Postgres
# treats NULLs as distinct in a unique index, so leaving their key NULL is safe.
BACKFILL_STATEMENTS: list[tuple[str, str]] = [
    (
        "backfill_proposed_idempotency_key",
        "UPDATE decision_record "
        "SET idempotency_key = 'migrated:' || id::text "
        "WHERE idempotency_key IS NULL AND status = 'proposed'",
    ),
]

# (3) Indexes patched onto the pre-existing tables. Names match the ORM exactly.
# ``CREATE [UNIQUE] INDEX IF NOT EXISTS`` finds an existing same-named
# relation/constraint by name and no-ops (matching the proven test-fixture
# behavior). ``portfolio_balance`` is a whole new Epic 6 table built in full by
# ``create_all``, so its unique index can assume the table exists.
INDEX_STATEMENTS: list[tuple[str, str]] = [
    (
        "uq_decision_record_idempotency_key",
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "uq_decision_record_idempotency_key "
        "ON decision_record (idempotency_key)",
    ),
    (
        "ix_decision_record_broker_ref",
        "CREATE INDEX IF NOT EXISTS ix_decision_record_broker_ref "
        "ON decision_record (broker_ref)",
    ),
    (
        "ix_decision_record_owner_co_signed_at",
        "CREATE INDEX IF NOT EXISTS ix_decision_record_owner_co_signed_at "
        "ON decision_record (owner_id, co_signed_at)",
    ),
    (
        "uq_portfolio_balance_owner",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_portfolio_balance_owner "
        "ON portfolio_balance (owner_id)",
    ),
]


async def run_startup_migrations(engine: AsyncEngine) -> None:
    """Provision every Epic 6 schema addition idempotently on an existing DB.

    Must run AFTER ``create_db_and_tables`` (``create_all``): ``create_all`` builds
    any brand-new table in full, and this migration only patches columns/indexes
    onto pre-existing tables. In a single transaction, executes in order:
    columns → backfill → indexes. Re-running is a clean no-op.

    On any failure it logs which named step failed and re-raises: a migration
    error must fail-fast (abort startup) rather than serve the money path without
    the double-place backstop the go-live correctness guarantees depend on.
    """
    async with engine.begin() as conn:
        # Serialize concurrent boots so two instances never race the same DDL.
        await conn.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _MIGRATION_LOCK_KEY},
        )
        for phase, statements in (
            ("column", COLUMN_STATEMENTS),
            ("backfill", BACKFILL_STATEMENTS),
            ("index", INDEX_STATEMENTS),
        ):
            for name, sql in statements:
                try:
                    await conn.execute(text(sql))
                except Exception:
                    logger.exception(
                        "Startup migration step failed (phase=%s, step=%s)",
                        phase,
                        name,
                    )
                    raise
    logger.info("Startup migrations applied (idempotent, %d steps).",
                len(COLUMN_STATEMENTS) + len(BACKFILL_STATEMENTS) + len(INDEX_STATEMENTS))
