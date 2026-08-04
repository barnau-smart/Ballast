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
    Holding,
    OrderNotPlaceableError,
    OrderOutcome,
    OrderStatus,
    PortfolioSnapshot,
)
from coach.recommendation import OrderIntent, OrderSide, OrderType
from money import format_money

# Schwab's OAuth authorize endpoint (used only to reconstruct the received-url
# for the code exchange; the actual authorization_url is built by schwab-py).
_SCHWAB_AUTHORIZE_URL = "https://api.schwabapi.com/v1/oauth/authorize"


class SchwabNotConfiguredError(RuntimeError):
    """Raised when the Schwab adapter is used without the required credentials.

    This is a configuration error (fail-loud), deliberately distinct from an
    import failure or a network error.
    """


class SchwabAccountSelectionError(SchwabNotConfiguredError):
    """Raised when the Schwab login exposes >1 account and no unambiguous choice.

    A configuration/selection fault, DELIBERATELY subclassing
    :class:`SchwabNotConfiguredError` so the read path (``fetch_portfolio`` /
    reconcile / import-on-connect) already treats it as a distinct config fault
    (not a :class:`SchwabReadError`) for free, and import-on-connect keeps
    swallowing it (the link survives). Fires when a login returns more than one
    account and ``SCHWAB_ACCOUNT_ID`` is unset (ambiguous — never ``accounts[0]``),
    or when ``SCHWAB_ACCOUNT_ID`` matches none of the returned account numbers. A
    pre-placement/pre-read refusal — no order is ever placed. Never carries token
    or secret material.
    """


