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
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from typing import Callable
from urllib.parse import urlencode

import httpx

from api.config import get_settings
from brokers.port import (
    BrokerPort,
    BrokerTokens,
    OrderNotPlaceableError,
    OrderOutcome,
    OrderStatus,
    PortfolioSnapshot,
)
from coach.recommendation import OrderIntent, OrderSide

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

    # Schwab order-status strings that are terminally NOT a live/fill state — a
    # canceled/expired/rejected order is a truthful ``REJECTED`` OrderOutcome. A
    # ``FILLED`` maps to FILLED; anything else still open reconciles to PENDING
    # (or PARTIAL when some — but not all — has filled). Uppercased for match.
    _REJECTED_STATUSES = frozenset({"REJECTED", "CANCELED", "EXPIRED"})

    def __init__(
        self, *, token_read_func: Callable[[], dict] | None = None
    ) -> None:
        settings = get_settings()
        self._client_id = settings.SCHWAB_CLIENT_ID
        self._client_secret = settings.SCHWAB_CLIENT_SECRET
        self._callback_url = settings.SCHWAB_CALLBACK_URL
        # The per-user decrypted-token accessor (Story 6.3). ``None`` for the
        # auth-only usage (``authorization_url`` / ``exchange_code`` never need a
        # stored token); the trading methods require it and refuse loudly if it
        # is absent. Bound by ``get_execution_broker`` on the placement path so
        # the otherwise-scopeless adapter gets THIS user's token. Never logged.
        self._token_read_func = token_read_func
        # Built lazily on first trading call and cached for the life of the
        # adapter instance (one request); no disk/network at construction.
        self._client = None
        self._account_hash_cache: str | None = None
        # In-instance idempotency cache: idempotency_key -> Schwab order id (or
        # ``None`` when the placement returned no order id). Lives for exactly one
        # request (``get_broker`` returns a fresh adapter per request), so the
        # in-request reconcile can look up the placed order WITHOUT a
        # broker-honored client key (Schwab echoes none) — durable cross-request
        # reconciliation is Story 6.7. Mirrors the fake's ``self._orders``.
        self._orders: dict[str, str | None] = {}
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
        """Place a whole-share MARKET order on Schwab and return its outcome (Story 6.3).

        The single execution write (AD-7) — no poll/retry/wait-loop. Sizing is the
        locked v1 decision: fetch a placement-time quote, size a WHOLE-SHARE market
        order (``quantity = floor(amount / ask)``) and refuse calmly via
        :class:`~brokers.port.OrderNotPlaceableError` if the dollar ``amount`` buys
        less than one share (or the quote is unusable) — no order is placed. Buy
        vs. sell chooses the matching share-quantity equity builder (schwab-py
        1.5.1 exposes no notional/fractional builder).

        Failure classes are kept honest and distinct: an HTTP-error placement
        response is a truthful broker ``REJECTED``; any ``httpx``/SDK TRANSPORT
        failure (anywhere in hash/quote/place/read) is INDETERMINATE →
        ``OrderStatus.TIMEOUT`` with NO raw exception leaking the port and NO
        phantom fill (the caller reconciles once via :meth:`get_order_status`). The
        placed Schwab order id is cached under ``idempotency_key`` so the
        in-request reconcile can read it back (Schwab honors no client key); a
        placement that returns no order id caches ``None`` and surfaces
        ``TIMEOUT`` → ``pending`` (never auto-searched — Story 6.7 owns durable
        cross-request recovery). Never logs token/secret material.
        """
        self._require_configured()
        # Building the client does no network (schwab-py docs) — a missing token
        # is a config error that must surface plainly, NOT masquerade as a
        # timeout, so it is constructed OUTSIDE the transport net below.
        client = self._trading_client()
        from schwab.orders.equities import equity_buy_market, equity_sell_market
        from schwab.utils import (
            AccountHashMismatchException,
            UnsuccessfulOrderException,
            Utils,
        )

        # Known once the placement returns an order id; kept in the outer scope so
        # the transport/parse ``except`` below can PRESERVE it on an indeterminate
        # status-read failure (a landed order must stay reconcilable — Story 6.7).
        order_ref: str | None = None
        try:
            account_hash = self._account_hash(client)
            ask = self._quote_ask(client, order_intent.symbol)
            # Whole-share sizing: floor(amount / ask). ``amount``/``ask`` are
            # positive Decimals, so integral-floor is an honest whole-share count.
            quantity = int(
                (order_intent.amount / ask).to_integral_value(rounding=ROUND_FLOOR)
            )
            if quantity < 1:
                raise OrderNotPlaceableError(
                    f"${order_intent.amount:.2f} buys less than one whole share "
                    f"of {order_intent.symbol} at about ${ask:.2f} — no order "
                    "was placed."
                )
            builder = (
                equity_buy_market(order_intent.symbol, quantity)
                if order_intent.side == OrderSide.BUY
                else equity_sell_market(order_intent.symbol, quantity)
            )
            resp = client.place_order(account_hash, builder)
        except OrderNotPlaceableError:
            # A deliberate, calm pre-placement refusal — surface it, never a fill.
            raise
        except (
            httpx.HTTPError,
            UnsuccessfulOrderException,
            AccountHashMismatchException,
            ValueError,
            ArithmeticError,
            KeyError,
            IndexError,
            TypeError,
        ):
            # A TRANSPORT/SDK/parse/shape failure BEFORE the single placement write
            # returned: the order did NOT land (``place_order`` was never reached or
            # itself raised), so there is no id to preserve. Never leak a raw
            # exception past the port; never a phantom fill. Covers a transport
            # failure on hash/quote/place, plus a malformed account-numbers body
            # (Key/Index/Type). A missing token/account/hash is a
            # ``SchwabNotConfiguredError`` — deliberately NOT in this tuple — so it
            # still surfaces plainly as a config error, not a masked timeout.
            # Surface TIMEOUT → the caller reconciles once (AD-13).
            return OrderOutcome(
                status=OrderStatus.TIMEOUT,
                filled_qty=Decimal("0"),
                avg_price=None,
                broker_ref=None,
            )
        # --- The single placement write has RETURNED. From here on NO exception
        # may escape this method: a landed order plus ANY raise would let
        # ``approve`` release the atomic claim and re-place a SECOND real order
        # (Schwab honors no client key). This post-placement region is a FENCE
        # (a bare ``except Exception``), NOT a curated exception blocklist — a
        # blocklist can always miss a type (an SDK-specific error, an
        # ``AttributeError`` from an unexpected response shape, a ``RuntimeError``)
        # and the cost of a miss here is a duplicate REAL order.
        try:
            if resp.is_error:
                # A truthful broker rejection (definitive) — NOT a phantom fill,
                # NOT an indeterminate timeout. No order id assigned.
                return OrderOutcome(
                    status=OrderStatus.REJECTED,
                    filled_qty=Decimal("0"),
                    avg_price=None,
                    broker_ref=None,
                )
            order_id = Utils(client, account_hash).extract_order_id(resp)
            order_ref = None if order_id is None else str(order_id)
            # Record the placement under the client key BEFORE any read, so the
            # in-request reconcile can find it even if the status read times out.
            self._orders[idempotency_key] = order_ref
            if order_ref is None:
                # 2xx placement but no order id (no ``Location``) — indeterminate.
                # Surface TIMEOUT → the caller reconciles → honest ``pending``.
                return OrderOutcome(
                    status=OrderStatus.TIMEOUT,
                    filled_qty=Decimal("0"),
                    avg_price=None,
                    broker_ref=None,
                )
            status_resp = client.get_order(order_id, account_hash)
            return self._map_order(status_resp.json(), broker_ref=order_ref)
        except Exception:
            # ANY post-placement failure is INDETERMINATE: the order may have
            # landed. Never leak a raw exception past the port; never a phantom
            # fill. PRESERVE any order id obtained (a landed-but-unread order stays
            # reconcilable — Story 6.7) and cache the (possibly ``None``) ref so the
            # in-request reconcile can find it. ``ArithmeticError`` (a NaN Decimal
            # comparison), ``ValueError`` (non-JSON body), ``AttributeError``
            # (unexpected response shape) and SDK-specific errors are ALL caught
            # here by construction. Surface TIMEOUT (AD-13).
            self._orders[idempotency_key] = order_ref
            return OrderOutcome(
                status=OrderStatus.TIMEOUT,
                filled_qty=Decimal("0"),
                avg_price=None,
                broker_ref=order_ref,
            )

    async def get_order_status(self, idempotency_key: str) -> OrderOutcome:
        """Reconcile an already-placed Schwab order by its client key (Story 6.3).

        The single reconciliation read (AD-13) — never re-places, never guesses.
        Schwab echoes no client idempotency key, so the placed order id is looked
        up in this instance's in-request ``idempotency_key → order_id`` cache. An
        UNKNOWN key, or a key that mapped to NO order id (a true no-order_id
        timeout), returns an honest ``PENDING`` (``filled_qty`` 0, no
        ``broker_ref``) — it NEVER calls ``get_orders_for_account`` and NEVER
        attribute-matches recent orders (the locked decision; durable
        cross-request recovery is Story 6.7). With a known order id it reads the
        authoritative order and maps it; a transport failure surfaces ``TIMEOUT``
        with no raw exception leaking the port. Never logs token/secret material.
        """
        self._require_configured()
        order_ref = self._orders.get(idempotency_key)
        if order_ref is None:
            # Unknown key OR a placement that got no order id — honest pending.
            # NEVER search, NEVER guess (decision #2 / FR22 / NFR3).
            return OrderOutcome(
                status=OrderStatus.PENDING,
                filled_qty=Decimal("0"),
                avg_price=None,
                broker_ref=None,
            )
        client = self._trading_client()
        try:
            account_hash = self._account_hash(client)
            status_resp = client.get_order(int(order_ref), account_hash)
            return self._map_order(status_resp.json(), broker_ref=order_ref)
        except Exception:
            # The reconcile read is over an order KNOWN to have been placed (the key
            # mapped to an id), so — exactly like ``place_order``'s post-placement
            # region — NO exception may escape: a raw leak here would let ``approve``
            # release the atomic claim and re-place a SECOND real order. This is a
            # FENCE (bare ``except Exception``), not a curated blocklist. A transport
            # failure, a non-JSON/non-dict body, a NaN Decimal comparison
            # (``ArithmeticError``), an unexpected response shape (``AttributeError``)
            # or any SDK-specific error is INDETERMINATE — never leak past the port.
            # PRESERVE the known ``order_ref`` so the order stays reconcilable
            # (Story 6.7). Surface TIMEOUT.
            return OrderOutcome(
                status=OrderStatus.TIMEOUT,
                filled_qty=Decimal("0"),
                avg_price=None,
                broker_ref=order_ref,
            )

    def _trading_client(self):
        """Build (once) an authenticated schwab-py trading client from the token.

        Uses ``client_from_access_functions`` with the injected in-memory
        ``token_read_func`` (the decrypted per-user token) — NO disk and NO
        network at construction (schwab-py docs). Cached for the life of this
        (one-request) adapter instance. Refreshed tokens are held in-memory only
        and NOT persisted here (v1 prompts weekly re-auth rather than refreshing
        behind the user's back — AD-11). Refuses loudly if no token was bound (a
        config error, not a timeout).
        """
        if self._token_read_func is None:
            raise SchwabNotConfiguredError(
                "Schwab trading requires a linked, decrypted brokerage token; "
                "none was provided to this adapter."
            )
        if self._client is None:
            from schwab import auth  # lazy: only place schwab-py is imported

            self._client = auth.client_from_access_functions(
                api_key=self._client_id,
                app_secret=self._client_secret,
                token_read_func=self._token_read_func,
                token_write_func=self._noop_token_write,
                enforce_enums=False,
            )
        return self._client

    @staticmethod
    def _noop_token_write(token, *args, **kwargs) -> None:
        """Discard a mid-request token refresh (held in-memory only; never logged)."""
        return None

    def _account_hash(self, client) -> str:
        """Resolve (once) and cache the account hash Schwab keys placements on."""
        if self._account_hash_cache is None:
            resp = client.get_account_numbers()
            accounts = resp.json()
            # Schwab returns a list of ``{accountNumber, hashValue}``; v1 uses the
            # first account. The hash — not the raw account number — is what the
            # trading endpoints key on. An empty list or a missing hash is a clear
            # account/config problem (surfaced plainly, never a phantom fill),
            # distinct from a transport failure. A malformed non-list body raises a
            # Key/Index/Type error, caught as INDETERMINATE by the callers.
            if not accounts:
                raise SchwabNotConfiguredError(
                    "Schwab returned no account for this login; cannot place an order."
                )
            first = accounts[0]
            if not isinstance(first, dict):
                # A non-dict first element (e.g. a bare string/number) would raise
                # a raw ``AttributeError`` on ``.get`` — surface it plainly as an
                # account/config problem instead (pre-placement; no order placed).
                raise SchwabNotConfiguredError(
                    "Schwab account body is malformed; cannot place an order."
                )
            hash_value = first.get("hashValue")
            if not hash_value:
                raise SchwabNotConfiguredError(
                    "Schwab account is missing its trading hash; cannot place an order."
                )
            self._account_hash_cache = hash_value
        return self._account_hash_cache

    def _quote_ask(self, client, symbol: str) -> Decimal:
        """Read the current ask for ``symbol`` (Decimal); refuse if unusable.

        Sizing uses the ask (the locked decision). A missing or non-positive ask
        means we cannot honestly size a whole-share order, so we refuse calmly
        via :class:`~brokers.port.OrderNotPlaceableError` rather than placing on a
        guessed price. The exact quote JSON shape is fixture-driven and
        re-confirmed at go-live.
        """
        resp = client.get_quote(symbol)
        data = resp.json() or {}
        node = data.get(symbol) or {}
        quote = node.get("quote") or {}
        ask_raw = quote.get("askPrice")
        if ask_raw is None:
            raise OrderNotPlaceableError(
                f"No usable quote for {symbol} right now — no order was placed."
            )
        try:
            ask = Decimal(str(ask_raw))
        except (InvalidOperation, ValueError):
            ask = Decimal("0")
        # A NaN/Infinity ask parses to a valid-but-non-finite Decimal (no raise
        # above), and comparing it with ``<=`` would raise ``InvalidOperation``
        # (an ``ArithmeticError``) — reject it as unusable up front so a garbage
        # quote is a calm refusal, never a raw exception leaking the port.
        if not ask.is_finite() or ask <= 0:
            raise OrderNotPlaceableError(
                f"No usable quote for {symbol} right now — no order was placed."
            )
        return ask

    def _map_order(self, order_json: dict, *, broker_ref: str | None) -> OrderOutcome:
        """Map a Schwab order-status JSON body → a normalized :class:`OrderOutcome`.

        ``FILLED`` → FILLED; ``REJECTED``/``CANCELED``/``EXPIRED`` → REJECTED; a
        partial fill (some — but not all — filled) → PARTIAL; anything else still
        open → honest PENDING. ``filled_qty`` / ``avg_price`` are read
        DEFENSIVELY (tolerate missing → 0 / None); the exact field names are
        fixture-driven and re-confirmed at go-live. Money stays ``Decimal``.
        """
        if not isinstance(order_json, dict):
            # A non-dict status body (e.g. a bare list/None) is unreadable — surface
            # an honest PENDING carrying the known ``broker_ref`` rather than let a
            # raw ``AttributeError`` escape the port. The order stays reconcilable.
            order_json = {}
        status_raw = str(order_json.get("status") or "").strip().upper()
        filled_qty = self._decimal_or_zero(order_json.get("filledQuantity"))
        quantity = self._decimal_or_zero(order_json.get("quantity"))
        avg_price = self._extract_avg_price(order_json)
        if filled_qty > 0 and (
            status_raw == "FILLED" or (quantity > 0 and filled_qty >= quantity)
        ):
            # Trust a fully-filled QUANTITY even if the status string is a variant
            # (e.g. "PARTIALLY_FILLED" that has since completed) — a fully-filled
            # order must never be surfaced as still-open PENDING. But require a
            # POSITIVE filled quantity: a "FILLED" status string with a missing/zero
            # ``filledQuantity`` is a contradictory/incomplete body, so fall through
            # to PENDING rather than persist a "filled" order that moved 0 shares
            # with no price (``quantity`` alone can be 0 in the variant case).
            status = OrderStatus.FILLED
        elif status_raw in self._REJECTED_STATUSES:
            status = OrderStatus.REJECTED
        elif filled_qty > 0 and (quantity <= 0 or filled_qty < quantity):
            status = OrderStatus.PARTIAL
        else:
            status = OrderStatus.PENDING
        return OrderOutcome(
            status=status,
            filled_qty=filled_qty,
            avg_price=avg_price if filled_qty > 0 else None,
            broker_ref=broker_ref,
        )

    @staticmethod
    def _decimal_or_zero(value) -> Decimal:
        """Coerce a broker-JSON numeric to ``Decimal``; missing/garbage → 0."""
        if value is None:
            return Decimal("0")
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return Decimal("0")
        # A NaN/Infinity numeric parses to a valid-but-non-finite Decimal; treat it
        # as 0 so a later ``>=``/``>`` comparison in ``_map_order`` can never raise
        # ``InvalidOperation`` (which, escaping post-placement, would let ``approve``
        # release the claim and re-place a second real order).
        return result if result.is_finite() else Decimal("0")

    @staticmethod
    def _extract_avg_price(order_json: dict) -> Decimal | None:
        """Best-effort average fill price (Decimal) or ``None`` — tolerant of shape.

        Prefers a direct broker field; otherwise derives a quantity-weighted
        average from the execution legs. Returns ``None`` rather than raising when
        the shape is absent (fixture-driven; re-confirmed at go-live).
        """
        for key in ("avgFillPrice", "averagePrice", "price"):
            val = order_json.get(key)
            if val is not None:
                try:
                    return Decimal(str(val))
                except (InvalidOperation, ValueError):
                    return None
        total_qty = Decimal("0")
        total_cost = Decimal("0")
        for activity in order_json.get("orderActivityCollection") or []:
            for leg in (activity or {}).get("executionLegs") or []:
                q = SchwabAdapter._decimal_or_zero((leg or {}).get("quantity"))
                price = (leg or {}).get("price")
                if price is None or q <= 0:
                    continue
                try:
                    total_cost += q * Decimal(str(price))
                    total_qty += q
                except (InvalidOperation, ValueError):
                    continue
        if total_qty > 0:
            return total_cost / total_qty
        return None

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
