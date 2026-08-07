"""In-process daily market-data ingest scheduler (2026-08-07).

Keeps ``market_daily`` fresh unattended: on each tick it re-ingests a short
recent window (``lookback_days``) for the configured symbols via
:func:`~marketdata.ingest.ingest_market_daily` — idempotent upserts, so it
catches the latest EOD bars plus any late vendor corrections. A full historical
backfill remains the manual CLI (``python -m marketdata.ingest``); this only
keeps the tail current.

Mirrors the decisions-maintenance scheduler (``coach/maintenance.py``): a
background ``asyncio`` task started in the app lifespan, resilient to per-tick
errors, stopped cleanly on shutdown. One deliberate difference: this loop is
**work-first** (refresh immediately on start, then sleep between ticks). With a
long (daily) interval, a sleep-first loop could be skipped entirely on an app
that restarts more often than the interval; work-first guarantees a refresh each
boot, and the ingest is cheap + idempotent so the overlap is harmless. The tick
runs in a background task, so it never blocks startup.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from marketdata.factory import get_market_data
from marketdata.ingest import IngestResult, ingest_market_daily
from db.session import async_session_maker

if TYPE_CHECKING:
    from api.config import Settings

logger = logging.getLogger("ballast.marketdata.scheduler")

SessionMaker = Callable[[], AbstractAsyncContextManager[AsyncSession]]


async def run_ingest_once(
    *,
    symbols: list[str],
    lookback_days: int,
    session_maker: SessionMaker = async_session_maker,
    source_factory: Callable[[], object] = get_market_data,
    now: datetime.datetime | None = None,
) -> IngestResult | None:
    """Refresh the recent ``lookback_days`` window for ``symbols``. Resilient.

    ``end`` is today (UTC), ``start`` is ``today - lookback_days``. Uses the
    configured market-data adapter (fake or tiingo). A failure (e.g. vendor down)
    is logged and returns ``None`` — never propagates, so the scheduler loop
    survives. Returns the :class:`~marketdata.ingest.IngestResult` on success.
    """
    if not symbols:
        logger.warning("market_data ingest tick skipped: no symbols configured")
        return None
    effective_now = now or datetime.datetime.now(datetime.timezone.utc)
    end = effective_now.date()
    start = end - datetime.timedelta(days=lookback_days)
    try:
        source = source_factory()
        async with session_maker() as session:
            result = await ingest_market_daily(session, source, symbols, start, end)
    except Exception:
        logger.exception("market_data ingest tick failed; will retry next interval")
        return None
    logger.info(
        "market_data_ingest_tick rows=%d ok=%d failed=%d start=%s end=%s",
        result.rows_written,
        len(result.symbols_ingested),
        len(result.symbols_failed),
        start.isoformat(),
        end.isoformat(),
    )
    return result


class MarketDataIngestScheduler:
    """Owns the background market-data ingest loop's lifecycle (start / stop)."""

    def __init__(
        self,
        *,
        interval_seconds: float,
        symbols: list[str],
        lookback_days: int,
        session_maker: SessionMaker = async_session_maker,
    ) -> None:
        self._interval = interval_seconds
        self._symbols = symbols
        self._lookback_days = lookback_days
        self._session_maker = session_maker
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @classmethod
    def from_settings(
        cls, settings: Settings, *, session_maker: SessionMaker = async_session_maker
    ) -> MarketDataIngestScheduler:
        symbols = [
            s.strip()
            for s in (settings.MARKETDATA_INGEST_SYMBOLS or "").split(",")
            if s.strip()
        ]
        return cls(
            interval_seconds=settings.MARKETDATA_INGEST_INTERVAL_SECONDS,
            symbols=symbols,
            lookback_days=settings.MARKETDATA_INGEST_LOOKBACK_DAYS,
            session_maker=session_maker,
        )

    def start(self) -> None:
        """Launch the background loop (idempotent — a second call is a no-op)."""
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="marketdata-ingest")

    async def _run(self) -> None:
        logger.info(
            "market_data_ingest_scheduler_started interval_s=%s lookback_days=%d "
            "symbols=%d",
            self._interval,
            self._lookback_days,
            len(self._symbols),
        )
        try:
            while not self._stop.is_set():
                # Work-first: refresh now, then wait one interval (see module doc).
                try:
                    await run_ingest_once(
                        symbols=self._symbols,
                        lookback_days=self._lookback_days,
                        session_maker=self._session_maker,
                    )
                except Exception:
                    # run_ingest_once already isolates failures; belt-and-suspenders
                    # so the loop can never die and leave data stale forever.
                    logger.exception(
                        "market_data_ingest_tick crashed; loop continues"
                    )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            logger.info("market_data_ingest_scheduler_stopped")

    async def stop(self) -> None:
        """Signal the loop to stop and await its clean exit (idempotent)."""
        self._stop.set()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:  # pragma: no cover - defensive
                pass
            self._task = None
