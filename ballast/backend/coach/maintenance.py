"""In-process decisions-maintenance scheduler (pre-unattended-prod hardening).

A background ``asyncio`` task, started in the FastAPI app lifespan, that
periodically:

- **reclaims** crash-orphaned ``cosigning`` decision records — Story 7.2's
  :func:`~coach.decision_record.reclaim_orphaned_cosigning`. Such a row may carry
  a LIVE order and is invisible to both history and the pruner, so without a
  running reclamation path it strands FOREVER. Before this scheduler the reclaimer
  had no production caller at all (it recovered orphans only if a human ran it),
  which is exactly the go-live gap the 2026-08-06 readiness sweep flagged.
- **prunes** stale never-co-signed ``proposed`` records — Story 6.6's
  :func:`~coach.decision_record.prune_stale_proposed_decisions` — so the table
  stays bounded. This replaces the manual ``python -m coach.prune_job`` cron shim
  for the single-instance deployment (that CLI still works for out-of-band runs).

Both operations are idempotent and rowcount-gated, so overlapping runs (a second
app instance, or a manual prune) are safe — no external scheduler/cron is required
for the single-instance deployment this app assumes (mirroring the ``create_all``
startup convention). The loop SLEEPS one interval before its first tick (no
synchronous DB work at boot) and stops promptly and cooperatively on shutdown.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from coach.decision_record import (
    prune_stale_proposed_decisions,
    reclaim_orphaned_cosigning,
)
from db.session import async_session_maker

if TYPE_CHECKING:
    from api.config import Settings

logger = logging.getLogger("ballast.coach.maintenance")

#: A zero-arg callable returning an async-context-managed ``AsyncSession`` — i.e.
#: ``async_session_maker``. Parameterized so tests can inject their own maker.
SessionMaker = Callable[[], AbstractAsyncContextManager[AsyncSession]]


async def run_maintenance_once(
    *,
    reclaim_older_than: datetime.timedelta,
    retention_days: int,
    reclaim_batch_limit: int,
    session_maker: SessionMaker = async_session_maker,
) -> tuple[int, int]:
    """Run ONE maintenance pass: reclaim orphans, then prune stale proposals.

    Each operation runs in its OWN fresh session and is isolated: a transient
    failure in one (e.g. a connection blip) is logged and never prevents the
    other from running this pass — both are idempotent, so the failed one simply
    retries next tick. Returns ``(reclaimed, pruned)`` best-effort counts (a
    failed operation contributes ``0``). The reclaimer additionally isolates each
    ROW internally (see :func:`~coach.decision_record.reclaim_orphaned_cosigning`).
    """
    reclaimed = 0
    pruned = 0

    try:
        async with session_maker() as session:
            reclaimed = await reclaim_orphaned_cosigning(
                session=session,
                older_than=reclaim_older_than,
                limit=reclaim_batch_limit,
            )
    except Exception:
        logger.exception("maintenance reclaim step failed; will retry next tick")

    try:
        async with session_maker() as session:
            pruned = await prune_stale_proposed_decisions(
                session=session, older_than_days=retention_days
            )
    except Exception:
        logger.exception("maintenance prune step failed; will retry next tick")

    return reclaimed, pruned


class MaintenanceScheduler:
    """Owns the background maintenance loop's lifecycle (start / stop).

    Construct with :meth:`from_settings`, :meth:`start` in the lifespan, and
    ``await`` :meth:`stop` on shutdown. Cooperative shutdown only — no task
    cancellation mid-tick: an in-flight pass finishes, then the loop exits.
    """

    def __init__(
        self,
        *,
        interval_seconds: float,
        reclaim_older_than: datetime.timedelta,
        retention_days: int,
        reclaim_batch_limit: int,
        session_maker: SessionMaker = async_session_maker,
    ) -> None:
        self._interval = interval_seconds
        self._reclaim_older_than = reclaim_older_than
        self._retention_days = retention_days
        self._reclaim_batch_limit = reclaim_batch_limit
        self._session_maker = session_maker
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @classmethod
    def from_settings(
        cls, settings: Settings, *, session_maker: SessionMaker = async_session_maker
    ) -> MaintenanceScheduler:
        """Build a scheduler from the app :class:`~api.config.Settings`."""
        return cls(
            interval_seconds=settings.DECISION_MAINTENANCE_INTERVAL_SECONDS,
            reclaim_older_than=datetime.timedelta(
                seconds=settings.DECISION_COSIGNING_RECLAIM_AFTER_SECONDS
            ),
            retention_days=settings.DECISION_PROPOSED_RETENTION_DAYS,
            reclaim_batch_limit=settings.DECISION_RECLAIM_BATCH_LIMIT,
            session_maker=session_maker,
        )

    def start(self) -> None:
        """Launch the background loop (idempotent — a second call is a no-op)."""
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="decisions-maintenance")

    async def _run(self) -> None:
        logger.info(
            "maintenance_scheduler_started interval_s=%s reclaim_after=%s "
            "retention_days=%d batch_limit=%d",
            self._interval,
            self._reclaim_older_than,
            self._retention_days,
            self._reclaim_batch_limit,
        )
        try:
            while not self._stop.is_set():
                # Sleep FIRST, interruptibly: no DB work at boot, and a shutdown
                # signalled during the wait returns immediately (no timeout).
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self._interval
                    )
                except asyncio.TimeoutError:
                    pass
                if self._stop.is_set():
                    break
                try:
                    reclaimed, pruned = await run_maintenance_once(
                        reclaim_older_than=self._reclaim_older_than,
                        retention_days=self._retention_days,
                        reclaim_batch_limit=self._reclaim_batch_limit,
                        session_maker=self._session_maker,
                    )
                    logger.info(
                        "maintenance_tick reclaimed=%d pruned=%d", reclaimed, pruned
                    )
                except Exception:
                    # Belt-and-suspenders: run_maintenance_once already isolates
                    # each step, but the loop must survive ANYTHING so it never
                    # dies silently and leaves orphans unrecovered.
                    logger.exception(
                        "maintenance_tick failed; loop continues (retry next interval)"
                    )
        finally:
            logger.info("maintenance_scheduler_stopped")

    async def stop(self) -> None:
        """Signal the loop to stop and await its clean exit (idempotent)."""
        self._stop.set()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:  # pragma: no cover - defensive
                pass
            self._task = None
