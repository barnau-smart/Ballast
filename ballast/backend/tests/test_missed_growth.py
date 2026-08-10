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
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from db.connection import get_connection
from db.models import MarketDaily
from db.session import async_session_maker, engine
from precedent import MissedGrowthEstimate, estimate_missed_growth
from precedent.missed_growth import DEFAULT_MONEY_MARKET_APY, LOOKBACK_TRADING_DAYS

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
        settlement_cash=big,
        parked=big,
        reserved=big,
        money_market_apy=Decimal("0.04"),
        investable_base=big,
    )
    d = est.to_dict()
    for key in (
        "idle_cash",
        "forgone_growth",
        "window_return",
        "settlement_cash",
        "parked",
        "reserved",
        "investable_base",
    ):
        assert "E" not in d[key] and "e" not in d[key], (key, d[key])
    assert d["idle_cash"] == "1000000000000000000000000000"
    assert d["forgone_growth"] == "1000000000000000000000000000"
    assert d["window_return"] == "0.00000001"
    assert d["investable_base"] == "1000000000000000000000000000"
    assert d["money_market_apy"] == "0.04"
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
        settlement_cash=Decimal("0.00"),
        parked=Decimal("0.00"),
        reserved=None,
        money_market_apy=Decimal("0.04"),
        investable_base=Decimal("0.00"),
    )
    nd = none_est.to_dict()
    assert nd["window_return"] is None
    # Never-decided reserve renders as null (never a fabricated 0).
    assert nd["reserved"] is None


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


# --- Story 9.2: cash-state-aware + yield-aware engine ------------------------

SYM_TEN = "TEST_MG_TEN"
ALL_TEST_SYMBOLS.append(SYM_TEN)


def _ten_pct_closes() -> list[Decimal]:
    """253 bars: start 100.00, end 110.00 → exactly +10% over the trailing window."""
    n = LOOKBACK_TRADING_DAYS + 1
    closes = [Decimal("100.00")]
    for i in range(1, n - 1):
        closes.append(Decimal("100.00") + Decimal("10.00") * Decimal(i) / Decimal(n - 1))
    closes.append(Decimal("110.00"))
    return closes


@pytest.mark.asyncio
async def test_backward_compat_no_parked_no_reserve_matches_pre_92():
    """A caller passing only ``idle_cash`` behaves EXACTLY as pre-9.2: base ==
    settlement cash, no yield term, identical figure/window/statement copy."""
    _clean([SYM_RISING])
    _insert_series(SYM_RISING, _rising_closes())
    try:
        cash = Decimal("25000.00")
        async with async_session_maker() as session:
            est = await estimate_missed_growth(session, idle_cash=cash, symbol=SYM_RISING)
        assert est.window_return == Decimal("0.1400")
        # base == settlement cash, yield term vanishes → 25000 × 0.14.
        assert est.forgone_growth == Decimal("3500.00")
        assert est.idle_cash == Decimal("25000.00")
        assert est.investable_base == Decimal("25000.00")
        assert est.settlement_cash == Decimal("25000.00")
        assert est.parked == Decimal("0.00")
        assert est.reserved == Decimal("0.00")
        assert est.reason is None
        # Pure-settlement copy is unchanged in spirit (growth, no reserve clause).
        stmt = est.statement.lower()
        assert "growth" in stmt
        assert "reserve" not in stmt
        assert "money-market" not in stmt
    finally:
        _clean([SYM_RISING])


@pytest.mark.asyncio
async def test_yield_aware_split_reserve_parked_first_rising():
    """Matrix row: cash=5000, parked=5000, reserve=2000, +14%, apy 4%.

    Reserve drawn parked-first → parked_investable=3000, cash_investable=5000.
    forgone = 5000·0.14 + 3000·(0.14 − 0.04) = 700 + 300 = 1000.00."""
    _clean([SYM_RISING])
    _insert_series(SYM_RISING, _rising_closes())
    try:
        async with async_session_maker() as session:
            est = await estimate_missed_growth(
                session,
                idle_cash=Decimal("5000.00"),
                symbol=SYM_RISING,
                parked=Decimal("5000.00"),
                reserved=Decimal("2000.00"),
                money_market_apy=Decimal("0.04"),
            )
        assert est.reason is None
        assert est.window_return == Decimal("0.1400")
        assert est.investable_base == Decimal("8000.00")
        # 5000·0.14 + 3000·(0.14−0.04) = 700 + 300.
        assert est.forgone_growth == Decimal("1000.00")
        assert est.settlement_cash == Decimal("5000.00")
        assert est.parked == Decimal("5000.00")
        assert est.reserved == Decimal("2000.00")
        assert est.money_market_apy == Decimal("0.04")
        stmt = est.statement.lower()
        assert "reserve" in stmt and "protected" in stmt
        assert "$2,000.00" in est.statement
        # Discloses the yield assumption when parked > 0.
        assert "money-market" in stmt and "4% a year" in stmt
    finally:
        _clean([SYM_RISING])


