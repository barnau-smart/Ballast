"""Story 7.1 tests — the idempotent production DB migration path.

``Base.metadata.create_all`` creates missing *tables* but NEVER ALTERs an
already-existing one, so on a carried-over (pre-Epic-6) database the Epic 6
columns and indexes on ``decision_record`` would be silently absent.
:func:`db.migrations.run_startup_migrations` patches every Epic 6 addition onto
the pre-existing tables idempotently. These tests lock the I/O & Edge-Case
Matrix against a real Postgres (``docker compose up -d db``):

  (a) provisions-missing-schema — drop representative Epic 6 additions, run the
      migration, then assert every column and every named index exists again.
  (b) idempotent — run the migration twice with no error and the schema intact.
  (c) backfill — a carried-over ``proposed`` row with a NULL ``idempotency_key``
      is backfilled to a unique, non-NULL ``migrated:<id>`` value.

The tests are async and follow the ``pytest_asyncio`` conventions used across the
suite: an autouse fixture guarantees the full (fresh) schema exists first, then
each test manipulates and re-provisions it via the async ``engine``.
"""

from __future__ import annotations

import datetime
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text

from db.migrations import run_startup_migrations
from db.models import Base
from db.session import async_session_maker, engine

# Every Epic 6 column the migration must guarantee on ``decision_record``.
_EPIC6_COLUMNS = (
    "idempotency_key",
    "broker_ref",
    "reconciliation_snapshot",
    "reconciled_at",
)

# Every Epic 6 index the migration must guarantee (exact ORM identifier names).
_EPIC6_INDEXES = (
    "uq_decision_record_idempotency_key",
    "ix_decision_record_broker_ref",
    "ix_decision_record_owner_co_signed_at",
    "uq_portfolio_balance_owner",
)


@pytest_asyncio.fixture(autouse=True)
async def ensure_schema():
    """Ensure the full (fresh) schema exists before each test.

    Mirrors the app's own startup order: ``create_all`` builds any missing table
    in full (so ``portfolio_balance`` and ``decision_record`` exist), then the
    idempotent migration patches the Epic 6 additions on. This leaves every test
    starting from a fully-provisioned schema; the provisioning test then
    deliberately drops representative additions to prove they get restored.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await run_startup_migrations(engine)
    yield


# --- schema-introspection helpers --------------------------------------------


async def _column_exists(table: str, column: str) -> bool:
    """True if ``table.column`` exists (via ``information_schema.columns``)."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = :table AND column_name = :column"
            ),
            {"table": table, "column": column},
        )
        return result.first() is not None


