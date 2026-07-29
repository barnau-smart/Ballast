"""The Coach Engine's order-execution owner — the SOLE caller of the Broker Port
``place_order`` (AD-7, FR8/FR9/FR10).

Single execution path: ``propose → user-approve → Coach Engine → Broker Port``.
:func:`execute_approved_order` is the ONE place in the codebase that calls
:meth:`~brokers.port.BrokerPort.place_order`. The API ``/approve`` handler calls
THIS function; it never calls the broker directly. No other module places orders.

What this owner guarantees (4.6):

- **v1 order scope (FR10/AD-7):** an order is placed only for a broad index
  fund/ETF (:func:`strategy.index_core.is_index_core` is true) with ``amount > 0``
  (``side`` is already foreclosed to buy/sell by :class:`~coach.recommendation.OrderSide`).
  A blessed ``order_intent`` outside this scope raises :class:`OrderScopeError`
  BEFORE any broker call — the broker is never touched.
- **Idempotency key minting:** the client key is minted HERE at the single
  execution path and genuinely passed to the broker. Its retry-reuse and the
  ``get_order_status`` reconciliation are **Story 4.7**; 4.6 mints and passes it.

What this owner is NOT (deliberately deferred):

- No reconciliation / partial-fill / timeout / pending handling (Story 4.7).
- No approval→placement session-integrity or double-place prevention (Story 4.8);
  the live-session gate is enforced upstream at request time (AD-11).
- No decision-record persistence, co-sign, or snapshot (Story 4.9): this places
  the order and returns the outcome; it writes nothing.
"""

from __future__ import annotations

import uuid
from dataclasses import replace

from brokers.port import BrokerPort, OrderOutcome
from coach.recommendation import OrderIntent
from strategy.index_core import is_index_core


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
    """Validate v1 scope, then place the approved order through the Broker Port.

    The sole caller of :meth:`~brokers.port.BrokerPort.place_order`. Validates
    the v1 order scope (:func:`~strategy.index_core.is_index_core` on the symbol
    and ``amount > 0``; ``side`` is guaranteed buy/sell by
    :class:`~coach.recommendation.OrderSide`), raising :class:`OrderScopeError`
    on any violation BEFORE the broker is called. On a passing intent it mints an
    idempotency key (unless one is supplied) and awaits a single
    ``place_order``, returning its :class:`OrderOutcome`. Pure orchestration +
    gate: no persistence (Story 4.9), no polling/retry (Story 4.7).

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
    return await broker.place_order(canonical_intent, idempotency_key=key)
