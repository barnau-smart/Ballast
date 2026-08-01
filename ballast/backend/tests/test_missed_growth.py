"""Story 3.4 tests — the deterministic ``estimate_missed_growth`` engine function.

REAL DB (no mocks), mirroring ``test_precedent.py``: an autouse table-creation
fixture, per-test cleanup via sync psycopg, and TEST-prefixed symbols so these
DB-writing tests never collide with a real ingest over the shared GLOBAL
``market_daily`` table. NO network, NO LLM, NO credentials.

We insert crafted, fully deterministic ``adj_close`` series over a full
``LOOKBACK_TRADING_DAYS + 1`` window so every asserted ``Decimal`` is pinned, then
walk the I/O & Edge-Case Matrix on the engine function: rising window → positive
``forgone_growth`` == ``cash × window_return``; falling window → negative (loss
avoided); ``< lookback`` bars → ``insufficient_history``; ``idle_cash <= 0`` →
``no_idle_cash`` with forgone ``0.00``.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from db.connection import get_connection
from db.models import MarketDaily
from db.session import async_session_maker, engine
from precedent import MissedGrowthEstimate, estimate_missed_growth
from precedent.missed_growth import LOOKBACK_TRADING_DAYS

import pytest_asyncio

# TEST-prefixed symbols — safe against the shared global table.
SYM_RISING = "TEST_MG_RISING"
SYM_FALLING = "TEST_MG_FALLING"
SYM_SHORT = "TEST_MG_SHORT"
SYM_ABSENT = "TEST_MG_ABSENT"
SYM_FLAT = "TEST_MG_FLAT"
SYM_BOUNDARY = "TEST_MG_BOUNDARY"

ALL_TEST_SYMBOLS = [
    SYM_RISING,
    SYM_FALLING,
    SYM_SHORT,
    SYM_ABSENT,
    SYM_FLAT,
    SYM_BOUNDARY,
]

BASE_DAY = date(2015, 1, 1)


@pytest_asyncio.fixture(autouse=True)
async def ensure_table():
    """Ensure the market_daily table exists for these real-DB tests."""
    async with engine.begin() as conn:
        await conn.run_sync(MarketDaily.__table__.create, checkfirst=True)
    yield


def _clean(symbols: list[str]) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM market_daily WHERE symbol = ANY(%s)", (symbols,))
        conn.commit()


def _insert_series(symbol: str, closes: list[Decimal]) -> None:
    """Insert a deterministic series: one bar/day, adj_close = close = value."""
    ingested_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    with get_connection() as conn:
        with conn.cursor() as cur:
            for i, v in enumerate(closes):
                day = BASE_DAY + timedelta(days=i)
                cur.execute(
                    "INSERT INTO market_daily "
                    "(id, symbol, day, open, high, low, close, adj_close, "
                    " volume, source, ingested_at) "
                    "VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s, %s, "
                    "        %s, %s, %s)",
                    (symbol, day, v, v, v, v, v, 1000, "test", ingested_at),
                )
        conn.commit()


def _rising_closes() -> list[Decimal]:
    """253 bars: start 100.00, end 114.00 → exactly +14% over the trailing window.

    ``LOOKBACK_TRADING_DAYS + 1`` bars means ``start`` = the first bar (100.00) and
    ``end`` = the last bar (114.00); ``window_return = 14/100 = 0.14`` exactly.
    Intermediate values are irrelevant to the endpoint math (only first + last
    matter), so we interpolate a gentle monotonic climb for realism.
    """
    n = LOOKBACK_TRADING_DAYS + 1  # 253
    closes = [Decimal("100.00")]
    for i in range(1, n - 1):
        closes.append(Decimal("100.00") + Decimal("14.00") * Decimal(i) / Decimal(n - 1))
    closes.append(Decimal("114.00"))
    return closes


def _falling_closes() -> list[Decimal]:
    """253 bars: start 100.00, end 90.00 → exactly −10% over the trailing window."""
    n = LOOKBACK_TRADING_DAYS + 1  # 253
    closes = [Decimal("100.00")]
    for i in range(1, n - 1):
        closes.append(Decimal("100.00") - Decimal("10.00") * Decimal(i) / Decimal(n - 1))
    closes.append(Decimal("90.00"))
    return closes


# --- Idle cash + rising market -----------------------------------------------


@pytest.mark.asyncio
async def test_rising_window_positive_forgone_growth_matches_cash_times_return():
    _clean([SYM_RISING])
    _insert_series(SYM_RISING, _rising_closes())
    try:
        cash = Decimal("25000.00")
        async with async_session_maker() as session:
            est = await estimate_missed_growth(session, idle_cash=cash, symbol=SYM_RISING)
        assert isinstance(est, MissedGrowthEstimate)
        assert est.sufficient is True
        assert est.reason is None
        # +14% over the trailing window, pinned by the crafted first/last bars.
        assert est.window_return == Decimal("0.1400")
        # forgone_growth == cash × window_return, quantized to cents.
        assert est.forgone_growth == Decimal("3500.00")
        assert est.forgone_growth == (cash * est.window_return).quantize(Decimal("0.01"))
        assert est.idle_cash == Decimal("25000.00")
        assert est.benchmark == SYM_RISING
        assert est.trading_days == LOOKBACK_TRADING_DAYS
        assert est.window_start == BASE_DAY
        assert est.window_end == BASE_DAY + timedelta(days=LOOKBACK_TRADING_DAYS)
        assert est.as_of == est.window_end
        assert "growth" in est.statement.lower()
        assert est.source == f"{SYM_RISING} daily close (market_daily)"
        # Decimals are Decimal (never float).
        assert isinstance(est.window_return, Decimal)
        assert isinstance(est.forgone_growth, Decimal)
    finally:
        _clean([SYM_RISING])


# --- Idle cash + falling market (loss avoided, never a "cost") ---------------


@pytest.mark.asyncio
async def test_falling_window_negative_forgone_growth_is_loss_avoided():
    _clean([SYM_FALLING])
    _insert_series(SYM_FALLING, _falling_closes())
    try:
        cash = Decimal("10000.00")
        async with async_session_maker() as session:
            est = await estimate_missed_growth(session, idle_cash=cash, symbol=SYM_FALLING)
        assert est.sufficient is True
        assert est.reason is None
        # −10% over the window → negative return and negative forgone figure.
        assert est.window_return == Decimal("-0.1000")
        assert est.forgone_growth == Decimal("-1000.00")
        assert est.forgone_growth < 0
        # Honest framing: an AVOIDED loss, never a "cost" of holding cash.
        stmt = est.statement.lower()
        assert "avoided" in stmt
        assert "cost" not in stmt
    finally:
        _clean([SYM_FALLING])


# --- Flat market (a non-event must never read as missed growth) --------------


@pytest.mark.asyncio
async def test_flat_window_is_neutral_not_missed_growth():
    """A flat window (0% return → $0.00 forgone) must read as a calm non-event,
    never a green "sat out ~$0.00 of growth" overstatement."""
    _clean([SYM_FLAT])
    # Identical closes across the whole window → exactly 0% return.
    _insert_series(SYM_FLAT, [Decimal("100.00")] * (LOOKBACK_TRADING_DAYS + 1))
    try:
        async with async_session_maker() as session:
            est = await estimate_missed_growth(
                session, idle_cash=Decimal("25000.00"), symbol=SYM_FLAT
            )
        assert est.sufficient is True
        assert est.reason is None
        assert est.window_return == Decimal("0.0000")
        assert est.forgone_growth == Decimal("0.00")
        stmt = est.statement.lower()
        # Neutral framing — not the "sat out ... of growth" (missed-gain) copy.
        assert "flat" in stmt or "not missed" in stmt
        assert "sat out" not in stmt
    finally:
        _clean([SYM_FLAT])


# --- Sufficiency boundary (exactly lookback + 1 bars is enough) --------------


@pytest.mark.asyncio
async def test_exactly_lookback_plus_one_bars_is_sufficient():
    """Pins the off-by-one: ``lookback + 1`` bars is the minimum sufficient window
    (its sibling ``test_fewer_than_lookback_plus_one_bars`` covers one fewer)."""
    _clean([SYM_BOUNDARY])
    _insert_series(SYM_BOUNDARY, _rising_closes())  # exactly LOOKBACK + 1 bars
    try:
        async with async_session_maker() as session:
            est = await estimate_missed_growth(
                session, idle_cash=Decimal("1000.00"), symbol=SYM_BOUNDARY
            )
        assert est.sufficient is True
        assert est.reason is None
        assert est.window_return == Decimal("0.1400")
    finally:
        _clean([SYM_BOUNDARY])


# --- Insufficient history ----------------------------------------------------


@pytest.mark.asyncio
async def test_fewer_than_lookback_plus_one_bars_is_insufficient_history():
    _clean([SYM_SHORT])
    # Exactly lookback bars (one short of lookback+1) → insufficient.
    _insert_series(SYM_SHORT, [Decimal("100.00")] * LOOKBACK_TRADING_DAYS)
    try:
        async with async_session_maker() as session:
            est = await estimate_missed_growth(
                session, idle_cash=Decimal("5000.00"), symbol=SYM_SHORT
            )
        assert est.sufficient is False
        assert est.reason == "insufficient_history"
        assert est.window_return is None
        assert est.window_start is None
        assert est.forgone_growth == Decimal("0.00")
        # Idle cash is still echoed; framing is informational, never an error.
        assert est.idle_cash == Decimal("5000.00")
        assert "not enough" in est.statement.lower()
    finally:
        _clean([SYM_SHORT])


@pytest.mark.asyncio
async def test_absent_symbol_is_insufficient_history_no_crash():
    _clean([SYM_ABSENT])
    async with async_session_maker() as session:
        est = await estimate_missed_growth(
            session, idle_cash=Decimal("5000.00"), symbol=SYM_ABSENT
        )
    assert est.sufficient is False
    assert est.reason == "insufficient_history"
    assert est.forgone_growth == Decimal("0.00")
    assert est.window_return is None


# --- No idle cash ------------------------------------------------------------


@pytest.mark.asyncio
async def test_zero_idle_cash_is_no_idle_cash_with_forgone_zero():
    _clean([SYM_RISING])
    _insert_series(SYM_RISING, _rising_closes())
    try:
        async with async_session_maker() as session:
            est = await estimate_missed_growth(
                session, idle_cash=Decimal("0"), symbol=SYM_RISING
            )
        assert est.sufficient is True
        assert est.reason == "no_idle_cash"
        assert est.forgone_growth == Decimal("0.00")
        assert est.window_return is None
        assert est.idle_cash == Decimal("0.00")
        assert "no idle cash" in est.statement.lower()
    finally:
        _clean([SYM_RISING])


@pytest.mark.asyncio
async def test_negative_idle_cash_is_no_idle_cash():
    """A defensive non-positive cash read degrades to the calm no-idle state."""
    async with async_session_maker() as session:
        est = await estimate_missed_growth(session, idle_cash=Decimal("-50.00"))
    assert est.reason == "no_idle_cash"
    assert est.forgone_growth == Decimal("0.00")


# --- Fixed-point money serialization (Story 6.4, no DB) ----------------------


def test_to_dict_money_fields_are_fixed_point_no_exponent():
    """`MissedGrowthEstimate.to_dict()` money fields render fixed-point (no E+/E-)
    for extreme-large/tiny values; None optional stays None; round-trips."""
    big = Decimal("1E29") / Decimal("100")  # str() → "1E+27"
    tiny = Decimal("1E-8")  # str() → "1E-8"
    est = MissedGrowthEstimate(
        idle_cash=big,
        benchmark="TEST",
        window_return=tiny,
        window_start=BASE_DAY,
        window_end=BASE_DAY,
        forgone_growth=big,
        trading_days=252,
        statement="",
        source="test",
        as_of=BASE_DAY,
        sufficient=True,
        reason=None,
    )
    d = est.to_dict()
    for key in ("idle_cash", "forgone_growth", "window_return"):
        assert "E" not in d[key] and "e" not in d[key], (key, d[key])
    assert d["idle_cash"] == "1000000000000000000000000000"
    assert d["forgone_growth"] == "1000000000000000000000000000"
    assert d["window_return"] == "0.00000001"
    assert Decimal(d["idle_cash"]) == big
    assert Decimal(d["window_return"]) == tiny
    # None-guarded optional stays None (never "None").
    none_est = MissedGrowthEstimate(
        idle_cash=Decimal("0.00"),
        benchmark="TEST",
        window_return=None,
        window_start=None,
        window_end=None,
        forgone_growth=Decimal("0.00"),
        trading_days=0,
        statement="",
        source="test",
        as_of=None,
        sufficient=False,
        reason="no_idle_cash",
    )
    assert none_est.to_dict()["window_return"] is None


# --- Determinism + JSON-safety -----------------------------------------------


@pytest.mark.asyncio
async def test_estimate_is_deterministic_and_json_safe():
    _clean([SYM_RISING])
    _insert_series(SYM_RISING, _rising_closes())
    try:
        cash = Decimal("25000.00")
        async with async_session_maker() as session:
            first = await estimate_missed_growth(session, idle_cash=cash, symbol=SYM_RISING)
        async with async_session_maker() as session:
            second = await estimate_missed_growth(session, idle_cash=cash, symbol=SYM_RISING)
        # Same market data + same cash → byte-identical dict (determinism).
        assert first.to_dict() == second.to_dict()

        d = first.to_dict()
        # JSON-safe: Decimals rendered as strings, dates as ISO, round-trips.
        assert d["window_return"] == "0.1400"
        assert d["forgone_growth"] == "3500.00"
        assert d["idle_cash"] == "25000.00"
        assert d["window_start"] == "2015-01-01"
        assert d["sufficient"] is True
        assert d["reason"] is None
        assert isinstance(d["trading_days"], int)
        encoded = json.dumps(d)
        assert json.loads(encoded)["forgone_growth"] == "3500.00"
    finally:
        _clean([SYM_RISING])
