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
from brokers.port import OrderNotPlaceableError, OrderOutcome, OrderStatus
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
async def test_non_marketable_buy_rests_pending_stable_ref():
    # Story 8.2: a non-marketable BUY limit is NO LONGER refused — it is a
    # legitimate RESTING order that co-signs PENDING with a stable broker_ref
    # (never a phantom fill), recorded so the later reconcile-by-ref can find it.
    adapter = FakeBrokerAdapter()
    outcome = await adapter.place_order(
        _limit(OrderSide.BUY, "500", "90"), idempotency_key="nm1"
    )
    assert outcome.status is OrderStatus.PENDING
    assert outcome.filled_qty == Decimal("0")
    assert outcome.avg_price is None
    assert outcome.broker_ref == "fake-order-nm1"
    # Recorded under both the key and the ref (durable reconcile can find it).
    assert adapter._orders["nm1"] == outcome
    assert adapter._orders_by_ref["fake-order-nm1"] == outcome


@pytest.mark.asyncio
async def test_non_marketable_sell_rests_pending_stable_ref():
    # Story 8.2: a non-marketable SELL limit rests PENDING (never a phantom fill).
    adapter = FakeBrokerAdapter()
    outcome = await adapter.place_order(
        _limit(OrderSide.SELL, "500", "110"), idempotency_key="nm2"
    )
    assert outcome.status is OrderStatus.PENDING
    assert outcome.filled_qty == Decimal("0")
    assert outcome.broker_ref == "fake-order-nm2"


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


# --- Story 8.2: resting reconcile, cancel, and the zero-money seam proof ------


@pytest.mark.asyncio
async def test_resting_order_reconciles_to_seeded_terminal():
    # A resting limit co-signs PENDING; a later reconcile-by-ref (seeded, the
    # offline stand-in for "the broker now reports FILLED") resolves it truthfully.
    adapter = FakeBrokerAdapter()
    placed = await adapter.place_order(
        _limit(OrderSide.BUY, "500", "90"), idempotency_key="rest1"
    )
    assert placed.status is OrderStatus.PENDING
    filled = OrderOutcome(
        status=OrderStatus.FILLED,
        filled_qty=Decimal("5"),
        avg_price=Decimal("90"),
        broker_ref=placed.broker_ref,
    )
    adapter.seed_order_status_by_ref(placed.broker_ref, filled)
    read = await adapter.get_order_status_by_ref(placed.broker_ref)
    assert read.status is OrderStatus.FILLED
    assert read.filled_qty == Decimal("5")


@pytest.mark.asyncio
async def test_cancel_maps_to_rejected_and_reads_back_rejected():
    # Story 8.2: cancel a resting order → REJECTED (reusing the closed contract,
    # NOT a new member); the by-ref read-back now observes the cancelled state.
    adapter = FakeBrokerAdapter()
    placed = await adapter.place_order(
        _limit(OrderSide.BUY, "500", "90"), idempotency_key="c1"
    )
    cancelled = await adapter.cancel_order(placed.broker_ref)
    assert cancelled.status is OrderStatus.REJECTED
    assert cancelled.broker_ref == placed.broker_ref
    # A subsequent by-ref read observes the cancelled (rejected) state.
    read = await adapter.get_order_status_by_ref(placed.broker_ref)
    assert read.status is OrderStatus.REJECTED


