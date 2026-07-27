"""Market-data adapter factory (AD-8).

Selects the concrete :class:`~marketdata.port.MarketDataPort` implementation from
config (``MARKETDATA_ADAPTER``) and returns it typed as the port. Callers (the
ingestion job) depend on the port only; swapping fake <-> tiingo is a config
change, not a code change.

- ``MARKETDATA_ADAPTER=fake`` (default): :class:`FakeMarketDataAdapter` — no creds.
- ``MARKETDATA_ADAPTER=tiingo``: :class:`TiingoAdapter` — raises a clear
  :class:`~marketdata.tiingo_adapter.TiingoNotConfiguredError` if ``TIINGO_API_KEY``
  is absent.
"""

from __future__ import annotations

from api.config import get_settings
from marketdata.fake_adapter import FakeMarketDataAdapter
from marketdata.port import MarketDataPort


class UnknownMarketDataAdapterError(RuntimeError):
    """Raised when ``MARKETDATA_ADAPTER`` names an adapter that does not exist."""


def get_market_data() -> MarketDataPort:
    """Return the configured market-data adapter as a :class:`MarketDataPort`.

    The ``tiingo`` client is imported lazily (only when the tiingo adapter is
    selected), so the default fake path never touches the SDK.
    """
    adapter = (get_settings().MARKETDATA_ADAPTER or "fake").strip().lower()

    if adapter == "fake":
        return FakeMarketDataAdapter()

    if adapter == "tiingo":
        # Import lazily so selecting fake never loads the tiingo adapter module.
        from marketdata.tiingo_adapter import TiingoAdapter

        return TiingoAdapter()

    raise UnknownMarketDataAdapterError(
        f"Unknown MARKETDATA_ADAPTER '{adapter}'. Expected 'fake' or 'tiingo'."
    )
