"""Target-allocation selection endpoints (Story 10.1, Epic 10 Allocation Coach).

Two authenticated, user-scoped endpoints back the Settings "Target mix" card and
the set-or-decline prompt (``GET``/``PUT /api/target-allocation``) — both funnel
through the fail-closed scope (AD-10), so a user only ever reads or changes their
OWN selection.

``GET`` is READ-ONLY (it never creates a row — an undecided user simply reads
``model: null``); the row is created on the first ``PUT``. An unknown model key
is refused with a calm 422. Weights render as fixed-point strings via
``WireMoney``. This story only records the target — the gap-to-target analysis is
Story 10-2.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from allocation.config import get_config, resolve, set_model
from api.deps import get_scope
from db.scope import Scope
from db.session import get_async_session
from money import WireMoney
from strategy.target_allocation import is_valid_model, list_models

logger = logging.getLogger("ballast.api.target_allocation")

router = APIRouter(prefix="/api/target-allocation", tags=["target-allocation"])


# --- Schemas -----------------------------------------------------------------


class ModelChoiceOut(BaseModel):
    """One offered model portfolio — its key, name, plain-English description, and
    target weights by asset class (weights render as fixed-point strings)."""

    key: str
    name: str
    description: str
    weights: dict[str, WireMoney]


class ResolvedTargetOut(BaseModel):
    """The user's resolved target: weights + canonical buy-fund by asset class.
    Absent (``null``) when the user is undecided."""

    model: str
    weights: dict[str, WireMoney]
    funds: dict[str, str]


class TargetAllocationOut(BaseModel):
    """The current user's target selection + the available choices + the resolved
    target. ``model`` is ``null`` when undecided."""

    model: str | None = None
    choices: list[ModelChoiceOut]
    resolved: ResolvedTargetOut | None = None


class TargetAllocationIn(BaseModel):
    """The chosen model portfolio key from the Settings card / prompt."""

    model: str


# --- Helpers -----------------------------------------------------------------


def _choices() -> list[ModelChoiceOut]:
    return [
        ModelChoiceOut(
            key=m.key, name=m.name, description=m.description, weights=dict(m.weights)
        )
        for m in list_models()
    ]


def _resolved_out(resolved: dict | None) -> ResolvedTargetOut | None:
    if resolved is None:
        return None
    return ResolvedTargetOut(
        model=resolved.get("model"),
        weights=resolved.get("weights", {}),
        funds=resolved.get("funds", {}),
    )


# --- Endpoints ---------------------------------------------------------------


@router.get("", response_model=TargetAllocationOut)
async def read_target_allocation(
    scope: Scope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> TargetAllocationOut:
    """Return the current user's target selection, the choices, and the resolved
    target. READ-ONLY — never writes (undecided reads ``model: null``)."""
    config = await get_config(scope, session)
    # Guard a stale stored key (e.g. a model retired from the reference data):
    # treat it as undecided rather than echo a key with a null resolved target.
    model = (
        config.model_key
        if config is not None and is_valid_model(config.model_key)
        else None
    )
    return TargetAllocationOut(
        model=model,
        choices=_choices(),
        resolved=_resolved_out(resolve(config)),
    )


@router.put("", response_model=TargetAllocationOut)
async def update_target_allocation(
    body: TargetAllocationIn,
    scope: Scope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> TargetAllocationOut:
    """Set the current user's target model portfolio (takes effect now).

    An unknown model key is a config fault the user can fix — surfaced as a calm
    422, never a raw 500 (NFR8). Fail-closed scoped write.
    """
    try:
        config = await set_model(scope, session, body.model)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return TargetAllocationOut(
        model=config.model_key,
        choices=_choices(),
        resolved=_resolved_out(resolve(config)),
    )
