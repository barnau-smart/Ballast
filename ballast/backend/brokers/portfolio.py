"""The portfolio projection (Story 2.3 / 6.5) — the SINGLE writer of
``portfolio_cache`` AND ``portfolio_balance``.

AD-14: both tables are a *read model with one writer*. The broker is the
authoritative source; they are derived projections. This module is that one
writer — nothing else in the codebase writes them. Every other consumer (the
dashboard read in ``api.portfolio``, missed-growth, coach reads) reads them
READ-ONLY through the fail-closed :class:`ScopedRepository` (AD-10).

Idle cash lives in a DEDICATED balances source (Story 6.5, AD-14 cash-only gap
closed): ``portfolio_balance`` holds ONE row per user (``cash``, ``as_of``), so a
cash-only / cash-heavy account (few or zero holdings) still reports its true
cash. Idle cash is NEVER derived from a ``portfolio_cache`` holdings row anymore.

Reconcile-wins, keyed on the persisted balance ``as_of``
--------------------------------------------------------
On any conflict a fresh broker reconciliation wins over existing local state.
:func:`reconcile_portfolio` reads the authoritative :class:`PortfolioSnapshot`
from the Broker Port, compares its ``as_of`` to the persisted
``portfolio_balance.as_of`` (present even for a cash-only account, unlike zero
holdings rows), and:

- if the snapshot is NEWER (or nothing is cached): UPSERTS the balance row and
  atomically REPLACES the user's cached holdings rows in ONE commit
  (delete-then-add — the same atomic-replace discipline the 2.1 link callback
  uses for token rows);
- if the snapshot is NOT newer (a stale/duplicate reconcile): leaves the balance
  and holdings untouched, so an out-of-order reconcile never clobbers newer truth
  (now protecting cash-only accounts too).

Post-trade optimistic writes (Epic 4) are always superseded by the next broker
reconcile — the same rule, keyed on the same ``as_of``.

Money stays ``Decimal`` end to end (never binary float). All access is scoped to
one user via :class:`ScopedRepository`, so a reconcile can only ever touch that
user's own cache + balance.
"""

from __future__ import annotations

import logging
import anyio.to_thread
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from brokers.port import BrokerPort, PortfolioSnapshot
from db.atomic import conditional_claim
from db.models import PortfolioBalance, PortfolioCache
from db.repository import ScopedRepository
from db.scope import Scope
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("ballast.portfolio")


@dataclass(frozen=True)
class PortfolioView:
    """A read-only view of a user's cached portfolio (holdings + cash + as_of).

    Plain projection data — no token/secret values. ``holdings`` come from
    ``portfolio_cache``; ``cash``/``as_of`` come from the dedicated
    ``portfolio_balance`` row (AD-14, Story 6.5) so a cash-only account (zero
    holdings, cash > 0) still reports its true cash. ``as_of`` is ``None`` only
    when the user has no balance row yet (never imported).

    The public shape (``holdings``, ``cash``, ``as_of``, ``is_empty``) is FIXED —
    every downstream consumer (dashboard, missed-growth, coach oversized-lump)
    depends on it unchanged.
    """

    holdings: list[PortfolioCache]
    cash: Decimal
    as_of: datetime | None
    # The broker's account type (``"MARGIN"``/``"CASH"``/``None``) from the balance
    # row — informational only (Story 10.10). Additive with a default so every
    # existing consumer of the fixed shape is unaffected; NEVER used in money math.
    account_type: str | None = None

    @property
    def is_empty(self) -> bool:
        return not self.holdings


def _normalize_as_of(as_of: datetime) -> datetime:
    """Coerce to tz-aware UTC (the column is tz-aware UTC; be defensive)."""
    return as_of if as_of.tzinfo is not None else as_of.replace(tzinfo=timezone.utc)


