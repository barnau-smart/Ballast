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

from datetime import datetime, timezone

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import get_settings
from api.deps import get_scope
from brokers.fake_adapter import FakeBrokerAdapter
from brokers.port import BrokerPort
from db.scope import Scope
from db.session import get_async_session


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


async def get_execution_broker(
    scope: Scope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
    broker: BrokerPort = Depends(get_broker),
) -> BrokerPort:
    """Return a placement-ready broker, binding THIS user's decrypted token (Story 6.3).

    The scopeless :func:`get_broker` builds the configured adapter; the Schwab
    trading path additionally needs the current user's decrypted brokerage token
    to construct an authenticated client (`client_from_access_functions`). This
    dependency COMPOSES :func:`get_broker` (rather than replacing it) so the
    existing ``dependency_overrides[get_broker]`` in tests still applies — an
    injected fake/spy is not a :class:`SchwabAdapter` and passes straight through
    untouched; only the real Schwab path pays the per-user token load + decrypt.

    The token is read through the fail-closed :class:`~db.repository.ScopedRepository`
    (AD-10) so it can only ever be this user's own, and decrypted in-memory
    (:func:`~brokers.crypto.decrypt_token`) — the plaintext is never persisted or
    logged. It is handed to the adapter as an in-memory ``token_read_func``.
    """
    # Import lazily so the fake path (and any non-schwab adapter) never loads the
    # schwab adapter module or the token-crypto path.
    from brokers.schwab_adapter import SchwabAdapter, SchwabNotConfiguredError

    if not isinstance(broker, SchwabAdapter):
        return broker

    from brokers.crypto import decrypt_token
    from db.models import BrokerageToken
    from db.repository import ScopedRepository

    repo = ScopedRepository(BrokerageToken, scope, session)
    rows = await repo.list()
    if not rows:
        # The request-entry live-session gate should have already refused an
        # unlinked user; refuse loudly rather than build a tokenless client.
        raise SchwabNotConfiguredError(
            "No linked Schwab token for this user; cannot place an order."
        )
    # Pick the latest-expiring row, exactly as ``get_brokerage_session`` does, so
    # that if the one-row invariant is ever violated the placement client is built
    # from the SAME (live) token the session gate validated — never a stale row.
    row = max(
        rows,
        key=lambda r: r.expires_at or datetime.min.replace(tzinfo=timezone.utc),
    )
    access = decrypt_token(row.access_token)
    refresh = decrypt_token(row.refresh_token)
    # Normalize to tz-aware UTC before the epoch conversion (the model stores
    # tz-aware UTC, but a naive ``.timestamp()`` would assume server-local time and
    # skew the token's expiry).
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    # The schwab-py token envelope the trading client reads back. Only the
    # fields we persist are reconstructed; the exact live round-trip is a
    # documented go-live manual check (see the story's Design Notes).
    token_dict = {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "expires_at": int(expires_at.timestamp()),
    }
    return SchwabAdapter(token_read_func=lambda: token_dict)
