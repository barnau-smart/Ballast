"""Story 6.4 tests — the shared fixed-point money serializer (``money.py``).

Pure unit tests (no DB, no network): walk the I/O & Edge-Case Matrix for
:func:`money.format_money` — extreme-large and tiny-fractional Decimals render as
plain fixed-point (never ``E+``/``E-`` exponent, never binary float), decimal
scale is preserved, and every emitted string round-trips: ``Decimal(s) == x``.
Also proves the reusable ``WireMoney`` Pydantic annotated type serializes a
``Decimal`` field fixed-point and passes ``None`` through on JSON output.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel

from money import WireMoney, format_money


def test_extreme_large_is_fixed_point_no_exponent():
    # str(Decimal("1E29")/Decimal("100")) is "1E+27" — format_money must not be.
    value = Decimal("1E29") / Decimal("100")
    out = format_money(value)
    assert out == "1000000000000000000000000000"
    assert "E" not in out and "e" not in out


def test_tiny_fractional_is_fixed_point_no_exponent():
    out = format_money(Decimal("1E-8"))
    assert out == "0.00000001"
    assert "E" not in out and "e" not in out


def test_normal_money_preserves_scale():
    assert format_money(Decimal("100.00")) == "100.00"


def test_negative_value():
    out = format_money(Decimal("-1234.56"))
    assert out == "-1234.56"
    assert "E" not in out


def test_zero_value():
    assert format_money(Decimal("0")) == "0"
    assert format_money(Decimal("0.00")) == "0.00"


def test_round_trips_through_decimal():
    for value in (
        Decimal("1E29") / Decimal("100"),
        Decimal("1E-8"),
        Decimal("100.00"),
        Decimal("-1234.56"),
        Decimal("0"),
        Decimal("0.00"),
    ):
        assert Decimal(format_money(value)) == value


# --- The reusable Pydantic WireMoney type ------------------------------------


class _Model(BaseModel):
    amount: WireMoney
    optional: WireMoney | None = None


def test_wire_money_serializes_decimal_fixed_point():
    m = _Model(amount=Decimal("1E29") / Decimal("100"), optional=Decimal("1E-8"))
    dumped = m.model_dump(mode="json")
    assert dumped["amount"] == "1000000000000000000000000000"
    assert dumped["optional"] == "0.00000001"
    assert "E" not in dumped["amount"] and "E" not in dumped["optional"]
    # Round-trips through the documented Decimal(str(...)) consumer.
    assert Decimal(dumped["amount"]) == Decimal("1E29") / Decimal("100")


def test_wire_money_passes_none_through():
    m = _Model(amount=Decimal("100.00"))
    dumped = m.model_dump(mode="json")
    assert dumped["optional"] is None
    assert dumped["amount"] == "100.00"
