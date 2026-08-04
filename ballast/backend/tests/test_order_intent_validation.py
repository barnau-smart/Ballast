"""Story 8.1 — the order model (enums + extended ``OrderIntent``) and the pure
execution-gate validator ``coach.execution.validate_order_intent``.

Covers the field-requirement matrix: the enum string values, the backward-compatible
``OrderIntent`` defaults (existing 3-arg construction still valid), and every
row of the gate contract — a deferred feature (stop/stop_limit/am/pm/gtc) raises
``OrderNotSupportedError``; a field-shape violation raises ``OrderScopeError``; a
clean MARKET and a clean marketable LIMIT pass. No I/O, no broker.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from coach.execution import (
    OrderNotSupportedError,
    OrderScopeError,
    validate_order_intent,
)
from coach.recommendation import (
    Duration,
    OrderIntent,
    OrderSide,
    OrderType,
    Session,
)


# --- Enum values + OrderIntent defaults --------------------------------------


def test_enum_values_are_lowercase_strings():
    assert OrderType.MARKET.value == "market"
    assert OrderType.LIMIT.value == "limit"
    assert OrderType.STOP.value == "stop"
    assert OrderType.STOP_LIMIT.value == "stop_limit"
    assert Session.REGULAR.value == "regular"
    assert Session.AM.value == "am"
    assert Session.PM.value == "pm"
    assert Duration.DAY.value == "day"
    assert Duration.GTC.value == "gtc"
    # str-backed (like OrderSide) so they serialize cleanly into JSON snapshots.
    assert isinstance(OrderType.LIMIT, str)


def test_order_intent_defaults_are_market_regular_day():
    # Existing 3-arg construction (positional) STILL works and defaults to a
    # plain MARKET order — every pre-8.1 call site stays valid.
    intent = OrderIntent("VTI", OrderSide.BUY, Decimal("500"))
    assert intent.order_type is OrderType.MARKET
    assert intent.limit_price is None
    assert intent.stop_price is None
    assert intent.session is Session.REGULAR
    assert intent.duration is Duration.DAY


def test_order_intent_keyword_limit_construction():
    intent = OrderIntent(
        symbol="VOO",
        side=OrderSide.BUY,
        amount=Decimal("500"),
        order_type=OrderType.LIMIT,
        limit_price=Decimal("100.00"),
    )
    assert intent.order_type is OrderType.LIMIT
    assert intent.limit_price == Decimal("100.00")


# --- validate_order_intent: passing cases ------------------------------------


def test_clean_market_passes():
    validate_order_intent(OrderIntent("VTI", OrderSide.BUY, Decimal("500")))


def test_clean_limit_passes():
    validate_order_intent(
        OrderIntent(
            "VTI",
            OrderSide.BUY,
            Decimal("500"),
            order_type=OrderType.LIMIT,
            limit_price=Decimal("100.00"),
        )
    )


# --- validate_order_intent: field-shape violations → OrderScopeError ---------


def test_market_with_limit_price_is_scope_error():
    with pytest.raises(OrderScopeError):
        validate_order_intent(
            OrderIntent(
                "VTI",
                OrderSide.BUY,
                Decimal("500"),
                limit_price=Decimal("100.00"),
            )
        )


def test_market_with_stop_price_is_scope_error():
    with pytest.raises(OrderScopeError):
        validate_order_intent(
            OrderIntent(
                "VTI",
                OrderSide.BUY,
                Decimal("500"),
                stop_price=Decimal("90.00"),
            )
        )


@pytest.mark.parametrize("bad_price", [None, Decimal("0"), Decimal("-5")])
def test_limit_without_positive_price_is_scope_error(bad_price):
    with pytest.raises(OrderScopeError):
        validate_order_intent(
            OrderIntent(
                "VTI",
                OrderSide.BUY,
                Decimal("500"),
                order_type=OrderType.LIMIT,
                limit_price=bad_price,
            )
        )


def test_limit_with_nonfinite_price_is_scope_error():
    with pytest.raises(OrderScopeError):
        validate_order_intent(
            OrderIntent(
                "VTI",
                OrderSide.BUY,
                Decimal("500"),
                order_type=OrderType.LIMIT,
                limit_price=Decimal("NaN"),
            )
        )


def test_limit_with_stop_price_is_scope_error():
    with pytest.raises(OrderScopeError):
        validate_order_intent(
            OrderIntent(
                "VTI",
                OrderSide.BUY,
                Decimal("500"),
                order_type=OrderType.LIMIT,
                limit_price=Decimal("100.00"),
                stop_price=Decimal("90.00"),
            )
        )


# --- validate_order_intent: deferred features → OrderNotSupportedError --------


@pytest.mark.parametrize("order_type", [OrderType.STOP, OrderType.STOP_LIMIT])
def test_stop_types_not_supported(order_type):
    with pytest.raises(OrderNotSupportedError):
        validate_order_intent(
            OrderIntent(
                "VTI",
                OrderSide.BUY,
                Decimal("500"),
                order_type=order_type,
                stop_price=Decimal("90.00"),
            )
        )


@pytest.mark.parametrize("session", [Session.AM, Session.PM])
def test_extended_sessions_not_supported(session):
    with pytest.raises(OrderNotSupportedError):
        validate_order_intent(
            OrderIntent(
                "VTI",
                OrderSide.BUY,
                Decimal("500"),
                session=session,
            )
        )


def test_gtc_market_is_scope_error():
    # (PATCH 2) Story 8.2: GTC is coupled to LIMIT. GTC is meaningless on a MARKET
    # order (it fills immediately; the schwab adapter only applies GOOD_TILL_CANCEL
    # in its LIMIT branch, so a GTC MARKET would be placed as DAY — intent/placed
    # order diverge). A GTC MARKET intent is a field-shape violation → OrderScopeError.
    with pytest.raises(OrderScopeError):
        validate_order_intent(
            OrderIntent(
                "VTI",
                OrderSide.BUY,
                Decimal("500"),
                duration=Duration.GTC,
            )
        )


def test_gtc_limit_accepted():
    # A GTC LIMIT (the primary resting-order shape) passes the gate.
    validate_order_intent(
        OrderIntent(
            "VTI",
            OrderSide.BUY,
            Decimal("500"),
            order_type=OrderType.LIMIT,
            limit_price=Decimal("90.00"),
            duration=Duration.GTC,
        )
    )
