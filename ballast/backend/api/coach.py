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
``RECOMMENDATION_OUTPUT_SCHEMA``.

PERSISTENCE lands HERE as of Story 4.9 (FR16, AD-5/AD-6): ``/recommend`` persists
the blessed recommendation as ONE immutable **proposed**
:class:`~db.models.DecisionRecord` and returns its ``decision_id``; ``/approve``
carries that ``decision_id`` and, on a successful execution, CO-SIGNS the
referenced record exactly once (proposed→cosigned). Both delegate to the Coach
Engine's SOLE decision-record writer (:mod:`coach.decision_record`) — this
handler never constructs or writes the model itself (AD-6). Re-approving an
already-cosigned decision returns the RECORDED outcome and never re-touches the
broker — a SEQUENTIAL re-approve is idempotent via the persisted ``status``.
Hardening against simultaneous IN-FLIGHT approves of the same ``decision_id``
(an atomic proposed→cosigned status claim plus a stable per-decision idempotency
key reused across placements) lands with real-broker wiring; v1 has no live
broker (the Schwab adapter is a credential-gated stub) so no real order can be
double-placed today. No replay/history endpoint and no UI (Story 4.10).
"""

from __future__ import annotations

import logging
from decimal import Decimal

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import RECONNECT_MESSAGE, get_scope, require_live_broker_session
from brokers.factory import get_broker
from brokers.port import BrokerPort, OrderOutcome
from brokers.portfolio import get_portfolio
from brokers.session import BrokerageSession
from coach.decision_record import (
    cosign,
    list_cosigned_decisions,
    load_decision,
    record_proposal,
)
from coach.execution import (
    OrderScopeError,
    SessionIntegrityError,
    execute_approved_order,
    mint_idempotency_key,
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
    """The blessed recommendation surfaced to the user (never an execution).

    Carries the ``decision_id`` of the immutable **proposed**
    :class:`~db.models.DecisionRecord` persisted for this recommendation (Story
    4.9) — the client echoes it back on ``/approve`` so the co-sign attaches to
    the EXACT recommendation the user saw.
    """

    decision_id: str
    action_label: str
    reasoning: str
    evidence: list[EvidenceOut]
    uncertainties: list[str]
    order_intent: OrderIntentOut | None = None


class ApproveRequest(BaseModel):
    """The explicit approval carrying the ``order_intent`` to execute (FR8/FR9).

    Also carries the ``decision_id`` (Story 4.9) of the proposed decision record
    the user is approving, so the co-sign attaches to the exact recommendation
    they saw at ``/recommend`` time (not a re-derived one).
    """

    decision_id: UUID
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


class DecisionSummaryOut(BaseModel):
    """One co-signed decision in the history list (Story 4.10, read-only).

    A compact summary for the Decisions surface: enough to label and order the
    row without shipping the full snapshot. ``symbol`` comes from the EXECUTED
    ``cosign_snapshot.order_intent`` (the offline seam: the proposed order_intent
    can be ``None`` on the fake default plan, but a cosigned row always carries
    an executed intent). ``co_signed_at`` is an ISO-8601 UTC string.
    """

    decision_id: str
    action_label: str
    symbol: str | None = None
    co_signed_at: str
    outcome_status: str


class DecisionListResponse(BaseModel):
    """The user's co-signed decisions, newest-first (Story 4.10)."""

    decisions: list[DecisionSummaryOut]


class DecisionDetailResponse(BaseModel):
    """The verbatim replay payload for one co-signed decision (Story 4.10).

    The persisted ``recommendation_snapshot`` and ``cosign_snapshot`` are passed
    through as stored JSON (``dict`` passthrough) — NEVER recomputed, the pipeline
    is NEVER re-run, and precedent is NEVER re-hydrated (AD-5). Timestamps are
    serialized via ``.isoformat()``.
    """

    decision_id: str
    schema_version: int
    status: str
    created_at: str
    co_signed_at: str | None = None
    recommendation_snapshot: dict
    cosign_snapshot: dict | None = None


# --- Serialization helpers ---------------------------------------------------


def _order_intent_out(intent: OrderIntent | None) -> OrderIntentOut | None:
    if intent is None:
        return None
    return OrderIntentOut(
        symbol=intent.symbol,
        side=intent.side,
        amount=str(intent.amount),
    )


