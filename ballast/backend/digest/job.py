"""The SYSTEM-scope weekly digest job (Story 5.1, FR21).

Sends the opt-in calm weekly email to every user who asked for it. This is a
non-user batch context (AD-10): it enumerates opted-in preferences under the
explicit :meth:`Scope.system`, then resolves EACH user's data under that user's
own :meth:`Scope.for_user` cage — never a cross-user data read. Recipient email
comes from the ``user`` table (the owner, not an owned entity), read directly.

Design guarantees (mirroring :mod:`marketdata.ingest`):

- **Idempotent:** each user carries a ``last_sent_week`` ISO year-week marker. A
  user whose marker already equals the current week is skipped, so a re-run (or
  an overlapping run) never double-sends. Only a successful send advances the
  marker (committed per-user).
- **Failure-isolated:** each user's send is wrapped so one failure logs a warning
  and the run CONTINUES with the rest; that user is left UNMARKED and picked up
  next run. Commit is per-user so one failure can't discard another's progress.

Scheduling is a deployment concern, intentionally not built here: :func:`main` is
a thin CLI (``python -m digest.job``) a weekly cron / task-runner would invoke;
because the job is idempotent, overlapping / retried runs are safe.

The digest is EMAIL ONLY — it sends solely through the :class:`EmailPort` and
computes no market statistics of its own.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from brokers.portfolio import get_portfolio
from db.models import DigestPreference, User
from db.scope import Scope
from digest.compose import compose_digest
from digest.email_port import EmailPort
from digest.preferences import list_opted_in

logger = logging.getLogger("ballast.digest.job")


def iso_week_key(now: datetime) -> str:
    """The ISO year-week marker (``"YYYY-Www"``) — the idempotency key."""
    iso = now.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


@dataclass
class DigestRunResult:
    """A small summary of one weekly-digest run (SYSTEM scope, no single user).

    ``sent`` are the recipients emailed this run; ``skipped`` were already sent
    this week (idempotent no-op); ``failed`` maps a failed recipient (or owner id)
    to a short error — the run did not abort on these.
    """

    sent: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True when no recipient failed."""
        return not self.failed


async def send_weekly_digests(
    session: AsyncSession,
    sender: EmailPort,
    *,
    unsubscribe_base_url: str,
    now: datetime | None = None,
) -> DigestRunResult:
    """Send the weekly digest to every opted-in user not yet sent this week.

    Runs under the explicit SYSTEM scope to enumerate opted-in users, then reads
    each user's portfolio under their own scope. Idempotent (``last_sent_week``)
    and failure-isolated (per-user commit + try/except). Returns a
    :class:`DigestRunResult`.
    """
    week_key = iso_week_key(now or datetime.now(timezone.utc))
    base = unsubscribe_base_url.rstrip("/")
    result = DigestRunResult()

    prefs = await list_opted_in(session)
    # Snapshot the scalar fields up front. A per-user failure below calls
    # session.rollback(), which EXPIRES every ORM instance in the identity map
    # (independent of expire_on_commit); re-reading an expired attribute (e.g.
    # the next pref's owner_id) in this async context raises MissingGreenlet and
    # would abort the whole run — defeating the per-user failure isolation. Work
    # from plain values instead, and write the marker via an explicit UPDATE.
    targets = [(p.owner_id, p.last_sent_week, p.unsubscribe_token) for p in prefs]
    for owner_id, pref_last_sent_week, unsubscribe_token in targets:
        label = str(owner_id)
        try:
            # Idempotent: never a second send in the same ISO week.
            if pref_last_sent_week == week_key:
                result.skipped.append(label)
                continue

            # Recipient email from the user table (the owner, not an owned/scoped
            # entity) — read directly under the system context.
            user = (
                await session.execute(select(User).where(User.id == owner_id))
            ).scalars().first()
            if user is None:
                # An opted-in preference with no user is a data anomaly, not a
                # send — record and move on (the CASCADE should prevent this).
                result.failed[label] = "no matching user"
                logger.warning("digest_no_user owner_id=%s", owner_id)
                continue
            label = user.email
            if not user.is_active:
                # A deactivated/disabled account never receives the proactive
                # email — skip quietly (not a failure); a reactivated user is
                # picked up next run.
                result.skipped.append(user.email)
                continue

            # Plan status read under the USER's own cage (never cross-user).
            view = await get_portfolio(Scope.for_user(owner_id), session)
            unsubscribe_url = (
                f"{base}/api/digest/unsubscribe?token={unsubscribe_token}"
            )
            message = compose_digest(
                view,
                unsubscribe_url=unsubscribe_url,
                recipient_email=user.email,
            )
            sender.send(message)

            # Advance the idempotency marker ONLY after a successful send, via an
            # explicit UPDATE (not ORM-instance mutation) so it never touches
            # expire-on-rollback state, and commit per-user so one later failure
            # can't undo this progress.
            await session.execute(
                update(DigestPreference)
                .where(DigestPreference.owner_id == owner_id)
                .values(
                    last_sent_week=week_key,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            await session.commit()

            result.sent.append(user.email)
            logger.info("digest_sent to=%s week=%s", user.email, week_key)
        except Exception as exc:  # noqa: BLE001 — isolate per-user failures
            # Roll back this user's uncommitted marker change so it is retried
            # next run; guard the rollback itself so it can't abort the run.
            try:
                await session.rollback()
            except Exception:  # noqa: BLE001 — a failed rollback must not abort
                logger.warning("digest_rollback_failed owner_id=%s", owner_id)
            error = f"{type(exc).__name__}: {exc}"
            result.failed[label] = error
            logger.warning("digest_send_failed recipient=%s error=%s", label, error)

    logger.info(
        "digest_run_done sent=%d skipped=%d failed=%d week=%s",
        len(result.sent),
        len(result.skipped),
        len(result.failed),
        week_key,
    )
    return result


async def _run_cli() -> DigestRunResult:
    """Wire the factory + a real DB session and run one digest send (CLI helper)."""
    # Imported here so importing this module for its function does not require a
    # configured DB / adapter.
    from api.config import get_settings
    from db.session import async_session_maker, create_db_and_tables
    from digest.factory import get_email_sender

    await create_db_and_tables()
    sender = get_email_sender()
    base = get_settings().DIGEST_UNSUBSCRIBE_BASE_URL
    async with async_session_maker() as session:
        return await send_weekly_digests(
            session, sender, unsubscribe_base_url=base
        )


def main(argv: list[str] | None = None) -> int:
    """Thin CLI entrypoint. A weekly scheduler/cron would invoke this.

    Example::

        python -m digest.job
    """
    parser = argparse.ArgumentParser(
        description="Send the opt-in calm weekly email digest to opted-in users."
    )
    parser.parse_args(argv)

    # Install the app's structured formatter so the run's summary is visible when
    # invoked from a shell / scheduler.
    from api.logging_config import configure_logging

    configure_logging()

    result = asyncio.run(_run_cli())
    # A one-line human summary to stdout regardless of log level.
    print(
        f"weekly digest: sent={len(result.sent)} "
        f"skipped={len(result.skipped)} failed={len(result.failed)}"
    )
    if result.failed:
        print("failed recipients:")
        for recipient, err in result.failed.items():
            print(f"  {recipient}: {err}")
    # Non-zero exit if any recipient failed, so a scheduler can alert.
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover — CLI shim
    raise SystemExit(main())