@pytest.mark.asyncio
async def test_fully_reserved_when_reserve_covers_all_cash():
    """Matrix row: cash=1000, parked=1000, reserve=5000 → base clamps to 0,
    reason=fully_reserved, calm reserve-aware statement, forgone 0.00."""
    _clean([SYM_RISING])
    _insert_series(SYM_RISING, _rising_closes())
    try:
        async with async_session_maker() as session:
            est = await estimate_missed_growth(
                session,
                idle_cash=Decimal("1000.00"),
                symbol=SYM_RISING,
                parked=Decimal("1000.00"),
                reserved=Decimal("5000.00"),
            )
        assert est.reason == "fully_reserved"
        assert est.sufficient is True
        assert est.forgone_growth == Decimal("0.00")
        assert est.investable_base == Decimal("0.00")
        stmt = est.statement.lower()
        assert "reserve covers all" in stmt
        assert "nothing is sitting idle" in stmt
    finally:
        _clean([SYM_RISING])


@pytest.mark.asyncio
async def test_parked_only_no_settlement_cash_yield_offset():
    """Matrix row: cash=0, parked=4000, reserve=0, +10%, apy 4%.

    forgone = 4000·(0.10 − 0.04) = 4000·0.06 = 240.00; discloses the yield
    assumption. (The spec matrix's "2400.00" is an arithmetic typo — 4000·0.06 is
    240.00; the engine formula is authoritative and correct here.)"""
    _clean([SYM_TEN])
    _insert_series(SYM_TEN, _ten_pct_closes())
    try:
        async with async_session_maker() as session:
            est = await estimate_missed_growth(
                session,
                idle_cash=Decimal("0.00"),
                symbol=SYM_TEN,
                parked=Decimal("4000.00"),
                money_market_apy=Decimal("0.04"),
            )
        assert est.reason is None
        assert est.window_return == Decimal("0.1000")
        # 4000 · (0.10 − 0.04) = 4000 · 0.06 = 240.00.
        assert est.forgone_growth == Decimal("240.00")
        assert est.investable_base == Decimal("4000.00")
        assert est.settlement_cash == Decimal("0.00")
        assert est.parked == Decimal("4000.00")
        stmt = est.statement.lower()
        assert "money-market" in stmt and "4% a year" in stmt
        # No reserve was set → no fabricated reserve clause.
        assert "reserve" not in stmt
    finally:
        _clean([SYM_TEN])


@pytest.mark.asyncio
async def test_negative_window_parked_avoided_framing_never_cost():
    """A falling window with parked money frames as loss AVOIDED, never a cost;
    the parked term stays signed."""
    _clean([SYM_FALLING])
    _insert_series(SYM_FALLING, _falling_closes())
    try:
        async with async_session_maker() as session:
            est = await estimate_missed_growth(
                session,
                idle_cash=Decimal("5000.00"),
                symbol=SYM_FALLING,
                parked=Decimal("5000.00"),
                money_market_apy=Decimal("0.04"),
            )
        assert est.reason is None
        assert est.window_return == Decimal("-0.1000")
        # 5000·(−0.10) + 5000·(−0.10 − 0.04) = −500 − 700 = −1200.
        assert est.forgone_growth == Decimal("-1200.00")
        assert est.forgone_growth < 0
        stmt = est.statement.lower()
        assert "avoided" in stmt
        assert "cost" not in stmt
    finally:
        _clean([SYM_FALLING])


@pytest.mark.asyncio
async def test_default_apy_constant_is_documented_placeholder():
    """The disclosed default APY is a named 4% constant (tunable placeholder)."""
    assert DEFAULT_MONEY_MARKET_APY == Decimal("0.04")


# --- Story 9.2 review patches: honesty of market-DIRECTION wording -----------

SYM_MODEST = "TEST_MG_MODEST"
ALL_TEST_SYMBOLS.append(SYM_MODEST)

# The calm word-list (mirrors tests/test_digest_compose.py FORBIDDEN + _assert_calm):
# the missed-growth statement must never betray the calm-coach voice. Word-boundary
# matched so "red" flags "red" but not "covered"/"required".
_FORBIDDEN = [
    "urgent", "hurry", "act now", "act fast", "don't miss", "dont miss",
    "missing out", "miss out", "last chance", "limited time", "warning",
    "alarm", "panic", "crash", "plunge", "fear", "red", "alert", "immediately",
]


def _assert_calm(text: str) -> None:
    blob = text.lower()
    for word in _FORBIDDEN:
        pattern = r"\b" + re.escape(word) + r"\b"
        assert not re.search(pattern, blob), (
            f"missed-growth statement should never say {word!r}: {text!r}"
        )


def _modest_rise_closes() -> list[Decimal]:
    """253 bars: start 100.00, end 102.00 → exactly +2% over the trailing window.

    +2% is strictly between 0 and the 4%-over-window money-market yield, so parked
    money OUTPACES this modestly rising market — the honesty case that made
    ``forgone_growth`` NEGATIVE even though the market ROSE."""
    n = LOOKBACK_TRADING_DAYS + 1
    closes = [Decimal("100.00")]
    for i in range(1, n - 1):
        closes.append(Decimal("100.00") + Decimal("2.00") * Decimal(i) / Decimal(n - 1))
    closes.append(Decimal("102.00"))
    return closes


