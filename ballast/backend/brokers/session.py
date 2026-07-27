"""Brokerage session status (Story 2.2) — the ~7-day expiry lifecycle.

AD-11: the refresh-token expiry (~weekly) is tolerated with NO data loss. On
expiry the user is *prompted to re-authenticate*; read/coach continue in a
degraded mode, but execution requires a *live* session (an order is NEVER placed
on an expired session). This module owns the small, pure computation of that
session state from the stored ``BrokerageToken.expires_at`` — NO network call is
needed (nor made) to decide it.

There is deliberately no silent token-refresh network path here: v1 prompts
re-auth rather than refreshing behind the user's back (AD-11). The concrete
gate that consumes this state lives in ``api.deps.require_live_broker_session``;
order placement itself is Epic 4.

The three states:

- ``unlinked`` — the user has never linked a brokerage (no token row).
- ``live``     — a token exists and ``expires_at`` is in the future.
- ``expired``  — a token exists but ``expires_at`` is in the past.

Token *values* are never read or returned here — only the (non-secret)
``expires_at`` and ``provider`` metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from db.models import BrokerageToken
from db.repository import ScopedRepository
from db.scope import Scope
from sqlalchemy.ext.asyncio import AsyncSession

# The wire/domain vocabulary for a brokerage session. Kept as a Literal so the
# three legal values are greppable and type-checked end to end.
SessionState = Literal["unlinked", "live", "expired"]


@dataclass(frozen=True)
class BrokerageSession:
    """A user's brokerage session state — plain metadata, NEVER token values.

    ``expires_at`` / ``provider`` are ``None`` when ``state == "unlinked"``.
    ``is_live`` is the single predicate the degraded-mode gate keys on.
    """

    state: SessionState
    expires_at: datetime | None = None
    provider: str | None = None

    @property
    def is_live(self) -> bool:
        """True iff execution-class actions may proceed (session is live)."""
        return self.state == "live"


def compute_state(
    expires_at: datetime | None, *, now: datetime | None = None
) -> SessionState:
    """Pure state computation from a token's ``expires_at`` (or ``None``).

    ``None`` -> ``"unlinked"``. A tz-aware ``expires_at`` in the future ->
    ``"live"``; in the past (or exactly now) -> ``"expired"``. Naive datetimes
    are treated as UTC (the model stores tz-aware UTC, but be defensive).
    """
    if expires_at is None:
        return "unlinked"
    reference = now or datetime.now(timezone.utc)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return "live" if expires_at > reference else "expired"


async def get_brokerage_session(
    scope: Scope, session: AsyncSession, *, now: datetime | None = None
) -> BrokerageSession:
    """Read the current user's brokerage session state via the scoped repo.

    Uses the fail-closed :class:`ScopedRepository` (AD-10) so a user's session
    state can only ever reflect that user's own token. No network call is made;
    the state is derived purely from the stored ``expires_at``.
    """
    repo = ScopedRepository(BrokerageToken, scope, session)
    rows = await repo.list()
    if not rows:
        return BrokerageSession(state="unlinked")
    # The callback enforces one token row per user (it deletes existing rows
    # before adding), so there is normally exactly one. Pick the latest-expiring
    # row defensively so status stays deterministic even if that invariant is
    # ever violated — never let a stale row mask a live one.
    row = max(
        rows,
        key=lambda r: r.expires_at or datetime.min.replace(tzinfo=timezone.utc),
    )
    state = compute_state(row.expires_at, now=now)
    return BrokerageSession(
        state=state, expires_at=row.expires_at, provider=row.provider
    )