def _to_recommend_response(
    blessed: BlessedRecommendation, *, decision_id: str
) -> RecommendResponse:
    return RecommendResponse(
        decision_id=decision_id,
        action_label=blessed.action_label,
        reasoning=blessed.reasoning,
        evidence=[EvidenceOut(**record.to_dict()) for record in blessed.evidence],
        uncertainties=list(blessed.uncertainties),
        order_intent=_order_intent_out(blessed.order_intent),
    )


def _money_str(value: Decimal) -> str:
    """Serialize wire money as fixed-point (never binary float, never ``E+``).

    ``format(Decimal, "f")`` matches the persisted co-sign snapshot's
    :func:`coach.decision_record._money`, so the live ``/approve`` outcome and its
    idempotent replay (rebuilt from the snapshot) return byte-identical money
    strings — and an extreme amount never surfaces as e.g. ``"1E+27"`` on the wire
    (the money-format gap the Story 4.7 ledger flagged, closed for this path).
    """
    return format(value, "f")


def _to_approve_response(outcome: OrderOutcome) -> ApproveResponse:
    return ApproveResponse(
        status=outcome.status.value,
        filled_qty=_money_str(outcome.filled_qty),
        avg_price=None if outcome.avg_price is None else _money_str(outcome.avg_price),
        broker_ref=outcome.broker_ref,
    )


def _decision_summary_out(record) -> DecisionSummaryOut:
    """Map a cosigned record to its history summary (verbatim snapshot reads).

    ``action_label`` comes from the immutable ``recommendation_snapshot``; the
    ``symbol`` and ``outcome_status`` come from the co-sign snapshot's executed
    intent + reconciled outcome. Nothing is recomputed.
    """
    snapshot = record.recommendation_snapshot or {}
    cosign_snapshot = record.cosign_snapshot or {}
    order_intent = cosign_snapshot.get("order_intent") or {}
    outcome = cosign_snapshot.get("outcome") or {}
    return DecisionSummaryOut(
        decision_id=str(record.id),
        action_label=snapshot.get("action_label", ""),
        symbol=order_intent.get("symbol"),
        co_signed_at=record.co_signed_at.isoformat(),
        outcome_status=outcome.get("status", ""),
    )


