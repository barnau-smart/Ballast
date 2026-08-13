"""Story 10.9 tests — backend cash-cover safety: a BUY never places on margin.

Locks the cash-coverage gate added to
:func:`coach.execution.execute_approved_order` (the SOLE broker-place caller): a
**BUY** places only when its dollar amount is covered by the account's KNOWN real
settled cash (``max(0, view.cash)`` — settlement cash, the same ``ready_to_trade``
the 9-3 liquidator anchors to). Parked money-market does NOT count (it is deployable
on paper but not spendable until sold+settled) and the reserve is NOT subtracted
(the execution invariant is strictly no-margin). A **SELL** is never coverage-gated
(it raises cash). The gate enforces only against a KNOWN balance (``view.as_of is
not None``); a scope-less direct call or a never-imported account (no balance row)
is not blocked — the production ``/approve`` path always supplies a user scope, and
the proven margin hole (10-8: known cash $12,182.82 < a $65,949 deploy buy) is with a
real balance row.

Offline: fake broker, no network. Seeds ``portfolio_balance``/``portfolio_cache``/
``cash_config`` via sync psycopg and calls the execution owner directly under a real
user scope against the docker Postgres (mirrors test_execution / test_liquidation).
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
from coach.execution import (
    InsufficientSettledCashError,
    execute_approved_order,
)
from coach.recommendation import OrderIntent, OrderSide
from db.connection import get_connection
from db.models import CashConfig, PortfolioBalance, PortfolioCache
from db.scope import Scope
from db.session import async_session_maker, engine

AS_OF = datetime(2026, 8, 13, tzinfo=timezone.utc)


@pytest_asyncio.fixture(autouse=True)
async def ensure_tables():
    async with engine.begin() as conn:
        await conn.run_sync(PortfolioCache.__table__.create, checkfirst=True)
        await conn.run_sync(PortfolioBalance.__table__.create, checkfirst=True)
        await conn.run_sync(CashConfig.__table__.create, checkfirst=True)
    yield


class _SpyBroker(FakeBrokerAdapter):
    """A fake broker that counts ``place_order`` calls (proves 'broker never touched')."""

    def __init__(self) -> None:
        super().__init__()
        self.place_calls = 0

    async def place_order(self, order_intent, *, idempotency_key):  # type: ignore[override]
        self.place_calls += 1
        return await super().place_order(order_intent, idempotency_key=idempotency_key)


def _live_session(provider: str = "fake") -> BrokerageSession:
    return BrokerageSession(
        state="live",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        provider=provider,
    )


def _make_user() -> uuid.UUID:
    uid = uuid.uuid4()
    email = f"cash-cover-{uid.hex}@example.com"
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


def _seed_balance(uid: uuid.UUID, cash: str, as_of: datetime = AS_OF) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO portfolio_balance (id, owner_id, cash, as_of) "
                "VALUES (%s, %s, %s, %s)",
                (str(uuid.uuid4()), str(uid), cash, as_of),
            )
        conn.commit()


def _seed_holding(
    uid: uuid.UUID,
    symbol: str,
    quantity: str,
    market_value: str,
    as_of: datetime = AS_OF,
) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO portfolio_cache "
                "(id, owner_id, symbol, quantity, market_value, cost_basis, cash, as_of) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    str(uuid.uuid4()),
                    str(uid),
                    symbol,
                    quantity,
                    market_value,
                    None,
                    "0",
                    as_of,
                ),
            )
        conn.commit()


def _seed_config(uid: uuid.UUID, *, reserve_amount: str | None, parked_symbols: list[str]) -> None:
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
                    reserve_amount,
                    reserve_amount is not None,
                    json.dumps(parked_symbols),
                    now,
                    now,
                ),
            )
        conn.commit()


# --- AC1: a BUY beyond real settled cash is refused (no margin) --------------


@pytest.mark.asyncio
async def test_buy_over_settled_cash_is_refused_broker_untouched():
    uid = _make_user()
    try:
        _seed_balance(uid, "12182.82")  # MasterB's real settlement cash
        broker = _SpyBroker()
        intent = OrderIntent(symbol="VTI", side=OrderSide.BUY, amount=Decimal("65949.08"))
        async with async_session_maker() as session:
            with pytest.raises(InsufficientSettledCashError):
                await execute_approved_order(
                    intent,
                    broker=broker,
                    broker_session=_live_session(),
                    scope=Scope.for_user(uid),
                    session=session,
                )
        assert broker.place_calls == 0  # broker NEVER touched
    finally:
        _delete_user(uid)


# --- AC2: parked money-market does NOT count toward coverage -----------------


@pytest.mark.asyncio
async def test_parked_money_market_does_not_count_as_coverage():
    """Deployable-on-paper (parked MM) is not spendable cash: the BUY is refused."""
    uid = _make_user()
    try:
        _seed_balance(uid, "12182.82")
        _seed_holding(uid, "SWVXX", "93766.26", "93766.26")  # parked money-market
        _seed_config(uid, reserve_amount="40000", parked_symbols=["SWVXX"])
        broker = _SpyBroker()
        # $20k < the $65,949 "investable" (settlement+parked-reserve) but > $12,182.82 cash.
        intent = OrderIntent(symbol="VTI", side=OrderSide.BUY, amount=Decimal("20000"))
        async with async_session_maker() as session:
            with pytest.raises(InsufficientSettledCashError):
                await execute_approved_order(
                    intent,
                    broker=broker,
                    broker_session=_live_session(),
                    scope=Scope.for_user(uid),
                    session=session,
                )
        assert broker.place_calls == 0
    finally:
        _delete_user(uid)


# --- AC3: a covered BUY places unchanged -------------------------------------


@pytest.mark.asyncio
async def test_covered_buy_places_unchanged():
    uid = _make_user()
    try:
        _seed_balance(uid, "5000.00")
        broker = _SpyBroker()
        intent = OrderIntent(symbol="VTI", side=OrderSide.BUY, amount=Decimal("1000.00"))
        async with async_session_maker() as session:
            outcome = await execute_approved_order(
                intent,
                broker=broker,
                broker_session=_live_session(),
                scope=Scope.for_user(uid),
                session=session,
            )
        assert outcome.status is OrderStatus.FILLED
        assert broker.place_calls == 1
    finally:
        _delete_user(uid)


@pytest.mark.asyncio
async def test_buy_exactly_at_settled_cash_places():
    """Boundary: amount == settled cash is covered (not a shortfall)."""
    uid = _make_user()
    try:
        _seed_balance(uid, "1000.00")
        broker = _SpyBroker()
        intent = OrderIntent(symbol="VTI", side=OrderSide.BUY, amount=Decimal("1000.00"))
        async with async_session_maker() as session:
            outcome = await execute_approved_order(
                intent,
                broker=broker,
                broker_session=_live_session(),
                scope=Scope.for_user(uid),
                session=session,
            )
        assert outcome.status is OrderStatus.FILLED
    finally:
        _delete_user(uid)


# --- AC4: SELLs are never coverage-gated -------------------------------------


@pytest.mark.asyncio
async def test_index_core_sell_not_gated_at_zero_cash():
    uid = _make_user()
    try:
        _seed_balance(uid, "0.00")
        intent = OrderIntent(symbol="VTI", side=OrderSide.SELL, amount=Decimal("500"))
        async with async_session_maker() as session:
            outcome = await execute_approved_order(
                intent,
                broker=FakeBrokerAdapter(),
                broker_session=_live_session(),
                scope=Scope.for_user(uid),
                session=session,
            )
        assert outcome.status is OrderStatus.FILLED  # a SELL raises cash — never gated
    finally:
        _delete_user(uid)


@pytest.mark.asyncio
async def test_parked_sell_not_gated_at_zero_cash():
    """The 9-3 funding SELL of parked MM must not be blocked by the coverage gate."""
    uid = _make_user()
    try:
        _seed_balance(uid, "0.00")
        _seed_holding(uid, "SWVXX", "500", "500")
        _seed_config(uid, reserve_amount=None, parked_symbols=["SWVXX"])
        intent = OrderIntent(symbol="SWVXX", side=OrderSide.SELL, amount=Decimal("500"))
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


# --- AC5: known-cash enforcement boundaries ----------------------------------


@pytest.mark.asyncio
async def test_no_balance_row_does_not_block_buy():
    """A never-imported account (no balance row, as_of None) has no settled-cash
    truth to assert an overdraw against → the BUY is not blocked (pre-existing
    behavior; not reachable via the deploy path which needs a balance)."""
    uid = _make_user()
    try:
        broker = _SpyBroker()
        intent = OrderIntent(symbol="VTI", side=OrderSide.BUY, amount=Decimal("500"))
        async with async_session_maker() as session:
            outcome = await execute_approved_order(
                intent,
                broker=broker,
                broker_session=_live_session(),
                scope=Scope.for_user(uid),
                session=session,
            )
        assert outcome.status is OrderStatus.FILLED
        assert broker.place_calls == 1
    finally:
        _delete_user(uid)


@pytest.mark.asyncio
async def test_scopeless_buy_is_not_gated():
    """A scope-less direct engine/test call cannot read user cash → not gated (the
    production /approve path always supplies a user scope)."""
    broker = _SpyBroker()
    intent = OrderIntent(symbol="VTI", side=OrderSide.BUY, amount=Decimal("500"))
    outcome = await execute_approved_order(
        intent, broker=broker, broker_session=_live_session()
    )
    assert outcome.status is OrderStatus.FILLED
    assert broker.place_calls == 1


@pytest.mark.asyncio
async def test_non_finite_cash_fails_closed():
    """A present-but-corrupt (NaN) cached cash is not trustworthy → fail closed
    (available treated as 0) → a BUY is refused."""
    uid = _make_user()
    try:
        _seed_balance(uid, "NaN")
        broker = _SpyBroker()
        intent = OrderIntent(symbol="VTI", side=OrderSide.BUY, amount=Decimal("500"))
        async with async_session_maker() as session:
            with pytest.raises(InsufficientSettledCashError):
                await execute_approved_order(
                    intent,
                    broker=broker,
                    broker_session=_live_session(),
                    scope=Scope.for_user(uid),
                    session=session,
                )
        assert broker.place_calls == 0
    finally:
        _delete_user(uid)


# --- AC8: per-user scope isolation -------------------------------------------


@pytest.mark.asyncio
async def test_coverage_reads_only_callers_own_cash():
    """User A's tiny cash gates A's buy even when a rich user B exists — the gate
    reads ONLY the caller's own scoped balance (AD-10)."""
    a = _make_user()
    b = _make_user()
    try:
        _seed_balance(a, "100.00")
        _seed_balance(b, "100000.00")
        broker = _SpyBroker()
        intent = OrderIntent(symbol="VTI", side=OrderSide.BUY, amount=Decimal("500"))
        async with async_session_maker() as session:
            with pytest.raises(InsufficientSettledCashError):
                await execute_approved_order(
                    intent,
                    broker=broker,
                    broker_session=_live_session(),
                    scope=Scope.for_user(a),
                    session=session,
                )
        assert broker.place_calls == 0
    finally:
        _delete_user(a)
        _delete_user(b)


# --- Regression: the flipped 10-8 repro at EXECUTION -------------------------


@pytest.mark.asyncio
async def test_ten_eight_repro_deployable_on_paper_is_refused_at_execution():
    """MasterB's real setup: analysis says 'deploy $65,949' (settlement+parked-reserve),
    but co-signing the primary deploy BUY is REFUSED at execution — proving Phase-1
    analysis (deployable) and Phase-2 execution safety (placeable) are now consistent:
    money-market must be sold + settled first (via the existing 9-3 liquidation)."""
    uid = _make_user()
    try:
        _seed_balance(uid, "12182.82")
        _seed_holding(uid, "SWVXX", "93766.26", "93766.26")
        _seed_config(uid, reserve_amount="40000", parked_symbols=["SWVXX"])
        broker = _SpyBroker()
        intent = OrderIntent(symbol="VTI", side=OrderSide.BUY, amount=Decimal("65949.08"))
        async with async_session_maker() as session:
            with pytest.raises(InsufficientSettledCashError):
                await execute_approved_order(
                    intent,
                    broker=broker,
                    broker_session=_live_session(),
                    scope=Scope.for_user(uid),
                    session=session,
                )
        assert broker.place_calls == 0
    finally:
        _delete_user(uid)
