"""Story 3.1 tests — market-data ingestion into the GLOBAL ``market_daily`` store.

REAL DB (no mocks), matching the Epic 1 / 2 style. Requires the docker Postgres
(`docker compose up -d db`). NO network and NO credentials are ever exercised —
the deterministic FakeMarketDataAdapter is the tested path.

Covers: deterministic fake bars (exact Decimal values), ingest writes one row per
symbol/day, idempotent re-runs (AC2), source-hiccup tolerance (AC3), the global
(no-owner_id) shape of ``market_daily`` (AC1), and the factory default.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio

from db.connection import get_connection
from db.models import MarketDaily
from db.session import async_session_maker, engine
from marketdata.factory import get_market_data
from marketdata.fake_adapter import FakeMarketDataAdapter
from marketdata.ingest import ingest_market_daily
from marketdata.port import DailyBar, MarketDataPort

START = date(2024, 1, 1)
END = date(2024, 1, 10)  # 10 inclusive calendar days
DAYS_IN_RANGE = 10

# Test-only symbols with a clearly-fake unique prefix so the real-DB (DB-writing)
# tests can never collide with a real ingest run over the global universe. The
# FakeMarketDataAdapter serves any symbol (it has a default base price), so these
# are safe. The pure fake-adapter determinism tests keep using "VTI".
TEST_SYMBOLS = ["TEST_VTI", "TEST_VXUS", "TEST_BND"]
TEST_BROKEN = "TEST_BROKEN"
TEST_UPSERT = "TEST_UPSERT"


@pytest_asyncio.fixture(autouse=True)
async def ensure_table():
    """Ensure the market_daily table exists for these real-DB tests."""
    async with engine.begin() as conn:
        await conn.run_sync(MarketDaily.__table__.create, checkfirst=True)
    yield


def _clean(symbols: list[str]) -> None:
    """Remove any rows for these symbols so each test starts clean (global table)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM market_daily WHERE symbol = ANY(%s)", (symbols,)
            )
        conn.commit()


def _count(symbols: list[str]) -> int:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM market_daily WHERE symbol = ANY(%s)",
                (symbols,),
            )
            (n,) = cur.fetchone()
    return int(n)


def _row(symbol: str, day: date):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT open, high, low, close, adj_close, volume, source "
                "FROM market_daily WHERE symbol = %s AND day = %s",
                (symbol, day),
            )
            return cur.fetchone()


# --- Fake adapter determinism -------------------------------------------------


def test_fake_bars_are_deterministic_and_decimal():
    a = FakeMarketDataAdapter().fetch_eod("VTI", START, END)
    b = FakeMarketDataAdapter().fetch_eod("VTI", START, END)
    assert a == b  # byte-for-byte reproducible (load-bearing for precedent)
    assert len(a) == DAYS_IN_RANGE
    assert [bar.day for bar in a] == [
        date(2024, 1, d) for d in range(1, DAYS_IN_RANGE + 1)
    ]
    bar0 = a[0]
    assert isinstance(bar0, DailyBar)
    # Money is Decimal, never float.
    for field in ("open", "high", "low", "close", "adj_close"):
        assert isinstance(getattr(bar0, field), Decimal)
    assert isinstance(bar0.volume, int)
    # OHLC internally consistent — high/low bracket open, close AND adj_close.
    assert bar0.low <= bar0.open <= bar0.high
    assert bar0.low <= bar0.close <= bar0.high
    assert bar0.low <= bar0.adj_close <= bar0.high
    # The full invariant holds across the whole series, not just bar0.
    for bar in a:
        assert bar.low <= bar.open <= bar.high
        assert bar.low <= bar.close <= bar.high
        assert bar.low <= bar.adj_close <= bar.high


def test_fake_bars_exact_values():
    """Pin exact Decimal values so a change to the generator is caught."""
    bar = FakeMarketDataAdapter().fetch_eod("VTI", START, START)[0]
    # Recomputed from the deterministic seed formula; these must be stable.
    assert bar.symbol == "VTI"
    assert bar.day == date(2024, 1, 1)
    assert bar.close == Decimal("198.01")
    assert bar.open == Decimal("199.50")
    assert bar.high == Decimal("199.98")
    assert bar.low == Decimal("197.53")
    assert bar.adj_close == Decimal("198.01")
    assert bar.volume == 2306686


def test_fake_reversed_range_is_empty():
    assert FakeMarketDataAdapter().fetch_eod("VTI", END, START) == []


# --- Ingest writes rows (real DB) --------------------------------------------


@pytest.mark.asyncio
async def test_ingest_writes_one_row_per_symbol_day():
    symbols = list(TEST_SYMBOLS)
    _clean(symbols)
    try:
        async with async_session_maker() as session:
            result = await ingest_market_daily(
                session, FakeMarketDataAdapter(), symbols, START, END
            )
        assert result.ok
        assert set(result.symbols_ingested) == set(symbols)
        assert result.rows_written == len(symbols) * DAYS_IN_RANGE
        assert _count(symbols) == len(symbols) * DAYS_IN_RANGE

        # Values round-trip as Decimal, source recorded.
        sym = TEST_SYMBOLS[0]
        expected = FakeMarketDataAdapter().fetch_eod(sym, START, START)[0]
        row = _row(sym, START)
        assert row is not None, f"expected a market_daily row for {sym}/{START}"
        open_, high, low, close, adj_close, volume, source = row
        assert Decimal(str(close)) == expected.close
        assert Decimal(str(open_)) == expected.open
        assert Decimal(str(adj_close)) == expected.adj_close
        assert int(volume) == expected.volume
        assert source == "fake"
    finally:
        _clean(symbols)


