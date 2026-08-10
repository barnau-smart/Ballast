"""Cash-configuration + just-in-time liquidation endpoints (Epic 9 Cash Intelligence).

Story 9.1 — two authenticated, user-scoped endpoints back the Settings "Cash
setup" card and the set-or-decline prompt (``GET``/``PUT /api/cash/config``).

Story 9.3 — four more back the just-in-time liquidation + deferred/resumed buy
flow: ``POST /liquidation-plan`` (compute the pre-filled money-market SELL for a
buy's shortfall + record a durable pending buy), ``GET /pending-buys`` (list
awaiting-funds pending buys with a live ``funds_ready``), and
``POST /pending-buys/{id}/resume`` + ``/cancel`` (resume once cash settles, or
cancel). Nothing is EVER placed here — the human executes the sell and the
resumed buy via the existing ``/api/coach/approve`` co-sign path.

All endpoints funnel through the fail-closed scope (AD-10), so a user only ever
reads or changes their OWN rows. Money crosses the wire as fixed-point strings via
``WireMoney`` (never binary float, never ``E+``/``E-``); a config fault is refused
with a calm 422 the same way ``/api/portfolio/refresh`` does.
"""

from __future__ import annotations

import datetime
import logging
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_scope
from brokers.portfolio import get_portfolio
from cash.config import get_or_create_config, set_config
from cash.liquidation import LiquidationPlan, plan_liquidation
from coach.decision_record import record_proposal
from coach.recommendation import (
    Duration,
    OrderIntent,
    OrderSide,
    OrderType,
    Recommendation,
    Session,
)
from coach.validation import BlessedRecommendation, validate_recommendation
from db.models import PendingBuy
from db.repository import ScopedRepository
from db.scope import Scope
from db.session import get_async_session
from llm.factory import get_llm_gateway
from money import WireMoney, format_money
from precedent.evidence import EvidenceKind, EvidenceRecord, make_id

logger = logging.getLogger("ballast.api.cash")

router = APIRouter(prefix="/api/cash", tags=["cash"])


# --- Schemas -----------------------------------------------------------------


class CashConfigOut(BaseModel):
    """The current user's cash configuration.

    ``reserve_amount`` is the RAW stored amount: the set value, or ``null`` when
    never-decided OR declined (``reserve_decided`` disambiguates). Money renders
    as a fixed-point string via ``WireMoney``.
    """

    reserve_amount: WireMoney | None = None
    reserve_decided: bool
    parked_symbols: list[str]


class CashConfigIn(BaseModel):
    """A desired cash configuration from the Settings card / set-or-decline.

    ``reserve_amount`` in as ``Decimal | None`` (Pydantic coerces the JSON
    string/number). ``reserve_decided`` records that the user has made an explicit
    decision (set OR declined) — a PUT is always an explicit act, so it is
    ``True`` by default. ``reserve_amount = null`` with ``reserve_decided = true``
    means "I don't keep a reserve" (declined → resolves to 0).
    """

    reserve_amount: Decimal | None = None
    reserve_decided: bool = True
    parked_symbols: list[str] = []


# --- Endpoints ---------------------------------------------------------------


def _to_out(config) -> CashConfigOut:
    return CashConfigOut(
        reserve_amount=config.reserve_amount,
        reserve_decided=config.reserve_decided,
        parked_symbols=list(config.parked_symbols or []),
    )


