"""The generalized atomic-claim primitive (Story 7.2) — ONE shared mechanism.

The Story 6.1 atomic claim proved the pattern at the ``/approve`` seam only: a
scoped, rowcount-gated conditional ``UPDATE … WHERE`` that admits EXACTLY ONE
winner under concurrency. Every later Epic 6 seam re-opened that same atomicity
class as a hand-rolled read-modify-write. This module extracts the 6.1 pattern
into ONE reusable, tested toolkit applied at all three concurrency seams (the
placement claim, the balance reconcile, and the reconcile endpoint).

Two strategies — by NECESSITY, not preference:

- :func:`conditional_claim` — the OPTIMISTIC guard: execute a caller-built
  conditional ``UPDATE`` statement and return whether it matched exactly one row
  (``rowcount == 1``). Postgres serializes the row update, so of any number of
  concurrent claims exactly one wins. It does NOT commit — the CALLER owns the
  commit boundary, because the placement claim must commit BEFORE the network
  ``place_order`` call (so it can't hold a lock), whereas a reconcile commits
  together with its read.
- :func:`lock_row` — the PESSIMISTIC guard: a scoped ``SELECT … FOR UPDATE`` that
  serializes concurrent writers on one row across sessions. Used where the guard
  cannot be expressed as a portable ``WHERE`` predicate (e.g. the reconcile
  endpoint's monotonic terminal guard lives in a JSON column), so a row lock makes
  the existing in-writer guard effective across sessions.

Model-AGNOSTIC on purpose (canary preservation): both take a caller-built
statement or a model+ids, so every ``update(DecisionRecord)`` and the model
construction text stays inside the AD-6 sole-writer module and every
``update(PortfolioBalance)`` stays inside the AD-14 sole-writer module. The AD-6
construction canary and the single-writer conventions are untouched.
"""

from __future__ import annotations

from typing import TypeVar
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Executable

ModelT = TypeVar("ModelT")


async def conditional_claim(session: AsyncSession, statement: Executable) -> bool:
    """Execute a caller-built conditional ``UPDATE`` and return whether it won.

    The OPTIMISTIC atomic claim (generalized from Story 6.1). The caller builds a
    rowcount-gated conditional statement — e.g.
    ``update(DecisionRecord).where(id, owner_id, status='proposed').values(...)``
    — and this executes it and returns ``result.rowcount == 1``: ``True`` iff this
    caller matched exactly the one row it intended to claim (the winner), ``False``
    when a concurrent claim already moved the row out from under the predicate
    (rowcount 0). Postgres serializes the row update, so of any number of
    concurrent claims EXACTLY ONE gets ``rowcount == 1``.

    Deliberately does NOT commit: the CALLER owns the commit boundary. The
    placement claim must COMMIT before the ``place_order`` network round-trip (so
    no DB row lock is held across the network), while a balance reconcile commits
    the winning update together with the holdings replace in one transaction.
    Keeping the primitive model-agnostic (a caller-built statement) is what keeps
    every ``update(Model)`` inside the model's sole-writer module (AD-6/AD-14).
    """
    result = await session.execute(statement)
    return result.rowcount == 1


async def lock_row(
    session: AsyncSession,
    model: type[ModelT],
    *,
    entity_id: UUID,
    owner_id: UUID,
) -> ModelT | None:
    """Scoped ``SELECT … FOR UPDATE`` — the PESSIMISTIC single-row lock.

    Issues ``SELECT * FROM <model> WHERE id = :entity_id AND owner_id = :owner_id
    FOR UPDATE`` and returns the locked instance (or ``None`` when no such row is
    visible to this owner). The row lock serializes concurrent writers on that one
    row ACROSS sessions: a second caller blocks on the ``FOR UPDATE`` until the
    first commits, then reads the just-committed state — which is what makes an
    in-writer guard that cannot be expressed as a portable ``WHERE`` (e.g. the
    reconcile endpoint's monotonic terminal guard in a JSON column) effective
    across sessions.

    The explicit ``owner_id`` predicate enforces per-user isolation on the raw
    select (a foreign row is invisible → ``None``), mirroring the
    :class:`~db.repository.ScopedRepository` cage. Model-agnostic so the calling
    sole-writer module still owns every reference to its model.
    """
    result = await session.execute(
        select(model)
        .where(model.id == entity_id, model.owner_id == owner_id)
        .with_for_update()
    )
    return result.scalars().first()