def _to_view(
    rows: list[PortfolioCache], balance: PortfolioBalance | None
) -> PortfolioView:
    """Compose the fixed-shape view: holdings from the cache, cash/as_of from the
    dedicated balance row (0 / None when the user has never imported)."""
    cash = balance.cash if balance is not None else Decimal("0")
    as_of = _normalize_as_of(balance.as_of) if balance is not None else None
    account_type = balance.account_type if balance is not None else None
    return PortfolioView(
        holdings=rows, cash=cash, as_of=as_of, account_type=account_type
    )


async def _read_balance(
    scope: Scope, session: AsyncSession
) -> PortfolioBalance | None:
    """Read the user's single ``portfolio_balance`` row (or ``None``) — scoped.

    The single writer keeps exactly one row per user; if the one-row invariant is
    ever violated we deterministically pick the NEWEST ``as_of`` (``list()`` has
    no ORDER BY) so a stale row can never mask newer cash truth.
    """
    repo = ScopedRepository(PortfolioBalance, scope, session)
    rows = await repo.list()
    if not rows:
        return None
    return max(rows, key=lambda r: _normalize_as_of(r.as_of))


async def get_portfolio(scope: Scope, session: AsyncSession) -> PortfolioView:
    """Read a user's cached portfolio READ-ONLY (never writes). AD-14/AD-10.

    This is the sanctioned read path; it must never mutate rows. Holdings come
    from ``portfolio_cache`` and cash/as_of from the dedicated ``portfolio_balance``
    row (so a cash-only account reports its true cash), both through the scoped repo.
    """
    repo = ScopedRepository(PortfolioCache, scope, session)
    rows = await repo.list()
    balance = await _read_balance(scope, session)
    return _to_view(rows, balance)


