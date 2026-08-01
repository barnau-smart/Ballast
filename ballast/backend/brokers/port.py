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
    ``None`` when the broker did not assign one). Frozen so an outcome is an
    immutable value. Story 4.6 returns exactly one of these from
    :meth:`BrokerPort.place_order`; polling/reconciliation is Story 4.7.
    """

    status: OrderStatus
    filled_qty: Decimal
    avg_price: Decimal | None = None
    broker_ref: str | None = None


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