async def _index_exists(name: str) -> bool:
    """True if an index relation named ``name`` exists (via ``pg_class``)."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT 1 FROM pg_class "
                "WHERE relkind = 'i' AND relname = :name"
            ),
            {"name": name},
        )
        return result.first() is not None


# --- (a) provisions-missing-schema -------------------------------------------


@pytest.mark.asyncio
async def test_migration_provisions_missing_schema():
    """Dropping representative Epic 6 additions is fully restored by the migration.

    Simulates a carried-over pre-Epic-6 DB by dropping two indexes and one column,
    then runs the migration and asserts ALL four columns and ALL four named
    indexes exist with the exact ORM identifier names.
    """
    # Drop representative additions (create_all would never re-ALTER these back).
    # This includes the two UNIQUE objects: on a carried-over prod DB they do NOT
    # pre-exist (create_all skips the existing table), so the migration must
    # genuinely CREATE them — not just no-op against a fresh DB's constraint. The
    # DROP CONSTRAINT + DROP INDEX pair covers both forms (fresh-DB constraint vs
    # carried-over plain index).
    async with engine.begin() as conn:
        await conn.execute(
            text("DROP INDEX IF EXISTS ix_decision_record_owner_co_signed_at")
        )
        await conn.execute(
            text("DROP INDEX IF EXISTS ix_decision_record_broker_ref")
        )
        await conn.execute(
            text(
                "ALTER TABLE decision_record DROP COLUMN IF EXISTS broker_ref"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE decision_record "
                "DROP CONSTRAINT IF EXISTS uq_decision_record_idempotency_key"
            )
        )
        await conn.execute(
            text("DROP INDEX IF EXISTS uq_decision_record_idempotency_key")
        )
        await conn.execute(
            text(
                "ALTER TABLE portfolio_balance "
                "DROP CONSTRAINT IF EXISTS uq_portfolio_balance_owner"
            )
        )
        await conn.execute(
            text("DROP INDEX IF EXISTS uq_portfolio_balance_owner")
        )

    # Confirm the drops took effect before re-provisioning.
    assert not await _column_exists("decision_record", "broker_ref")
    assert not await _index_exists("ix_decision_record_broker_ref")
    assert not await _index_exists("ix_decision_record_owner_co_signed_at")
    assert not await _index_exists("uq_decision_record_idempotency_key")
    assert not await _index_exists("uq_portfolio_balance_owner")

    await run_startup_migrations(engine)

    for column in _EPIC6_COLUMNS:
        assert await _column_exists(
            "decision_record", column
        ), f"column {column} missing after migration"
    for index in _EPIC6_INDEXES:
        assert await _index_exists(index), f"index {index} missing after migration"


# --- (b) idempotent ----------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_is_idempotent():
    """Running the migration twice raises no error and leaves the schema intact."""
    # The autouse fixture already ran it once; run it two more times.
    await run_startup_migrations(engine)
    await run_startup_migrations(engine)

    for column in _EPIC6_COLUMNS:
        assert await _column_exists("decision_record", column)
    for index in _EPIC6_INDEXES:
        assert await _index_exists(index)


# --- (c) backfill ------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_backfills_null_idempotency_key():
    """A carried-over ``proposed`` row with NULL ``idempotency_key`` is backfilled.

    Persists a real ``proposed`` ``decision_record`` row owned by a fresh user
    with ``idempotency_key = NULL`` (simulating a pre-Epic-6 carried-over row),
    runs the migration, then asserts the key is now the unique, non-NULL
    ``migrated:<id>`` derived from the immutable primary key.
    """
    user_id = uuid.uuid4()
    decision_id = uuid.uuid4()
    email = f"migration-test-{uuid.uuid4().hex}@example.com"
    now = datetime.datetime.now(datetime.timezone.utc)

    async with async_session_maker() as session:
        # A minimal owning user (FK target for decision_record.owner_id).
        await session.execute(
            text(
                'INSERT INTO "user" '
                "(id, email, hashed_password, is_active, is_superuser, is_verified) "
                "VALUES (:id, :email, :pw, true, false, false)"
            ),
            {"id": user_id, "email": email, "pw": "x"},
        )
        # A carried-over proposed row with a NULL idempotency_key. Inserted via
        # raw SQL to model the pre-Epic-6 state (and to avoid the AD-6
        # sole-writer constructor, which is not the unit under test here).
        await session.execute(
            text(
                "INSERT INTO decision_record "
                "(id, owner_id, schema_version, recommendation_snapshot, status, "
                " created_at, idempotency_key) "
                "VALUES (:id, :owner, 1, '{}', 'proposed', :created_at, NULL)"
            ),
            {"id": decision_id, "owner": user_id, "created_at": now},
        )
        await session.commit()

    try:
        # Precondition: the key is genuinely NULL before the migration.
        async with engine.connect() as conn:
            (before,) = (
                await conn.execute(
                    text(
                        "SELECT idempotency_key FROM decision_record "
                        "WHERE id = :id"
                    ),
                    {"id": decision_id},
                )
            ).one()
        assert before is None

        await run_startup_migrations(engine)

        async with engine.connect() as conn:
            (after,) = (
                await conn.execute(
                    text(
                        "SELECT idempotency_key FROM decision_record "
                        "WHERE id = :id"
                    ),
                    {"id": decision_id},
                )
            ).one()
        assert after is not None
        assert after == f"migrated:{decision_id}"
    finally:
        # Cascades to the decision_record row via the ON DELETE CASCADE FK.
        async with async_session_maker() as session:
            await session.execute(
                text('DELETE FROM "user" WHERE id = :id'), {"id": user_id}
            )
            await session.commit()


@pytest.mark.asyncio
async def test_migration_backfills_multiple_rows_distinctly_then_builds_index():
    """Two NULL-key proposed rows get DISTINCT keys and the unique index builds.

    Proves the load-bearing ordering (backfill unique keys BEFORE creating the
    unique index): drops the unique idempotency index to model a carried-over DB,
    inserts two ``proposed`` rows with NULL ``idempotency_key``, runs the
    migration, then asserts each row got a distinct ``migrated:<id>`` key AND the
    unique index was (re)built without a violation.
    """
    user_id = uuid.uuid4()
    decision_a = uuid.uuid4()
    decision_b = uuid.uuid4()
    email = f"migration-multi-{uuid.uuid4().hex}@example.com"
    now = datetime.datetime.now(datetime.timezone.utc)

    async with async_session_maker() as session:
        await session.execute(
            text(
                'INSERT INTO "user" '
                "(id, email, hashed_password, is_active, is_superuser, is_verified) "
                "VALUES (:id, :email, :pw, true, false, false)"
            ),
            {"id": user_id, "email": email, "pw": "x"},
        )
        for decision_id in (decision_a, decision_b):
            await session.execute(
                text(
                    "INSERT INTO decision_record "
                    "(id, owner_id, schema_version, recommendation_snapshot, status, "
                    " created_at, idempotency_key) "
                    "VALUES (:id, :owner, 1, '{}', 'proposed', :created_at, NULL)"
                ),
                {"id": decision_id, "owner": user_id, "created_at": now},
            )
        await session.commit()

    try:
        # Model a carried-over DB where the unique object does not yet exist.
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "ALTER TABLE decision_record "
                    "DROP CONSTRAINT IF EXISTS uq_decision_record_idempotency_key"
                )
            )
            await conn.execute(
                text("DROP INDEX IF EXISTS uq_decision_record_idempotency_key")
            )
        assert not await _index_exists("uq_decision_record_idempotency_key")

        await run_startup_migrations(engine)

        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT id, idempotency_key FROM decision_record "
                        "WHERE id IN (:a, :b)"
                    ),
                    {"a": decision_a, "b": decision_b},
                )
            ).all()
        keys = {row_id: key for row_id, key in rows}
        assert keys[decision_a] == f"migrated:{decision_a}"
        assert keys[decision_b] == f"migrated:{decision_b}"
        assert keys[decision_a] != keys[decision_b]
        # The unique index built successfully over the backfilled (distinct) data.
        assert await _index_exists("uq_decision_record_idempotency_key")
    finally:
        async with async_session_maker() as session:
            await session.execute(
                text('DELETE FROM "user" WHERE id = :id'), {"id": user_id}
            )
            await session.commit()
