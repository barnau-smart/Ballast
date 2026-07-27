"""The Market-Data Port — the sole boundary between Ballast and any market-data vendor.

AD-8: external dependencies live behind ports. The Precedent Engine (Epic 3) and
every other consumer depend ONLY on the ``market_daily`` store (populated through
this port) — never on ``tiingo``, ``stooq``, ``yfinance``, or any concrete
vendor SDK. The concrete adapter (Tiingo vs Fake) is chosen by config and swapped
without touching a single caller.

Scope of THIS story (3.1): the read surface needed to ingest end-of-day bars —
:meth:`MarketDataPort.fetch_eod`. Money is ``Decimal`` (never binary float);
``day`` is a ``date``. This port returns DERIVED, normalized analytics bars
(OHLC + adjusted close) — NOT a raw redistribution of a vendor payload
(data-sourcing rule).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class DailyBar:
    """One normalized end-of-day bar for a single symbol/day — vendor-neutral.

    Money is ``Decimal``, NEVER binary float (consistency convention). ``day`` is
    a calendar ``date`` (no time component). ``adj_close`` is the split/dividend
    adjusted close used for drawdown + forward-return math; ``close`` is the raw
    session close. ``volume`` is share count (an ``int``). This is the derived
    analytics shape the Precedent Engine computes over — never a raw vendor blob.
    """

    symbol: str
    day: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    adj_close: Decimal
    volume: int


class MarketDataPort(ABC):
    """The abstract market-data boundary — the only vendor type callers depend on.

    Implementations: :class:`~marketdata.fake_adapter.FakeMarketDataAdapter`
    (local / dev / test, deterministic, zero credentials & zero network) and
    :class:`~marketdata.tiingo_adapter.TiingoAdapter` (real EOD source,
    credential-gated). A Stooq backup adapter is a future addition behind this
    same port; adding it changes no callers (AD-8).
    """

    @abstractmethod
    def fetch_eod(
        self, symbol: str, start: date, end: date
    ) -> list[DailyBar]:
        """Return the end-of-day :class:`DailyBar` series for ``symbol``.

        ``start`` and ``end`` are inclusive calendar dates. The result is ordered
        by ``day`` ascending and contains one bar per trading day in range
        (weekends/holidays may simply be absent — the store is keyed by the days
        that exist, not a dense calendar). Money is ``Decimal``.
        """
        raise NotImplementedError
