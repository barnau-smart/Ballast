"""The SYSTEM-scope market-data ingestion job (Story 3.1).

Reads end-of-day bars from the market-data port and UPSERTs them into the GLOBAL
``market_daily`` store. This is the "non-user SYSTEM context" AD-10 anticipates:
``market_daily`` is global reference data with no owner, so ingestion attaches NO
user scope and does NOT go through the fail-closed ``ScopedRepository`` (which is
for OWNED per-user entities). This is intentional, not a scoping bypass.

Design guarantees:

- **Idempotent (AC2):** each bar is UPSERTed on the (``symbol``, ``day``) unique
  key via Postgres ``INSERT ... ON CONFLICT DO UPDATE``. Re-running over the same
  or overlapping date ranges never duplicates rows — the store converges to one
  row per (symbol, day); a changed bar updates in place.
- **Source-hiccup tolerant (AC3):** each symbol's fetch+write is wrapped so one
  symbol failing (or a transient vendor error) logs a structured warning and the
  run CONTINUES with the remaining symbols. A later run backfills the gap. The
  returned :class:`IngestResult` reports rows written and which symbols failed.

Money stays ``Decimal`` end to end (never binary float). Secrets/API keys are
never logged. Only DERIVED analytics (OHLC + adjusted close) are stored — never a
raw redistribution of the vendor feed.

Scheduling (out of scope to WIRE, in scope to note): :func:`ingest_market_daily`
is a plain, idempotent async function, and :func:`main` is a thin CLI entrypoint.
A production daily scheduler (cron / a task runner) would simply invoke
``python -m marketdata.ingest --symbols VTI,VXUS,BND --start ... --end ...`` (or
call :func:`ingest_market_daily` directly) on a schedule; because the job is
idempotent, overlapping / retried runs are safe. Real cron wiring is a deployment
concern and is intentionally not built here.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

import anyio.to_thread
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import MarketDaily
from marketdata.port import MarketDataPort

logger = logging.getLogger("ballast.marketdata.ingest")


@dataclass
class IngestResult:
    """A small summary of one ingestion run (SYSTEM scope, no user).

    ``rows_written`` counts bars inserted-or-updated (an upsert of an existing
    row still counts as written). ``symbols_ingested`` are the symbols that
    completed without error; ``symbols_failed`` maps a failed symbol to a short
    error description (AC3 — the run did not abort on these).
    """

    rows_written: int = 0
    symbols_ingested: list[str] = field(default_factory=list)
    symbols_failed: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True when no symbol failed."""
        return not self.symbols_failed


