"""The SYSTEM-scope decisions retention prune job (Story 6.6).

Deletes never-co-signed ``proposed`` :class:`~db.models.DecisionRecord` rows
older than ``DECISION_PROPOSED_RETENTION_DAYS`` so the table stays bounded (one
``proposed`` row is inserted at every ``/recommend`` and most never co-sign).

Pruning WRITES, so it lives behind the sole-writer module (AD-6): this CLI is a
thin shim that opens a session and delegates to
:func:`coach.decision_record.prune_stale_proposed_decisions`, whose delete
predicate hard-pins ``status == "proposed"`` — a ``cosigned`` (on-the-record,
immutable) row can never be deleted.

Scheduling is a deployment concern, intentionally not built here: :func:`main`
is a thin CLI (``python -m coach.prune_job``) a scheduler/cron would invoke.
Because the delete is idempotent (re-running deletes nothing new), overlapping /
retried runs are safe.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

logger = logging.getLogger("ballast.coach.prune_job")


async def _run_cli() -> int:
    """Wire a real DB session and run one prune pass (CLI helper).

    Returns the number of stale ``proposed`` records deleted.
    """
    # Imported here so importing this module for its function does not require a
    # configured DB.
    from api.config import get_settings
    from coach.decision_record import prune_stale_proposed_decisions
    from db.session import async_session_maker, create_db_and_tables

    await create_db_and_tables()
    retention_days = get_settings().DECISION_PROPOSED_RETENTION_DAYS
    async with async_session_maker() as session:
        deleted = await prune_stale_proposed_decisions(
            session=session, older_than_days=retention_days
        )
    logger.info(
        "decision_prune_done deleted=%d older_than_days=%d",
        deleted,
        retention_days,
    )
    return deleted


def main(argv: list[str] | None = None) -> int:
    """Thin CLI entrypoint. A scheduler/cron would invoke this.

    Example::

        python -m coach.prune_job
    """
    parser = argparse.ArgumentParser(
        description=(
            "Prune never-co-signed proposed decision records older than the "
            "retention window (cosigned history is never touched)."
        )
    )
    parser.parse_args(argv)

    # Install the app's structured formatter so the run's summary is visible when
    # invoked from a shell / scheduler.
    from api.logging_config import configure_logging

    configure_logging()

    deleted = asyncio.run(_run_cli())
    # A one-line human summary to stdout regardless of log level.
    print(f"decision prune: deleted={deleted} stale proposed records")
    return 0


if __name__ == "__main__":  # pragma: no cover — CLI shim
    raise SystemExit(main())
