"""The Coach's propose-and-approve HTTP surface (Story 4.6 — FR8/FR9/FR10, AD-7/AD-11).

Two endpoints, mounted at ``/api/coach``:

- ``POST /recommend`` — the PROPOSE half. Runs the existing coach pipeline over
  the user's portfolio and returns the blessed recommendation (reasoning,
  evidence, uncertainties, optional ``order_intent``). It NEVER places an order
  and works in DEGRADED mode: it depends on :func:`~api.deps.get_scope` alone and
  does NOT gate on a live brokerage session (AD-11).
- ``POST /approve`` — the APPROVE (execute) half, the explicit-approval gate
  (FR8/FR9). On a request carrying an ``order_intent`` it requires a LIVE
  brokerage session (:func:`~api.deps.require_live_broker_session` → calm 409 on
  a non-live session) and delegates to the Coach Engine execution owner
  (:func:`coach.execution.execute_approved_order`) — the SOLE caller of the
  Broker Port. This handler NEVER calls the broker directly (AD-7).

Reconciliation of the order outcome (Story 4.7) is owned by the Coach Engine
execution owner, not this handler: ``/approve`` returns whatever true state
:func:`~coach.execution.execute_approved_order` reconciled. Money crosses the
wire as decimal STRINGS (never binary float), consistent with
``RECOMMENDATION_OUTPUT_SCHEMA``. This module adds NO persistence (decision
records are Story 4.9) and NO UI.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import RECONNECT_MESSAGE, get_scope, require_live_broker_session
from brokers.factory import get_broker
from brokers.port import BrokerPort, OrderOutcome
from brokers.portfolio import get_portfolio
from brokers.session import BrokerageSession
from coach.execution import (
    OrderScopeError,
    SessionIntegrityError,
    execute_approved_order,
)
from coach.pipeline import CoachDecision, run_coach_pipeline
from coach.recommendation import OrderIntent, OrderSide
from coach.validation import BlessedRecommendation
from db.scope import Scope
from db.session import get_async_session

logger = logging.getLogger("ballast.api.coach")

router = APIRouter(prefix="/api/coach", tags=["coach"])


# --- Schemas -----------------------------------------------------------------


class OrderIntentIn(BaseModel):
    """The typed executable payload the user approves.

    ``amount`` is accepted as a Decimal (Pydantic parses a JSON string/number to
    ``Decimal``) and stays ``Decimal`` end to end — never binary float.
    """

    symbol: str
    side: OrderSide
    amount: Decimal


class OrderIntentOut(BaseModel):
    """A serialized ``order_intent`` — amount as a decimal STRING on the wire."""

    symbol: str
    side: OrderSide
    amount: str


class EvidenceOut(BaseModel):
    """A serialized :class:`~precedent.evidence.EvidenceRecord` (AD-12 shape).

    Uses the record's own JSON-safe ``to_dict()`` (Decimal→str, date→ISO), so
    the six-field evidence contract is preserved verbatim for a later UI.
    """

    id: str
    kind: str
    statement: str
    stats: dict
    source: str
    as_of: str


class RecommendRequest(BaseModel):
    """The user-initiated decision (pull-not-push) seeding the pipeline.

    Mirrors :class:`~coach.pipeline.CoachDecision`'s user-facing fields. ``amount``
    is ``Decimal`` (never float); ``side`` is optional and, when present, feeds
    the FR11 self-destructive-move detector.
    """

    symbol: str | None = None
    question: str = ""
    amount: Decimal | None = None
    side: OrderSide | None = None


class RecommendResponse(BaseModel):
    """The blessed recommendation surfaced to the user (never an execution)."""

    action_label: str
    reasoning: str
    evidence: list[EvidenceOut]
    uncertainties: list[str]
    order_intent: OrderIntentOut | None = None


class ApproveRequest(BaseModel):
    """The explicit approval carrying the ``order_intent`` to execute (FR8/FR9)."""

    order_intent: OrderIntentIn


class ApproveResponse(BaseModel):
    """The reconciled order :class:`~brokers.port.OrderOutcome` (the true state,
    Story 4.7), money as strings. Any of the five statuses
    (``filled``/``partial``/``rejected``/``timeout``/``pending``) is surfaced
    honestly — a non-``filled`` outcome is truthful data, never an app error."""

    status: str
    filled_qty: str
    avg_price: str | None = None
    broker_ref: str | None = None


# --- Serialization helpers ---------------------------------------------------


def _order_intent_out(intent: OrderIntent | None) -> OrderIntentOut | None:
    if intent is None:
        return None
    return OrderIntentOut(
        symbol=intent.symbol,
        side=intent.side,
        amount=str(intent.amount),
    )


def _to_recommend_response(blessed: BlessedRecommendation) -> RecommendResponse:
    return RecommendResponse(
        action_label=blessed.action_label,
        reasoning=blessed.reasoning,
        evidence=[EvidenceOut(**record.to_dict()) for record in blessed.evidence],
        uncertainties=list(blessed.uncertainties),
        order_intent=_order_intent_out(blessed.order_intent),
    )


def _to_approve_response(outcome: OrderOutcome) -> ApproveResponse:
    return ApproveResponse(
        status=outcome.status.value,
        filled_qty=str(outcome.filled_qty),
        avg_price=None if outcome.avg_price is None else str(outcome.avg_price),
        broker_ref=outcome.broker_ref,
    )


# --- Endpoints ---------------------------------------------------------------


@router.post("/recommend", response_model=RecommendResponse)
async def recommend(
    body: RecommendRequest,
    scope: Scope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> RecommendResponse:
    """PROPOSE: run the pipeline over the user's portfolio; NEVER place an order.

    Degraded-mode safe (AD-11): depends on :func:`get_scope` alone, so it works
    while the brokerage session is expired. Fetches the user's ``PortfolioView``
    so FR11 warnings surface in ``reasoning``, runs
    :func:`~coach.pipeline.run_coach_pipeline` (never a dead-end — always returns
    a blessed recommendation), and serializes it (Decimal→string). No broker
    call is made or possible from this path.
    """
    portfolio = await get_portfolio(scope, session)
    # A blank/whitespace-only symbol falls back to CoachDecision's own default
    # benchmark (rather than the fragile ``or`` idiom, which passed a
    # whitespace-only string straight through to the pipeline).
    raw_symbol = (body.symbol or "").strip()
    decision_kwargs: dict = {
        "question": body.question,
        "amount": body.amount,
        "side": None if body.side is None else body.side.value,
    }
    if raw_symbol:
        decision_kwargs["symbol"] = raw_symbol
    decision = CoachDecision(**decision_kwargs)
    blessed = await run_coach_pipeline(session, decision, portfolio=portfolio)
    return _to_recommend_response(blessed)


@router.post("/approve", response_model=ApproveResponse)
async def approve(
    body: ApproveRequest,
    scope: Scope = Depends(get_scope),
    broker_session: BrokerageSession = Depends(require_live_broker_session),
    broker: BrokerPort = Depends(get_broker),
    session: AsyncSession = Depends(get_async_session),
) -> ApproveResponse:
    """APPROVE (execute): the explicit-approval, live-session execution gate.

    Only reached on an authenticated user AND a live brokerage session
    (:func:`require_live_broker_session` raises the calm 409 reconnect response
    otherwise — no order attempted, AD-11). Delegates to the Coach Engine
    execution owner (the SOLE caller of the Broker Port); this handler never
    calls ``broker.place_order`` directly (AD-7). The execution owner re-asserts
    placement-time integrity (Story 4.8): the session must still be live AND its
    ``provider`` must match the placing adapter, else it raises
    :class:`SessionIntegrityError`, mapped HERE to the same calm 409
    ``RECONNECT_MESSAGE`` as the entry gate — the broker is never touched. An
    out-of-v1-scope intent raises :class:`OrderScopeError`, mapped to a 422
    through the app error envelope BEFORE any broker call. Returns the reconciled
    order outcome (the
    true state, Story 4.7), money as decimal strings. Any of the five resolved
    statuses (``filled``/``partial``/``rejected``/``timeout``/``pending``) is
    returned at HTTP 200 with the honest body — a broker ``rejected``/``pending``
    is truthful data, NOT coerced into the error envelope and never a phantom
    fill. Only scope/auth/session failures use the error envelope.
    """
    intent = OrderIntent(
        symbol=body.order_intent.symbol,
        side=body.order_intent.side,
        amount=body.order_intent.amount,
    )
    try:
        outcome = await execute_approved_order(
            intent, broker=broker, broker_session=broker_session
        )
    except SessionIntegrityError as exc:
        # Session lapsed or provider mismatched at placement time; refuse with the
        # same calm reconnect envelope as the entry gate — broker never touched.
        raise HTTPException(
            status_code=409, detail=RECONNECT_MESSAGE
        ) from exc
    except OrderScopeError as exc:
        # Rejected before any broker call; surface through the app envelope.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _to_approve_response(outcome)