@pytest.mark.asyncio
async def test_zero_money_seam_proof_place_rest_confirm_cancel_never_fills():
    # ZERO-MONEY SEAM PROOF (Story 8.2): this is the deterministic, offline
    # proof of the resting-order lifecycle PORT CONTRACT that costs NOTHING —
    # place a non-marketable resting limit, confirm it maps to PENDING (never a
    # phantom fill), cancel it, and assert it NEVER fills. It exercises the
    # FakeBrokerAdapter only (no credentials, no network, no money at risk); the
    # live Schwab DELETE + read-back path shares NO code with the fake's cancel
    # and is covered separately by the schwab_adapter cancel tests. This proves
    # the seam's SHAPE offline, not the live broker round-trip.
    adapter = FakeBrokerAdapter()
    placed = await adapter.place_order(
        _limit(OrderSide.BUY, "500", "80"), idempotency_key="seam"
    )
    # Confirm PENDING mapping — a resting order, never a fill.
    assert placed.status is OrderStatus.PENDING
    assert placed.filled_qty == Decimal("0")
    assert placed.avg_price is None
    # Cancel it → terminal REJECTED, not re-placeable.
    cancelled = await adapter.cancel_order(placed.broker_ref)
    assert cancelled.status is OrderStatus.REJECTED
    # It NEVER filled: the reconcile read reports the cancelled (rejected) state,
    # never a FILLED — no shares moved, no money spent.
    read = await adapter.get_order_status_by_ref(placed.broker_ref)
    assert read.status is OrderStatus.REJECTED
    assert read.status is not OrderStatus.FILLED
    assert read.filled_qty == Decimal("0")


@pytest.mark.asyncio
async def test_cancel_on_filled_ref_returns_filled_unchanged_no_clobber():
    # (PATCH 4) cancel_order on an already-FILLED ref must NOT clobber it with a
    # fresh rejection — it returns the FILLED outcome unchanged (filled_qty
    # preserved). Defense-in-depth: the cancel owner short-circuits a terminal
    # order, but a stray call here must never erase real filled shares.
    adapter = FakeBrokerAdapter()
    filled = OrderOutcome(
        status=OrderStatus.FILLED,
        filled_qty=Decimal("7"),
        avg_price=Decimal("100.00"),
        broker_ref="fake-order-f1",
    )
    adapter.seed_order_status_by_ref("fake-order-f1", filled)
    result = await adapter.cancel_order("fake-order-f1")
    assert result.status is OrderStatus.FILLED
    assert result.filled_qty == Decimal("7")
    assert result.avg_price == Decimal("100.00")
    # The store is untouched — a later read still observes FILLED.
    read = await adapter.get_order_status_by_ref("fake-order-f1")
    assert read.status is OrderStatus.FILLED
    assert read.filled_qty == Decimal("7")


@pytest.mark.asyncio
async def test_cancel_on_pending_or_unknown_ref_returns_rejected():
    # (PATCH 4) cancel_order on a pending/unknown ref becomes REJECTED (the normal
    # cancel path). An unknown ref (never placed/seeded) is non-terminal, so it is
    # cancelled to rejected.
    adapter = FakeBrokerAdapter()
    # A pending resting order placed on this adapter.
    placed = await adapter.place_order(
        _limit(OrderSide.BUY, "500", "80"), idempotency_key="p1"
    )
    assert placed.status is OrderStatus.PENDING
    cancelled = await adapter.cancel_order(placed.broker_ref)
    assert cancelled.status is OrderStatus.REJECTED
    # An entirely unknown ref (never placed/seeded) is also rejected.
    unknown = await adapter.cancel_order("fake-order-never-seen")
    assert unknown.status is OrderStatus.REJECTED


@pytest.mark.asyncio
async def test_cancel_twice_is_idempotent_same_rejected_outcome():
    # (PATCH 4) Calling cancel_order twice on the same ref is idempotent: the first
    # rejects it (terminal), the second sees the terminal REJECTED and returns it
    # unchanged (filled_qty still 0).
    adapter = FakeBrokerAdapter()
    placed = await adapter.place_order(
        _limit(OrderSide.BUY, "500", "80"), idempotency_key="i1"
    )
    first = await adapter.cancel_order(placed.broker_ref)
    assert first.status is OrderStatus.REJECTED
    assert first.filled_qty == Decimal("0")
    second = await adapter.cancel_order(placed.broker_ref)
    assert second.status is OrderStatus.REJECTED
    assert second.filled_qty == Decimal("0")
    assert second == first
