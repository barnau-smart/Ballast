"""SchwabAdapter — the real :class:`BrokerPort` implementation via schwab-py.

Code-complete but CREDENTIAL-GATED (per the story's fake-first strategy):

- Importing this module NEVER imports ``schwab-py`` and NEVER crashes, even
  with no credentials. ``schwab-py`` is imported LAZILY inside the methods
  (AD-8: it is the only place in the codebase that touches the SDK).
- Constructing/using the adapter without ``SCHWAB_CLIENT_ID`` /
  ``SCHWAB_CLIENT_SECRET`` / ``SCHWAB_CALLBACK_URL`` raises a clear
  :class:`SchwabNotConfiguredError` — a configuration error, not an import
  crash and not a network error.
- Real network calls happen ONLY when the adapter is properly configured (i.e.
  never in tests). Tokens and the OAuth code are never logged (schwab-py's
  ``register_redactions`` also scrubs token material from its own logs).

When the user's Schwab developer app is approved, set the three ``SCHWAB_*``
env vars and ``BROKER_ADAPTER=schwab``; nothing else changes (AD-8).
"""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlencode

from api.config import get_settings
from brokers.port import BrokerPort, BrokerTokens, OrderOutcome, PortfolioSnapshot
from coach.recommendation import OrderIntent

# Schwab's OAuth authorize endpoint (used only to reconstruct the received-url
# for the code exchange; the actual authorization_url is built by schwab-py).
_SCHWAB_AUTHORIZE_URL = "https://api.schwabapi.com/v1/oauth/authorize"


class SchwabNotConfiguredError(RuntimeError):
    """Raised when the Schwab adapter is used without the required credentials.

    This is a configuration error (fail-loud), deliberately distinct from an
    import failure or a network error.
    """


class SchwabAdapter(BrokerPort):
    """Real brokerage adapter backed by schwab-py. Gated on credentials."""

    provider = "schwab"

    def __init__(self) -> None:
        settings = get_settings()
        self._client_id = settings.SCHWAB_CLIENT_ID
        self._client_secret = settings.SCHWAB_CLIENT_SECRET
        self._callback_url = settings.SCHWAB_CALLBACK_URL
        # Fail loudly at construction if creds are missing, so the factory's
        # gating is unambiguous. schwab-py is NOT imported here.
        self._require_configured()

    def _require_configured(self) -> None:
        if not (self._client_id and self._client_secret and self._callback_url):
            raise SchwabNotConfiguredError(
                "Schwab is not configured. Set SCHWAB_CLIENT_ID, "
                "SCHWAB_CLIENT_SECRET, and SCHWAB_CALLBACK_URL (and "
                "BROKER_ADAPTER=schwab) to enable the Schwab adapter."
            )

    def authorization_url(self, state: str) -> str:
        """Return Schwab's authorization URL, embedding ``state`` for CSRF.

        Imports schwab-py lazily; makes no network call.
        """
        self._require_configured()
        from schwab import auth  # lazy: only place schwab-py is imported

        context = auth.get_auth_context(
            api_key=self._client_id,
            callback_url=self._callback_url,
            state=state,
        )
        return context.authorization_url

    def exchange_code(self, code: str, state: str) -> BrokerTokens:
        """Exchange the OAuth ``code`` for real Schwab tokens (network call).

        Uses schwab-py's ``client_from_received_url`` with a capturing
        token-write callback so we can read the freshly-minted token material
        WITHOUT persisting it to disk (Ballast stores it encrypted in the DB
        via the scoped repo instead). Never logs the code or tokens.

        ``state`` MUST be the same value that :meth:`authorization_url`
        embedded: authlib (under schwab-py) validates that the state on the
        redirect matches the state that initiated the flow. We therefore both
        (a) recreate the auth context bound to this state, and (b) include the
        state on the reconstructed received URL, so the check passes.
        """
        self._require_configured()
        from schwab import auth  # lazy import (AD-8)

        # schwab-py's helper takes the full redirect URL the browser landed on.
        # Reconstruct it from the callback URL + the returned code AND the state
        # (the same shape Schwab redirects to) so authlib's state check passes.
        received_url = (
            f"{self._callback_url}?{urlencode({'code': code, 'state': state})}"
        )

        context = auth.get_auth_context(
            api_key=self._client_id,
            callback_url=self._callback_url,
            state=state,
        )

        captured: dict = {}

        def _capture_token(token, *args, **kwargs) -> None:
            # Do not write to disk; capture in memory only. Never logged.
            captured["token"] = token

        auth.client_from_received_url(
            api_key=self._client_id,
            app_secret=self._client_secret,
            auth_context=context,
            received_url=received_url,
            token_write_func=_capture_token,
        )

        token = captured.get("token")
        if not token:
            raise SchwabNotConfiguredError(
                "Schwab code exchange did not return a token."
            )
        return self._to_broker_tokens(token)

    def fetch_portfolio(self) -> PortfolioSnapshot:
        """Fetch the account's holdings + cash from Schwab (network call).

        CREDENTIAL-GATED like the rest of this adapter: the fake adapter is the
        default/tested path (Story 2.3 is fake-first). The real schwab-py
        positions/balances mapping is deferred until the Schwab developer app is
        approved — wiring it here without live creds/fixtures would be untested
        code. Fails loudly (config error), never silently returns empty holdings
        that would reconcile the cache to nothing.
        """
        self._require_configured()
        raise SchwabNotConfiguredError(
            "Schwab portfolio fetch is not wired yet. The fake adapter is the "
            "tested path for Story 2.3; the real schwab-py positions/balances "
            "mapping lands when live Schwab access is available."
        )

    async def place_order(
        self, order_intent: OrderIntent, *, idempotency_key: str
    ) -> OrderOutcome:
        """Place a real Schwab order (network call) — CREDENTIAL-GATED stub.

        Mirrors :meth:`fetch_portfolio`: the fake adapter is the default/tested
        path (Story 4.6 is fake-first). The real schwab-py order-placement +
        outcome-normalization mapping is deferred until the Schwab developer app
        is approved — wiring it here without live creds/fixtures would be
        untested code that could place a real, uncovered order. Fails loudly
        with a configuration error, never silently returns a phantom fill.
        """
        self._require_configured()
        raise SchwabNotConfiguredError(
            "Schwab order placement is not wired yet. The fake adapter is the "
            "tested path for Story 4.6; the real schwab-py order placement + "
            "OrderOutcome mapping lands when live Schwab access is available."
        )

    @staticmethod
    def _to_broker_tokens(token: dict) -> BrokerTokens:
        """Normalise schwab-py's token dict into :class:`BrokerTokens`."""
        access = token.get("access_token", "")
        refresh = token.get("refresh_token", "")
        # OAuth tokens carry either an absolute ``expires_at`` (epoch seconds)
        # or a relative ``expires_in``; prefer the absolute value.
        expires_at_raw = token.get("expires_at")
        if expires_at_raw is not None:
            expires_at = datetime.fromtimestamp(
                float(expires_at_raw), tz=timezone.utc
            )
        else:
            expires_in = float(token.get("expires_in", 0))
            expires_at = datetime.now(timezone.utc)
            if expires_in:
                from datetime import timedelta

                expires_at = expires_at + timedelta(seconds=expires_in)
        return BrokerTokens(
            access_token=access,
            refresh_token=refresh,
            expires_at=expires_at,
        )
