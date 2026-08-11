"""Story 9.3 tests — the just-in-time liquidation PLANNER (deterministic, offline).

Locks :func:`cash.liquidation.plan_liquidation` + :func:`narrate_liquidation`
against the I/O & Edge-Case Matrix, entirely OFFLINE (fake LLM, no network, no
live quote, no broker). Each test seeds the user's cached portfolio
(``portfolio_balance`` for ready-to-trade cash + ``portfolio_cache`` holdings) and
their ``cash_config`` via sync psycopg (so it works under the TestClient loop),
then calls the planner directly under a real user scope against the docker
Postgres.

Matrix rows covered: sufficient cash (no liquidation); coverable shortfall
(largest parked, amount≈shortfall); partial coverage (parked above reserve <
shortfall); no parked / all reserved (nothing to liquidate); reserve-aware
exclusion (the reserve is never in liquidatable value); determinism; fixed-point
JSON on the pre-filled intent; ``as_of`` carried.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio

from cash.liquidation import narrate_liquidation, plan_liquidation
from coach.recommendation import OrderSide, OrderType
from db.connection import get_connection
from db.models import CashConfig, PortfolioBalance, PortfolioCache
from db.scope import Scope
from db.session import async_session_maker, engine
from llm.fake_adapter import FakeLLMGateway
from llm.port import LLMGateway, LLMResponse

AS_OF = datetime(2026, 7, 27, tzinfo=timezone.utc)


@pytest_asyncio.fixture(autouse=True)
async def ensure_tables():
    async with engine.begin() as conn:
        await conn.run_sync(PortfolioCache.__table__.create, checkfirst=True)
        await conn.run_sync(PortfolioBalance.__table__.create, checkfirst=True)
        await conn.run_sync(CashConfig.__table__.create, checkfirst=True)
    yield


def _make_user() -> uuid.UUID:
    uid = uuid.uuid4()
    email = f"liq-plan-{uid.hex}@example.com"
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
    uid: uuid.UUID, symbol: str, quantity: str, market_value: str, as_of: datetime = AS_OF
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


def _seed_config(
    uid: uuid.UUID,
    *,
    reserve_amount: str | None,
    reserve_decided: bool,
    parked_symbols: list[str],
) -> None:
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
                    reserve_decided,
                    json.dumps(parked_symbols),
                    now,
                    now,
                ),
            )
        conn.commit()


async def _plan(uid: uuid.UUID, *, symbol: str, amount: str):
    scope = Scope.for_user(uid)
    from coach.recommendation import OrderIntent

    buy_intent = OrderIntent(symbol=symbol, side=OrderSide.BUY, amount=Decimal(amount))
    async with async_session_maker() as session:
        return await plan_liquidation(
            scope,
            session,
            buy_symbol=symbol,
            buy_amount=Decimal(amount),
            buy_intent=buy_intent,
            gateway=FakeLLMGateway(),
        )


# --- Matrix rows -------------------------------------------------------------


@pytest.mark.asyncio
async def test_sufficient_cash_no_liquidation():
    uid = _make_user()
    try:
        _seed_balance(uid, "1000.00")
        _seed_config(
            uid, reserve_amount=None, reserve_decided=False, parked_symbols=["SWVXX"]
        )
        plan = await _plan(uid, symbol="VTI", amount="500.00")
        assert plan.needs_liquidation is False
        assert plan.coverable is True
        assert plan.sell_symbol is None
        assert plan.sell_order_intent is None
        assert plan.shortfall == Decimal("0.00")
        assert plan.ready_to_trade == Decimal("1000.00")
        assert plan.as_of == AS_OF
    finally:
        _delete_user(uid)


@pytest.mark.asyncio
async def test_shortfall_parked_covers_it():
    uid = _make_user()
    try:
        _seed_balance(uid, "200.00")  # ready-to-trade
        _seed_holding(uid, "SWVXX", "5000", "5000.00")  # parked, $1 NAV
        _seed_config(
            uid, reserve_amount="0", reserve_decided=True, parked_symbols=["SWVXX"]
        )
        plan = await _plan(uid, symbol="VTI", amount="700.00")
        assert plan.needs_liquidation is True
        assert plan.coverable is True
        assert plan.shortfall == Decimal("500.00")  # 700 - 200
        assert plan.sell_symbol == "SWVXX"
        assert plan.sell_amount == Decimal("500.00")  # sized to the shortfall
        # NAV $1 → 500 whole shares.
        assert plan.est_shares == 500
        intent = plan.sell_order_intent
        assert intent is not None
        assert intent.side is OrderSide.SELL
        assert intent.order_type is OrderType.MARKET
        assert intent.amount == Decimal("500.00")
    finally:
        _delete_user(uid)


@pytest.mark.asyncio
async def test_partial_coverage_parked_below_shortfall():
    uid = _make_user()
    try:
        _seed_balance(uid, "100.00")
        _seed_holding(uid, "SWVXX", "300", "300.00")  # only $300 parked
        _seed_config(
            uid, reserve_amount="0", reserve_decided=True, parked_symbols=["SWVXX"]
        )
        plan = await _plan(uid, symbol="VTI", amount="1000.00")
        assert plan.needs_liquidation is True
        assert plan.coverable is False  # available parked (300) < shortfall (900)
        assert plan.shortfall == Decimal("900.00")
        assert plan.sell_symbol == "SWVXX"
        # Sized to the whole parked holding (can't cover the full shortfall).
        assert plan.sell_amount == Decimal("300.00")
        assert plan.sell_order_intent is not None
    finally:
        _delete_user(uid)


@pytest.mark.asyncio
async def test_buy_symbol_excluded_from_sell_candidates():
    """If the user tags their BUY target as parked, the planner must not propose
    selling the very fund they're buying (Story 9.3 hardening) — it picks the next
    parked holding instead of the (larger) buy target."""
    uid = _make_user()
    try:
        _seed_balance(uid, "200.00")  # ready-to-trade
        _seed_holding(uid, "SWVXX", "5000", "5000.00")  # parked + the buy target
        _seed_holding(uid, "VMFXX", "400", "400.00")  # another parked fund
        _seed_config(
            uid,
            reserve_amount="0",
            reserve_decided=True,
            parked_symbols=["SWVXX", "VMFXX"],
        )
        # Buying SWVXX: it must be excluded from sell candidates even though it's
        # the largest parked holding — VMFXX is chosen instead of "sell SWVXX to
        # buy SWVXX".
        plan = await _plan(uid, symbol="SWVXX", amount="700.00")
        assert plan.needs_liquidation is True
        assert plan.sell_symbol == "VMFXX"
    finally:
        _delete_user(uid)


@pytest.mark.asyncio
async def test_buy_symbol_only_parked_holding_nothing_sellable():
    """When the buy target is the ONLY parked holding, excluding it leaves nothing
    sellable — honestly coverable=False with no sell."""
    uid = _make_user()
    try:
        _seed_balance(uid, "200.00")
        _seed_holding(uid, "SWVXX", "5000", "5000.00")
        _seed_config(
            uid, reserve_amount="0", reserve_decided=True, parked_symbols=["SWVXX"]
        )
        plan = await _plan(uid, symbol="SWVXX", amount="700.00")
        assert plan.needs_liquidation is True
        assert plan.coverable is False
        assert plan.sell_symbol is None
    finally:
        _delete_user(uid)


@pytest.mark.asyncio
async def test_no_parked_nothing_to_liquidate():
    uid = _make_user()
    try:
        _seed_balance(uid, "100.00")
        # A holding exists but it is NOT declared parked.
        _seed_holding(uid, "VTI", "1", "300.00")
        _seed_config(
            uid, reserve_amount=None, reserve_decided=False, parked_symbols=[]
        )
        plan = await _plan(uid, symbol="VTI", amount="500.00")
        assert plan.needs_liquidation is True
        assert plan.coverable is False
        assert plan.sell_symbol is None
        assert plan.sell_amount is None
        assert plan.sell_order_intent is None
        # A calm "nothing to liquidate" narration still comes back.
        assert isinstance(plan.reasoning, str) and plan.reasoning.strip()
    finally:
        _delete_user(uid)


@pytest.mark.asyncio
async def test_reserve_is_never_liquidated():
    uid = _make_user()
    try:
        _seed_balance(uid, "100.00")
        _seed_holding(uid, "SWVXX", "5000", "5000.00")  # $5000 parked
        # Reserve of 4800 → only $200 is liquidatable above the reserve.
        _seed_config(
            uid, reserve_amount="4800", reserve_decided=True, parked_symbols=["SWVXX"]
        )
        plan = await _plan(uid, symbol="VTI", amount="700.00")
        assert plan.needs_liquidation is True
        assert plan.shortfall == Decimal("600.00")  # 700 - 100
        # available parked = 5000 - 4800 = 200 < 600 → not coverable.
        assert plan.coverable is False
        assert plan.sell_symbol == "SWVXX"
        # The sell never exceeds the reserve-aware headroom ($200).
        assert plan.sell_amount == Decimal("200.00")
        assert plan.reserved == Decimal("4800.00")
        assert plan.reserve_decided is True
    finally:
        _delete_user(uid)


@pytest.mark.asyncio
async def test_largest_parked_holding_selected():
    uid = _make_user()
    try:
        _seed_balance(uid, "0.00")
        _seed_holding(uid, "SWVXX", "100", "100.00")
        _seed_holding(uid, "VMFXX", "900", "900.00")  # larger parked
        _seed_config(
            uid,
            reserve_amount="0",
            reserve_decided=True,
            parked_symbols=["SWVXX", "VMFXX"],
        )
        plan = await _plan(uid, symbol="VTI", amount="400.00")
        assert plan.sell_symbol == "VMFXX"  # the largest-value parked holding
        assert plan.coverable is True  # 1000 available >= 400 shortfall
        assert plan.sell_amount == Decimal("400.00")
    finally:
        _delete_user(uid)


@pytest.mark.asyncio
async def test_coverable_reflects_single_holding_not_aggregate():
    # Honesty regression: v1 sells only the SINGLE largest parked holding, so
    # ``coverable`` must reflect what THAT one sell can free up — never the
    # aggregate across funds. Here two funds sum to $900 (> $600 shortfall) but no
    # single one covers it, so the plan is honestly partial (coverable=False).
    uid = _make_user()
    try:
        _seed_balance(uid, "0.00")
        _seed_holding(uid, "SWVXX", "500", "500.00")  # largest, still < shortfall
        _seed_holding(uid, "VMFXX", "400", "400.00")
        _seed_config(
            uid,
            reserve_amount="0",
            reserve_decided=True,
            parked_symbols=["SWVXX", "VMFXX"],
        )
        plan = await _plan(uid, symbol="VTI", amount="600.00")
        assert plan.needs_liquidation is True
        assert plan.shortfall == Decimal("600.00")
        assert plan.sell_symbol == "SWVXX"  # the single largest
        # Aggregate ($900) covers, but the single sell ($500) does NOT — honest.
        assert plan.coverable is False
        assert plan.sell_amount == Decimal("500.00")  # capped to the one holding
    finally:
        _delete_user(uid)


@pytest.mark.asyncio
async def test_determinism_same_inputs_same_plan():
    uid = _make_user()
    try:
        _seed_balance(uid, "200.00")
        _seed_holding(uid, "SWVXX", "5000", "5000.00")
        _seed_config(
            uid, reserve_amount="0", reserve_decided=True, parked_symbols=["SWVXX"]
        )
        p1 = await _plan(uid, symbol="VTI", amount="700.00")
        p2 = await _plan(uid, symbol="VTI", amount="700.00")
        assert p1.shortfall == p2.shortfall
        assert p1.sell_symbol == p2.sell_symbol
        assert p1.sell_amount == p2.sell_amount
        assert p1.est_shares == p2.est_shares
        assert p1.coverable == p2.coverable
    finally:
        _delete_user(uid)


# --- narrate_liquidation resilience ------------------------------------------


class _CrashingGateway(LLMGateway):
    provider = "crash"

    def complete(self, request):  # noqa: ANN001
        raise RuntimeError("gateway down")


def test_narrate_falls_back_on_gateway_crash():
    reasoning = narrate_liquidation(
        _CrashingGateway(),
        {
            "sell_symbol": "SWVXX",
            "sell_amount": Decimal("500.00"),
            "coverable": True,
            "shortfall": Decimal("500.00"),
            "reserved": Decimal("1000.00"),
        },
    )
    assert isinstance(reasoning, str) and reasoning.strip()
    # The deterministic fallback states the facts honestly + protects the reserve.
    assert "SWVXX" in reasoning
    assert "500.00" in reasoning
    assert "reserve" in reasoning.lower()


def test_narrate_returns_real_reasoning_from_fake_gateway():
    reasoning = narrate_liquidation(
        FakeLLMGateway(),
        {
            "sell_symbol": "SWVXX",
            "sell_amount": Decimal("500.00"),
            "coverable": True,
            "shortfall": Decimal("500.00"),
            "reserved": None,
        },
    )
    assert isinstance(reasoning, str) and reasoning.strip()


def test_narrate_stub_gateway_reasoning_is_used():
    """A gateway returning a distinct reasoning is passed through verbatim (the
    number is computed BEFORE narration, so narration is blind to the math)."""

    class _StubGateway(LLMGateway):
        provider = "stub"

        def complete(self, request):  # noqa: ANN001
            return LLMResponse(
                output={"reasoning": "STUB-REASONING-XYZ"},
                model="stub",
                provider="stub",
            )

    reasoning = narrate_liquidation(
        _StubGateway(),
        {
            "sell_symbol": "SWVXX",
            "sell_amount": Decimal("500.00"),
            "coverable": True,
            "shortfall": Decimal("500.00"),
            "reserved": None,
        },
    )
    assert reasoning == "STUB-REASONING-XYZ"
