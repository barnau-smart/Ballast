"""Read/write helpers for the per-user target-allocation selection (Story 10.1, AD-10).

The request-path helpers (:func:`get_or_create_config`, :func:`get_config`,
:func:`set_model`) go through the fail-closed
:class:`~db.repository.ScopedRepository` under the caller's user scope — a user
can only ever read or change their OWN target selection.

The selection is honest-by-construction: ``model_key is None`` is the **undecided**
state (drives the calm set-or-decline prompt, never silently a default model);
a non-null key is always a *valid* model (validated on write). :func:`resolve`
is the ONE place the stored key becomes a concrete target (weights + funds by
asset class) for downstream analysis (Story 10-2).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import TargetAllocationConfig
from db.repository import ScopedRepository
from db.scope import Scope
from strategy.target_allocation import get_model, resolve_target

logger = logging.getLogger("ballast.allocation.config")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def get_config(
    scope: Scope, session: AsyncSession
) -> TargetAllocationConfig | None:
    """Return the caller's target-allocation config if it exists, else ``None`` —
    READ-ONLY.

    Never writes: the read/prompt path uses this so a plain read can't INSERT a
    row (or race on ``uq_target_allocation_config_owner``), and treats an absent
    config as the calm undecided default. Fail-closed per-user via the scoped repo.
    """
    repo = ScopedRepository(TargetAllocationConfig, scope, session)
    rows = await repo.list()
    return rows[0] if rows else None


async def get_or_create_config(
    scope: Scope, session: AsyncSession
) -> TargetAllocationConfig:
    """Return the caller's config, creating a calm undecided one if absent.

    Fail-closed per-user. A freshly created row reads as the calm default —
    ``model_key=None`` (undecided). Commits when it creates a row so the default
    persists. Handles the first-request race (SPA firing GET on mount + PUT on
    save) the same way the digest/cash configs do: on ``IntegrityError`` roll back
    and re-read.
    """
    repo = ScopedRepository(TargetAllocationConfig, scope, session)
    rows = await repo.list()
    if rows:
        return rows[0]

    now = _now()
    try:
        config = await repo.add(
            model_key=None,
            created_at=now,
            updated_at=now,
        )
        await session.commit()
        return config
    except IntegrityError:
        await session.rollback()
        rows = await repo.list()
        if rows:
            return rows[0]
        raise


async def set_model(
    scope: Scope, session: AsyncSession, key: str
) -> TargetAllocationConfig:
    """Set the caller's chosen model portfolio — idempotent, fail-closed.

    ``key`` must name a known model in
    :data:`strategy.target_allocation.MODEL_PORTFOLIOS` (case-insensitive), else a
    ``ValueError`` is raised (the API surfaces a calm 422). The stored key is
    normalized to the model's canonical key.
    """
    model = get_model(key)
    if model is None:
        raise ValueError(
            "Unknown target-allocation model. Pick one of the offered mixes."
        )

    repo = ScopedRepository(TargetAllocationConfig, scope, session)
    now = _now()

    async def _apply(config: TargetAllocationConfig) -> TargetAllocationConfig:
        config.model_key = model.key
        config.updated_at = now
        await session.commit()
        return config

    rows = await repo.list()
    if rows:
        return await _apply(rows[0])

    # First-time selection: create the row WITH the chosen model in a SINGLE
    # commit (no separate default-row commit) — matches the single-write
    # discipline of cash.set_config. Handle the lost-race the same way
    # get_or_create does.
    try:
        config = await repo.add(model_key=model.key, created_at=now, updated_at=now)
        await session.commit()
        return config
    except IntegrityError:
        await session.rollback()
        rows = await repo.list()
        if rows:
            return await _apply(rows[0])
        raise


def resolve(config: TargetAllocationConfig | None) -> dict | None:
    """Resolve a config to its concrete target (weights + funds by asset class),
    or ``None`` when undecided/absent. The sole contract Story 10-2 consumes."""
    if config is None:
        return None
    return resolve_target(config.model_key)
