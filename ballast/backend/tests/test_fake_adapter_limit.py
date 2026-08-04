"""Story 8.1 — the FakeBrokerAdapter marketable-LIMIT branch.

Deterministic, offline: ``FAKE_FILL_PRICE`` (``100.00``) is the reference quote
for both ask and bid. Covers a marketable buy/sell fill (floor sizing, fill at
the limit price, stable broker_ref, idempotent re-place), the non-marketable
refusals, the sub-share refusal, and — crucially — that the existing MARKET fill
is byte-for-byte UNCHANGED (AC 5: fractional, no flooring, no refusal).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from brokers.fake_adapter import FAKE_FILL_PRICE, FakeBrokerAdapter
from brokers.port import OrderNotPlaceableError, OrderStatus
from coach.recommendation import OrderIntent, OrderSide, OrderType


def _limit(side, amount, limit_price) -> OrderIntent:
    return OrderIntent(
        symbol="VTI",
        side=side,
        amount=Decimal(amount),
        order_type=OrderType.LIMIT,
        limit_price=Decimal(limit_price),
    )


@pytest.mark.asyncio
async def test_marketable_buy_limit_fills_floor_sized_at_limit_price():
    adapter = FakeBrokerAdapter()
    # limit 125 >= 100 reference → marketable; floor(500 / 125) = 4 shares.
    outcome = await adapter.place_order(
        _limit(OrderSide.BUY, "500", "125"), idempotency_key="k1"
    )
    assert outcome.status is OrderStatus.FILLED
    assert outcome.filled_qty == Decimal("4")
    assert outcome.avg_price == Decimal("125")
    assert outcome.broker_ref == "fake-order-k1"


@pytest.mark.asyncio
async def test_marketable_sell_limit_fills():
    adapter = FakeBrokerAdapter()
    # SELL limit 80 <= 100 reference → marketable; floor(500 / 80) = 6 shares.
    outcome = await adapter.place_order(
        _limit(OrderSide.SELL, "500", "80"), idempotency_key="k2"
    )
    assert outcome.status is OrderStatus.FILLED
    assert outcome.filled_qty == Decimal("6")
    assert outcome.avg_price == Decimal("80")


@pytest.mark.asyncio
async def test_marketable_limit_reprice_is_idempotent():
    adapter = FakeBrokerAdapter()
    first = await adapter.place_order(
        _limit(OrderSide.BUY, "500", "125"), idempotency_key="dup"
    )
    second = await adapter.place_order(
        _limit(OrderSide.BUY, "500", "125"), idempotency_key="dup"
    )
    assert first == second  # frozen outcome, recorded once
    assert len(adapter._orders) == 1


@pytest.mark.asyncio
async def test_non_marketable_buy_refuses_nothing_recorded():
    adapter = FakeBrokerAdapter()
    with pytest.raises(OrderNotPlaceableError, match="coming later"):
        await adapter.place_order(
            _limit(OrderSide.BUY, "500", "90"), idempotency_key="nm1"
        )
    assert adapter._orders == {}


@pytest.mark.asyncio
async def test_non_marketable_sell_refuses_nothing_recorded():
    adapter = FakeBrokerAdapter()
    with pytest.raises(OrderNotPlaceableError, match="coming later"):
        await adapter.place_order(
            _limit(OrderSide.SELL, "500", "110"), idempotency_key="nm2"
        )
    assert adapter._orders == {}


@pytest.mark.asyncio
async def test_sub_share_limit_refuses():
    adapter = FakeBrokerAdapter()
    # marketable (125 >= 100) but floor(100 / 125) = 0 → sub-share refusal.
    with pytest.raises(OrderNotPlaceableError):
        await adapter.place_order(
            _limit(OrderSide.BUY, "100", "125"), idempotency_key="ss1"
        )
    assert adapter._orders == {}


@pytest.mark.asyncio
async def test_market_fill_unchanged_fractional_no_floor():
    # AC 5 regression: the MARKET path stays fractional with NO flooring and NO
    # sub-share refusal — byte-for-byte unchanged from pre-8.1.
    adapter = FakeBrokerAdapter()
    outcome = await adapter.place_order(
        OrderIntent(symbol="VTI", side=OrderSide.BUY, amount=Decimal("250")),
        idempotency_key="m1",
    )
    assert outcome.status is OrderStatus.FILLED
    assert outcome.filled_qty == Decimal("250") / FAKE_FILL_PRICE  # 2.5, fractional
    assert outcome.avg_price == FAKE_FILL_PRICE
    assert outcome.broker_ref == "fake-order-m1"
