"""The portfolio projection (Story 2.3) — the SINGLE writer of ``portfolio_cache``.

AD-14: ``portfolio_cache`` is a *read model with one writer*. The broker is the
authoritative source; the cache is a derived projection. This module is that one
writer — nothing else in the codebase writes ``portfolio_cache``. Every other
consumer (the dashboard read in ``api.portfolio``, Story 2.4, later coach reads)
reads it READ-ONLY through the fail-closed :class:`ScopedRepository` (AD-10).

Reconcile-wins, keyed on broker ``as_of``
-----------------------------------------
On any conflict a fresh broker reconciliation wins over existing local state.
:func:`reconcile_portfolio` reads the authoritative :class:`PortfolioSnapshot`
from the Broker Port, compares its ``as_of`` to what is already cached, and:

- if the snapshot is NEWER than the cache (or the cache is empty): atomically
  REPLACES the user's cached rows with the snapshot (delete-then-add — the same
  atomic-replace discipline the 2.1 link callback uses for token rows);
- if the snapshot is NOT newer (a stale/duplicate reconcile): leaves the cache
  untouched, so an out-of-order reconcile never clobbers newer truth.

Post-trade optimistic writes (Epic 4) are always superseded by the next broker
reconcile — the same rule, keyed on the same ``as_of``. There are no trades yet
in this story, so the reconciler here is the only writer that exists.

Money stays ``Decimal`` end to end (never binary float). All access is scoped to
one user via :class:`ScopedRepository`, so a reconcile can only ever touch that
user's own cache.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from brokers.port import BrokerPort, PortfolioSnapshot
from db.models import PortfolioCache
from db.repository import ScopedRepository
from db.scope import Scope
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("ballast.portfolio")


@dataclass(frozen=True)
class PortfolioView:
    """A read-only view of a user's cached portfolio (holdings + cash + as_of).

    Plain projection data — no token/secret values. ``as_of`` is ``None`` only
    when the user has no cache yet (never imported).
    """

    holdings: list[PortfolioCache]
    cash: Decimal
    as_of: datetime | None

    @property
    def is_empty(self) -> bool:
        return not self.holdings


def _normalize_as_of(as_of: datetime) -> datetime:
    """Coerce to tz-aware UTC (the column is tz-aware UTC; be defensive)."""
    return as_of if as_of.tzinfo is not None else as_of.replace(tzinfo=timezone.utc)


def _representative_row(rows: list[PortfolioCache]) -> PortfolioCache | None:
    """The row carrying the authoritative snapshot as_of/cash for the user.

    The single writer replaces all of a user's rows atomically, so they share
    one snapshot's ``as_of``/``cash``. We still pick the row with the newest
    ``as_of`` deterministically (rather than trusting ``rows[0]`` — ``list()``
    has no ORDER BY) so a hypothetical future single-writer violation can never
    let a stale row mask newer truth.
    """
    if not rows:
        return None
    return max(rows, key=lambda r: _normalize_as_of(r.as_of))


def _cached_as_of(rows: list[PortfolioCache]) -> datetime | None:
    """The snapshot ``as_of`` currently cached, or ``None`` if never imported."""
    row = _representative_row(rows)
    return _normalize_as_of(row.as_of) if row is not None else None


def _to_view(rows: list[PortfolioCache]) -> PortfolioView:
    row = _representative_row(rows)
    cash = row.cash if row is not None else Decimal("0")
    return PortfolioView(holdings=rows, cash=cash, as_of=_cached_as_of(rows))


async def get_portfolio(scope: Scope, session: AsyncSession) -> PortfolioView:
    """Read a user's cached portfolio READ-ONLY (never writes). AD-14/AD-10.

    This is the sanctioned read path for the cache; it must never mutate rows.
    """
    repo = ScopedRepository(PortfolioCache, scope, session)
    rows = await repo.list()
    return _to_view(rows)


async def reconcile_portfolio(
    scope: Scope,
    session: AsyncSession,
    broker: BrokerPort,
    *,
    snapshot: PortfolioSnapshot | None = None,
) -> PortfolioView:
    """Reconcile a user's ``portfolio_cache`` from the authoritative broker.

    THE single writer of ``portfolio_cache`` (AD-14). Reads the broker snapshot
    (or uses one passed in — e.g. tests), and applies reconcile-wins keyed on
    ``as_of``: replaces the cache only when the snapshot is newer than what is
    cached; a stale/duplicate snapshot leaves the cache untouched. Commits.

    All access is via the fail-closed scoped repo, so only THIS user's cache is
    ever touched. Returns the resulting :class:`PortfolioView`.
    """
    snap = snapshot if snapshot is not None else broker.fetch_portfolio()
    incoming_as_of = snap.as_of
    if incoming_as_of.tzinfo is None:
        incoming_as_of = incoming_as_of.replace(tzinfo=timezone.utc)

    repo = ScopedRepository(PortfolioCache, scope, session)
    existing = await repo.list()
    cached_as_of = _cached_as_of(existing)

    # Reconcile-wins: only a strictly-newer snapshot supersedes the cache. An
    # equal or older as_of is a stale/duplicate reconcile — leave truth intact.
    if cached_as_of is not None and incoming_as_of <= cached_as_of:
        logger.info(
            "portfolio_reconcile_skipped_stale owner_id=%s incoming_as_of=%s "
            "cached_as_of=%s",
            scope.user_id,
            incoming_as_of.isoformat(),
            cached_as_of.isoformat(),
        )
        return _to_view(existing)

    # Atomic replace: drop the old rows, write the fresh snapshot. Mirrors the
    # 2.1 callback's delete-then-add discipline (ScopedRepository has no bulk
    # delete; per-row delete under the same session commits together).
    for row in existing:
        await session.delete(row)

    written: list[PortfolioCache] = []
    for holding in snap.holdings:
        row = await repo.add(
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
    # state (and a cash-only/empty snapshot yields an empty, consistent view).
    return _to_view(await repo.list())