@router.get("/config", response_model=CashConfigOut)
async def read_config(
    scope: Scope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> CashConfigOut:
    """Return the current user's cash config (calm default for a new user)."""
    config = await get_or_create_config(scope, session)
    return _to_out(config)


@router.put("/config", response_model=CashConfigOut)
async def update_config(
    body: CashConfigIn,
    scope: Scope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> CashConfigOut:
    """Set the reserve (or decline) and the parked-symbol set (takes effect now).

    An invalid reserve (negative, non-finite, out of range, or over two decimal
    places) is a config fault the user can fix — surfaced as a calm 422, never a
    raw 500 (NFR8). The whole config is written in one atomic commit through the
    fail-closed scoped helper, so a user can only ever change their OWN config and
    a failure never half-applies.
    """
    try:
        config = await set_config(
            scope,
            session,
            amount=body.reserve_amount,
            decided=body.reserve_decided,
            symbols=body.parked_symbols,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _to_out(config)


# --- Story 9.3: just-in-time liquidation + deferred/resumed buy --------------


class OrderIntentField(BaseModel):
    """A serialized :class:`~coach.recommendation.OrderIntent` (money as strings).

    The pre-filled SELL (in a liquidation plan) and the pre-filled resumed BUY (on
    resume) both cross the wire in this shape — the frontend populates the existing
    ``/api/coach/approve`` controls from it. Money fields are fixed-point strings.
    """

    symbol: str
    side: OrderSide
    amount: str
    order_type: OrderType = OrderType.MARKET
    limit_price: str | None = None
    stop_price: str | None = None
    session: Session = Session.REGULAR
    duration: Duration = Duration.DAY


class LiquidationPlanRequest(BaseModel):
    """The decided buy whose shortfall we plan a liquidation for (Story 9.3).

    ``symbol``/``amount`` are the buy the user reached the buy step with; ``amount``
    is ``Decimal`` (never float). The backend COMPUTES the shortfall + the pre-filled
    SELL deterministically off the user's cached portfolio — no live quote.
    """

    symbol: str
    amount: Decimal


class LiquidationPlanResponse(BaseModel):
    """A just-in-time liquidation plan, money as fixed-point strings (Story 9.3).

    ``needs_liquidation`` is ``False`` when ready-to-trade cash already covers the
    buy (no sell, no pending buy). Otherwise ``coverable`` says whether parked value
    ABOVE the reserve covers the whole ``shortfall``; ``sell_symbol``/``sell_amount``/
    ``est_shares``/``sell_order_intent`` pre-fill the money-market SELL (``null`` when
    there is nothing to liquidate). ``reserved`` is the protected reserve (``null``
    never-decided) and is NEVER included in liquidatable value. ``as_of`` surfaces
    data freshness on every figure. ``pending_buy_id`` is the durable pending buy
    recorded when ``needs_liquidation``; ``sell_decision_id`` is the proposed SELL
    decision (present whenever there is a sell to make — coverable OR partial; ``null``
    only when there is nothing to liquidate). ``reasoning`` is the LLM's calm prose.
    """

    needs_liquidation: bool
    coverable: bool
    ready_to_trade: WireMoney
    shortfall: WireMoney
    sell_symbol: str | None = None
    sell_amount: WireMoney | None = None
    est_shares: int | None = None
    sell_order_intent: OrderIntentField | None = None
    reserved: WireMoney | None = None
    reserve_decided: bool
    as_of: datetime.datetime | None = None
    reasoning: str
    pending_buy_id: str | None = None
    sell_decision_id: str | None = None


class PendingBuyOut(BaseModel):
    """One durable pending buy awaiting settled funds (Story 9.3), money as strings.

    ``funds_ready`` is computed LIVE (``ready_to_trade >= amount`` from the
    authoritative ``portfolio_balance``) — never a fabricated T+2 timer — so the
    resume control appears exactly when the money is genuinely spendable.
    ``buy_intent`` pre-fills the resumed buy exactly as first stated. ``as_of``
    surfaces the freshness of the ready-to-trade figure the compare used.
    """

    pending_buy_id: str
    buy_intent: OrderIntentField
    amount: WireMoney
    status: str
    funds_ready: bool
    ready_to_trade: WireMoney
    sell_decision_id: str | None = None
    created_at: datetime.datetime
    as_of: datetime.datetime | None = None


class PendingBuyListResponse(BaseModel):
    """The caller's awaiting-funds pending buys (Story 9.3), pull-only on visit."""

    pending_buys: list[PendingBuyOut]


class ResumeResponse(BaseModel):
    """The result of resuming a pending buy (Story 9.3), money as strings.

    ``decision_id`` is the freshly-minted proposed BUY decision; ``order_intent`` is
    the pre-filled buy the user submits through the unchanged ``/api/coach/approve``
    co-sign path. Nothing is placed here.
    """

    decision_id: str
    order_intent: OrderIntentField
    pending_buy_id: str


class CancelPendingBuyResponse(BaseModel):
    """The result of cancelling a pending buy (Story 9.3)."""

    pending_buy_id: str
    status: str


def _order_intent_field(intent: OrderIntent) -> OrderIntentField:
    """Serialize an :class:`OrderIntent` onto the wire (money fixed-point)."""
    return OrderIntentField(
        symbol=intent.symbol,
        side=intent.side,
        amount=format_money(intent.amount),
        order_type=intent.order_type,
        limit_price=(
            None if intent.limit_price is None else format_money(intent.limit_price)
        ),
        stop_price=(
            None if intent.stop_price is None else format_money(intent.stop_price)
        ),
        session=intent.session,
        duration=intent.duration,
    )


def _order_intent_from_snapshot(snapshot: dict) -> OrderIntent:
    """Rebuild an :class:`OrderIntent` from a stored ``buy_intent`` JSON snapshot.

    The snapshot is the OMIT-WHEN-DEFAULT shape ``coach.decision_record`` writes
    (money as fixed-point strings). Absent order-model keys take their MARKET /
    REGULAR / DAY / ``None`` defaults, exactly matching how the intent was frozen.
    """
    return OrderIntent(
        symbol=snapshot["symbol"],
        side=OrderSide(snapshot["side"]),
        amount=Decimal(snapshot["amount"]),
        order_type=OrderType(snapshot.get("order_type", OrderType.MARKET.value)),
        limit_price=(
            None
            if snapshot.get("limit_price") is None
            else Decimal(snapshot["limit_price"])
        ),
        stop_price=(
            None
            if snapshot.get("stop_price") is None
            else Decimal(snapshot["stop_price"])
        ),
        session=Session(snapshot.get("session", Session.REGULAR.value)),
        duration=Duration(snapshot.get("duration", Duration.DAY.value)),
    )


def _liquidation_blessed(
    *, action_label: str, reasoning: str, order_intent: OrderIntent, as_of
) -> BlessedRecommendation:
    """Build a minimal :class:`BlessedRecommendation` for a deterministic decision.

    Story 9.3 mints proposed decisions for a computed SELL (in the plan) and the
    resumed BUY (on resume) WITHOUT running the coach pipeline — the numbers are
    deterministic, not LLM-authored. ``record_proposal`` needs a blessed shape with
    at least one real :class:`~precedent.evidence.EvidenceRecord` and a non-empty
    uncertainty, so we attach a single self-describing STRATEGY evidence record
    (the honest "this is a mechanical liquidation/deferred-buy step, not a market
    call"). Constructed directly (not via the validator) because there is no LLM
    candidate to validate — the evidence + uncertainty are supplied here by
    construction so the immutable snapshot stays well-formed for replay (AD-5).
    """
    stat_day = (as_of.date() if as_of is not None else datetime.date.today())
    stats = {"purpose": "just-in-time-liquidation", "symbol": order_intent.symbol}
    evidence = EvidenceRecord(
        id=make_id(EvidenceKind.STRATEGY, order_intent.symbol, stat_day, stats),
        kind=EvidenceKind.STRATEGY,
        statement=(
            "This is a mechanical cash step to fund the buy you chose — not a "
            "market call. Your protected reserve is never sold, and nothing is "
            "placed until you review and submit it yourself."
        ),
        stats=stats,
        source="cash.liquidation (deterministic)",
        as_of=stat_day,
    )
    candidate = Recommendation(
        action_label=action_label,
        reasoning=reasoning,
        evidence=(evidence.id,),
        uncertainties=(
            "A money-market price can shift slightly before the sell settles, so "
            "the exact freed-up amount is an estimate until it fills.",
        ),
        order_intent=order_intent,
    )
    # Route through the SOLE producer of a blessed recommendation (NFR2) — the
    # evidence record is supplied in the retrieved set so the cited id resolves.
    return validate_recommendation(candidate, (evidence,))


def _plan_response(
    plan: LiquidationPlan,
    *,
    pending_buy_id: str | None,
    sell_decision_id: str | None,
) -> LiquidationPlanResponse:
    return LiquidationPlanResponse(
        needs_liquidation=plan.needs_liquidation,
        coverable=plan.coverable,
        ready_to_trade=plan.ready_to_trade,
        shortfall=plan.shortfall,
        sell_symbol=plan.sell_symbol,
        sell_amount=plan.sell_amount,
        est_shares=plan.est_shares,
        sell_order_intent=(
            None
            if plan.sell_order_intent is None
            else _order_intent_field(plan.sell_order_intent)
        ),
        reserved=plan.reserved,
        reserve_decided=plan.reserve_decided,
        as_of=plan.as_of,
        reasoning=plan.reasoning,
        pending_buy_id=pending_buy_id,
        sell_decision_id=sell_decision_id,
    )


#: ``Numeric(20, 2)`` upper bound — mirrors ``cash.config._RESERVE_MAX`` so the
#: buy amount can't overflow the column (raw 500) or corrupt the wire figure.
_BUY_AMOUNT_MAX = Decimal("10") ** 18


def _validate_buy_amount(amount: Decimal) -> Decimal:
    """Return a valid, 2dp buy amount or raise ``ValueError`` (→ calm 422).

    A money field on the money path must be fail-CLOSED, exactly like the 9-1
    reserve (:func:`cash.config._validate_reserve`): finite, positive, within
    ``Numeric(20, 2)`` range, and at most two decimal places (silent rounding is
    dishonest). Quantizing to 2dp here also keeps the pending-buy dedupe key stable
    (an unquantized re-visit can't mint a second durable pending buy).
    """
    if amount is None or not amount.is_finite():
        raise ValueError("The buy amount must be a real number.")
    if amount <= 0:
        raise ValueError("The buy amount must be greater than zero.")
    if amount >= _BUY_AMOUNT_MAX:
        raise ValueError("That buy amount is too large.")
    if amount.as_tuple().exponent < -2:
        raise ValueError("The buy amount can have at most two decimal places.")
    return amount.quantize(Decimal("0.01"))


async def _find_awaiting_pending_buy(
    scope: Scope,
    session: AsyncSession,
    *,
    symbol: str,
    amount: Decimal,
) -> PendingBuy | None:
    """Find an existing ``awaiting_funds`` pending buy for ``(symbol, amount)`` — scoped.

    The dedupe key for the plan endpoint (a re-visit of the same buy step must NOT
    mint a second durable pending buy). Reads ONLY the caller's rows (AD-10), matches
    the hoisted ``amount`` and the buy_intent's normalized symbol.
    """
    repo = ScopedRepository(PendingBuy, scope, session)
    rows = await repo.list()
    target = (symbol or "").strip().upper()
    for row in rows:
        if row.status != "awaiting_funds":
            continue
        row_symbol = str((row.buy_intent or {}).get("symbol", "")).strip().upper()
        if row_symbol == target and row.amount == amount:
            return row
    return None


@router.post("/liquidation-plan", response_model=LiquidationPlanResponse)
async def liquidation_plan(
    body: LiquidationPlanRequest,
    scope: Scope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> LiquidationPlanResponse:
    """PLAN a just-in-time liquidation for a decided buy's shortfall (Story 9.3).

    Deterministically computes (no live quote) whether the buy exceeds ready-to-trade
    cash and, if so, a pre-filled money-market SELL of the shortfall drawn ONLY from
    parked funds above the protected reserve (:func:`cash.liquidation.plan_liquidation`).
    When ``needs_liquidation``, records a DURABLE :class:`~db.models.PendingBuy`
    (idempotently — dedupes an existing ``awaiting_funds`` pending buy for the same
    ``(symbol, amount)``) so a missed notification can't lose the intent, and —
    whenever there is a sell to make (coverable OR partial) — a proposed SELL decision
    via the sole writer
    (:func:`coach.decision_record.record_proposal`). PLACES NOTHING: the human
    submits the sell via ``/api/coach/approve``. 401 unauth; money as fixed-point
    strings.
    """
    try:
        amount = _validate_buy_amount(body.amount)
        buy_intent = OrderIntent(
            symbol=(body.symbol or "").strip().upper(),
            side=OrderSide.BUY,
            amount=amount,
        )
        plan = await plan_liquidation(
            scope,
            session,
            buy_symbol=body.symbol,
            buy_amount=amount,
            buy_intent=buy_intent,
            gateway=get_llm_gateway(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if not plan.needs_liquidation:
        # The buy is covered — nothing to liquidate, no durable pending buy.
        return _plan_response(plan, pending_buy_id=None, sell_decision_id=None)

    # Durable pending buy (idempotent dedupe on the same (symbol, amount)).
    existing = await _find_awaiting_pending_buy(
        scope, session, symbol=buy_intent.symbol, amount=amount
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    sell_decision_id: str | None = None

    if existing is not None:
        pending_buy_id = str(existing.id)
        # Re-surface the previously-linked sell decision if there is one.
        sell_decision_id = (
            None if existing.sell_decision_id is None else str(existing.sell_decision_id)
        )
    else:
        # Mint the proposed SELL decision first (whenever there is a sell to make —
        # a PARTIAL sell is submittable too, so the user frees up what they can now
        # and the rest resumes when more cash settles; never a dead-end) so the
        # pending buy can link it, then persist the pending buy — one commit for both.
        if plan.sell_order_intent is not None:
            blessed = _liquidation_blessed(
                action_label=f"Sell {plan.sell_symbol} to free up cash",
                reasoning=plan.reasoning,
                order_intent=plan.sell_order_intent,
                as_of=plan.as_of,
            )
            sell_record = await record_proposal(
                blessed, scope=scope, session=session
            )
            sell_decision_id = str(sell_record.id)

        repo = ScopedRepository(PendingBuy, scope, session)
        pending = await repo.add(
            buy_intent={
                "symbol": buy_intent.symbol,
                "side": buy_intent.side.value,
                "amount": format_money(buy_intent.amount),
            },
            amount=amount,
            status="awaiting_funds",
            sell_decision_id=(
                None if sell_decision_id is None else UUID(sell_decision_id)
            ),
            created_at=now,
        )
        await session.commit()
        pending_buy_id = str(pending.id)

    return _plan_response(
        plan, pending_buy_id=pending_buy_id, sell_decision_id=sell_decision_id
    )


def _pending_buy_out(
    pending: PendingBuy, *, ready_to_trade: Decimal, as_of
) -> PendingBuyOut:
    intent = _order_intent_from_snapshot(pending.buy_intent)
    return PendingBuyOut(
        pending_buy_id=str(pending.id),
        buy_intent=_order_intent_field(intent),
        amount=pending.amount,
        status=pending.status,
        # Live funds_ready — never a fabricated timer (Design Note).
        funds_ready=ready_to_trade >= pending.amount,
        ready_to_trade=ready_to_trade,
        sell_decision_id=(
            None if pending.sell_decision_id is None else str(pending.sell_decision_id)
        ),
        created_at=pending.created_at,
        as_of=as_of,
    )


@router.get("/pending-buys", response_model=PendingBuyListResponse)
async def list_pending_buys(
    scope: Scope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> PendingBuyListResponse:
    """LIST the caller's ``awaiting_funds`` pending buys with a live ``funds_ready``.

    Pull-only (surfaces on visit, never pushed). Reads the user's rows through the
    fail-closed scoped repo (AD-10) and computes ``funds_ready`` live from the
    authoritative ``portfolio_balance`` ready-to-trade cash — never a fabricated
    settlement timer. Money as fixed-point strings; ``as_of`` surfaces freshness.
    401 unauth.
    """
    view = await get_portfolio(scope, session)
    repo = ScopedRepository(PendingBuy, scope, session)
    rows = await repo.list()
    awaiting = [r for r in rows if r.status == "awaiting_funds"]
    awaiting.sort(key=lambda r: r.created_at, reverse=True)
    return PendingBuyListResponse(
        pending_buys=[
            _pending_buy_out(r, ready_to_trade=view.cash, as_of=view.as_of)
            for r in awaiting
        ]
    )


@router.post("/pending-buys/{pending_buy_id}/resume", response_model=ResumeResponse)
async def resume_pending_buy(
    pending_buy_id: UUID,
    scope: Scope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> ResumeResponse:
    """RESUME a pending buy once ready-to-trade cash covers it (Story 9.3).

    Loads the caller's own pending buy (a foreign/unknown id is invisible → 404).
    Guards ``funds_ready`` LIVE (``ready_to_trade >= amount``); if the money hasn't
    settled yet it is calmly refused (409). On a ready buy it mints a proposed BUY
    decision from the stored ``buy_intent`` via the sole writer
    (:func:`coach.decision_record.record_proposal`), transitions the pending buy
    ``awaiting_funds → resumed``, and returns the ``decision_id`` + pre-filled buy so
    the user submits it through the unchanged ``/api/coach/approve`` co-sign flow.
    PLACES NOTHING. 401 unauth; money as fixed-point strings.
    """
    repo = ScopedRepository(PendingBuy, scope, session)
    pending = await repo.get(pending_buy_id)
    if pending is None:
        raise HTTPException(status_code=404, detail="Pending buy not found.")
    if pending.status != "awaiting_funds":
        # Already resumed or cancelled — nothing to resume.
        raise HTTPException(
            status_code=409,
            detail="This pending buy is no longer waiting to be resumed.",
        )

    view = await get_portfolio(scope, session)
    if view.cash < pending.amount:
        # Honest, calm refusal — the money genuinely hasn't settled yet.
        raise HTTPException(
            status_code=409,
            detail=(
                "Your cash hasn't settled yet — this will be ready to resume once "
                "there's enough ready-to-trade cash to cover it."
            ),
        )

    intent = _order_intent_from_snapshot(pending.buy_intent)
    blessed = _liquidation_blessed(
        action_label=f"Buy {intent.symbol} (resumed)",
        reasoning=(
            "Your cash has settled, so the buy you set aside earlier is ready to "
            "go. Nothing is placed until you review and submit it yourself."
        ),
        order_intent=intent,
        as_of=view.as_of,
    )
    record = await record_proposal(blessed, scope=scope, session=session)
    pending.status = "resumed"
    pending.resumed_at = datetime.datetime.now(datetime.timezone.utc)
    await session.commit()

    return ResumeResponse(
        decision_id=str(record.id),
        order_intent=_order_intent_field(intent),
        pending_buy_id=str(pending.id),
    )


@router.post(
    "/pending-buys/{pending_buy_id}/cancel", response_model=CancelPendingBuyResponse
)
async def cancel_pending_buy(
    pending_buy_id: UUID,
    scope: Scope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> CancelPendingBuyResponse:
    """CANCEL a pending buy (Story 9.3).

    Loads the caller's own pending buy (foreign/unknown id → 404) and transitions it
    ``awaiting_funds → cancelled``. Idempotent-friendly: an already-cancelled row is
    returned as-is; an already-resumed row can no longer be cancelled (409). 401
    unauth.
    """
    repo = ScopedRepository(PendingBuy, scope, session)
    pending = await repo.get(pending_buy_id)
    if pending is None:
        raise HTTPException(status_code=404, detail="Pending buy not found.")
    if pending.status == "cancelled":
        return CancelPendingBuyResponse(
            pending_buy_id=str(pending.id), status=pending.status
        )
    if pending.status != "awaiting_funds":
        raise HTTPException(
            status_code=409,
            detail="This pending buy has already been resumed and can't be cancelled.",
        )
    pending.status = "cancelled"
    pending.cancelled_at = datetime.datetime.now(datetime.timezone.utc)
    await session.commit()
    return CancelPendingBuyResponse(
        pending_buy_id=str(pending.id), status=pending.status
    )
