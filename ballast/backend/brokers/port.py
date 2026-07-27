"""The Broker Port — the sole boundary between Ballast and any brokerage.

AD-8: external dependencies live behind ports. The Coach/API layers depend
ONLY on :class:`BrokerPort` (this module) — never on ``schwab-py`` or any
concrete SDK. The concrete adapter (Schwab vs Fake) is chosen by config and
swapped without touching a single caller.

AD-6: the Broker Port is the one owner of brokerage state. Nothing else in the
codebase talks to a brokerage.

Scope of THIS story (2.1): only the OAuth *linking* surface is defined here —
``authorization_url`` and ``exchange_code``. The full Broker Port Contract
(order placement, ``OrderOutcome``, ``get_order_status``, AD-13) arrives with
execution in Epic 4. This interface is deliberately narrow and WILL extend
then; do NOT add order/execution methods here.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


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


class BrokerPort(ABC):
    """The abstract brokerage boundary — the only type callers depend on.

    Implementations: :class:`~brokers.fake_adapter.FakeBrokerAdapter` (local /
    dev / test, zero credentials) and
    :class:`~brokers.schwab_adapter.SchwabAdapter` (real, credential-gated).

    NOTE: this contract currently covers ONLY OAuth linking. Order execution
    (``place_order`` / ``get_order_status`` / ``OrderOutcome``) is Epic 4 and
    will be added to this same port so callers still depend on one interface.
    """

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
        NOT an execution method — it places nothing — so it does not breach the
        "no order/execution methods here" note above (``place_order`` /
        ``OrderOutcome`` / ``get_order_status`` remain Epic 4).
        """
        raise NotImplementedError