@pytest.mark.asyncio
async def test_modestly_rising_market_parked_outpaces_never_says_market_fell():
    """Review patch: a PARKED-heavy portfolio in a MODESTLY rising market
    (0 < window_return=+2% < yield_over_window=4%) yields a NEGATIVE forgone_growth
    even though the market ROSE. The statement must NEVER claim the market fell —
    it must say the parked cash kept pace with a modestly rising market and came
    out ahead. Direction wording keys on window_return, not the dollar sign."""
    _clean([SYM_MODEST])
    _insert_series(SYM_MODEST, _modest_rise_closes())
    try:
        async with async_session_maker() as session:
            est = await estimate_missed_growth(
                session,
                idle_cash=Decimal("0.00"),
                symbol=SYM_MODEST,
                parked=Decimal("5000.00"),
                money_market_apy=Decimal("0.04"),
            )
        assert est.reason is None
        assert est.sufficient is True
        # The market ROSE (+2%), yet the yield-adjusted figure is NEGATIVE.
        assert est.window_return == Decimal("0.0200")
        assert est.window_return > 0
        # 5000 · (0.02 − 0.04) = 5000 · (−0.02) = −100.00.
        assert est.forgone_growth == Decimal("-100.00")
        assert est.forgone_growth < 0

        stmt = est.statement
        low = stmt.lower()
        # HONESTY: must NEVER say the market fell (it rose).
        assert "market fell" not in low
        # Must convey "kept pace" with a modestly rising market AND "came out ahead".
        assert "kept pace" in low
        assert "modestly rising market" in low
        assert "came out" in low and "ahead" in low
        # Never the "avoided ... loss" framing (that is only for a falling market).
        assert "avoided" not in low
        assert "cost" not in low
        # Discloses the yield assumption (parked money in the calc).
        assert "money-market" in low and "4% a year" in low
        # Passes the calm / forbidden-word bar.
        _assert_calm(stmt)
    finally:
        _clean([SYM_MODEST])


@pytest.mark.asyncio
async def test_falling_window_reserve_and_parked_avoided_framing_full_disclosure():
    """Blind-Hunter gap #5: a FALLING window WITH both reserved>0 and parked>0.

    The composed statement must carry the "avoided ... loss" framing AND the
    reserve clause AND the yield disclosure, use "avoided" (never "cost"), and
    pass the calm bar."""
    _clean([SYM_FALLING])
    _insert_series(SYM_FALLING, _falling_closes())
    try:
        async with async_session_maker() as session:
            est = await estimate_missed_growth(
                session,
                idle_cash=Decimal("5000.00"),
                symbol=SYM_FALLING,
                parked=Decimal("5000.00"),
                reserved=Decimal("2000.00"),
                money_market_apy=Decimal("0.04"),
            )
        assert est.reason is None
        assert est.window_return == Decimal("-0.1000")
        # Reserve drawn parked-first: parked_investable=3000, cash_investable=5000.
        # 5000·(−0.10) + 3000·(−0.10 − 0.04) = −500 − 420 = −920.00.
        assert est.investable_base == Decimal("8000.00")
        assert est.forgone_growth == Decimal("-920.00")
        assert est.forgone_growth < 0

        stmt = est.statement
        low = stmt.lower()
        # AVOIDED-loss framing (falling market), never a "cost".
        assert "the market fell" in low
        assert "avoided" in low and "loss" in low
        assert "cost" not in low
        # The reserve clause (drawn parked-first, real $2,000.00) is present.
        assert "reserve" in low and "protected" in low
        assert "$2,000.00" in stmt
        # The yield disclosure (parked money in the calc) is present.
        assert "money-market" in low and "4% a year" in low
        # Passes the calm / forbidden-word bar.
        _assert_calm(stmt)
    finally:
        _clean([SYM_FALLING])


@pytest.mark.asyncio
async def test_yield_aware_estimate_is_deterministic():
    """Same market data + same cash-state inputs → byte-identical dict."""
    _clean([SYM_RISING])
    _insert_series(SYM_RISING, _rising_closes())
    try:
        kwargs = dict(
            idle_cash=Decimal("5000.00"),
            symbol=SYM_RISING,
            parked=Decimal("5000.00"),
            reserved=Decimal("2000.00"),
        )
        async with async_session_maker() as session:
            first = await estimate_missed_growth(session, **kwargs)
        async with async_session_maker() as session:
            second = await estimate_missed_growth(session, **kwargs)
        assert first.to_dict() == second.to_dict()
        d = first.to_dict()
        # All new money fields are fixed-point strings (no exponent).
        for key in ("settlement_cash", "parked", "reserved", "investable_base"):
            assert "E" not in d[key] and "e" not in d[key]
        assert d["investable_base"] == "8000.00"
        assert d["forgone_growth"] == "1000.00"
    finally:
        _clean([SYM_RISING])
