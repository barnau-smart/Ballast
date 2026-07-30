"""Read/write helpers for the per-user digest opt-in (Story 5.1, AD-10).

The request-path helpers (:func:`get_or_create_preference`, :func:`set_opt_in`)
go through the fail-closed :class:`~db.repository.ScopedRepository` under the
caller's user scope — a user can only ever read or change their OWN preference.

The batch-path helpers (:func:`list_opted_in`, :func:`unsubscribe_by_token`) run
under the explicit, greppable :meth:`Scope.system` — the sanctioned cross-user
path. ``list_opted_in`` enumerates who to send to; ``unsubscribe_by_token``
backs the unauthenticated one-click unsubscribe link (the clicker has no session,
only their unguessable token).
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import DigestPreference
from db.repository import ScopedRepository
from db.scope import Scope

logger = logging.getLogger("ballast.digest.preferences")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def get_or_create_preference(
    scope: Scope, session: AsyncSession
) -> DigestPreference:
    """Return the caller's preference row, creating an opted-out one if absent.

    Fail-closed per-user: reads/writes only THIS user's row via the scoped repo.
    A freshly created row is opted OUT (default) and carries a minted
    ``unsubscribe_token``. Commits when it creates a row so the token persists.
    """
    repo = ScopedRepository(DigestPreference, scope, session)
    rows = await repo.list()
    if rows:
        return rows[0]

    now = _now()
    try:
        pref = await repo.add(
            opted_in=False,
            unsubscribe_token=secrets.token_urlsafe(32),
            last_sent_week=None,
            created_at=now,
            updated_at=now,
        )
        await session.commit()
        return pref
    except IntegrityError:
        # A concurrent first-time request (e.g. the SPA firing GET on mount and
        # PUT on toggle) can lose the race on UniqueConstraint(owner_id). That is
        # not an error for the caller — the row now exists; roll back and read it.
        await session.rollback()
        rows = await repo.list()
        if rows:
            return rows[0]
        raise


async def set_opt_in(
    scope: Scope, session: AsyncSession, opted_in: bool
) -> DigestPreference:
    """Set the caller's opt-in flag (idempotent) and persist it. Fail-closed."""
    pref = await get_or_create_preference(scope, session)
    pref.opted_in = opted_in
    pref.updated_at = _now()
    await session.commit()
    return pref


async def list_opted_in(session: AsyncSession) -> list[DigestPreference]:
    """Return every opted-in preference row (SYSTEM scope — cross-user).

    This is the sanctioned non-user SYSTEM context (AD-10): a deliberate,
    greppable cross-user read (a direct ``select`` — there is no narrower scope
    for "every opted-in user", and the ``opted_in`` filter means the job never
    even loads opted-out users). It reads only the opt-in flag + token, never any
    per-user financial data — that is resolved later under each user's own cage.
    """
    stmt = select(DigestPreference).where(DigestPreference.opted_in.is_(True))
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def unsubscribe_by_token(session: AsyncSession, token: str) -> bool:
    """Flip a user's opt-in OFF by their unsubscribe token (unauthenticated).

    Returns ``True`` if a matching row was found and turned off (or was already
    off), ``False`` if the token was blank or matched no row. The caller returns
    the SAME calm confirmation either way, so a blank/unknown token reveals
    nothing (no account enumeration). SYSTEM scope: the token, not a session,
    authorises the change.
    """
    if not token:
        return False

    # Sanctioned non-user SYSTEM context (AD-10): the unguessable token — not a
    # session — authorises this single cross-user lookup and opt-out.
    #
    # Guard the WHOLE DB interaction: the caller returns the SAME calm 200 for a
    # True or False result, so an unhandled error here would both break that
    # "always calm 200" contract AND leak token validity — a valid-token commit
    # failure would bubble out as a 500 while a blank/unknown token returns 200,
    # a distinguishing enumeration signal. On any failure, roll back best-effort
    # and report "not matched" so the response stays identical either way.
    try:
        stmt = select(DigestPreference).where(
            DigestPreference.unsubscribe_token == token
        )
        result = await session.execute(stmt)
        pref = result.scalars().first()
        if pref is None:
            return False

        pref.opted_in = False
        pref.updated_at = _now()
        await session.commit()
        return True
    except Exception:  # noqa: BLE001 — never surface a 500 / enumeration signal
        logger.warning("digest_unsubscribe_failed")
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001 — a failed rollback must not abort
            logger.warning("digest_unsubscribe_rollback_failed")
        return False
