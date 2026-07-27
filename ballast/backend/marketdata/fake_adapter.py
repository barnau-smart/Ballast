"""FakeMarketDataAdapter — the credential-free implementation of :class:`MarketDataPort`.

This is the DEFAULT adapter (``MARKETDATA_ADAPTER=fake``). It makes the entire
ingestion path — job -> ``market_daily`` -> (later) precedent stats — fully
runnable and testable locally with ZERO credentials and ZERO network calls.

Determinism is LOAD-BEARING: the precedent stories (3.2-3.4) will assert exact
statistics computed over these bars, so the series MUST be reproducible. The bars
are generated from a pure, seeded formula of (symbol, day) with NO wall-clock and
NO random-module global state — the same (symbol, start, end) always yields the
identical byte-for-byte ``Decimal`` series. When a Tiingo key is available,
flipping ``MARKETDATA_ADAPTER=tiingo`` swaps in the real source with no caller
changes (AD-8).
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from decimal import Decimal

from marketdata.port import DailyBar, MarketDataPort

# A small, obviously-fake but realistic symbol universe. These broad index funds
# mirror the v1 "stable core" universe the rest of the app already uses.
FAKE_SYMBOLS: tuple[str, ...] = ("VTI", "VXUS", "BND")

# Per-symbol deterministic base price (in dollars). Chosen so each symbol has a
# visibly distinct level; the series oscillates deterministically around it.
_BASE_PRICE: dict[str, Decimal] = {
    "VTI": Decimal("200.00"),
    "VXUS": Decimal("55.00"),
    "BND": Decimal("72.00"),
}
# Fallback base for any symbol not in the table above (still fully deterministic).
_DEFAULT_BASE_PRICE = Decimal("100.00")

# Prices are quantized to cents; volume to whole shares.
_CENTS = Decimal("0.01")


def _seed(symbol: str, day: date) -> int:
    """A stable non-negative integer seed derived purely from (symbol, day).

    Uses a hash (not Python's salted ``hash()``) so the value is identical across
    processes and runs — determinism is load-bearing here.
    """
    digest = hashlib.sha256(f"{symbol}|{day.isoformat()}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def _bar_for(symbol: str, day: date) -> DailyBar:
    """Compute the one deterministic :class:`DailyBar` for a symbol/day.

    Pure function of (symbol, day): no wall-clock, no RNG global state. The
    oscillation is a bounded deterministic offset so OHLC stay internally
    consistent (low <= open/close <= high) and money stays ``Decimal``.
    """
    base = _BASE_PRICE.get(symbol, _DEFAULT_BASE_PRICE)
    seed = _seed(symbol, day)

    # A bounded deterministic offset in cents, in roughly [-500, +500] cents.
    offset_cents = (seed % 1001) - 500
    close = (base + Decimal(offset_cents) * _CENTS).quantize(_CENTS)

    # Intraday spread derived deterministically from a different slice of the seed.
    spread_cents = (seed // 1001) % 300  # 0..299 cents
    half = (Decimal(spread_cents) * _CENTS / 2).quantize(_CENTS)

    open_ = (close - half).quantize(_CENTS)
    high = (close + half).quantize(_CENTS)
    low = (close - half).quantize(_CENTS)
    # adj_close is the (deterministically) adjusted close — a small, stable
    # per-symbol/day adjustment so it differs from raw close but round-trips.
    adj_close = (close - (Decimal(seed % 7) * _CENTS)).quantize(_CENTS)

    # Deterministic whole-share volume in a realistic band.
    volume = 1_000_000 + (seed % 9_000_000)

    return DailyBar(
        symbol=symbol,
        day=day,
        open=open_,
        high=high,
        low=low,
        close=close,
        adj_close=adj_close,
        volume=volume,
    )


class FakeMarketDataAdapter(MarketDataPort):
    """A deterministic, offline stand-in for a real market-data vendor.

    Never uses wall-clock time and never touches the network, so every fetched
    series is fully reproducible. Bars are emitted for EVERY calendar day in
    ``[start, end]`` (the fake has no market-holiday calendar — the store is keyed
    by the days that exist, which is all that ingestion/idempotency care about).
    """

    provider = "fake"

    def fetch_eod(
        self, symbol: str, start: date, end: date
    ) -> list[DailyBar]:
        """Return the deterministic bar series for ``symbol`` over ``[start, end]``.

        Inclusive of both endpoints, ordered by ``day`` ascending. No network.
        """
        if end < start:
            return []
        bars: list[DailyBar] = []
        day = start
        while day <= end:
            bars.append(_bar_for(symbol, day))
            day += timedelta(days=1)
        return bars
