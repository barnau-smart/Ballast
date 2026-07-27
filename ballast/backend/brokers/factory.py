"""Broker adapter factory (AD-8).

Selects the concrete :class:`~brokers.port.BrokerPort` implementation from
config (``BROKER_ADAPTER``) and returns it typed as the port. Callers depend on
the port only; swapping fake <-> schwab is a config change, not a code change.

- ``BROKER_ADAPTER=fake`` (default): :class:`FakeBrokerAdapter` — no creds.
- ``BROKER_ADAPTER=schwab``: :class:`SchwabAdapter` — raises a clear
  configuration error if the ``SCHWAB_*`` creds are absent.

``get_broker`` doubles as a FastAPI dependency for the endpoints.
"""

from __future__ import annotations

from api.config import get_settings
from brokers.fake_adapter import FakeBrokerAdapter
from brokers.port import BrokerPort


class UnknownBrokerAdapterError(RuntimeError):
    """Raised when ``BROKER_ADAPTER`` names an adapter that does not exist."""


def get_broker() -> BrokerPort:
    """Return the configured broker adapter as a :class:`BrokerPort`.

    ``schwab-py`` is imported lazily (only when the schwab adapter is selected),
    so the default fake path never touches the SDK.
    """
    adapter = (get_settings().BROKER_ADAPTER or "fake").strip().lower()

    if adapter == "fake":
        return FakeBrokerAdapter()

    if adapter == "schwab":
        # Import lazily so selecting fake never loads the schwab adapter module.
        from brokers.schwab_adapter import SchwabAdapter

        return SchwabAdapter()

    raise UnknownBrokerAdapterError(
        f"Unknown BROKER_ADAPTER '{adapter}'. Expected 'fake' or 'schwab'."
    )
