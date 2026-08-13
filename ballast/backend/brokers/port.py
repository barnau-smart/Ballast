"""The Broker Port — the sole boundary between Ballast and any brokerage.

AD-8: external dependencies live behind ports. The Coach/API layers depend
ONLY on :class:`BrokerPort` (this module) — never on ``schwab-py`` or any
concrete SDK. The concrete adapter (Schwab vs Fake) is chosen by config and
swapped without touching a single caller.

AD-6: the Broker Port is the one owner of brokerage state. Nothing else in the
codebase talks to a brokerage.

Scope history: Story 2.1 defined only the OAuth *linking* surface
(``authorization_url`` / ``exchange_code``) plus the read (``fetch_portfolio``).
Story 4.6 added the execution contract: ``place_order`` returning a normalized
:class:`OrderOutcome`. Reconciliation — ``get_order_status`` plus the
partial/rejected/timeout/pending handling and idempotency retry-reuse — LANDS in
**Story 4.7**: ``get_order_status(idempotency_key)`` reads the authoritative
state for an already-placed order (reusing the client key), so an indeterminate
placement (``timeout``/``pending``) is reconciled read-only, never re-placed. The
:class:`OrderStatus` enum defined all five values from 4.6 as the fixed contract;
4.7 makes the fake adapter honestly report them via reconciliation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum

from coach.recommendation import OrderIntent


@dataclass(frozen=True)
class BrokerTokens:
    """OAuth tokens returned by a brokerage after a successful code exchange.

    These are the crown jewels (AD-10 / NFR1): they are encrypted at the
    application layer before storage and are NEVER logged. ``expires_at`` is a
    timezone-aware UTC datetime.
    """

    access_token: str
    refresh_token: str
    expires_at: datetime


@dataclass(frozen=True)
class Holding:
    """One position in a user's brokerage account — a broker-neutral shape.

    Money is ``Decimal``, NEVER binary float (consistency convention). ``symbol``
    is the broker's instrument symbol. ``cost_basis`` may be ``None`` when the
    broker does not report it. This is read-only projection data — no order or
    execution semantics live here.
    """

    symbol: str
    quantity: Decimal
    market_value: Decimal
    cost_basis: Decimal | None = None


@dataclass(frozen=True)
class PortfolioSnapshot:
    """An authoritative point-in-time read of a user's holdings + cash.

    ``as_of`` is the broker's timestamp for this snapshot (tz-aware UTC) and is
    the key the single-writer projection reconciles on (AD-14): a newer ``as_of``
    supersedes cached rows; a stale one never clobbers newer truth. ``cash`` is
    the settled cash balance as ``Decimal``.
    """

    as_of: datetime
    cash: Decimal
    holdings: list[Holding] = field(default_factory=list)
    # The broker's account type, normalized upper (``"MARGIN"`` / ``"CASH"``) or
    # ``None`` when the broker doesn't report it (Story 10.10). Informational only —
    # used to gently warn a user their account is a margin account; NEVER changes the
    # cash figure or any money math (the engine still anchors to ``cashBalance``).
    account_type: str | None = None


class OrderStatus(str, Enum):
    """The closed set of normalized order outcome states (the fixed contract).

    All five values were defined as the durable contract in Story 4.6 even
    though 4.6 only ever produced ``FILLED``. The remaining states
    (``partial``/``rejected``/``timeout``/``pending``) are honestly reconciled
    as of **Story 4.7** via ``get_order_status`` — an indeterminate placement
    (``timeout``/``pending``) reads authoritative state rather than assuming.
    """

    FILLED = "filled"
    PARTIAL = "partial"
    REJECTED = "rejected"
    TIMEOUT = "timeout"
    PENDING = "pending"


@dataclass(frozen=True)
class OrderOutcome:
    """The normalized result of a placed order (AD-13, broker-neutral).

    ``status`` is an :class:`OrderStatus`; ``filled_qty`` and ``avg_price`` are
    ``Decimal`` (NEVER binary float) — ``avg_price`` may be ``None`` when nothing
    filled; ``broker_ref`` is the broker's stable reference for the order (or
    ``None`` when the broker did not assign one). ``account_ref`` (Story 7.5) is
    the broker-neutral account the order was placed/read against (the resolved
    Schwab trading hash), ``None`` for the fake or when unknown; it is persisted
    into the decision record's ``cosign_snapshot`` (no schema change) as the audit
    of which account the order actually landed in. Frozen so an outcome is an
    immutable value. Story 4.6 returns exactly one of these from
    :meth:`BrokerPort.place_order`; polling/reconciliation is Story 4.7.
    """

    status: OrderStatus
    filled_qty: Decimal
    avg_price: Decimal | None = None
    broker_ref: str | None = None
    account_ref: str | None = None


@dataclass(frozen=True)
class Quote:
    """A live top-of-book quote for a symbol — ``bid``/``ask`` as ``Decimal`` (8.4).

    The clean read seam the coach's suggest-order engine depends on so it never
    reaches into a concrete adapter for a price. ``bid``/``ask`` are ``Decimal``
    (NEVER binary float, money discipline) and represent the best current
    bid/ask. Frozen so a quote is an immutable value. Producing it is a READ — it
    places nothing. An adapter that cannot read a usable ask/bid refuses via
    :class:`OrderNotPlaceableError` (mapped to the same calm 422 as an
    out-of-scope order) rather than returning a guessed price.
    """

    bid: Decimal
    ask: Decimal


class OrderNotPlaceableError(ValueError):
    """Raised when a whole-share market order cannot be constructed (Story 6.3).

    A deliberate, calm PRE-PLACEMENT refusal — the broker is never touched. It
    fires when a dollar ``amount`` buys less than one whole share
    (``floor(amount / ask) < 1``), or when the placement-time quote is unusable
    (missing / non-positive ask). This is vendor-neutral and distinct from the
    transport→``OrderStatus.TIMEOUT`` mapping: a transport failure is an
    INDETERMINATE placement (an order may have landed), whereas this is a
    definite "no order was placed, here is a clear reason". The API layer maps it
    to the same calm 422 as :class:`~coach.execution.OrderScopeError` and
    RELEASES the atomic claim so the decision stays retryable.
    """


class BrokerPort(ABC):
    """The abstract brokerage boundary — the only type callers depend on.

    Implementations: :class:`~brokers.fake_adapter.FakeBrokerAdapter` (local /
    dev / test, zero credentials) and
    :class:`~brokers.schwab_adapter.SchwabAdapter` (real, credential-gated).

    This contract covers OAuth linking, the portfolio read, order placement
    (:meth:`place_order` → :class:`OrderOutcome`, Story 4.6), and — as of Story
    4.7 — reconciliation (:meth:`get_order_status` → :class:`OrderOutcome`),
    which reads the authoritative state of an already-placed order by its client
    idempotency key. Callers keep depending on one interface.
    """

    #: The brokerage this adapter places through — a contract-level attribute
    #: (``"fake"`` / ``"schwab"``). Every concrete adapter sets it; declaring it
    #: on the port makes ``broker.provider`` a guaranteed part of the contract
    #: rather than an incidental attribute, so placement-time integrity can assert
    #: the authenticated session's ``provider`` matches the placing adapter
    #: (Story 4.8, AD-11). It is non-secret metadata, never a token.
    provider: str

    @abstractmethod
    def authorization_url(self, state: str) -> str:
        """Return the brokerage authorization URL the user should visit.

        ``state`` is an opaque CSRF token the caller generated; the adapter
        embeds it so it round-trips back on the callback and can be validated.
        """
        raise NotImplementedError

    @abstractmethod
    def exchange_code(self, code: str, state: str) -> BrokerTokens:
        """Exchange the OAuth ``code`` from the callback for :class:`BrokerTokens`.

        ``state`` is the SAME CSRF token that was embedded in
        :meth:`authorization_url` and round-tripped back on the callback. It is
        passed here because real OAuth (authlib/schwab-py) validates that the
        state returned on the redirect matches the state that initiated the
        flow — the adapter must reconstruct the redirect URL with this state or
        the exchange is rejected. The ``code`` is a one-time secret — never log
        it.
        """
        raise NotImplementedError

    @abstractmethod
    def fetch_portfolio(self) -> PortfolioSnapshot:
        """Return the broker-authoritative :class:`PortfolioSnapshot` (a READ).

        This is the holdings/cash read the single-writer portfolio projection
        (Story 2.3, AD-14) consumes to reconcile ``portfolio_cache``. The broker
        is the authoritative source; the cache is a derived read-model. This is
        NOT an execution method (it places nothing), so it is
        distinct from the Epic 4 execution/reconciliation surface
        (``place_order`` -> ``OrderOutcome``, Story 4.6; ``get_order_status`` ->
        ``OrderOutcome``, Story 4.7).
        """
        raise NotImplementedError

    @abstractmethod
    async def place_order(
        self, order_intent: OrderIntent, *, idempotency_key: str
    ) -> OrderOutcome:
        """Place an order for ``order_intent`` and return its :class:`OrderOutcome`.

        The SINGLE execution write on the port (AD-7). It is called ONLY by the
        Coach Engine's execution owner
        (:func:`coach.execution.execute_approved_order`) — never by an API
        handler directly and never on an expired session (the live-session gate
        is enforced upstream, Story 4.6 / AD-11). ``idempotency_key`` is the
        client-minted key carried on the request; as of **Story 4.7** its
        retry-reuse makes a re-place with the same key return the already-recorded
        order (no double-place), and :meth:`get_order_status` reads state back by
        that same key. This method itself must NOT poll, retry, or wait-loop; it
        returns exactly one placement ``OrderOutcome``.

        Money in the returned outcome is ``Decimal`` (never binary float). This
        method never logs token/secret material.
        """
        raise NotImplementedError

    @abstractmethod
    async def get_order_status(self, idempotency_key: str) -> OrderOutcome:
        """Read the authoritative :class:`OrderOutcome` for an already-placed order.

        The reconciliation primitive (AD-13), landing in Story 4.7. Given the
        SAME client ``idempotency_key`` that :meth:`place_order` used, it returns
        the broker's current authoritative state for that order — never a guess.
        The Coach Engine execution owner
        (:func:`coach.execution.execute_approved_order`) calls this ONCE, and only
        when a placement was indeterminate (``timeout``/``pending``), to surface
        the true state read-only — it never re-places (AD-7, FR22/NFR3). This is
        a single read, not a wait-until-terminal poll; if the broker still reports
        ``pending``, that honest state is surfaced.

        Money in the returned outcome is ``Decimal`` (never binary float). This
        method never logs token/secret material.
        """
        raise NotImplementedError

    @abstractmethod
    async def get_order_status_by_ref(self, broker_ref: str) -> OrderOutcome:
        """Reconcile a previously-placed order **by its persisted broker order id**.

        The DURABLE, cross-request reconciliation primitive (Story 6.7, AD-13).
        Unlike :meth:`get_order_status` — which keys on the client
        ``idempotency_key`` and can only read the adapter's in-request cache
        (empty in any later request) — this keys on the queryable ``broker_ref``
        (order id) that the co-sign persisted, so an ambiguous
        ``timeout``/``pending`` placement can be resolved in a LATER request. It
        is called ONLY by the Coach Engine's ``reconcile_pending_decision``
        (:func:`coach.execution.reconcile_pending_decision`, AD-7).

        READ-ONLY and single-shot: a single ``get_order`` read — it NEVER places
        (:meth:`place_order` is never called), NEVER mints a key, NEVER loops or
        polls, and NEVER calls ``get_orders_for_account`` or attribute/amount/time
        fuzzy-matching. An unknown/empty/unusable ``broker_ref`` returns an honest
        ``PENDING`` (``filled_qty=0``, ``avg_price=None``, ``broker_ref=None``)
        WITHOUT touching the broker — it never searches or guesses. A
        transport/SDK/parse error on the read is INDETERMINATE → ``TIMEOUT`` with
        the ``broker_ref`` PRESERVED and NO raw exception leaking past the port
        (mirrors the :meth:`get_order_status` fence discipline), so a landed order
        stays reconcilable.

        Money in the returned outcome is ``Decimal`` (never binary float). This
        method never logs token/secret material.
        """
        raise NotImplementedError

    @abstractmethod
    async def get_quote(self, symbol: str) -> Quote:
        """Read the current top-of-book :class:`Quote` for ``symbol`` (Story 8.4).

        The clean READ seam the coach's suggest-order engine
        (:func:`coach.suggest.suggest_resting_order`) depends on so it never
        reaches into a concrete adapter to learn the live ask. It places NOTHING —
        it is a pure read, the sibling of 8.2's ``cancel_order`` port addition.

        The concrete adapter returns ``bid``/``ask`` as ``Decimal`` (never binary
        float). A missing / non-positive / non-finite ask or bid is not a usable
        price, so the adapter refuses via :class:`OrderNotPlaceableError` (the API
        maps it to the same calm 422 as an out-of-scope order) rather than
        returning a guessed number. Never logs token/secret material.
        """
        raise NotImplementedError

    @abstractmethod
    async def cancel_order(self, broker_ref: str) -> OrderOutcome:
        """Cancel a previously-placed order **by its persisted broker order id** (8.2).

        The only NEW broker verb of Story 8.2 (the resting-order lifecycle): a
        resting limit that co-signed ``pending`` can be affirmatively cancelled.
        Cancel reuses the closed 5-member :class:`OrderStatus` contract — a
        cancelled order surfaces as ``REJECTED`` (terminal, not re-placeable);
        there is NO new ``cancelled`` member (Schwab already normalizes
        ``CANCELED`` → ``REJECTED`` in ``_map_order``).

        Called ONLY by the Coach Engine's cancel owner
        (:func:`coach.execution.cancel_pending_decision`, AD-7) — never by an API
        handler directly. That owner asserts placement-time session integrity
        FIRST and short-circuits a terminal / no-``broker_ref`` order WITHOUT ever
        reaching this method, so this is only ever invoked with a usable
        ``broker_ref`` on a live, provider-matched session.

        The concrete adapter cancels the order at the broker and reads back the
        authoritative post-cancel state (mapping ``CANCELED`` → ``REJECTED``). A
        transport/SDK/parse failure is INDETERMINATE → ``TIMEOUT`` with the
        ``broker_ref`` PRESERVED and NO raw exception leaking past the port
        (mirrors the :meth:`get_order_status_by_ref` fence discipline), so the
        order stays reconcilable.

        Money in the returned outcome is ``Decimal`` (never binary float). This
        method never logs token/secret material.
        """
        raise NotImplementedError
