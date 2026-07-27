"""TiingoAdapter — the real :class:`MarketDataPort` implementation via Tiingo.

Code-shaped but CREDENTIAL-GATED (per the story's fake-first strategy), mirroring
:class:`~brokers.schwab_adapter.adapter.SchwabAdapter`'s fail-loud posture:

- Importing this module NEVER imports the ``tiingo`` client and NEVER crashes,
  even with no credentials. The client is imported LAZILY inside the methods
  (AD-8: this adapter is the only place in the codebase that would touch a
  market-data vendor SDK).
- Constructing/using the adapter without ``TIINGO_API_KEY`` raises a clear
  :class:`TiingoNotConfiguredError` — a configuration error, not an import crash
  and not a network error.
- Real network calls happen ONLY when the adapter is properly configured (i.e.
  never in tests / the default fake path). The API key is never logged.

When a Tiingo key is available, set ``TIINGO_API_KEY`` and
``MARKETDATA_ADAPTER=tiingo``; nothing else changes (AD-8). A Stooq backup would
be a sibling adapter behind this same port — noted, not built (out of scope 3.1).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from api.config import get_settings
from marketdata.port import DailyBar, MarketDataPort


class TiingoNotConfiguredError(RuntimeError):
    """Raised when the Tiingo adapter is used without the required credentials.

    This is a configuration error (fail-loud), deliberately distinct from an
    import failure or a network error.
    """


class TiingoAdapter(MarketDataPort):
    """Real market-data adapter backed by Tiingo EOD. Gated on credentials."""

    provider = "tiingo"

    def __init__(self) -> None:
        settings = get_settings()
        self._api_key = settings.TIINGO_API_KEY
        # Fail loudly at construction if the key is missing, so the factory's
        # gating is unambiguous. The tiingo client is NOT imported here.
        self._require_configured()

    def _require_configured(self) -> None:
        if not self._api_key:
            raise TiingoNotConfiguredError(
                "Tiingo is not configured. Set TIINGO_API_KEY (and "
                "MARKETDATA_ADAPTER=tiingo) to enable the Tiingo adapter."
            )

    def fetch_eod(
        self, symbol: str, start: date, end: date
    ) -> list[DailyBar]:
        """Fetch the EOD bar series for ``symbol`` from Tiingo (network call).

        Imports the ``tiingo`` client lazily (AD-8). Normalizes Tiingo's response
        into vendor-neutral :class:`DailyBar` values — derived analytics
        (OHLC + adjusted close), NOT a raw redistribution of the vendor payload.
        Money is parsed as ``Decimal`` (never float). The API key is never logged.
        """
        self._require_configured()
        # Lazy import: selecting the fake adapter never loads the tiingo SDK.
        from tiingo import TiingoClient

        client = TiingoClient({"api_key": self._api_key, "session": True})
        raw = client.get_ticker_price(
            symbol,
            startDate=start.isoformat(),
            endDate=end.isoformat(),
            frequency="daily",
            fmt="json",
        )

        bars: list[DailyBar] = []
        for row in raw:
            # Tiingo timestamps are ISO-8601; take the calendar date only.
            day = date.fromisoformat(str(row["date"])[:10])
            bars.append(
                DailyBar(
                    symbol=symbol,
                    day=day,
                    open=_to_decimal(row["open"]),
                    high=_to_decimal(row["high"]),
                    low=_to_decimal(row["low"]),
                    close=_to_decimal(row["close"]),
                    # Tiingo exposes split/dividend adjusted fields prefixed
                    # ``adj*``; fall back to raw close if absent.
                    adj_close=_to_decimal(row.get("adjClose", row["close"])),
                    volume=int(row.get("volume", 0) or 0),
                )
            )
        bars.sort(key=lambda b: b.day)
        return bars


def _to_decimal(value: object) -> Decimal:
    """Parse a vendor numeric into ``Decimal`` via ``str`` (no binary-float drift)."""
    return Decimal(str(value))