def _recorded_outcome_response(record) -> ApproveResponse:
    """Rebuild the recorded :class:`ApproveResponse` from a cosigned record.

    Used on an idempotent RE-approve: the reconciled outcome persisted in
    ``cosign_snapshot`` is returned verbatim WITHOUT re-touching the broker
    (Story 4.9). The snapshot money is already a fixed-point decimal string.
    """
    outcome = record.cosign_snapshot["outcome"]
    return ApproveResponse(
        status=outcome["status"],
        filled_qty=outcome["filled_qty"],
        avg_price=outcome["avg_price"],
        broker_ref=outcome["broker_ref"],
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

    Persistence (Story 4.9): the blessed recommendation is written as ONE
    immutable **proposed** :class:`~db.models.DecisionRecord` via the Coach
    Engine's sole writer (:func:`coach.decision_record.record_proposal`, owner
    stamped from ``scope``), and its ``decision_id`` is returned so the client can
    echo it on ``/approve``. This handler never constructs the model itself (AD-6).
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
    # Persist the blessed recommendation as an immutable proposed record and
    # return its decision_id (delegated to the sole writer — AD-6).
    record = await record_proposal(blessed, scope=scope, session=session)
    await session.commit()
    return _to_recommend_response(blessed, decision_id=str(record.id))


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

    CO-SIGN (Story 4.9): the request carries a ``decision_id``. The referenced
    proposed record is loaded through the sole writer
    (:func:`coach.decision_record.load_decision`, per-user scoped — a foreign or
    unknown id is invisible → 404). If it is ALREADY cosigned, the RECORDED
    outcome is returned and the broker is NEVER re-invoked (idempotent re-approve;
    no double-place across requests). Otherwise the idempotency key is minted
    HERE and passed through to :func:`execute_approved_order`; on success the
    referenced record is co-signed EXACTLY once (proposed→cosigned) via
    :func:`coach.decision_record.cosign` and the session committed. The 409
    (session integrity) and 422 (scope) refusal arms occur BEFORE co-sign, so a
    refusal leaves the record **proposed** and writes no co-sign.

    The already-cosigned early-return makes a SEQUENTIAL re-approve idempotent;
    a fully atomic guard against two simultaneous in-flight approves of the same
    ``decision_id`` is deferred to real-broker wiring (see the module docstring).
    Money in the returned outcome is serialized fixed-point (no ``E+``), matching
    the persisted co-sign snapshot so a first approve and its idempotent replay
    return byte-identical money strings.
    """
    record = await load_decision(body.decision_id, scope=scope, session=session)
    if record is None:
        # Unknown or foreign decision_id → invisible under this user's scope.
        raise HTTPException(status_code=404, detail="Decision record not found.")

    # Idempotent re-approve: an already-cosigned decision returns the RECORDED
    # outcome and never re-touches the broker (cross-request no-double-place).
    if record.status == "cosigned":
        return _recorded_outcome_response(record)

    intent = OrderIntent(
        symbol=body.order_intent.symbol,
        side=body.order_intent.side,
        amount=body.order_intent.amount,
    )
    try:
        key = mint_idempotency_key()
        outcome = await execute_approved_order(
            intent,
            broker=broker,
            broker_session=broker_session,
            idempotency_key=key,
        )
    except SessionIntegrityError as exc:
        # Session lapsed or provider mismatched at placement time; refuse with the
        # same calm reconnect envelope as the entry gate — broker never touched,
        # record stays proposed (no co-sign written).
        raise HTTPException(
            status_code=409, detail=RECONNECT_MESSAGE
        ) from exc
    except OrderScopeError as exc:
        # Rejected before any broker call; surface through the app envelope. The
        # record stays proposed (no co-sign written).
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Order was actually placed and reconciled: co-sign the referenced record
    # exactly once (delegated to the sole writer — AD-6).
    cosign(record, order_intent=intent, outcome=outcome, idempotency_key=key)
    await session.commit()
    return _to_approve_response(outcome)


@router.get("/decisions", response_model=DecisionListResponse)
async def list_decisions(
    scope: Scope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> DecisionListResponse:
    """LIST the caller's co-signed decisions, newest-first (Story 4.10, FR16).

    Read-only history over the immutable record: delegates to the Coach Engine's
    sole reader (:func:`coach.decision_record.list_cosigned_decisions`), which
    returns this user's ``cosigned`` rows ordered by ``co_signed_at`` desc through
    the fail-closed :class:`~db.repository.ScopedRepository` (a foreign row is
    never visible; ``proposed`` rows are excluded). This handler never queries the
    model itself (AD-6) and NEVER re-runs the pipeline or recomputes anything
    (AD-5). No broker/live-session dependency — history reads work in degraded mode.
    """
    records = await list_cosigned_decisions(scope=scope, session=session)
    return DecisionListResponse(
        decisions=[_decision_summary_out(record) for record in records]
    )


@router.get("/decisions/{decision_id}", response_model=DecisionDetailResponse)
async def get_decision(
    decision_id: UUID,
    scope: Scope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> DecisionDetailResponse:
    """REPLAY one co-signed decision VERBATIM (Story 4.10, FR16, AD-5).

    Loads the owned record through the sole reader
    (:func:`coach.decision_record.load_decision`, per-user scoped — an unknown or
    foreign id is invisible → ``None`` → 404). Returns the stored
    ``recommendation_snapshot`` (action_label, reasoning, full evidence records,
    uncertainties, proposed order_intent) and ``cosign_snapshot`` (executed
    order_intent + reconciled outcome) EXACTLY as 4.9 persisted them (``dict``
    passthrough), plus ``schema_version``/``status`` and the ISO-8601 timestamps.
    Nothing is recomputed, the pipeline is not re-run, precedent is not
    re-hydrated, and the record is not mutated. No broker/live-session dependency.
    """
    record = await load_decision(decision_id, scope=scope, session=session)
    if record is None:
        # Unknown or foreign decision_id → invisible under this user's scope.
        raise HTTPException(status_code=404, detail="Decision record not found.")
    return DecisionDetailResponse(
        decision_id=str(record.id),
        schema_version=record.schema_version,
        status=record.status,
        created_at=record.created_at.isoformat(),
        co_signed_at=(
            None if record.co_signed_at is None else record.co_signed_at.isoformat()
        ),
        recommendation_snapshot=record.recommendation_snapshot,
        cosign_snapshot=record.cosign_snapshot,
    )
