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

from decimal import Decimal

from allocation.engine import ActionItem, Plan, build_plan
from allocation.narrate import AllocationNarration, narrate_plan
from allocation.review import (
    Coverage,
    NarratedFinding,
    build_coverage,
    build_review,
    coverage_message,
)
from api.deps import get_scope
from db.scope import Scope
from db.session import get_async_session
from llm.factory import get_llm_gateway
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
    target_weights: dict[str, str]
    unclassified: dict[str, object]
    investable_cash: str
    undeployed_cash: str
    # Story 10.8 AC5 — honest funding split of ``investable_cash`` (deploy only):
    # settled cash vs the part that comes from selling the parked money-market, plus
    # the protected reserve and the money-market fund symbol(s) that would be sold.
    settlement_cash: str
    from_money_market: str
    reserve: str
    money_market_symbols: list[str]
    # Story 10.10 — the broker's account type ("MARGIN"/"CASH"/null); the frontend
    # shows a gentle margin note when this is "MARGIN". Informational only.
    account_type: str | None = None
    reason: str
    as_of: datetime.datetime | None = None


class NarrationOut(BaseModel):
    """The advisor narration over a deploy plan (Story 10.3).

    ``action_label``/``reasoning`` are the plain-English call + why; ``uncertainties``
    is what is explicitly unknown; ``evidence`` is the cited ``STRATEGY`` records
    serialized via ``record.to_dict()`` (Decimal→fixed-point string). Empty
    ``evidence`` for a no-action status."""

    action_label: str
    reasoning: str
    uncertainties: list[str]
    evidence: list[dict]


class NarrationResponse(BaseModel):
    """The ``GET /api/allocation/narration`` payload — the unchanged plan + its
    advisor narration."""

    plan: PlanOut
    narration: NarrationOut


class SellOrderOut(BaseModel):
    """A SELL MARKET order the coach console pre-fills into the shared order
    controls for the human to co-sign (Story 10.4). ``amount`` is a fixed-point
    string; ``side`` is always ``"sell"`` and ``order_type`` always ``"market"`` —
    populate, never submit."""

    symbol: str
    side: str
    amount: str
    order_type: str


class ReviewFindingOut(BaseModel):
    """One SELL-side analysis finding (Story 10.4 / 11.2).

    ``kind`` ∈ ``{concentration, cost, bond_floor}``. ``switch_to`` is the fund to BUY
    next (the cheaper canonical for ``cost``; the broad bond fund for ``bond_floor``;
    ``null`` for ``concentration``). ``order`` is the ready-to-approve SELL MARKET order;
    ``narration`` is the fiduciary-advisor card. ``current_weight`` / ``target_weight``
    are fixed-point PERCENT strings present ONLY for ``bond_floor`` (current vs chosen-target
    bond %); ``null`` otherwise."""

    kind: str
    symbol: str
    switch_to: str | None = None
    order: SellOrderOut
    narration: NarrationOut
    current_weight: str | None = None
    target_weight: str | None = None


class CoverageOut(BaseModel):
    """The classifiable-coverage meta-check (Story 11.1) — how much of the portfolio the
    review can categorize. ``coverage`` is a fixed-point PERCENT string (e.g. "60.00");
    ``unclassified_value`` is fixed-point money. ``message`` is the calm informational line,
    present ONLY when ``adequate`` is false (nothing to say at good coverage). Carries NO
    order — informational, never money-path."""

    coverage: str
    adequate: bool
    unclassified_value: str
    unclassified_symbols: list[str]
    message: str | None = None


class ReviewResponse(BaseModel):
    """The ``GET /api/allocation/review`` payload — the ranked findings (empty when
    there is nothing to fix), plus the coverage meta-check (Story 11.1; ``null`` when the
    portfolio is empty/never-imported)."""

    findings: list[ReviewFindingOut]
    coverage: CoverageOut | None = None


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
        target_weights={
            cls: format_money(weight) for cls, weight in plan.target_weights.items()
        },
        unclassified={
            "market_value": format_money(plan.unclassified_value),
            "symbols": list(plan.unclassified_symbols),
        },
        investable_cash=format_money(plan.investable_cash),
        undeployed_cash=format_money(plan.undeployed_cash),
        settlement_cash=format_money(plan.settlement_cash),
        from_money_market=format_money(plan.from_money_market),
        reserve=format_money(plan.reserve),
        money_market_symbols=list(plan.money_market_symbols),
        account_type=plan.account_type,
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