class SchwabReadError(RuntimeError):
    """Raised when a Schwab portfolio READ fails (transport/parse/shape error).

    The failure surface of :meth:`SchwabAdapter.fetch_portfolio` (Story 6.5): a
    FAILED read must RAISE (never return an empty/partial snapshot that would
    reconcile the cache to nothing — the AD-14 failure contract). It also keeps
    raw ``schwab-py``/``httpx`` exceptions from leaking past the port (AD-8) by
    wrapping them. Distinct from :class:`SchwabNotConfiguredError` (a missing
    token/account is a config error, not a read failure). Never carries token or
    secret material.
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

    @staticmethod
    def _preflight_capture(seam: str, payload) -> None:
        """Passive pre-flight shape capture (Story 7.6) — a NO-OP unless enabled.

        Guarded so that when ``PREFLIGHT_CAPTURE_DIR`` is empty (the default) this
        does NOTHING and constructs NOTHING: it checks the env flag FIRST (a cheap
        ``os.environ`` read — the app's ``get_settings()`` is intentionally
        uncached, so building a ``Settings`` on every parse would be real
        hot-path overhead), and only then imports the helper, builds settings, and
        reduces/serializes. So the OFF path leaves adapter behavior byte-for-byte
        unchanged with zero added work.
        """
        import os

        if not os.environ.get("PREFLIGHT_CAPTURE_DIR", ""):
            return
        from preflight.capture import capture

        capture(get_settings(), seam, payload)

    def fetch_portfolio(self) -> PortfolioSnapshot:
        """Fetch the account's holdings + cash from Schwab (network call, Story 6.5).

        The AD-14 balances READ the single-writer projection reconciles on. Cash
        is sourced from a DEDICATED balances field
        (``securitiesAccount.currentBalances.cashBalance``), NEVER inferred from
        positions, so a cash-only account reports its true cash. Positions map to
        broker-neutral :class:`~brokers.port.Holding` rows. Parsing is DEFENSIVE
        (missing cash → 0; a position with no symbol is skipped). ``as_of`` is
        stamped ``now(UTC)`` — this is a live read.

        FAILURE CONTRACT: on ANY transport/parse/shape failure this RAISES
        :class:`SchwabReadError` — it NEVER returns an empty/partial snapshot
        (which would reconcile the cache to nothing). An HTTP-error response, a
        non-dict body, or a body missing the ``securitiesAccount`` envelope all
        raise; a raw ``httpx``/SDK exception is wrapped so nothing leaks past the
        port (AD-8). A missing token/account is a plain
        :class:`SchwabNotConfiguredError` (raised by ``_trading_client`` /
        ``_account_hash``), deliberately distinct from a read failure. Never logs
        token/secret material; schwab-py stays lazily imported (via
        ``_trading_client``).
        """
        self._require_configured()
        # Build the client + resolve the account hash first — a missing token /
        # account is a config error that must surface plainly (these raise
        # SchwabNotConfiguredError), NOT be masked into an empty snapshot.
        client = self._trading_client()
        account_hash = self._account_hash(client)

        try:
            resp = client.get_account(
                account_hash, fields=client.Account.Fields.POSITIONS
            )
            if resp.is_error:
                # An HTTP-error body (expired token, 5xx, ...) is NOT a portfolio —
                # raise rather than parse it into an empty snapshot.
                raise SchwabReadError(
                    f"Schwab account read failed (status {resp.status_code})."
                )
            body = resp.json()
            # Passive pre-flight tap (Story 7.6): capture the RAW account body's
            # shape (redacted) only when capture is enabled; no-op when OFF.
            self._preflight_capture("portfolio", body)
            if not isinstance(body, dict):
                raise SchwabReadError("Schwab account body is not an object.")
            acct = body.get("securitiesAccount")
            if not isinstance(acct, dict):
                # A valid account read ALWAYS carries the ``securitiesAccount``
                # envelope; its absence is an error/unexpected shape — RAISE, never
                # silently reconcile the cache to empty (AD-14). MISSING SUB-FIELDS
                # (currentBalances / positions) inside a valid envelope ARE
                # tolerated below (a sparse-but-real account).
                raise SchwabReadError(
                    "Schwab account body is missing the securitiesAccount envelope."
                )
            cash = self._decimal_or_zero(
                (acct.get("currentBalances") or {}).get("cashBalance")
            )

            holdings: list[Holding] = []
            for position in acct.get("positions") or []:
                position = position or {}
                instrument = position.get("instrument") or {}
                symbol = instrument.get("symbol")
                if not symbol:
                    # A position with no symbol is unusable — skip it (defensive),
                    # never fabricate a nameless holding.
                    continue
                quantity = self._decimal_or_zero(position.get("longQuantity"))
                if quantity <= 0:
                    # A short / closed / zero-long position (longQuantity 0 or
                    # reported only under shortQuantity) is outside the v1 long-only
                    # index-fund universe — skip it rather than emit a junk holding
                    # (0 shares with a non-zero market value / bogus 0 cost basis).
                    continue
                market_value = self._decimal_or_zero(position.get("marketValue"))
                # Cost basis: averagePrice × quantity when BOTH are present, else
                # None (the broker does not always report an average price).
                avg_price_raw = position.get("averagePrice")
                cost_basis: Decimal | None = None
                if avg_price_raw is not None:
                    avg_price = self._decimal_or_zero(avg_price_raw)
                    cost_basis = avg_price * quantity
                holdings.append(
                    Holding(
                        symbol=symbol,
                        quantity=quantity,
                        market_value=market_value,
                        cost_basis=cost_basis,
                    )
                )
        except SchwabReadError:
            raise
        except Exception as exc:
            # Any transport/SDK/parse failure is a FAILED read — wrap it as a clear
            # typed error so no raw SDK/httpx exception leaks past the port (AD-8)
            # and the cache is NEVER reconciled to an empty snapshot. Message
            # carries only the exception TYPE name, never body/token material.
            raise SchwabReadError(
                f"Schwab account read failed: {type(exc).__name__}."
            ) from exc

        return PortfolioSnapshot(
            as_of=datetime.now(timezone.utc),
            cash=cash,
            holdings=holdings,
        )

    async def place_order(
        self, order_intent: OrderIntent, *, idempotency_key: str
    ) -> OrderOutcome:
        """Place a whole-share MARKET or marketable LIMIT order on Schwab (Story 6.3 / 8.1).

        The single execution write (AD-7) — no poll/retry/wait-loop. Sizing is the
        locked v1 decision: fetch a placement-time quote, size a WHOLE-SHARE order
        (``quantity = floor(amount / price)``, price = ask for MARKET, limit_price
        for LIMIT) and refuse calmly via
        :class:`~brokers.port.OrderNotPlaceableError` if the dollar ``amount`` buys
        less than one share (or the quote is unusable) — no order is placed. Buy
        vs. sell chooses the matching share-quantity equity builder (schwab-py
        1.5.1 exposes no notional/fractional builder).

        A LIMIT order (Story 8.1) is MARKETABLE-only: it is guarded to be
        immediately fillable against the side-relevant leg (buy→ask, sell→bid) and
        refused if not (resting limit orders are Story B). Its price is passed to
        the ``equity_*_limit`` builder as a fixed-point STRING (money discipline).
        Only the builder + quantity construction differs from the market path; the
        post-placement fence below is shared and unchanged.

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
        from schwab.orders.equities import (
            equity_buy_limit,
            equity_buy_market,
            equity_sell_limit,
            equity_sell_market,
        )
        from schwab.utils import (
            AccountHashMismatchException,
            UnsuccessfulOrderException,
            Utils,
        )

        # Known once the placement returns an order id; kept in the outer scope so
        # the transport/parse ``except`` below can PRESERVE it on an indeterminate
        # status-read failure (a landed order must stay reconcilable — Story 6.7).
        order_ref: str | None = None
        # The resolved account hash (Story 7.5): kept in the outer scope so it can
        # ride back on every returned ``OrderOutcome.account_ref`` (the audit of
        # which account the order landed against). ``None`` until resolved.
        account_hash: str | None = None
        try:
            account_hash = self._account_hash(client)
            is_buy = order_intent.side == OrderSide.BUY
            if order_intent.order_type == OrderType.LIMIT:
                # Marketable LIMIT branch (Story 8.1): size on the LIMIT price,
                # guard that the limit is immediately fillable against the
                # side-relevant leg (buy→ask, sell→bid), and build the DAY/NORMAL
                # limit spec. Only the builder + quantity differ from the market
                # path; the post-placement fence below is shared and unchanged.
                limit_price = order_intent.limit_price
                quote = self._read_quote(client, order_intent.symbol)
                reference = self._usable_price(
                    quote,
                    "askPrice" if is_buy else "bidPrice",
                    order_intent.symbol,
                )
                # Whole-share sizing on the limit price.
                quantity = int(
                    (order_intent.amount / limit_price).to_integral_value(
                        rounding=ROUND_FLOOR
                    )
                )
                if quantity < 1:
                    raise OrderNotPlaceableError(
                        f"${order_intent.amount:.2f} buys less than one whole "
                        f"share of {order_intent.symbol} at a ${limit_price:.2f} "
                        "limit — no order was placed."
                    )
                # Marketable guard: a buy must meet/exceed the ask, a sell must
                # meet/undercut the bid — otherwise the limit isn't immediately
                # fillable (resting limit orders are coming later, Story B).
                if is_buy and limit_price < reference:
                    raise OrderNotPlaceableError(
                        f"A buy limit at ${limit_price:.2f} is below the current "
                        f"ask (${reference:.2f}), so this limit isn't immediately "
                        "fillable; resting limit orders are coming later — no "
                        "order was placed."
                    )
                if not is_buy and limit_price > reference:
                    raise OrderNotPlaceableError(
                        f"A sell limit at ${limit_price:.2f} is above the current "
                        f"bid (${reference:.2f}), so this limit isn't immediately "
                        "fillable; resting limit orders are coming later — no "
                        "order was placed."
                    )
                # CRITICAL: pass the price as a fixed-point STRING, never a
                # Decimal — schwab-py's ``set_price`` stores a str verbatim but
                # runs a Decimal/float through binary-float truncation (money
                # discipline; Story 8.1 §CRITICAL).
                price_str = format_money(limit_price)
                builder = (
                    equity_buy_limit(order_intent.symbol, quantity, price_str)
                    if is_buy
                    else equity_sell_limit(order_intent.symbol, quantity, price_str)
                )
            else:
                ask = self._quote_ask(client, order_intent.symbol)
                # Whole-share sizing: floor(amount / ask). ``amount``/``ask`` are
                # positive Decimals, so integral-floor is an honest whole-share
                # count.
                quantity = int(
                    (order_intent.amount / ask).to_integral_value(
                        rounding=ROUND_FLOOR
                    )
                )
                if quantity < 1:
                    raise OrderNotPlaceableError(
                        f"${order_intent.amount:.2f} buys less than one whole "
                        f"share of {order_intent.symbol} at about ${ask:.2f} — no "
                        "order was placed."
                    )
                builder = (
                    equity_buy_market(order_intent.symbol, quantity)
                    if is_buy
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
                account_ref=account_hash,
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
                    account_ref=account_hash,
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
                    account_ref=account_hash,
                )
            status_resp = client.get_order(order_id, account_hash)
            return self._map_order(
                status_resp.json(), broker_ref=order_ref, account_ref=account_hash
            )
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
                account_ref=account_hash,
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
        account_hash: str | None = None
        try:
            account_hash = self._account_hash(client)
            status_resp = client.get_order(int(order_ref), account_hash)
            return self._map_order(
                status_resp.json(), broker_ref=order_ref, account_ref=account_hash
            )
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
                account_ref=account_hash,
            )

    async def get_order_status_by_ref(self, broker_ref: str) -> OrderOutcome:
        """Reconcile a placed Schwab order by its persisted order id (Story 6.7).

        The DURABLE cross-request reconciliation read (AD-13): unlike
        :meth:`get_order_status`, which keys on the client idempotency key and can
        only see this instance's in-request cache, this keys on the queryable
        ``broker_ref`` (Schwab order id) the co-sign persisted — so an ambiguous
        placement is resolvable in a LATER request against a fresh adapter. Called
        ONLY by :func:`coach.execution.reconcile_pending_decision` (AD-7).

        READ-ONLY, single ``get_order``: an empty / non-usable ``broker_ref``
        (``None``/blank/non-integer) short-circuits to an honest ``PENDING``
        (``filled_qty`` 0, no ``avg_price``, no ``broker_ref``) WITHOUT ever
        building the client or touching the SDK — it NEVER searches
        (``get_orders_for_account``) and NEVER attribute-matches. With a usable id
        it builds the client, resolves the account hash, reads the one order
        (``client.get_order(int(broker_ref), hash)``) and maps it via
        :meth:`_map_order`. The whole build+read is wrapped in the SAME fence as
        :meth:`get_order_status` — any transport/SDK/parse failure surfaces
        ``TIMEOUT`` with the ``broker_ref`` PRESERVED and no raw exception leaking
        the port, so the landed order stays reconcilable. Never re-places, never
        logs token/secret material.
        """
        self._require_configured()
        # An empty / non-integer ref cannot address a Schwab order — surface an
        # honest PENDING WITHOUT touching the SDK. NEVER search, NEVER guess
        # (decision #2 / FR22 / NFR3); durable recovery is by order id or not at
        # all. ``int()`` guards a non-numeric ref up front so a garbage value can
        # never reach ``get_order`` (a wasted call) or raise past the port.
        if broker_ref is None or not str(broker_ref).strip():
            return OrderOutcome(
                status=OrderStatus.PENDING,
                filled_qty=Decimal("0"),
                avg_price=None,
                broker_ref=None,
            )
        try:
            order_id = int(str(broker_ref).strip())
        except (ValueError, TypeError):
            # A non-numeric ref (e.g. "not-an-id") degrades safely to an honest
            # PENDING — never a search, never a raw exception past the port.
            return OrderOutcome(
                status=OrderStatus.PENDING,
                filled_qty=Decimal("0"),
                avg_price=None,
                broker_ref=None,
            )
        account_hash: str | None = None
        try:
            client = self._trading_client()
            account_hash = self._account_hash(client)
            status_resp = client.get_order(order_id, account_hash)
            return self._map_order(
                status_resp.json(), broker_ref=broker_ref, account_ref=account_hash
            )
        except SchwabNotConfiguredError:
            # A DETERMINISTIC config/auth fault — raised ONLY at client build
            # (``_trading_client``/``_account_hash``), never by the actual
            # ``client.get_order`` read. Surface it DISTINCTLY instead of
            # laundering it into TIMEOUT below: a config fault is not an
            # indeterminate transport blip, and mapping it to TIMEOUT would create
            # a soft dead-end where every retry re-launders the same fault with no
            # honest "reconnect" signal. This is SAFE here because the method is
            # READ-ONLY — the fault means the read never happened, so there is no
            # phantom fill and no order-status ambiguity; it never re-places and
            # never searches. Mirrors ``place_order``'s deliberate exclusion of
            # ``SchwabNotConfiguredError`` from its pre-placement ``except`` tuple.
            # The reconcile endpoint maps this to a calm 409 reconnect.
            raise
        except Exception:
            # The read is over an order KNOWN to have been placed (its id was
            # persisted at co-sign), so — exactly like ``get_order_status`` — this
            # is a FENCE (bare ``except Exception``), not a curated blocklist. A
            # transport failure, a non-JSON/non-dict body, a NaN Decimal
            # comparison (``ArithmeticError``), an unexpected response shape
            # (``AttributeError``), a missing token (``SchwabNotConfiguredError``)
            # or any SDK-specific error is INDETERMINATE — never leak past the
            # port, never a phantom fill. PRESERVE the known ``broker_ref`` so the
            # order stays reconcilable. Surface TIMEOUT. This is a READ: it never
            # re-places, never searches.
            return OrderOutcome(
                status=OrderStatus.TIMEOUT,
                filled_qty=Decimal("0"),
                avg_price=None,
                broker_ref=broker_ref,
                account_ref=account_hash,
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
        """Resolve (once) and cache the account hash Schwab keys placements on.

        SELECTION-AWARE (Story 7.5): Schwab returns a list of
        ``{accountNumber, hashValue}``. The hash — not the raw account number — is
        what the trading endpoints key on, but the hash is OPAQUE and can rotate,
        so the stable ``accountNumber`` is the selector (``SCHWAB_ACCOUNT_ID``) and
        the resolved ``hashValue`` is what gets used + recorded. An empty list or a
        missing hash on the selected account is a clear account/config problem
        (surfaced plainly, never a phantom fill), distinct from a transport
        failure. A malformed non-list body raises a Key/Index/Type error, caught as
        INDETERMINATE by the callers.

        Selection:
        - ``SCHWAB_ACCOUNT_ID`` set → pick the account whose ``accountNumber``
          matches; refuse with :class:`SchwabAccountSelectionError` if none does.
        - else exactly ONE account → use it (unambiguous).
        - else (>1 account, no id) → refuse with
          :class:`SchwabAccountSelectionError`. NEVER ``accounts[0]``.
        """
        if self._account_hash_cache is None:
            resp = client.get_account_numbers()
            accounts = resp.json()
            # Passive pre-flight tap (Story 7.6): capture the RAW account-numbers
            # list's shape (redacted) only when capture is enabled; no-op when OFF.
            self._preflight_capture("account_numbers", accounts)
            if not accounts:
                raise SchwabNotConfiguredError(
                    "Schwab returned no account for this login; cannot place an order."
                )
            # Build ``(accountNumber, hashValue)`` pairs DEFENSIVELY: skip any
            # malformed / non-dict entry rather than raise a raw ``AttributeError``
            # on ``.get`` (pre-placement; no order placed). Ambiguity is judged on
            # the RAW ``accounts`` count, NOT the well-formed survivor count: a
            # multi-account login with one malformed entry must still REFUSE (never
            # silently trade the sole survivor), so the never-``accounts[0]``
            # invariant holds even on a partially-malformed body.
            raw_count = len(accounts)
            pairs: list[tuple[str, str]] = []
            for entry in accounts:
                if not isinstance(entry, dict):
                    continue
                account_number = entry.get("accountNumber")
                hash_value = entry.get("hashValue")
                pairs.append((account_number, hash_value))

            # ``.strip()`` so a whitespace-only value (a stray blank in an env
            # file) is treated as UNSET — falls through to the count-based branch
            # rather than raising a misleading "does not match any account".
            account_id = get_settings().SCHWAB_ACCOUNT_ID.strip()
            if account_id:
                # Match on the STABLE account number (``str``-normalized so a
                # numeric ``accountNumber`` from the live API still compares equal).
                # A DUPLICATE match is ambiguous — refuse rather than pick the first
                # (the same wrong-account risk this story removes).
                matches = [h for (num, h) in pairs if str(num) == account_id]
                if len(matches) == 0:
                    raise SchwabAccountSelectionError(
                        "SCHWAB_ACCOUNT_ID does not match any account on this "
                        "Schwab login; cannot place an order. No order was placed."
                    )
                if len(matches) > 1:
                    raise SchwabAccountSelectionError(
                        "SCHWAB_ACCOUNT_ID matches more than one Schwab account; "
                        "cannot safely choose one. No order was placed."
                    )
                hash_value = matches[0]
                if not hash_value:
                    raise SchwabNotConfiguredError(
                        "The selected Schwab account is missing its trading hash; "
                        "cannot place an order."
                    )
            elif raw_count == 1:
                if not pairs:
                    # The sole account entry was malformed (non-dict): a config
                    # problem, surfaced plainly (never a phantom placement).
                    raise SchwabNotConfiguredError(
                        "Schwab account body is malformed; cannot place an order."
                    )
                hash_value = pairs[0][1]
                if not hash_value:
                    raise SchwabNotConfiguredError(
                        "Schwab account is missing its trading hash; "
                        "cannot place an order."
                    )
            else:
                # More than one account and no explicit selection — NEVER
                # ``accounts[0]``. Refuse calmly, pre-placement/pre-read.
                raise SchwabAccountSelectionError(
                    "This Schwab login exposes more than one account; set "
                    "SCHWAB_ACCOUNT_ID to choose which one to trade. No order was "
                    "placed."
                )

            self._account_hash_cache = hash_value
        return self._account_hash_cache

    def _read_quote(self, client, symbol: str) -> dict:
        """Read ``symbol``'s inner quote node + run the passive pre-flight tap.

        Factored out of :meth:`_quote_ask` (Story 8.1) so both the MARKET ask read
        and a LIMIT's side-relevant leg (ask for a buy, bid for a sell) share one
        network read and the SAME Story 7.6 quote capture. Returns the innermost
        ``{...}["quote"]`` dict (``{}`` when absent). The exact quote JSON shape is
        fixture-driven and re-confirmed at go-live.
        """
        resp = client.get_quote(symbol)
        data = resp.json() or {}
        # Passive pre-flight tap (Story 7.6): capture the RAW quote body's shape
        # (redacted) only when capture is enabled; no-op when OFF.
        self._preflight_capture("quote", data)
        node = data.get(symbol) or {}
        return node.get("quote") or {}

    def _usable_price(self, quote: dict, field: str, symbol: str) -> Decimal:
        """Return the finite, positive ``quote[field]`` as ``Decimal``; refuse if not.

        The shared unusable-quote refusal (Story 8.1): a missing / non-numeric /
        non-positive / non-finite ``field`` (``askPrice`` or ``bidPrice``) means we
        cannot honestly size or guard against a real price, so refuse calmly via
        :class:`~brokers.port.OrderNotPlaceableError` rather than placing on a
        guessed price. A NaN/Infinity value parses to a valid-but-non-finite
        Decimal (no raise on parse), so ``is_finite()`` is checked before any ``<=``
        comparison (which would otherwise raise ``InvalidOperation``).
        """
        raw = quote.get(field)
        if raw is None:
            raise OrderNotPlaceableError(
                f"No usable quote for {symbol} right now — no order was placed."
            )
        try:
            price = Decimal(str(raw))
        except (InvalidOperation, ValueError):
            price = Decimal("0")
        if not price.is_finite() or price <= 0:
            raise OrderNotPlaceableError(
                f"No usable quote for {symbol} right now — no order was placed."
            )
        return price

    def _quote_ask(self, client, symbol: str) -> Decimal:
        """Read the current ask for ``symbol`` (Decimal); refuse if unusable.

        Sizing on the MARKET path uses the ask (the locked decision). Delegates to
        :meth:`_read_quote` + :meth:`_usable_price` so the market read and the
        Story 8.1 limit read share one code path and one pre-flight tap.
        """
        return self._usable_price(
            self._read_quote(client, symbol), "askPrice", symbol
        )

    def _map_order(
        self,
        order_json: dict,
        *,
        broker_ref: str | None,
        account_ref: str | None = None,
    ) -> OrderOutcome:
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
            account_ref=account_ref,
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
        the shape is absent (fixture-driven; re-confirmed at go-live). A non-finite
        parse (``NaN``/``Infinity``) is sanitized to ``None`` — mirroring the
        sibling :meth:`_decimal_or_zero` — so a non-finite average can never reach
        the wire or the persisted (reconciliation/cosign) snapshot as the literal
        ``"NaN"``/``"Infinity"`` (Story 6.7 makes this durably persisted).
        """
        for key in ("avgFillPrice", "averagePrice", "price"):
            val = order_json.get(key)
            if val is not None:
                try:
                    d = Decimal(str(val))
                except (InvalidOperation, ValueError):
                    return None
                return d if d.is_finite() else None
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
            avg = total_cost / total_qty
            return avg if avg.is_finite() else None
        return None

    @staticmethod
    def _to_broker_tokens(token: dict) -> BrokerTokens:
        """Normalise schwab-py's token dict into :class:`BrokerTokens`."""
        # Passive pre-flight tap (Story 7.6): capture the RAW token dict's shape
        # (redacted) only when capture is enabled; a true no-op when OFF.
        SchwabAdapter._preflight_capture("token", token)
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
