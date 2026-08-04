"""FakeBrokerAdapter — the credential-free implementation of :class:`BrokerPort`.

This is the DEFAULT adapter (``BROKER_ADAPTER=fake``). It makes the entire
OAuth link flow — authorize -> callback -> token storage -> status — fully
runnable and testable locally with ZERO credentials and ZERO network calls.

Everything is deterministic so tests can assert exact values. When the user's
real Schwab developer app is approved, flipping ``BROKER_ADAPTER=schwab`` swaps
in the real adapter with no caller changes (AD-8).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR
from urllib.parse import quote, urlencode

from coach.recommendation import OrderIntent, OrderSide, OrderType
from brokers.port import (
    BrokerPort,
    BrokerTokens,
    Holding,
    OrderNotPlaceableError,
    OrderOutcome,
    OrderStatus,
    PortfolioSnapshot,
)

# A recognisable, obviously-fake authorization host so it can never be mistaken
# for a real Schwab URL in logs or the UI.
_FAKE_AUTH_BASE = "https://fake-broker.ballast.local/oauth/authorize"

# Deterministic, obviously-fake token material. Tests assert the stored DB value
# differs from these plaintext strings (proving encryption at rest).
FAKE_ACCESS_TOKEN = "fake-access-token-ballast-local"
FAKE_REFRESH_TOKEN = "fake-refresh-token-ballast-local"

# A small, obviously-fake but realistic holdings set + cash. Deterministic so
# tests can assert exact values. Money is Decimal (never float). These broad
# index funds mirror the v1 "stable core" universe (Story 2.5 maps them).
FAKE_HOLDINGS: tuple[Holding, ...] = (
    Holding(
        symbol="VTI",
        quantity=Decimal("10"),
        market_value=Decimal("2500.00"),
        cost_basis=Decimal("2000.00"),
    ),
    Holding(
        symbol="VXUS",
        quantity=Decimal("20"),
        market_value=Decimal("1200.00"),
        cost_basis=Decimal("1100.00"),
    ),
    Holding(
        symbol="BND",
        quantity=Decimal("15"),
        market_value=Decimal("1050.00"),
        cost_basis=Decimal("1080.00"),
    ),
)
FAKE_CASH = Decimal("750.25")

# A deterministic, obviously-fake fill price the fake adapter reports for every
# placed order. No wall-clock, no market data, no randomness — so the same
# order placed twice yields a byte-identical OrderOutcome (tests assert this).
# Money is Decimal (never float).
FAKE_FILL_PRICE = Decimal("100.00")

# A fixed base ``as_of`` so reconcile-wins tests can drive older/newer snapshots
# deterministically. Callers/tests advance it via ``as_of_offset``; the default
# is a stable timestamp (no wall-clock, so assertions never flake).
FAKE_AS_OF_BASE = datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc)


class FakeBrokerAdapter(BrokerPort):
    """A deterministic, offline stand-in for a real brokerage.

    ``as_of_offset`` shifts the snapshot's ``as_of`` from :data:`FAKE_AS_OF_BASE`
    so tests can construct older/newer snapshots on demand to exercise the
    single-writer reconcile-wins rule (AD-14). It never uses wall-clock time, so
    every fetched snapshot is fully deterministic.
    """

    provider = "fake"

    def __init__(self, *, as_of_offset: timedelta | None = None) -> None:
        self._as_of_offset = as_of_offset or timedelta(0)
        # Per-instance idempotency store: idempotency_key -> recorded outcome.
        # ``get_broker()`` returns a fresh adapter per request, so this store
        # lives for exactly one approve flow (4.7 scope; cross-request key reuse
        # needs the persisted decision record, Story 4.9). Making the fake
        # idempotency-keyed is what makes "a timeout never double-places"
        # structural: a re-place with a seen key returns the recorded order and
        # records it only once (AD-13).
        self._orders: dict[str, OrderOutcome] = {}
        # Per-instance ``broker_ref -> OrderOutcome`` store for the DURABLE
        # cross-request reconcile read (Story 6.7). Populated in ``place_order``
        # whenever the recorded outcome carries a non-``None`` ``broker_ref``, plus
        # via ``seed_order_status_by_ref`` so a test can register the state a LATER
        # reconcile should observe (the real cross-request path reads a persisted
        # ``broker_ref``, which a fresh in-instance ``_orders`` cache would miss).
        self._orders_by_ref: dict[str, OrderOutcome] = {}

    def authorization_url(self, state: str) -> str:
        """Return a deterministic fake authorization URL embedding ``state``."""
        query = urlencode(
            {
                "response_type": "code",
                "client_id": "fake-client-id",
                "state": state,
            },
            quote_via=quote,
        )
        return f"{_FAKE_AUTH_BASE}?{query}"

    def exchange_code(self, code: str, state: str) -> BrokerTokens:
        """Return deterministic fake tokens for any ``code`` (no network).

        ``state`` is accepted to match :class:`BrokerPort` (the real adapter
        needs it for authlib's state check); the fake ignores it.
        """
        # Include a fixed suffix so the value is stable across runs; the code is
        # NOT echoed into the token (and is never logged).
        return BrokerTokens(
            access_token=FAKE_ACCESS_TOKEN,
            refresh_token=FAKE_REFRESH_TOKEN,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

    def fetch_portfolio(self) -> PortfolioSnapshot:
        """Return the deterministic fake holdings/cash snapshot (no network).

        ``as_of`` is :data:`FAKE_AS_OF_BASE` shifted by this adapter's
        ``as_of_offset`` — deterministic so the reconcile-wins tests can build a
        newer/older snapshot without touching the wall clock.
        """
        return PortfolioSnapshot(
            as_of=FAKE_AS_OF_BASE + self._as_of_offset,
            cash=FAKE_CASH,
            holdings=list(FAKE_HOLDINGS),
        )

    async def place_order(
        self, order_intent: OrderIntent, *, idempotency_key: str
    ) -> OrderOutcome:
        """Return a deterministic ``FILLED`` :class:`OrderOutcome` (no network).

        Idempotency-keyed (Story 4.7): if ``idempotency_key`` was already placed,
        the recorded outcome is returned unchanged and NO second order is
        recorded — a re-place with the same key never double-places (AD-13,
        FR22/NFR3). Otherwise the deterministic ``FILLED`` outcome is computed,
        recorded under the key, and returned.

        Fully deterministic — NO wall-clock, NO randomness — so an identical
        ``(order_intent, idempotency_key)`` always yields an equal (frozen)
        outcome (tests assert this). The dollar ``amount`` is converted to a
        non-negative share ``filled_qty`` at the fixed :data:`FAKE_FILL_PRICE`,
        and ``broker_ref`` is derived stably from ``idempotency_key`` so the
        reference round-trips without a wall-clock timestamp. The fake's default
        placement is always ``filled`` (unchanged from 4.6). Never logs
        token/secret material.
        """
        recorded = self._orders.get(idempotency_key)
        if recorded is not None:
            return recorded
        if order_intent.order_type == OrderType.LIMIT:
            # Marketable LIMIT branch (Story 8.1): floor sizing at the limit price,
            # a fill at exactly the limit price, and a marketable guard vs the
            # deterministic reference quote. A non-marketable / sub-share limit
            # raises ``OrderNotPlaceableError`` BEFORE anything is recorded.
            filled_qty, avg_price = self._limit_fill(order_intent)
        else:
            # MARKET path — byte-for-byte UNCHANGED (AC 5): fractional
            # ``amount / FAKE_FILL_PRICE``, no flooring, no <1-share refusal.
            filled_qty = order_intent.amount / FAKE_FILL_PRICE
            avg_price = FAKE_FILL_PRICE
        outcome = OrderOutcome(
            status=OrderStatus.FILLED,
            filled_qty=filled_qty,
            avg_price=avg_price,
            broker_ref=f"fake-order-{idempotency_key}",
        )
        self._orders[idempotency_key] = outcome
        # Also index by the broker reference so the durable cross-request reconcile
        # read (Story 6.7) can find this order by its persisted ``broker_ref``.
        if outcome.broker_ref is not None:
            self._orders_by_ref[outcome.broker_ref] = outcome
        return outcome

    def _limit_fill(self, order_intent: OrderIntent) -> tuple[Decimal, Decimal]:
        """Compute a deterministic marketable-LIMIT fill (Story 8.1).

        :data:`FAKE_FILL_PRICE` is the reference quote for BOTH the ask and the
        bid. Marketable guard: a BUY is refused when ``limit_price`` is below the
        (ask) reference; a SELL when it is above the (bid) reference — a
        non-marketable limit isn't immediately fillable, so it is refused calmly
        via :class:`~brokers.port.OrderNotPlaceableError` (resting limit orders are
        Story B). Sizing floors ``amount / limit_price`` to whole shares and
        refuses a sub-share order the same way. The fill is at exactly the limit
        price. Fully deterministic (no wall-clock, no randomness). Returns
        ``(filled_qty, avg_price)``.
        """
        limit_price = order_intent.limit_price
        # Defense-in-depth (Story 8.1 review): the execution gate
        # (validate_order_intent) already guarantees a finite, positive
        # limit_price for a LIMIT on the production path, but a direct/future
        # caller must never make the comparisons/division below raise a raw
        # TypeError/InvalidOperation — refuse calmly instead.
        if limit_price is None or not limit_price.is_finite() or limit_price <= 0:
            raise OrderNotPlaceableError(
                "This limit order has no usable limit price — no order was placed."
            )
        if order_intent.side == OrderSide.BUY:
            if limit_price < FAKE_FILL_PRICE:
                raise OrderNotPlaceableError(
                    f"A buy limit at ${limit_price:.2f} is below the current "
                    f"price (${FAKE_FILL_PRICE:.2f}), so this limit isn't "
                    "immediately fillable; resting limit orders are coming later "
                    "— no order was placed."
                )
        else:
            if limit_price > FAKE_FILL_PRICE:
                raise OrderNotPlaceableError(
                    f"A sell limit at ${limit_price:.2f} is above the current "
                    f"price (${FAKE_FILL_PRICE:.2f}), so this limit isn't "
                    "immediately fillable; resting limit orders are coming later "
                    "— no order was placed."
                )
        quantity = int(
            (order_intent.amount / limit_price).to_integral_value(
                rounding=ROUND_FLOOR
            )
        )
        if quantity < 1:
            verb = "buys" if order_intent.side == OrderSide.BUY else "sells"
            raise OrderNotPlaceableError(
                f"${order_intent.amount:.2f} {verb} less than one whole share at "
                f"a ${limit_price:.2f} limit — no order was placed."
            )
        return Decimal(quantity), limit_price

    async def get_order_status(self, idempotency_key: str) -> OrderOutcome:
        """Return the recorded :class:`OrderOutcome` for ``idempotency_key`` (no network).

        The reconciliation read (Story 4.7, AD-13). A key that was placed on this
        adapter returns its recorded outcome. An UNKNOWN key returns an honest
        ``pending`` outcome (``filled_qty`` 0, no ``avg_price``, no
        ``broker_ref``) — the fake NEVER invents a fill for an order it has no
        record of. Fully deterministic (no wall-clock, no randomness). Never logs
        token/secret material.
        """
        recorded = self._orders.get(idempotency_key)
        if recorded is not None:
            return recorded
        return OrderOutcome(
            status=OrderStatus.PENDING,
            filled_qty=Decimal("0"),
            avg_price=None,
            broker_ref=None,
        )

    def seed_order_status_by_ref(
        self, broker_ref: str, outcome: OrderOutcome
    ) -> None:
        """Register the :class:`OrderOutcome` a later reconcile-by-ref should see.

        A small TEST-SEED helper (Story 6.7): the durable cross-request reconcile
        reads an order by its persisted ``broker_ref``, but a fresh per-request
        adapter never placed that order (its ``_orders_by_ref`` is empty). This
        lets a test register the ``broker_ref → OrderOutcome`` state a subsequent
        :meth:`get_order_status_by_ref` should observe — the offline stand-in for
        "the broker now reports this order FILLED". Deterministic, no network.
        """
        self._orders_by_ref[broker_ref] = outcome

    async def get_order_status_by_ref(self, broker_ref: str) -> OrderOutcome:
        """Reconcile a placed order by its ``broker_ref`` (Story 6.7, no network).

        The durable cross-request reconciliation read (AD-13). A ``broker_ref``
        that was placed on (or seeded into) this adapter returns its recorded
        :class:`OrderOutcome`. An UNKNOWN or ``None`` ref returns an honest
        ``PENDING`` (``filled_qty`` 0, no ``avg_price``, no ``broker_ref``) — the
        fake NEVER invents a fill and NEVER searches for a matching order.
        Deterministic (no wall-clock, no randomness). Never logs token/secret
        material.
        """
        if broker_ref is None:
            return OrderOutcome(
                status=OrderStatus.PENDING,
                filled_qty=Decimal("0"),
                avg_price=None,
                broker_ref=None,
            )
        recorded = self._orders_by_ref.get(broker_ref)
        if recorded is not None:
            return recorded
        return OrderOutcome(
            status=OrderStatus.PENDING,
            filled_qty=Decimal("0"),
            avg_price=None,
            broker_ref=None,
        )
