"""The Coach Engine's order-execution owner — the SOLE caller of the Broker Port
``place_order`` (AD-7, FR8/FR9/FR10).

Single execution path: ``propose → user-approve → Coach Engine → Broker Port``.
:func:`execute_approved_order` is the ONE place in the codebase that calls
:meth:`~brokers.port.BrokerPort.place_order` AND
:meth:`~brokers.port.BrokerPort.get_order_status`. The API ``/approve`` handler
calls THIS function; it never calls the broker directly. No other module places
orders or reconciles them.

What this owner guarantees:

- **v1 order scope (FR10/AD-7):** an order is placed only for a broad index
  fund/ETF (:func:`strategy.index_core.is_index_core` is true) with ``amount > 0``
  (``side`` is already foreclosed to buy/sell by :class:`~coach.recommendation.OrderSide`).
  A blessed ``order_intent`` outside this scope raises :class:`OrderScopeError`
  BEFORE any broker call — the broker is never touched.
- **Idempotency key minting:** the client key is minted HERE at the single
  execution path and genuinely passed to the broker. It is reused verbatim on the
  reconciliation read so a timeout never double-places (4.7).
- **Reconciliation of indeterminate placements (Story 4.7, AD-13/FR22/NFR3):**
  after the single ``place_order``, if the placement outcome is INDETERMINATE
  (``timeout``/``pending``) the owner reads the authoritative state ONCE via
  ``get_order_status`` reusing the SAME idempotency key, and surfaces that.
  A definitive placement (``filled``/``partial``/``rejected``) is the broker's
  authoritative answer and is surfaced directly (no needless second round-trip).
  Never optimistic (never assumes a timeout filled/failed), never re-places,
  never loops.

What this owner is NOT (deliberately deferred):

- No wait-until-terminal poll: reconciliation is a SINGLE read; a still-``pending``
  state is surfaced honestly (Story 4.7 scope).
- No approval→placement session-integrity or cross-request key reuse (Story 4.8);
  the live-session gate is enforced upstream at request time (AD-11).
- No decision-record persistence, co-sign, or snapshot (Story 4.9): this places
  the order, reconciles it, and returns the outcome; it writes nothing.
"""

from __future__ import annotations

import uuid
from dataclasses import replace

from brokers.port import BrokerPort, OrderOutcome, OrderStatus
from coach.recommendation import OrderIntent
from strategy.index_core import is_index_core

# An indeterminate placement is one whose true state is not yet known from the
# placement call itself — it MUST be reconciled by reading authoritative state
# (AD-13: "never optimistic assumptions"), never guessed. A definitive placement
# (``filled``/``partial``/``rejected``) IS the broker's authoritative answer to
# the placement call and needs no second round-trip.
INDETERMINATE: frozenset[OrderStatus] = frozenset(
    {OrderStatus.TIMEOUT, OrderStatus.PENDING}
)


class OrderScopeError(ValueError):
    """Raised when an ``order_intent`` falls outside the v1 order scope (FR10/AD-7).

    The v1 universe is broad index funds/ETFs only, with a strictly positive
    amount. This is raised BEFORE any broker call, so the Broker Port is never
    reached for an out-of-scope order. The API layer maps it to the app error
    envelope (422).
    """


def mint_idempotency_key() -> str:
    """Mint a fresh client idempotency key for a single execution.

    A random UUID4 hex string. Minting the key at the single execution path is
    part of establishing that path; its retry-reuse semantics are Story 4.7.
    """
    return uuid.uuid4().hex


async def execute_approved_order(
    order_intent: OrderIntent,
    *,
    broker: BrokerPort,
    idempotency_key: str | None = None,
) -> OrderOutcome:
    """Validate v1 scope, place the approved order, then reconcile the outcome.

    The sole caller of both :meth:`~brokers.port.BrokerPort.place_order` and
    :meth:`~brokers.port.BrokerPort.get_order_status` (AD-7). Validates the v1
    order scope (:func:`~strategy.index_core.is_index_core` on the symbol and
    ``amount > 0``; ``side`` is guaranteed buy/sell by
    :class:`~coach.recommendation.OrderSide`), raising :class:`OrderScopeError`
    on any violation BEFORE the broker is called. On a passing intent it mints an
    idempotency key (unless one is supplied), awaits a single ``place_order``,
    then reconciles the placement via :func:`_reconcile` and returns the true
    :class:`OrderOutcome`. Pure orchestration + gate: no persistence (Story 4.9).

    The symbol is canonicalized (strip + upper) ONCE here so the scope check and
    the order actually placed operate on the same string — otherwise a symbol
    like ``" vti "`` could pass ``is_index_core`` (which normalizes internally)
    yet be sent raw to the broker. The ``amount`` gate rejects non-finite values
    (``NaN``/``Inf``) as well as non-positive ones, since ``NaN <= 0`` is False.
    """
    normalized_symbol = (order_intent.symbol or "").strip().upper()
    if not is_index_core(normalized_symbol):
        raise OrderScopeError(
            "This order is outside the v1 scope. Ballast v1 can only place "
            "orders in broad index funds and ETFs."
        )
    if not order_intent.amount.is_finite() or order_intent.amount <= 0:
        raise OrderScopeError(
            "An order amount must be a finite value greater than zero."
        )

    # Place the CANONICAL symbol so the gate and the placed order agree.
    canonical_intent = replace(order_intent, symbol=normalized_symbol)
    key = idempotency_key or mint_idempotency_key()
    placement = await broker.place_order(canonical_intent, idempotency_key=key)
    return await _reconcile(placement, broker=broker, idempotency_key=key)


async def _reconcile(
    placement: OrderOutcome,
    *,
    broker: BrokerPort,
    idempotency_key: str,
) -> OrderOutcome:
    """Return the true outcome, reconciling an indeterminate placement (AD-13).

    If ``placement`` is definitive (``filled``/``partial``/``rejected``) it IS the
    broker's authoritative answer — returned unchanged, no second round-trip. If
    it is INDETERMINATE (``timeout``/``pending``), read the authoritative state
    ONCE via :meth:`~brokers.port.BrokerPort.get_order_status`, reusing the SAME
    ``idempotency_key`` ``place_order`` used (so a timeout never double-places),
    and return whatever it authoritatively reports — which may still be
    ``pending`` (honestly surfaced). Never assumes, never re-places, never loops.
    """
    if placement.status not in INDETERMINATE:
        return placement
    return await broker.get_order_status(idempotency_key)
