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

from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import get_settings
from api.deps import RECONNECT_MESSAGE, get_scope
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


def _token_dict_from_broker_tokens(tokens) -> dict:
    """Build the schwab-py token envelope from :class:`~brokers.port.BrokerTokens`.

    Only the fields Ballast persists are reconstructed; the exact live round-trip
    is a documented go-live manual check (see the story's Design Notes). Never
    logs token material.
    """
    expires_at = tokens.expires_at
    # Normalize to tz-aware UTC before the epoch conversion (a naive
    # ``.timestamp()`` would assume server-local time and skew the token expiry).
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return {
        "access_token": tokens.access_token,
        "refresh_token": tokens.refresh_token,
        "token_type": "Bearer",
        "expires_at": int(expires_at.timestamp()),
    }


async def _bind_user_token(
    scope: Scope,
    session: AsyncSession,
    broker: BrokerPort,
    *,
    missing_message: str,
):
    """Bind THIS user's decrypted-in-memory token onto a Schwab adapter.

    Shared by :func:`get_execution_broker` (placement) and
    :func:`get_reading_broker` (read/refresh). A non-Schwab adapter (the fake, a
    test spy) passes straight through untouched so the default/tested path stays
    credential-free. For a :class:`SchwabAdapter`, the token is read through the
    fail-closed :class:`~db.repository.ScopedRepository` (AD-10) so it can only
    ever be this user's own, and decrypted in-memory
    (:func:`~brokers.crypto.decrypt_token`) — the plaintext is never persisted or
    logged. ``missing_message`` is surfaced (as a config error) when the user has
    no linked token, so the placement vs. read paths read naturally.
    """
    # Import lazily so the fake path (and any non-schwab adapter) never loads the
    # schwab adapter module or the token-crypto path.
    from brokers.schwab_adapter import SchwabAdapter, SchwabNotConfiguredError

    if not isinstance(broker, SchwabAdapter):
        return broker

    from brokers.crypto import TokenEncryptionError, decrypt_token
    from db.models import BrokerageToken
    from db.repository import ScopedRepository

    repo = ScopedRepository(BrokerageToken, scope, session)
    rows = await repo.list()
    if not rows:
        # The request-entry live-session gate should have already refused an
        # unlinked user; refuse loudly rather than build a tokenless client.
        raise SchwabNotConfiguredError(missing_message)
    # Pick the latest-expiring row, exactly as ``get_brokerage_session`` does, so
    # that if the one-row invariant is ever violated the client is built from the
    # SAME (live) token the session gate validated — never a stale row.
    row = max(
        rows,
        key=lambda r: r.expires_at or datetime.min.replace(tzinfo=timezone.utc),
    )
    from brokers.port import BrokerTokens

    try:
        tokens = BrokerTokens(
            access_token=decrypt_token(row.access_token),
            refresh_token=decrypt_token(row.refresh_token),
            expires_at=row.expires_at,
        )
    except TokenEncryptionError as exc:
        # An undecryptable stored token (rotated ``TOKEN_ENCRYPTION_KEY`` or
        # corrupt/tampered ciphertext) raised HERE — during FastAPI dependency
        # resolution — would otherwise escape the handler's own ``try/except``
        # entirely (a dependency-resolution error never reaches the handler body)
        # and surface as a raw 500 through the app's ``Exception`` handler. That
        # breaks the calm/honest/never-red voice (NFR8). Re-raise as the SAME calm
        # 409 reconnect envelope the entry gate uses: an undecryptable token is,
        # from the user's chair, exactly a "reconnect your Schwab" condition. This
        # single choke point — shared by ``get_execution_broker`` (``/approve`` and
        # ``/decisions/{id}/reconcile``) and ``get_reading_broker`` (``/refresh``)
        # — covers all three seams at once. The message never echoes token/key
        # material or the raw exception text (``from exc`` keeps the chain for
        # logs, not the response body).
        raise HTTPException(
            status_code=409, detail=RECONNECT_MESSAGE
        ) from exc
    token_dict = _token_dict_from_broker_tokens(tokens)
    return SchwabAdapter(token_read_func=lambda: token_dict)


def bind_freshly_exchanged_token(broker: BrokerPort, tokens) -> BrokerPort:
    """Return a token-bound Schwab adapter from just-exchanged in-memory ``tokens``.

    For the link callback's import-on-connect: the OAuth ``exchange_code`` returns
    plaintext :class:`~brokers.port.BrokerTokens` already in memory, so there is
    no DB round-trip needed to authenticate the immediate reconcile. A non-Schwab
    adapter (the fake) passes straight through untouched. Never logs token
    material.
    """
    from brokers.schwab_adapter import SchwabAdapter

    if not isinstance(broker, SchwabAdapter):
        return broker
    token_dict = _token_dict_from_broker_tokens(tokens)
    return SchwabAdapter(token_read_func=lambda: token_dict)


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
    """
    return await _bind_user_token(
        scope,
        session,
        broker,
        missing_message=(
            "No linked Schwab token for this user; cannot place an order."
        ),
    )


async def get_reading_broker(
    scope: Scope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
    broker: BrokerPort = Depends(get_broker),
) -> BrokerPort:
    """Return a read-ready broker, binding THIS user's decrypted token (Story 6.5).

    Mirrors :func:`get_execution_broker` for the READ/refresh path (a Schwab
    ``fetch_portfolio`` needs an authenticated client to read real balances). It
    COMPOSES :func:`get_broker` so ``dependency_overrides[get_broker]`` in tests
    still applies — an injected fake/spy passes straight through untouched; only
    the real Schwab path pays the per-user token load + decrypt. The message is
    neutral (a read, not a placement).
    """
    return await _bind_user_token(
        scope,
        session,
        broker,
        missing_message=(
            "No linked Schwab token for this user; cannot read the portfolio."
        ),
    )
