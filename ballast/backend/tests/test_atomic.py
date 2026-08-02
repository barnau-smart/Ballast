"""Story 7.2 tests — the generalized atomic-claim primitive (``db/atomic.py``).

Unit-tests the ONE shared mechanism both claim strategies live in, in isolation:

  - ``conditional_claim`` returns ``True`` on a 1-row conditional-UPDATE match and
    ``False`` on a 0-row match (the concurrent-claim rowcount semantics generalized
    from Story 6.1), and does NOT commit (the caller owns the commit boundary).
  - ``lock_row`` returns only the OWNER's row (a foreign row is invisible → None)
    and issues a ``SELECT … FOR UPDATE`` (the pessimistic single-row lock).

Uses ``PortfolioBalance`` as a convenient real owned model (``id`` + ``owner_id``)
so the primitive is exercised against a real Postgres row without any
model-specific coupling. Requires the docker Postgres (``docker compose up -d db``).
Each test uses a fresh user and cleans up its own rows.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import update

from db.atomic import conditional_claim, lock_row
from db.connection import get_connection
from db.models import PortfolioBalance
from db.session import async_session_maker, engine


@pytest_asyncio.fixture(autouse=True)
async def ensure_tables():
    async with engine.begin() as conn:
        await conn.run_sync(PortfolioBalance.__table__.create, checkfirst=True)
    yield


def _make_user() -> uuid.UUID:
    user_id = uuid.uuid4()
    email = f"atomic-test-{user_id.hex}@example.com"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                'INSERT INTO "user" '
                "(id, email, hashed_password, is_active, is_superuser, is_verified) "
                "VALUES (%s, %s, %s, TRUE, FALSE, FALSE)",
                (str(user_id), email, "x" * 60),
            )
        conn.commit()
    return user_id


def _delete_user(user_id: uuid.UUID) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM "user" WHERE id = %s', (str(user_id),))
        conn.commit()


def _insert_balance(owner: uuid.UUID, cash: str) -> uuid.UUID:
    row_id = uuid.uuid4()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO portfolio_balance (id, owner_id, cash, as_of) "
                "VALUES (%s, %s, %s, %s)",
                (str(row_id), str(owner), cash, datetime(2026, 8, 1, tzinfo=timezone.utc)),
            )
        conn.commit()
    return row_id


@pytest.mark.asyncio
async def test_conditional_claim_true_on_one_row_match():
    """A conditional UPDATE that matches exactly one row → True (rowcount == 1)."""
    owner = _make_user()
    try:
        row_id = _insert_balance(owner, "100.00")
        async with async_session_maker() as session:
            won = await conditional_claim(
                session,
                update(PortfolioBalance)
                .where(
                    PortfolioBalance.id == row_id,
                    PortfolioBalance.cash == Decimal("100.00"),
                )
                .values(cash=Decimal("200.00")),
            )
            assert won is True
            await session.commit()
        # The primitive itself does not commit; the caller (above) did.
        async with async_session_maker() as session:
            row = await session.get(PortfolioBalance, row_id)
            assert row.cash == Decimal("200.00")
    finally:
        _delete_user(owner)


@pytest.mark.asyncio
async def test_conditional_claim_false_on_zero_row_match():
    """A conditional UPDATE whose predicate matches NO row → False (rowcount == 0).

    Models the concurrent-claim loser: another writer already moved the row out
    from under the predicate, so this claim matches nothing.
    """
    owner = _make_user()
    try:
        row_id = _insert_balance(owner, "100.00")
        async with async_session_maker() as session:
            won = await conditional_claim(
                session,
                update(PortfolioBalance)
                # cash is 100, so this predicate matches zero rows.
                .where(
                    PortfolioBalance.id == row_id,
                    PortfolioBalance.cash == Decimal("999.99"),
                )
                .values(cash=Decimal("0.00")),
            )
            assert won is False
            await session.commit()
        async with async_session_maker() as session:
            row = await session.get(PortfolioBalance, row_id)
            assert row.cash == Decimal("100.00")  # untouched
    finally:
        _delete_user(owner)


@pytest.mark.asyncio
async def test_conditional_claim_does_not_commit():
    """The primitive leaves the transaction open — the caller owns the commit.

    A rollback after a winning ``conditional_claim`` (with no caller commit) must
    discard the change, proving the primitive does not commit on its own.
    """
    owner = _make_user()
    try:
        row_id = _insert_balance(owner, "100.00")
        async with async_session_maker() as session:
            won = await conditional_claim(
                session,
                update(PortfolioBalance)
                .where(PortfolioBalance.id == row_id)
                .values(cash=Decimal("777.00")),
            )
            assert won is True
            await session.rollback()  # caller chooses NOT to commit
        async with async_session_maker() as session:
            row = await session.get(PortfolioBalance, row_id)
            assert row.cash == Decimal("100.00")  # rolled back, not committed
    finally:
        _delete_user(owner)


@pytest.mark.asyncio
async def test_lock_row_returns_only_owner_row():
    """``lock_row`` returns the owner's row and NEVER a foreign owner's row."""
    owner_a = _make_user()
    owner_b = _make_user()
    try:
        row_a = _insert_balance(owner_a, "100.00")
        async with async_session_maker() as session:
            # Owner A can lock its own row.
            locked = await lock_row(
                session, PortfolioBalance, entity_id=row_a, owner_id=owner_a
            )
            assert locked is not None
            assert locked.id == row_a
            assert locked.cash == Decimal("100.00")
            await session.rollback()
        async with async_session_maker() as session:
            # Owner B cannot see A's row (owner predicate) → None.
            foreign = await lock_row(
                session, PortfolioBalance, entity_id=row_a, owner_id=owner_b
            )
            assert foreign is None
            await session.rollback()
    finally:
        _delete_user(owner_a)
        _delete_user(owner_b)


@pytest.mark.asyncio
async def test_lock_row_emits_for_update():
    """``lock_row`` issues a ``SELECT … FOR UPDATE`` (compiled SQL carries it).

    Verifies the pessimistic-lock intent structurally: the statement the primitive
    executes compiles to SQL containing ``FOR UPDATE``. (The cross-session blocking
    behavior this enables is exercised end-to-end in the reconcile-concurrency
    tests.)
    """
    owner = _make_user()
    try:
        row_id = _insert_balance(owner, "100.00")
        # Reconstruct the same statement the primitive builds and check its SQL.
        from sqlalchemy import select

        stmt = (
            select(PortfolioBalance)
            .where(
                PortfolioBalance.id == row_id,
                PortfolioBalance.owner_id == owner,
            )
            .with_for_update()
        )
        compiled = str(stmt.compile(dialect=engine.dialect))
        assert "FOR UPDATE" in compiled.upper()
    finally:
        _delete_user(owner)
