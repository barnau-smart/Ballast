"""Story 9.3 tests — the widened SELL scope gate in the execution owner.

Locks the bounded scope-gate widening in
:func:`coach.execution.execute_approved_order`: a **SELL** is in-scope when the
symbol is index-core OR one of the scope user's declared ``parked_symbols``; a
**BUY** stays index-core-only. All other hardening (session integrity, validation,
atomic claim/reconcile) is unchanged — a scope-less direct call keeps the strict
index-core-only behavior (fail-closed).

Offline: fake broker, no network. Seeds ``cash_config`` via sync psycopg and calls
the execution owner directly under a real user scope against the docker Postgres.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio

from brokers.fake_adapter import FakeBrokerAdapter
from brokers.port import OrderStatus
from brokers.session import BrokerageSession
from coach.execution import OrderScopeError, execute_approved_order
from coach.recommendation import OrderIntent, OrderSide
from db.connection import get_connection
from db.models import CashConfig
from db.scope import Scope
from db.session import async_session_maker, engine


@pytest_asyncio.fixture(autouse=True)
async def ensure_tables():
    async with engine.begin() as conn:
        await conn.run_sync(CashConfig.__table__.create, checkfirst=True)
    yield


def _live_session(provider: str = "fake") -> BrokerageSession:
    return BrokerageSession(
        state="live",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        provider=provider,
    )


def _make_user() -> uuid.UUID:
    uid = uuid.uuid4()
    email = f"exec-sell-{uid.hex}@example.com"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                'INSERT INTO "user" '
                "(id, email, hashed_password, is_active, is_superuser, is_verified) "
                "VALUES (%s, %s, %s, true, false, false)",
                (str(uid), email, "x"),
            )
        conn.commit()
    return uid


def _delete_user(uid: uuid.UUID) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM "user" WHERE id = %s', (str(uid),))
        conn.commit()


def _seed_parked(uid: uuid.UUID, parked_symbols: list[str]) -> None:
    import json

    now = datetime.now(timezone.utc)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO cash_config "
                "(id, owner_id, reserve_amount, reserve_decided, parked_symbols, "
                " created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    str(uuid.uuid4()),
                    str(uid),
                    None,
                    False,
                    json.dumps(parked_symbols),
                    now,
                    now,
                ),
            )
        conn.commit()


@pytest.mark.asyncio
async def test_sell_of_declared_parked_symbol_is_permitted():
    """A SELL of a declared parked money-market symbol (non-core) is placed."""
    uid = _make_user()
    try:
        _seed_parked(uid, ["SWVXX"])
        intent = OrderIntent(symbol="SWVXX", side=OrderSide.SELL, amount=Decimal("500"))
        async with async_session_maker() as session:
            outcome = await execute_approved_order(
                intent,
                broker=FakeBrokerAdapter(),
                broker_session=_live_session(),
                scope=Scope.for_user(uid),
                session=session,
            )
        # The fake adapter fills a placed order — the widened gate let it through.
        assert outcome.status is OrderStatus.FILLED
    finally:
        _delete_user(uid)


@pytest.mark.asyncio
async def test_sell_of_non_core_non_parked_symbol_is_refused():
    """A SELL of a symbol that is neither index-core nor declared parked → refused."""
    uid = _make_user()
    try:
        _seed_parked(uid, ["SWVXX"])  # AAPL is NOT parked
        intent = OrderIntent(symbol="AAPL", side=OrderSide.SELL, amount=Decimal("500"))
        async with async_session_maker() as session:
            with pytest.raises(OrderScopeError):
                await execute_approved_order(
                    intent,
                    broker=FakeBrokerAdapter(),
                    broker_session=_live_session(),
                    scope=Scope.for_user(uid),
                    session=session,
                )
    finally:
        _delete_user(uid)


@pytest.mark.asyncio
async def test_buy_stays_index_core_only_even_for_parked_symbol():
    """A BUY of a declared parked (non-core) symbol is STILL refused — BUY is
    index-core-only; the widening is SELL-only."""
    uid = _make_user()
    try:
        _seed_parked(uid, ["SWVXX"])
        intent = OrderIntent(symbol="SWVXX", side=OrderSide.BUY, amount=Decimal("500"))
        async with async_session_maker() as session:
            with pytest.raises(OrderScopeError):
                await execute_approved_order(
                    intent,
                    broker=FakeBrokerAdapter(),
                    broker_session=_live_session(),
                    scope=Scope.for_user(uid),
                    session=session,
                )
    finally:
        _delete_user(uid)


@pytest.mark.asyncio
async def test_sell_index_core_still_permitted():
    """An index-core SELL is placed regardless of parked config (unchanged path)."""
    uid = _make_user()
    try:
        _seed_parked(uid, [])
        intent = OrderIntent(symbol="VTI", side=OrderSide.SELL, amount=Decimal("500"))
        async with async_session_maker() as session:
            outcome = await execute_approved_order(
                intent,
                broker=FakeBrokerAdapter(),
                broker_session=_live_session(),
                scope=Scope.for_user(uid),
                session=session,
            )
        assert outcome.status is OrderStatus.FILLED
    finally:
        _delete_user(uid)


@pytest.mark.asyncio
async def test_sell_parked_without_scope_is_refused_failclosed():
    """A scope-less direct call CANNOT widen — a non-core parked SELL is refused
    (fail-closed: the widening only applies when a user scope+session are given)."""
    intent = OrderIntent(symbol="SWVXX", side=OrderSide.SELL, amount=Decimal("500"))
    with pytest.raises(OrderScopeError):
        await execute_approved_order(
            intent,
            broker=FakeBrokerAdapter(),
            broker_session=_live_session(),
        )
