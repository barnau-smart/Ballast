"""Gap-to-target deploy-my-cash endpoint (Story 10.2, Epic 10 Allocation Coach).

One authenticated, user-scoped, READ-ONLY endpoint — ``GET /api/allocation/plan``
— backs the coach console's "Deploy your cash toward your target" affordance. It
funnels through the fail-closed scope (AD-10), so a user only ever reads their OWN
holdings/cash/target. It computes NOTHING to the DB: it calls the pure
:func:`allocation.engine.build_plan`, which reads the cached portfolio (no live
broker session) and never places an order or writes a ``decision_record``.

The response serializes every money/weight value as a fixed-point string via
``format_money`` (never binary float, never ``E+``/``E-``). ``primary_order`` is
the largest-gap MARKET BUY the frontend pre-fills into the existing ``/approve``
order controls for the human to co-sign; it is ``null`` for any no-action status
(``at_target`` / ``no_cash`` / ``no_target`` / ``decide_reserve``). Nothing is ever
submitted here.
"""

from __future__ import annotations

import datetime
import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from allocation.engine import ActionItem, Plan, build_plan
from api.deps import get_scope
from db.scope import Scope
from db.session import get_async_session
from money import format_money

logger = logging.getLogger("ballast.api.allocation")

router = APIRouter(prefix="/api/allocation", tags=["allocation"])


# --- Schemas -----------------------------------------------------------------


class ActionItemOut(BaseModel):
    """One concrete cash-only BUY toward target — canonical fund + dollar amount
    (a fixed-point string)."""

    asset_class: str
    symbol: str
    amount: str


class PrimaryOrderOut(BaseModel):
    """The largest-gap buy, pre-filled into the coach console's order controls.

    Always a MARKET BUY (whole-share flooring happens later at execution, so the
    engine needs no live ask). ``amount`` is a fixed-point string. ``null`` for any
    no-action status."""

    symbol: str
    side: str
    amount: str
    order_type: str


class CurrentSleeveOut(BaseModel):
    """The user's current position in one asset class: market value + its share of
    the classified sleeve (both fixed-point strings)."""

    market_value: str
    weight: str


class PlanOut(BaseModel):
    """The gap-to-target plan. ``status`` ∈
    ``{deploy, at_target, no_cash, no_target, decide_reserve}``. ``action_items`` /
    ``primary_order`` are populated only for ``deploy``; ``reason`` carries the calm
    plain-English explanation for a no-action status. All money as fixed-point
    strings."""

    status: str
    action_items: list[ActionItemOut]
    primary_order: PrimaryOrderOut | None = None
    current: dict[str, CurrentSleeveOut]
    unclassified: dict[str, object]
    investable_cash: str
    undeployed_cash: str
    reason: str
    as_of: datetime.datetime | None = None


# --- Helpers -----------------------------------------------------------------


def _action_item_out(item: ActionItem) -> ActionItemOut:
    return ActionItemOut(
        asset_class=item.asset_class,
        symbol=item.symbol,
        amount=format_money(item.amount),
    )


def _primary_order_out(item: ActionItem | None) -> PrimaryOrderOut | None:
    if item is None:
        return None
    return PrimaryOrderOut(
        symbol=item.symbol,
        side="buy",
        amount=format_money(item.amount),
        order_type="market",
    )


def _plan_out(plan: Plan) -> PlanOut:
    return PlanOut(
        status=plan.status,
        action_items=[_action_item_out(it) for it in plan.action_items],
        primary_order=_primary_order_out(plan.primary_order),
        current={
            cls: CurrentSleeveOut(
                market_value=format_money(vals["market_value"]),
                weight=format_money(vals["weight"]),
            )
            for cls, vals in plan.current.items()
        },
        unclassified={
            "market_value": format_money(plan.unclassified_value),
            "symbols": list(plan.unclassified_symbols),
        },
        investable_cash=format_money(plan.investable_cash),
        undeployed_cash=format_money(plan.undeployed_cash),
        reason=plan.reason,
        as_of=plan.as_of,
    )


# --- Endpoints ---------------------------------------------------------------


@router.get("/plan", response_model=PlanOut)
async def read_plan(
    scope: Scope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> PlanOut:
    """Return the caller's deterministic gap-to-target deploy-my-cash plan.

    READ-ONLY, degraded-safe (cached portfolio, no live broker session), per-user
    scoped. Places NOTHING and writes no decision record — the human co-signs the
    ``primary_order`` through the existing ``/approve`` spine. 401 unauth; money as
    fixed-point strings.
    """
    plan = await build_plan(scope, session)
    return _plan_out(plan)