def _narration_out(narration: AllocationNarration) -> NarrationOut:
    return NarrationOut(
        action_label=narration.action_label,
        reasoning=narration.reasoning,
        uncertainties=list(narration.uncertainties),
        evidence=[record.to_dict() for record in narration.evidence],
    )


@router.get("/narration", response_model=NarrationResponse)
async def read_narration(
    scope: Scope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> NarrationResponse:
    """Return the caller's deploy plan + the fiduciary-advisor narration (Story 10.3).

    READ-ONLY, degraded-safe, per-user scoped: builds the same deterministic
    :func:`~allocation.engine.build_plan` as ``/plan`` (cached portfolio, no live
    broker session) and narrates it via
    :func:`~allocation.narrate.narrate_plan` against the configured LLM gateway.
    The narration itself is degraded-safe — any gateway/parse/gate failure during
    narration yields the deterministic templated fallback, never an unvalidated
    number (an upstream ``build_plan`` read error surfaces through the app's global
    calm-envelope handler, exactly as it does for ``/plan``). Places NOTHING and
    writes no ``decision_record`` — the human co-signs the
    unchanged ``primary_order`` through the existing ``/approve`` spine. 401 unauth;
    money as fixed-point strings.
    """
    plan = await build_plan(scope, session)
    gateway = get_llm_gateway()
    narration = narrate_plan(gateway, plan)
    return NarrationResponse(
        plan=_plan_out(plan),
        narration=_narration_out(narration),
    )


def _review_finding_out(narrated: NarratedFinding) -> ReviewFindingOut:
    finding = narrated.finding
    # bond_floor (Story 11.2) carries the current vs chosen-target bond % as fixed-point
    # percent strings for the UI x-ray; other kinds leave them null.
    current_weight = target_weight = None
    if finding.kind == "bond_floor" and finding.target_weight is not None:
        current_weight = format_money((finding.weight * Decimal("100")).quantize(Decimal("0.01")))
        target_weight = format_money((finding.target_weight * Decimal("100")).quantize(Decimal("0.01")))
    return ReviewFindingOut(
        kind=finding.kind,
        symbol=finding.symbol,
        switch_to=finding.switch_to,
        order=SellOrderOut(
            # Read the intent's own side (all review intents are SELL today; future-proof
            # against a bond_floor BUY without changing behavior — 11.2 review LOW).
            symbol=finding.order_intent.symbol,
            side=finding.order_intent.side.value,
            amount=format_money(finding.order_intent.amount),
            order_type="market",
        ),
        narration=_narration_out(narrated.narration),
        current_weight=current_weight,
        target_weight=target_weight,
    )


def _coverage_out(cov: Coverage | None) -> CoverageOut | None:
    """Serialize the coverage meta-check (Story 11.1): percent + money as fixed-point
    strings; the calm ``message`` only when coverage is inadequate (adequate → ``None`` so
    the UI shows nothing). ``None`` in → ``None`` out (empty/never-imported portfolio)."""
    if cov is None:
        return None
    pct = format_money((cov.coverage * Decimal("100")).quantize(Decimal("0.01")))
    return CoverageOut(
        coverage=pct,
        adequate=cov.adequate,
        unclassified_value=format_money(cov.unclassified_value.quantize(Decimal("0.01"))),
        unclassified_symbols=list(cov.unclassified_symbols),
        message=None if cov.adequate else coverage_message(cov),
    )


@router.get("/review", response_model=ReviewResponse)
async def read_review(
    scope: Scope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> ReviewResponse:
    """Return the caller's SELL-side portfolio-review findings (Story 10.4).

    READ-ONLY, degraded-safe, per-user scoped: reads the cached portfolio + the
    scoped cash config (no live broker session), runs the two deterministic analysis
    buckets (concentration / cost), and narrates each finding via
    :func:`~allocation.review.narrate_finding` against the configured LLM gateway.
    Each finding's narration is degraded-safe — any gateway/parse/gate failure yields
    the deterministic templated fallback, never an unvalidated number. "Nothing to
    fix" → ``{"findings": []}`` with NO LLM call. Places NOTHING and writes no
    ``decision_record`` — each finding's SELL MARKET order is co-signed by the human
    through the existing ``/approve`` spine. 401 unauth; money as fixed-point strings.
    """
    findings = await build_review(scope, session, get_llm_gateway())
    coverage = await build_coverage(scope, session)
    return ReviewResponse(
        findings=[_review_finding_out(f) for f in findings],
        coverage=_coverage_out(coverage),
    )