async def reconcile_portfolio(
    scope: Scope,
    session: AsyncSession,
    broker: BrokerPort,
    *,
    snapshot: PortfolioSnapshot | None = None,
) -> PortfolioView:
    """Reconcile a user's portfolio cache + balance from the authoritative broker.

    THE single writer of ``portfolio_cache`` AND ``portfolio_balance`` (AD-14).
    Reads the broker snapshot (or uses one passed in — e.g. tests), and applies
    reconcile-wins keyed on the persisted ``portfolio_balance.as_of`` (present
    even for a cash-only account, so a stale re-fetch never clobbers newer cash
    truth): on a strictly-newer snapshot it UPSERTS the balance row AND atomically
    replaces the holdings rows in ONE commit; an equal/older snapshot leaves both
    untouched.

    All access is via the fail-closed scoped repo, so only THIS user's cache +
    balance is ever touched. Returns the resulting :class:`PortfolioView`.
    """
    # Offload ONLY the blocking network read off the event loop (Story 7.5): a
    # slow live Schwab ``fetch_portfolio`` must not stall the loop for every
    # concurrent request. The pure network→``PortfolioSnapshot`` call crosses to a
    # worker thread via ``anyio.to_thread.run_sync``; the ``AsyncSession`` is never
    # touched off-thread — all repo/session work below stays on the event loop
    # (SQLAlchemy async sessions are not thread-safe). An injected ``snapshot``
    # (tests) is used directly with no offload.
    if snapshot is not None:
        snap = snapshot
    else:
        snap = await anyio.to_thread.run_sync(broker.fetch_portfolio)
    incoming_as_of = snap.as_of
    if incoming_as_of.tzinfo is None:
        incoming_as_of = incoming_as_of.replace(tzinfo=timezone.utc)

    cache_repo = ScopedRepository(PortfolioCache, scope, session)
    balance = await _read_balance(scope, session)

    if balance is not None:
        # Reconcile-wins as a CONDITIONAL, as_of-gated CLAIM (Story 7.2), not a
        # bare read-modify-write. The conditional ``UPDATE … WHERE as_of <
        # :incoming`` advances the row ONLY when the snapshot is strictly newer;
        # Postgres's row lock on that update serializes two concurrent reconciles
        # of the same user — the second blocks, then re-evaluates ``as_of <
        # :incoming`` and no-ops (rowcount 0) if it is now stale. The winner's
        # holdings replace commits in the SAME transaction as this update, so the
        # two-table unit lands atomically with no interleave. Routed through the
        # shared ``conditional_claim`` primitive; ``update(PortfolioBalance)`` stays
        # in this sole-writer module (AD-14).
        won = await conditional_claim(
            session,
            update(PortfolioBalance)
            .where(
                PortfolioBalance.owner_id == scope.user_id,
                PortfolioBalance.as_of < incoming_as_of,
            )
            .values(
                cash=snap.cash,
                as_of=incoming_as_of,
                account_type=snap.account_type,
            ),
        )
        if not won:
            # A stale/equal snapshot (or a concurrent newer reconcile already won):
            # leave BOTH tables untouched and return the committed view. Roll back
            # the no-op update, then RE-READ (the prior instances are expired by the
            # rollback) so the returned view reflects the current committed truth
            # (which, under a concurrent race, may be the OTHER reconcile's newer
            # state — correct: newest wins).
            await session.rollback()
            logger.info(
                "portfolio_reconcile_skipped_stale owner_id=%s incoming_as_of=%s",
                scope.user_id,
                incoming_as_of.isoformat(),
            )
            rows = await cache_repo.list()
            return _to_view(rows, await _read_balance(scope, session))
    else:
        # First-ever reconcile: INSERT the dedicated balance row, guarded by
        # ``uq_portfolio_balance_owner`` (the DB-level one-row backstop). A
        # concurrent first-insert that lost the race raises IntegrityError → treat
        # it as a lost race (rollback, re-read, return the current committed view),
        # never a crash / duplicate row.
        balance_repo = ScopedRepository(PortfolioBalance, scope, session)
        try:
            await balance_repo.add(
                cash=snap.cash,
                as_of=incoming_as_of,
                account_type=snap.account_type,
            )
            await session.flush()
        except IntegrityError as exc:
            # Only the owner-uniqueness backstop (a concurrent first-insert that lost
            # the race) is a benign lost race — roll back, re-read, return the
            # committed view. Any OTHER integrity fault (a NOT NULL / FK / etc. bug)
            # must NOT be masked as a lost race; roll back and re-raise so it surfaces
            # instead of silently returning a stale view.
            await session.rollback()
            if "uq_portfolio_balance_owner" not in str(exc.orig):
                raise
            logger.info(
                "portfolio_reconcile_lost_first_insert_race owner_id=%s "
                "incoming_as_of=%s",
                scope.user_id,
                incoming_as_of.isoformat(),
            )
            rows = await cache_repo.list()
            return _to_view(rows, await _read_balance(scope, session))

    # We won the balance claim/insert: atomically REPLACE the holdings (delete the
    # old rows, write the fresh snapshot) in the SAME transaction, so the balance
    # advance and the holdings replace commit together as one unit with no
    # interleave. Mirrors the 2.1 callback's delete-then-add discipline. The
    # now-vestigial per-row ``cash`` is still written (harmless denormalized copy).
    #
    # Read the holdings HERE — AFTER winning the balance claim (Story 7.2) — not
    # before it. The winning ``UPDATE`` holds the ``portfolio_balance`` row lock
    # until this transaction commits, so a concurrent reconcile is blocked and this
    # re-read sees the CURRENT committed holdings; deleting a pre-claim snapshot
    # instead would orphan a racing winner's rows (delete a stale set, add on top).
    existing = await cache_repo.list()
    for row in existing:
        await session.delete(row)

    written: list[PortfolioCache] = []
    for holding in snap.holdings:
        row = await cache_repo.add(
            symbol=holding.symbol,
            quantity=holding.quantity,
            market_value=holding.market_value,
            cost_basis=holding.cost_basis,
            cash=snap.cash,
            as_of=incoming_as_of,
        )
        written.append(row)

    await session.commit()
    logger.info(
        "portfolio_reconciled owner_id=%s holdings=%d as_of=%s",
        scope.user_id,
        len(written),
        incoming_as_of.isoformat(),
    )
    # Re-read through the scoped repo so the returned view reflects committed
    # state (and a cash-only/empty snapshot yields a consistent view whose cash
    # comes from the balance row).
    rows = await cache_repo.list()
    return _to_view(rows, await _read_balance(scope, session))
