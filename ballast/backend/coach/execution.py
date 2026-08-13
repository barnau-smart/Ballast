"""The Coach Engine's order-execution owner — the SOLE caller of the Broker Port
``place_order`` (AD-7, FR8/FR9/FR10).

Single execution path: ``propose → user-approve → Coach Engine → Broker Port``.
:func:`execute_approved_order` is the ONE place in the codebase that calls
:meth:`~brokers.port.BrokerPort.place_order` AND
:meth:`~brokers.port.BrokerPort.get_order_status`. The API ``/approve`` handler
calls THIS function; it never calls the broker directly. No other module places
orders or reconciles them.

What this owner guarantees:

- **Placement-time session + provider integrity (Story 4.8, FR23/AD-11):** as its
  FIRST action — before the v1-scope gate, key minting, or ``place_order`` — the
  owner asserts the handed :class:`~brokers.session.BrokerageSession` is live AND
  its ``provider`` matches the placing adapter's ``provider``. On either failure
  it raises :class:`SessionIntegrityError` and the broker is NEVER touched (no
  stale/partial order, no phantom key). This is a placement-time self-defense on
  top of the request-entry live-session gate, not a replacement for it; the API
  maps the error to the calm 409 reconnect envelope.
- **v1 order scope (FR10/AD-7):** an order is placed only for a broad index
  fund/ETF (:func:`strategy.index_core.is_index_core` is true) with ``amount > 0``
  (``side`` is already foreclosed to buy/sell by :class:`~coach.recommendation.OrderSide`).
  A blessed ``order_intent`` outside this scope raises :class:`OrderScopeError`
  BEFORE any broker call — the broker is never touched.
- **Cash-cover safety / no margin (Story 10.9):** a **BUY** is placed only when its
  dollar ``amount`` is covered by the account's KNOWN real settled cash
  (``max(0, view.cash)`` — settlement cash, NOT parked money-market, and the reserve
  is NOT subtracted). A shortfall raises :class:`InsufficientSettledCashError` BEFORE
  ``place_order`` — the broker is NEVER touched, so it can never fill on margin. A
  **SELL** raises cash and is never coverage-gated. Enforced only against a known
  balance (a scope-less call or a never-imported account is not blocked); the
  production ``/approve`` path always supplies the user scope.
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
- No cross-request persisted idempotency key (Story 4.9); the placement-time
  session + provider integrity check lands HERE as of Story 4.8.
- No decision-record persistence, co-sign, or snapshot (Story 4.9): this places
  the order, reconciles it, and returns the outcome; it writes nothing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from decimal import ROUND_FLOOR, Decimal
from typing import TYPE_CHECKING

from brokers.port import BrokerPort, OrderOutcome, OrderStatus
from brokers.session import BrokerageSession
from coach.recommendation import Duration, OrderIntent, OrderSide, OrderType, Session
from strategy.index_core import is_index_core

if TYPE_CHECKING:  # avoid a runtime import cycle (decision_record imports us)
    from db.models import DecisionRecord
    from db.scope import Scope
    from sqlalchemy.ext.asyncio import AsyncSession

# An indeterminate placement is one whose true state is not yet known from the
# placement call itself — it MUST be reconciled by reading authoritative state
# (AD-13: "never optimistic assumptions"), never guessed. A definitive placement
# (``filled``/``partial``/``rejected``) IS the broker's authoritative answer to
# the placement call and needs no second round-trip.
INDETERMINATE: frozenset[OrderStatus] = frozenset(
    {OrderStatus.TIMEOUT, OrderStatus.PENDING}
)

#: The DEFINITIVELY-placed outcome statuses — a real order exists at the broker.
#: ``filled``/``partial`` are unambiguous placements. Used by :func:`_is_placed`
#: (with the ``broker_ref`` rule for the indeterminate states) to gate any
#: post-placement side effect (Story 10.5: seeding the linked cost-switch BUY only
#: on a genuinely-placed SELL).
_DEFINITIVELY_PLACED: frozenset[OrderStatus] = frozenset(
    {OrderStatus.FILLED, OrderStatus.PARTIAL}
)


def _is_placed(outcome: OrderOutcome) -> bool:
    """True iff ``outcome`` reflects an order that genuinely reached the broker (10.5).

    A genuinely-placed order is one whose existence at the broker is CONFIRMED:

    - ``filled`` / ``partial`` — an unambiguous placement (shares moved), placed
      regardless of ``broker_ref``.
    - ``pending`` / ``timeout`` — INDETERMINATE: the order is confirmed to exist
      ONLY when it carries a ``broker_ref`` (a queryable id the broker assigned). A
      no-``broker_ref`` ``pending``/``timeout`` is an UNCONFIRMABLE placement (the
      order may never have reached the broker), so it is NOT treated as placed.
    - ``rejected`` — the broker refused it; nothing was placed.

    Used to gate the Story 10.5 linked cost-switch BUY: the deferred BUY is seeded
    ONLY on a genuinely-placed SELL, so an unconfirmable/rejected SELL never leaves
    a beginner with a dead pending buy AND a false "step 2 queued" reassurance.
    """
    if outcome.status in _DEFINITIVELY_PLACED:
        return True
    if outcome.status in INDETERMINATE:
        return outcome.broker_ref is not None
    return False


class OrderScopeError(ValueError):
    """Raised when an ``order_intent`` falls outside the v1 order scope (FR10/AD-7).

    The v1 universe is broad index funds/ETFs only, with a strictly positive
    amount. This is raised BEFORE any broker call, so the Broker Port is never
    reached for an out-of-scope order. The API layer maps it to the app error
    envelope (422).
    """


class OrderNotSupportedError(ValueError):
    """Raised for an order feature not yet supported in this version (Story 8.1).

    A calm, explicit refusal of the forward-compat order-model members Story A
    does NOT execute: ``order_type`` in {``stop``, ``stop_limit``}, ``session`` in
    {``am``, ``pm``}, or ``duration == gtc`` (all Story B). It is raised BEFORE any
    broker call — the Broker Port is never reached for an unsupported order. The
    API layer maps it to a calm 422 ("not supported in this version") and releases
    the atomic claim so the decision stays retryable, symmetric with
    :class:`OrderScopeError`.
    """


class InsufficientSettledCashError(ValueError):
    """Raised when a BUY exceeds the account's real settled cash (Story 10.9).

    The backend cash-cover safety gate: a **BUY** is placed only when its dollar
    ``amount`` is covered by the account's KNOWN settled cash (``max(0, view.cash)``
    — settlement cash, the same ``ready_to_trade`` the 9-3 liquidator anchors to).
    Parked money-market does NOT count (it is deployable *on paper* in the 10-8
    analysis but is not spendable until sold and settled) and the reserve is NOT
    subtracted — the execution invariant is strictly *no-margin*: never place a buy
    the account can't actually cover, so Schwab can never fill the shortfall on
    margin. A **SELL** is never coverage-gated (it raises cash). Raised BEFORE any
    ``place_order`` call — the broker is NEVER touched on a shortfall. The API layer
    maps it to a calm 422 that routes the user to free up cash via the existing 9-3
    liquidation (sell money-market → settle → the buy resumes as a ``PendingBuy``),
    and releases the atomic claim so the decision stays retryable — symmetric with
    :class:`OrderScopeError`. NB: this closes a PRE-EXISTING gap (the ``/approve``
    path never checked cash) that Story 10-8 made reachable + dangerous on a margin
    account.
    """


class SessionIntegrityError(ValueError):
    """Raised when the brokerage session cannot back a placement (FR23/AD-11).

    Signalled when the handed :class:`~brokers.session.BrokerageSession` is NOT
    live, OR its ``provider`` does not match the placing adapter's ``provider``.
    This is checked as the FIRST action in :func:`execute_approved_order` —
    BEFORE the v1-scope gate, key minting, or any ``place_order`` call — so the
    Broker Port is NEVER reached on an integrity failure: no stale/partial order,
    no phantom idempotency key. The API layer maps it to the same calm 409
    reconnect envelope the request-entry live-session gate uses.
    """


@dataclass(frozen=True)
class ReconcileResult:
    """The result of a durable cross-request reconcile (Story 6.7).

    ``outcome`` is the truthful :class:`~brokers.port.OrderOutcome` (the recorded
    terminal one, or the fresh broker read, or a still-pending indeterminate);
    ``reconciled`` is ``True`` iff the broker was actually READ this call (so the
    caller knows to persist ``reconciliation_snapshot``); ``needs_reconfirmation``
    is ``True`` when the true outcome could NOT be positively confirmed
    (``broker_ref is None``, or the read is still ``pending``/``timeout``), so a
    human is prompted to decide — never an auto-resolve, never a re-place.
    """

    outcome: OrderOutcome
    needs_reconfirmation: bool
    reconciled: bool


def _assert_session_integrity(
    broker: BrokerPort, broker_session: BrokerageSession
) -> None:
    """Assert placement-time session + provider integrity (Story 4.8, FR23/AD-11).

    The shared gate reused by BOTH :func:`execute_approved_order` and
    :func:`reconcile_pending_decision`: the handed ``broker_session`` must be live
    AND its ``provider`` must match the placing/reading ``broker``'s ``provider``,
    else it raises :class:`SessionIntegrityError` and the broker is NEVER touched.
    Providers are compared case/whitespace-insensitively and via ``getattr`` so a
    stored-provider casing drift, a ``None`` on either side, or a misconfigured
    adapter missing ``provider`` refuses with the calm 409 rather than a raw
    ``AttributeError`` 500 — the owner never touches the broker on doubt.
    """
    session_provider = (broker_session.provider or "").strip().lower()
    adapter_provider = (getattr(broker, "provider", None) or "").strip().lower()
    if not broker_session.is_live or session_provider != adapter_provider:
        raise SessionIntegrityError(
            "Your brokerage connection needs a quick reconnect before this order "
            "can go through."
        )


def whole_share_quantity(amount: Decimal, price: Decimal) -> int:
    """Return ``floor(amount / price)`` as a whole-share count (Story 8.4).

    The shared, reusable sub-share sizing helper mirroring the adapters' inline
    ``int((amount / price).to_integral_value(rounding=ROUND_FLOOR))`` flooring —
    factored here so the suggest-order engine (:mod:`coach.suggest`) sizes shares
    the SAME deterministic way the fake/schwab adapters do at placement, without
    reinventing it (the adapters keep their own inline flooring to stay 8.1/8.2
    byte-identical). ``amount``/``price`` are positive ``Decimal``; a
    non-positive/non-finite ``price`` (which would make the division meaningless)
    yields ``0`` rather than raising, so the caller refuses ``< 1`` share calmly.
    """
    if not price.is_finite() or price <= 0:
        return 0
    return int((amount / price).to_integral_value(rounding=ROUND_FLOOR))


def mint_idempotency_key() -> str:
    """Mint a fresh client idempotency key for a single execution.

    A random UUID4 hex string. Minting the key at the single execution path is
    part of establishing that path; its retry-reuse semantics are Story 4.7.
    """
    return uuid.uuid4().hex


def validate_order_intent(intent: OrderIntent) -> None:
    """Enforce the field-requirement matrix + deferred-feature rejection (Story 8.1).

    A pure, synchronous, NO-I/O gate run inside :func:`execute_approved_order`
    AFTER session integrity and the ``is_index_core`` + ``amount`` gate, BEFORE
    ``place_order`` — so the broker is never touched on any rejection. Two refusal
    classes, kept distinct:

    - **Deferred features** (still deferred after Story 8.2) →
      :class:`OrderNotSupportedError`: ``order_type`` in {``STOP``, ``STOP_LIMIT``}
      or ``session`` in {``AM``, ``PM``}. Checked FIRST so an unsupported feature is
      refused as "not supported in this version" rather than as a field-shape
      violation. ``duration == GTC`` is now ACCEPTED (Story 8.2 resting orders).
    - **Field-shape violations** (a malformed but in-scope order) →
      :class:`OrderScopeError`: a ``MARKET`` order carrying a ``limit_price`` or
      ``stop_price``; a ``LIMIT`` order missing a positive, finite ``limit_price``
      or carrying a ``stop_price``.

    Messages stay calm and specific. This mirrors the boundary-layer
    ``OrderIntentIn`` validator (``api/coach.py``) but is the AUTHORITATIVE gate.
    """
    # Deferred features first (Story B) — an explicit "not supported yet" refusal.
    if intent.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
        raise OrderNotSupportedError(
            "Stop and stop-limit orders aren't supported in this version yet."
        )
    if intent.session in (Session.AM, Session.PM):
        raise OrderNotSupportedError(
            "Extended-hours (pre-market / after-hours) sessions aren't supported "
            "in this version yet."
        )
    # Story 8.2: ``Duration.GTC`` is now ACCEPTED (a good-till-canceled resting
    # order) — but ONLY on a LIMIT order. STOP/STOP_LIMIT and AM/PM stay rejected
    # above.

    # Field-shape rule (Story 8.2): GTC is coupled to LIMIT. A market order fills
    # immediately, so "good-till-canceled" is meaningless on it — and the schwab
    # adapter only applies GOOD_TILL_CANCEL in its LIMIT branch, so a GTC MARKET
    # intent would be placed as DAY (intent/placed order diverge). Refuse it as a
    # field-shape violation (the authoritative engine gate).
    if intent.order_type != OrderType.LIMIT and intent.duration == Duration.GTC:
        raise OrderScopeError("A market order can't be good-till-canceled.")

    # Field-requirement matrix for the two supported order types.
    if intent.order_type == OrderType.MARKET:
        if intent.limit_price is not None or intent.stop_price is not None:
            raise OrderScopeError(
                "A market order can't carry a limit or stop price."
            )
        return
    # order_type == LIMIT (the only remaining supported type):
    if intent.stop_price is not None:
        raise OrderScopeError("A limit order can't carry a stop price.")
    if (
        intent.limit_price is None
        or not intent.limit_price.is_finite()
        or intent.limit_price <= 0
    ):
        raise OrderScopeError(
            "A limit order needs a limit price greater than zero."
        )


async def _scope_user_parked_symbols(
    scope: "Scope | None", session: "AsyncSession | None"
) -> set[str]:
    """Load the scope user's declared ``parked_symbols`` (read-only), normalized.

    The MINIMAL read the widened SELL scope gate (Story 9.3) needs: the money-market
    symbols the user has declared as parked cash-equivalents. Returns the empty set
    when no scope/session is supplied (e.g. a direct engine call with no user
    context), when the scope is system (no single owner), or when the user has no
    config — so the gate stays fail-closed (falls back to index-core-only) on any
    doubt. Reads ONLY the caller's own row through the fail-closed helper (AD-10).
    """
    if scope is None or session is None or scope.is_system:
        return set()
    # Imported here (not at module load) to avoid an import cycle at startup:
    # ``cash.config`` imports models/repo that are heavy at import time, and the
    # execution owner is imported very early (decision_record imports it).
    from cash.config import get_config, normalize_symbols

    config = await get_config(scope, session)
    if config is None:
        return set()
    return set(normalize_symbols(config.parked_symbols))


async def _assert_buy_covered_by_settled_cash(
    intent: OrderIntent,
    *,
    scope: "Scope | None",
    session: "AsyncSession | None",
) -> None:
    """Refuse a BUY that exceeds the account's KNOWN real settled cash (Story 10.9).

    The backend cash-cover safety gate. Enforced ONLY for a ``BUY`` (a SELL raises
    cash and is never gated) and ONLY against a KNOWN settled-cash figure:

    - No user ``scope``+``session`` (a scope-less direct engine/test call, or a
      system scope) → we cannot read the caller's cash, so the gate does not fire.
      The production ``/approve`` path ALWAYS supplies a user scope+session, so this
      escape is unreachable in production (it preserves scope-less direct-call tests).
    - No imported balance row (``view.as_of is None``) → the account has never
      imported a balance, so there is NO settled-cash truth to assert an overdraw
      against. The gate does not fire (pre-existing behavior; not reachable via the
      deploy path, which needs a balance to produce a buy). ``get_portfolio`` reports
      ``cash == 0`` for this case, so ``as_of`` (``None`` iff no row) is the honest
      "no data" signal — never fabricate a $0 refusal from absent data.
    - A KNOWN balance with settlement cash ``C`` → refuse when ``amount > max(0, C)``.
      Parked money-market is NOT added (deployable on paper ≠ spendable) and the
      reserve is NOT subtracted (the invariant is strictly no-margin). A non-finite
      (``NaN``/``Inf``) cached cash is present-but-untrustworthy → treated as ``0``
      (fail-closed), so a corrupt balance can never certify coverage.

    ``get_portfolio`` is imported lazily (mirrors :func:`_scope_user_parked_symbols`)
    to avoid an import cycle at startup. Read-only — reads ONLY the caller's own
    scoped balance (AD-10); the owner keeps its no-persistence contract (Story 4.9).
    """
    if scope is None or session is None or scope.is_system:
        return
    # Lazy import (module-load cycle): decision_record imports this module very early.
    from brokers.portfolio import get_portfolio

    view = await get_portfolio(scope, session)
    if view.as_of is None:
        # No imported balance row → no settled-cash truth to enforce against.
        return
    cash = view.cash
    available = max(Decimal("0"), cash) if cash.is_finite() else Decimal("0")
    if intent.amount > available:
        raise InsufficientSettledCashError(
            f"This buy needs about ${intent.amount:.2f}, but only ${available:.2f} "
            "of settled cash is available right now. Selling some of your "
            "money-market fund frees up the difference — this buy can go through "
            "once that cash settles."
        )


async def execute_approved_order(
    order_intent: OrderIntent,
    *,
    broker: BrokerPort,
    broker_session: BrokerageSession,
    idempotency_key: str | None = None,
    scope: "Scope | None" = None,
    session: "AsyncSession | None" = None,
    switch_to: str | None = None,
) -> OrderOutcome:
    """Assert session integrity, validate v1 scope, place, then reconcile.

    The sole caller of both :meth:`~brokers.port.BrokerPort.place_order` and
    :meth:`~brokers.port.BrokerPort.get_order_status` (AD-7). As its FIRST action
    it asserts placement-time integrity (Story 4.8, FR23/AD-11): the handed
    ``broker_session`` must be live AND its ``provider`` must match the placing
    ``broker``'s ``provider``, raising :class:`SessionIntegrityError` on either
    failure BEFORE the scope gate, key mint, or any broker call — so the broker is
    NEVER touched on an integrity failure (integrity runs before scope). It then
    validates the v1 order scope, raising :class:`OrderScopeError` on any violation
    BEFORE the broker is called.

    v1 ORDER SCOPE (widened for SELL, Story 9.3 + 10.5): a **BUY** stays
    index-core-only (:func:`~strategy.index_core.is_index_core`) and is NEVER
    widened. A **SELL** is in-scope when the symbol is index-core **OR** it is one
    of the scope user's declared ``parked_symbols`` (Story 9.3 just-in-time
    liquidation) **OR** a non-null ``switch_to`` is supplied (Story 10.5: a
    SERVER-VERIFIED cost-switch SELL of a high-fee held fund into its cheaper
    canonical). ``switch_to`` MUST already be server-verified by the caller
    (``api/coach.py`` re-derives it from the user's own holdings before threading it
    onto the immutable snapshot) — this owner NEVER re-checks it and NEVER trusts a
    raw client value; the ``/approve`` handler passes ONLY the snapshot's verified
    value. The gate still stops beginners *buying* random securities. The
    parked-symbol widening applies ONLY when a USER ``scope`` + ``session`` are
    supplied (the ``/approve`` path always supplies them); a scope-less direct
    engine call keeps the strict index-core-only behavior for the parked path
    (fail-closed). All other hardening — the ``amount > 0`` gate,
    ``validate_order_intent``, session integrity, key minting, atomic reconcile — is
    UNCHANGED.

    On a passing intent it mints an idempotency key (unless one is supplied),
    awaits a single ``place_order``, then reconciles the placement via
    :func:`_reconcile` and returns the true :class:`OrderOutcome`. Pure
    orchestration + gate: NO persistence (Story 4.9) — the linked cost-switch BUY
    (Story 10.5) is seeded by the ``/approve`` handler AFTER this returns a placed
    outcome (via :func:`_is_placed`), so this owner keeps its no-write contract and
    the approve handler (which owns the commit) wraps that seed transaction-safely.

    The symbol is canonicalized (strip + upper) ONCE here so the scope check and
    the order actually placed operate on the same string — otherwise a symbol
    like ``" vti "`` could pass ``is_index_core`` (which normalizes internally)
    yet be sent raw to the broker. The ``amount`` gate rejects non-finite values
    (``NaN``/``Inf``) as well as non-positive ones, since ``NaN <= 0`` is False.
    """
    # Placement-time integrity FIRST (before scope/key/place_order): the session
    # must be live and its provider must match the placing adapter, else refuse
    # without ever touching the broker (Story 4.8, FR23/AD-11). Factored into the
    # shared ``_assert_session_integrity`` so the durable reconcile path (Story
    # 6.7) enforces the EXACT same gate before any broker read.
    _assert_session_integrity(broker, broker_session)

    normalized_symbol = (order_intent.symbol or "").strip().upper()
    in_scope = is_index_core(normalized_symbol)
    if not in_scope and order_intent.side == OrderSide.SELL:
        # SELL widening (Story 10.5): a SERVER-VERIFIED cost-switch (a non-null
        # ``switch_to`` threaded from the immutable snapshot) is a legitimate sell of
        # a high-fee held fund into its cheaper canonical, even when the held fund is
        # not index-core. Safe to widen on because the caller re-derived and verified
        # the switch from the user's OWN holdings before it ever reached the snapshot
        # — a raw client ``switch_to`` never reaches here (a BUY is never widened).
        if switch_to is not None and str(switch_to).strip():
            in_scope = True
        else:
            # SELL widening (Story 9.3): a declared parked money-market symbol is a
            # legitimate sell even when it is not index-core. Read-only, scoped —
            # loaded ONLY when a user scope+session are supplied (the /approve path).
            parked = await _scope_user_parked_symbols(scope, session)
            in_scope = normalized_symbol in parked
    if not in_scope:
        raise OrderScopeError(
            "This order is outside the v1 scope. Ballast v1 can only place "
            "orders in broad index funds and ETFs."
        )
    if not order_intent.amount.is_finite() or order_intent.amount <= 0:
        raise OrderScopeError(
            "An order amount must be a finite value greater than zero."
        )

    # Place the CANONICAL symbol so the gate and the placed order agree
    # (``replace`` preserves the Story 8.1 order-model fields).
    canonical_intent = replace(order_intent, symbol=normalized_symbol)
    # Order-shape validation LAST before placement (Story 8.1): field-requirement
    # matrix + deferred-feature rejection. Runs after integrity + index-core/amount
    # so the broker is never touched on any rejection.
    validate_order_intent(canonical_intent)
    # Cash-cover safety (Story 10.9): a BUY is placed only when its dollar amount is
    # covered by the account's KNOWN real settled cash — never on margin. Parked
    # money-market does NOT count (deployable on paper ≠ spendable) and the reserve
    # is not subtracted. A SELL raises cash and is never gated. Raised BEFORE the key
    # mint / place_order, so the broker is never touched on a shortfall. This is the
    # last gate before placement.
    if canonical_intent.side == OrderSide.BUY:
        await _assert_buy_covered_by_settled_cash(
            canonical_intent, scope=scope, session=session
        )
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


async def reconcile_pending_decision(
    record: "DecisionRecord",
    *,
    broker: BrokerPort,
    broker_session: BrokerageSession,
) -> ReconcileResult:
    """Durably reconcile a cosigned decision by its persisted ``broker_ref`` (6.7).

    The SOLE caller of :meth:`~brokers.port.BrokerPort.get_order_status_by_ref`
    (AD-7). READ-ONLY: it NEVER calls ``place_order``, never mints a key, never
    loops/polls (one read), never searches. As its FIRST action it asserts the
    EXACT placement-time session + provider integrity gate
    (:func:`_assert_session_integrity`, shared with
    :func:`execute_approved_order`), raising :class:`SessionIntegrityError` before
    any broker read — so a reconcile can never read against a dead or
    mismatched-provider session.

    Then, keyed on the newest-known truth (:func:`effective_outcome_status`):

    - If the effective status is TERMINAL (``filled``/``rejected`` — ``partial`` is
      NO LONGER terminal as of 8.2) the broker is NEVER touched — the recorded
      outcome is returned, ``reconciled`` False, ``needs_reconfirmation`` False
      (idempotent).
    - If NON-terminal (``pending``/``timeout``/``partial``) but
      ``record.broker_ref is None``
      the broker is NEVER touched — the order is unconfirmable by id, so it stays
      pending with ``needs_reconfirmation`` True (never search/guess/re-place).
    - Otherwise it RE-READS the one order by ``record.broker_ref`` via
      ``get_order_status_by_ref`` (a non-terminal ``partial`` re-reads the broker
      and can advance to ``filled``); ``needs_reconfirmation`` is True iff that read
      is ``timeout`` — the ONLY outcome that cannot be positively confirmed (a
      successful read, even a still-``pending``/working order, IS a positive
      confirmation the order exists and is retryable-without-a-human) — and
      ``reconciled`` is True (the caller persists the reconciled outcome).
    """
    # Import here (not at module load) to avoid the import cycle: decision_record
    # imports ``mint_idempotency_key`` from this module.
    from coach.decision_record import effective_outcome_status, _is_terminal

    # Session + provider integrity FIRST — identical gate to a placement, so a
    # reconcile never reads against a dead/mismatched session (broker untouched).
    _assert_session_integrity(broker, broker_session)

    status = effective_outcome_status(record)
    if _is_terminal(status):
        # Already settled — return the recorded terminal outcome; NEVER re-read the
        # broker (idempotent, no needless round-trip).
        return ReconcileResult(
            outcome=_recorded_outcome(record),
            needs_reconfirmation=False,
            reconciled=False,
        )

    if record.broker_ref is None:
        # A true no-order_id timeout: no confirmed id, so the order is
        # unconfirmable by ref — NEVER touch the broker, NEVER search. Stay pending
        # and prompt an explicit human re-confirmation.
        return ReconcileResult(
            outcome=_recorded_outcome(record),
            needs_reconfirmation=True,
            reconciled=False,
        )

    outcome = await broker.get_order_status_by_ref(record.broker_ref)
    # ``needs_reconfirmation`` is True ONLY when the order could not be positively
    # confirmed — i.e. the read itself was indeterminate (``timeout``). A successful
    # read that reports a still-working ``pending`` order IS a positive confirmation
    # (the order exists at the broker), so it is retryable-without-a-human, NOT a
    # re-confirmation prompt (I/O matrix "Still working"; epic 6.7 AC "cannot be
    # positively confirmed"). Flagging a live working order would wrongly nudge a
    # human toward re-placing a duplicate.
    needs_reconfirmation = outcome.status is OrderStatus.TIMEOUT
    return ReconcileResult(
        outcome=outcome,
        needs_reconfirmation=needs_reconfirmation,
        reconciled=True,
    )


@dataclass(frozen=True)
class CancelResult:
    """The result of a cancel attempt on a cosigned resting order (Story 8.2).

    ``outcome`` is the truthful :class:`~brokers.port.OrderOutcome`: the broker's
    post-cancel state (``rejected`` on success) when the broker was actually
    called, or the recorded terminal/unconfirmable outcome when the cancel was
    calmly REFUSED without touching the broker. ``cancelled`` is ``True`` iff the
    broker's :meth:`~brokers.port.BrokerPort.cancel_order` was actually invoked
    (so the caller knows to persist the reconciliation snapshot). ``refused`` is
    ``True`` when the order could NOT be cancelled WITHOUT a broker call — it is
    already terminal/settled (``filled``/``rejected``), already PARTIALLY FILLED
    (``partial`` — non-cancellable in B1; cancelling it would let the broker's
    ``rejected`` cancel result overwrite the ``partial`` snapshot and erase the real
    filled shares), or has no ``broker_ref`` to address — so the API maps it to a
    calm 422 (NEVER a 500), and the broker is never touched.
    """

    outcome: OrderOutcome
    cancelled: bool
    refused: bool


async def cancel_pending_decision(
    record: "DecisionRecord",
    *,
    broker: BrokerPort,
    broker_session: BrokerageSession,
) -> CancelResult:
    """Cancel a cosigned resting order by its persisted ``broker_ref`` (Story 8.2).

    The SOLE caller of :meth:`~brokers.port.BrokerPort.cancel_order` (AD-7). As its
    FIRST action it asserts the EXACT placement-time session + provider integrity
    gate (:func:`_assert_session_integrity`, shared with
    :func:`execute_approved_order` / :func:`reconcile_pending_decision`), raising
    :class:`SessionIntegrityError` BEFORE any broker call — so a cancel can never
    run against a dead or mismatched-provider session.

    Then, keyed on the newest-known truth (:func:`effective_outcome_status`):

    - If the effective status is TERMINAL (``filled``/``rejected``) OR ``partial``
      the order can NO LONGER be cleanly cancelled: the broker is NEVER touched,
      ``refused`` is True (the API maps it to a calm 422 "already settled or
      partially filled"). For a terminal status this makes cancel idempotent — a
      second cancel sees the first's terminal ``rejected`` and refuses. For a
      ``partial`` (non-terminal as of 8.2, still re-reconcilable) B1 deliberately
      refuses: a remainder-cancel that preserves the filled shares is out of scope,
      and calling the broker would let its ``rejected`` cancel result overwrite the
      ``partial`` snapshot and erase the real filled shares.
    - If ``record.broker_ref is None`` the order is unaddressable (no id to
      cancel): the broker is NEVER touched, ``refused`` is True (calm 422).
    - Otherwise it cancels the one order by ``record.broker_ref`` via
      ``broker.cancel_order`` and returns its post-cancel ``outcome`` with
      ``cancelled`` True (the API persists it via ``record_reconciliation``,
      advancing ``pending`` → ``rejected``).
    """
    from coach.decision_record import effective_outcome_status, _is_terminal

    # Session + provider integrity FIRST — identical gate to a placement/reconcile,
    # so a cancel never runs against a dead/mismatched session (broker untouched).
    _assert_session_integrity(broker, broker_session)

    status = effective_outcome_status(record)
    if _is_terminal(status) or status == "partial":
        # Already settled (filled/rejected) OR already partially filled — in B1 a
        # partially-executed order can't be cleanly cancelled (remainder-cancel +
        # fill-preservation is out of scope), and cancelling it would let the
        # broker's ``rejected`` cancel result overwrite the ``partial`` snapshot and
        # ERASE the real filled shares. So refuse WITHOUT touching the broker
        # (idempotent for the terminal case: a second cancel sees the first's
        # terminal rejected). ``partial`` is non-terminal (still re-reconcilable) but
        # is deliberately not cancellable in B1.
        return CancelResult(
            outcome=_recorded_outcome(record),
            cancelled=False,
            refused=True,
        )

    if record.broker_ref is None:
        # No confirmed order id — the order is unaddressable, so it cannot be
        # cancelled by ref. NEVER touch the broker, NEVER search/guess.
        return CancelResult(
            outcome=_recorded_outcome(record),
            cancelled=False,
            refused=True,
        )

    outcome = await broker.cancel_order(record.broker_ref)
    return CancelResult(outcome=outcome, cancelled=True, refused=False)


def _recorded_outcome(record: "DecisionRecord") -> OrderOutcome:
    """Rebuild the newest recorded :class:`OrderOutcome` from a cosigned record.

    Reads the ``reconciliation_snapshot.outcome`` if a durable reconcile has
    already run, else the ``cosign_snapshot.outcome`` (the state surfaced at
    co-sign). Money strings round-trip back to ``Decimal``. Read-only; mutates
    nothing.
    """
    from decimal import Decimal

    reconciliation = record.reconciliation_snapshot or {}
    outcome = reconciliation.get("outcome") or (
        (record.cosign_snapshot or {}).get("outcome")
    ) or {}
    status = outcome.get("status") or OrderStatus.PENDING.value
    filled_raw = outcome.get("filled_qty")
    avg_raw = outcome.get("avg_price")
    return OrderOutcome(
        status=OrderStatus(status),
        filled_qty=Decimal(filled_raw) if filled_raw is not None else Decimal("0"),
        avg_price=None if avg_raw is None else Decimal(avg_raw),
        broker_ref=outcome.get("broker_ref"),
    )