# --- Idempotent re-runs (AC2) ------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_is_idempotent_on_rerun():
    symbols = list(TEST_SYMBOLS)
    _clean(symbols)
    try:
        async with async_session_maker() as session:
            await ingest_market_daily(
                session, FakeMarketDataAdapter(), symbols, START, END
            )
        first = _count(symbols)
        # Re-run over the SAME (and overlapping) range → no duplicate rows.
        async with async_session_maker() as session:
            await ingest_market_daily(
                session, FakeMarketDataAdapter(), symbols, START, END
            )
        async with async_session_maker() as session:
            await ingest_market_daily(
                session,
                FakeMarketDataAdapter(),
                symbols,
                date(2024, 1, 5),  # overlaps the first range
                date(2024, 1, 15),
            )
        # Same-range re-run adds nothing; the overlap only adds the NEW days.
        assert first == len(symbols) * DAYS_IN_RANGE
        # Union of [1..10] and [5..15] = [1..15] = 15 days per symbol.
        assert _count(symbols) == len(symbols) * 15
    finally:
        _clean(symbols)


@pytest.mark.asyncio
async def test_changed_bar_updates_in_place():
    symbol = TEST_UPSERT
    _clean([symbol])

    class _FixedAdapter(MarketDataPort):
        provider = "fake"

        def __init__(self, close: Decimal):
            self._close = close

        def fetch_eod(self, symbol, start, end):
            return [
                DailyBar(
                    symbol=symbol,
                    day=START,
                    open=self._close,
                    high=self._close,
                    low=self._close,
                    close=self._close,
                    adj_close=self._close,
                    volume=123,
                )
            ]

    try:
        async with async_session_maker() as session:
            await ingest_market_daily(
                session, _FixedAdapter(Decimal("10.00")), [symbol], START, START
            )
        assert _count([symbol]) == 1
        row = _row(symbol, START)
        assert row is not None, f"expected a market_daily row for {symbol}/{START}"
        assert Decimal(str(row[3])) == Decimal("10.00")

        # Same (symbol, day), changed close → updates in place, no new row.
        async with async_session_maker() as session:
            await ingest_market_daily(
                session, _FixedAdapter(Decimal("99.99")), [symbol], START, START
            )
        assert _count([symbol]) == 1  # still ONE row
        row = _row(symbol, START)
        assert row is not None, f"expected a market_daily row for {symbol}/{START}"
        assert Decimal(str(row[3])) == Decimal("99.99")
    finally:
        _clean([symbol])


# --- Source-hiccup tolerance (AC3) -------------------------------------------


class _OneSymbolFailsAdapter(MarketDataPort):
    """Raises for one symbol; returns deterministic fake bars for the rest."""

    provider = "fake"

    def __init__(self, failing_symbol: str):
        self._failing = failing_symbol
        self._fake = FakeMarketDataAdapter()

    def fetch_eod(self, symbol, start, end):
        if symbol == self._failing:
            raise RuntimeError("simulated source hiccup")
        return self._fake.fetch_eod(symbol, start, end)


@pytest.mark.asyncio
async def test_source_hiccup_does_not_abort_run():
    good_a, good_b = "TEST_VTI", "TEST_BND"
    symbols = [good_a, TEST_BROKEN, good_b]
    _clean(symbols)
    try:
        async with async_session_maker() as session:
            result = await ingest_market_daily(
                session, _OneSymbolFailsAdapter(TEST_BROKEN), symbols, START, END
            )
        # The run continued: the good symbols are ingested, the bad one reported.
        assert not result.ok
        assert TEST_BROKEN in result.symbols_failed
        assert set(result.symbols_ingested) == {good_a, good_b}
        assert _count([good_a]) == DAYS_IN_RANGE
        assert _count([good_b]) == DAYS_IN_RANGE
        assert _count([TEST_BROKEN]) == 0  # nothing written for the failed symbol
    finally:
        _clean(symbols)


# --- Global (no owner_id) + factory default (AC1) ----------------------------


def test_market_daily_has_no_owner_id():
    """market_daily is GLOBAL reference data — it must have no owner_id column."""
    from db.models import Base

    columns = set(MarketDaily.__table__.columns.keys())
    assert "owner_id" not in columns
    assert {"symbol", "day", "open", "high", "low", "close", "adj_close",
            "volume", "source", "ingested_at"} <= columns
    # The model is registered on the app's metadata, so the real startup path
    # (create_all) would create the table.
    assert "market_daily" in Base.metadata.tables


def test_factory_returns_fake_by_default():
    adapter = get_market_data()
    assert isinstance(adapter, FakeMarketDataAdapter)
    assert isinstance(adapter, MarketDataPort)