async def ingest_market_daily(
    session: AsyncSession,
    source: MarketDataPort,
    symbols: list[str],
    start: date,
    end: date,
) -> IngestResult:
    """Ingest EOD bars for ``symbols`` over ``[start, end]`` into ``market_daily``.

    Runs as the non-user SYSTEM context: writes the GLOBAL ``market_daily`` table
    directly (no user scope, no ``ScopedRepository``). For each symbol it fetches
    via the port and UPSERTs each bar on (``symbol``, ``day``) so re-runs never
    duplicate (AC2). Each symbol is isolated in try/except so one failure logs a
    warning and the run continues (AC3). Commits per symbol so one symbol's
    failure cannot discard an already-ingested symbol's rows (AC3), and returns an
    :class:`IngestResult`.
    """
    result = IngestResult()
    # Dedupe preserving order so a repeated symbol isn't ingested / counted twice.
    symbols = list(dict.fromkeys(symbols))
    provider = getattr(source, "provider", None) or source.__class__.__name__
    ingested_at = datetime.now(timezone.utc)

    for symbol in symbols:
        try:
            # Offload the vendor fetch (a BLOCKING network call, e.g. Tiingo) to a
            # worker thread so it never freezes the asyncio event loop. The
            # in-process ingest scheduler shares the loop with request handlers, so
            # a direct blocking fetch of N symbols would stall every request for the
            # duration (a 14-symbol boot tick froze the UI for ~10-20s, 2026-08-10).
            # Same offload pattern as brokers/portfolio.py::reconcile_portfolio.
            bars = await anyio.to_thread.run_sync(source.fetch_eod, symbol, start, end)
            written = 0
            for bar in bars:
                stmt = (
                    pg_insert(MarketDaily)
                    .values(
                        symbol=bar.symbol,
                        day=bar.day,
                        open=bar.open,
                        high=bar.high,
                        low=bar.low,
                        close=bar.close,
                        adj_close=bar.adj_close,
                        volume=bar.volume,
                        source=provider,
                        ingested_at=ingested_at,
                    )
                    # UPSERT on the (symbol, day) unique key → idempotent (AC2).
                    .on_conflict_do_update(
                        index_elements=["symbol", "day"],
                        set_={
                            "open": bar.open,
                            "high": bar.high,
                            "low": bar.low,
                            "close": bar.close,
                            "adj_close": bar.adj_close,
                            "volume": bar.volume,
                            "source": provider,
                            "ingested_at": ingested_at,
                        },
                    )
                )
                await session.execute(stmt)
                written += 1
            # Commit PER SYMBOL so a later symbol's failure/rollback can never
            # discard an already-ingested symbol's rows (AC3 durability).
            await session.commit()
            result.rows_written += written
            result.symbols_ingested.append(symbol)
            logger.info(
                "market_daily_ingest_symbol_ok symbol=%s bars=%d source=%s",
                symbol,
                len(bars),
                provider,
            )
        except Exception as exc:  # noqa: BLE001 — tolerate per-symbol hiccups (AC3)
            # Roll back only THIS symbol's partial (uncommitted) writes so a
            # mid-symbol error can't poison the transaction; then continue. The
            # rollback itself can fail on a dead connection — guard it so it can't
            # abort the whole run (AC3).
            try:
                await session.rollback()
            except Exception:  # noqa: BLE001 — a failed rollback must not abort the run
                logger.warning(
                    "market_daily_ingest_rollback_failed symbol=%s", symbol
                )
            # Keep the full error text (our own / vendor message — no secrets).
            error = f"{type(exc).__name__}: {exc}"
            result.symbols_failed[symbol] = error
            logger.warning(
                "market_daily_ingest_symbol_failed symbol=%s error=%s",
                symbol,
                error,
            )

    logger.info(
        "market_daily_ingest_done rows_written=%d symbols_ok=%d symbols_failed=%d "
        "source=%s start=%s end=%s",
        result.rows_written,
        len(result.symbols_ingested),
        len(result.symbols_failed),
        provider,
        start.isoformat(),
        end.isoformat(),
    )
    return result


async def _run_cli(symbols: list[str], start: date, end: date) -> IngestResult:
    """Wire the factory + a real DB session and run one ingestion (CLI helper)."""
    # Imported here so importing this module for its function does not require a
    # configured DB / adapter.
    from db.session import async_session_maker, create_db_and_tables
    from marketdata.factory import get_market_data

    await create_db_and_tables()
    source = get_market_data()
    async with async_session_maker() as session:
        return await ingest_market_daily(session, source, symbols, start, end)


def main(argv: list[str] | None = None) -> int:
    """Thin CLI entrypoint. A scheduler/cron would invoke this daily.

    Example::

        python -m marketdata.ingest --symbols VTI,VXUS,BND \
            --start 2024-01-01 --end 2024-01-31
    """
    from marketdata.fake_adapter import FAKE_SYMBOLS

    parser = argparse.ArgumentParser(description="Ingest EOD bars into market_daily.")
    parser.add_argument(
        "--symbols",
        default=",".join(FAKE_SYMBOLS),
        help="Comma-separated symbols (default: the fake universe).",
    )
    parser.add_argument("--start", required=True, help="Inclusive start date YYYY-MM-DD.")
    parser.add_argument("--end", required=True, help="Inclusive end date YYYY-MM-DD.")
    args = parser.parse_args(argv)

    # Install the app's structured formatter so the run's summary is visible
    # when invoked from a shell / scheduler.
    from api.logging_config import configure_logging

    configure_logging()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        parser.error("no symbols given")

    try:
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end)
    except ValueError as exc:
        parser.error(f"invalid date: {exc}")
    if start > end:
        parser.error("start date is after end date")

    result = asyncio.run(_run_cli(symbols, start, end))
    # A one-line human summary to stdout regardless of log level, so a shell /
    # scheduler run always shows the outcome.
    print(
        f"market_daily ingest: rows_written={result.rows_written} "
        f"symbols_ok={len(result.symbols_ingested)} "
        f"symbols_failed={len(result.symbols_failed)}"
    )
    if result.symbols_failed:
        print("failed symbols:")
        for sym, err in result.symbols_failed.items():
            print(f"  {sym}: {err}")
    # Non-zero exit if any symbol failed, so a scheduler can alert.
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover — CLI shim
    raise SystemExit(main())
