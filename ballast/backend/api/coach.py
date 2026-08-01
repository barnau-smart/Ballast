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
:class:`~db.models.DecisionRecord` (stamped with a STABLE per-decision idempotency
key at propose time) and returns its ``decision_id``; ``/approve`` carries that
``decision_id`` and, on a successful execution, CO-SIGNS the referenced record
exactly once. Both delegate to the Coach Engine's SOLE decision-record writer
(:mod:`coach.decision_record`) — this handler never constructs or writes the
model itself (AD-6).

ATOMIC CLAIM (Story 6.1): ``/approve`` closes the in-flight concurrent-approve
window with a real atomic claim, not merely a sequential guard. It loads the
referenced record, then: an already-``cosigned`` record returns the RECORDED
outcome (broker never re-invoked); a record already ``cosigning`` returns a calm
409 in-progress; otherwise it ATOMICALLY claims ``proposed → cosigning`` via
:func:`coach.decision_record.claim_for_cosign` (a rowcount-gated conditional
UPDATE committed BEFORE the broker call). Of two simultaneous in-flight approves
EXACTLY ONE wins the claim and places the order (with the STABLE persisted key);
the loser re-loads and gets the recorded outcome or a 409 in-progress — no double
placement is possible. A session-integrity (409) or scope (422) refusal RELEASES
the claim (``cosigning → proposed``) so the decision returns to ``proposed`` and
stays retryable; the broker is never touched. No replay/history endpoint and no
UI (Story 4.10).
"""

from __future__ import annotations

import logging
from decimal import Decimal

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import get_settings
from api.deps import RECONNECT_MESSAGE, get_scope, require_live_broker_session
from brokers.factory import get_execution_broker
from brokers.port import BrokerPort, OrderNotPlaceableError, OrderOutcome
from brokers.portfolio import get_portfolio
from brokers.session import BrokerageSession
from coach.decision_record import (
    claim_for_cosign,
    cosign,
    list_cosigned_decisions,
    load_decision,
    record_proposal,
    release_claim,
)
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
from money import format_money

logger = logging.getLogger("ballast.api.coach")

router = APIRouter(prefix="/api/coach", tags=["coach"])

#: Decisions-history pagination bounds (Story 6.6), resolved from settings ONCE
#: at import because a FastAPI ``Query`` default must be a concrete value at
#: function-definition time. These are process-level configuration (page size +
#: hard cap), not per-request inputs, so freezing them at import is correct; a
#: change requires a restart, like the other env-driven bounds.
_DECISION_SETTINGS = get_settings()
DECISION_PAGE_SIZE = _DECISION_SETTINGS.DECISION_PAGE_SIZE
DECISION_MAX_PAGE_SIZE = _DECISION_SETTINGS.DECISION_MAX_PAGE_SIZE

#: The calm 409 surfaced when an approve of THIS decision is already in flight
#: (another request won the atomic proposed→cosigning claim and is placing now).
#: Not an error the user caused — a "give it a moment" nudge (Story 6.1).
IN_PROGRESS_MESSAGE = (
    "This decision is being approved right now — give it a moment and check "
    "your Decisions."
)


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
    """The user's co-signed decisions, newest-first, paginated (Story 4.10/6.6).

    The ``decisions`` array is one bounded page (at most ``limit`` rows). The
    additive ``has_more``/``limit``/``offset`` fields (Story 6.6) let a client
    page without guessing; ``decisions`` stays intact so any pre-6.6 consumer
    keeps working.
    """

    decisions: list[DecisionSummaryOut]
    has_more: bool
    limit: int
    offset: int


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
        amount=format_money(intent.amount),
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
    Delegates to the shared :func:`money.format_money` single serializer.
    """
    return format_money(value)


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
    broker: BrokerPort = Depends(get_execution_broker),
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

    CO-SIGN + ATOMIC CLAIM (Story 4.9 / 6.1): the request carries a
    ``decision_id``. The referenced record is loaded through the sole writer
    (:func:`coach.decision_record.load_decision`, per-user scoped — a foreign or
    unknown id is invisible → 404). If it is ALREADY ``cosigned``, the RECORDED
    outcome is returned and the broker is NEVER re-invoked (idempotent re-approve;
    no double-place across requests). If it is ``cosigning``, another approve of
    this decision is in flight → a calm 409 in-progress. Otherwise this request
    ATOMICALLY claims ``proposed → cosigning`` via
    :func:`coach.decision_record.claim_for_cosign`; if it LOSES the claim
    (``rowcount == 0``, a concurrent approve won) it re-loads and branches
    (cosigned→recorded / cosigning→409 / missing→404) WITHOUT touching the broker.
    On a WON claim it places with the STABLE key persisted at propose
    (``record.idempotency_key``) via :func:`execute_approved_order`; on success it
    co-signs EXACTLY once (cosigning→cosigned) and commits. A session-integrity
    (409) or scope (422) refusal RELEASES the claim (cosigning→proposed) and
    commits that release BEFORE raising, so a refused decision returns to
    ``proposed`` and stays retryable — the broker was never touched.

    Because exactly one concurrent approve wins the atomic claim, ``place_order``
    runs at most once per decision even for two simultaneous in-flight approves —
    the in-flight double-place window is structurally closed (no longer deferred).
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
    # Another approve of this decision is already in flight (won the claim, is
    # placing now) → calm 409 in-progress, broker never touched.
    if record.status == "cosigning":
        raise HTTPException(status_code=409, detail=IN_PROGRESS_MESSAGE)

    # Atomically claim proposed→cosigning; only the winner (rowcount==1) places.
    won = await claim_for_cosign(body.decision_id, scope=scope, session=session)
    if not won:
        # A concurrent approve won the claim. Re-load to return the right calm
        # answer without ever touching the broker.
        record = await load_decision(body.decision_id, scope=scope, session=session)
        if record is None:
            raise HTTPException(status_code=404, detail="Decision record not found.")
        if record.status == "cosigned":
            return _recorded_outcome_response(record)
        # Still cosigning (the winner is placing) → calm 409 in-progress.
        raise HTTPException(status_code=409, detail=IN_PROGRESS_MESSAGE)

    # We won the claim: re-load so the ORM instance reflects the committed
    # ``cosigning`` status (the claim was a Core UPDATE bypassing this instance,
    # and expire_on_commit is off), then place with the STABLE per-decision key
    # persisted at propose.
    record = await load_decision(body.decision_id, scope=scope, session=session)
    if record is None:
        # The claimed row vanished (concurrent delete) between the committed
        # claim and this re-load → calm 404, symmetric with the loser branch
        # (never an AttributeError/500 on a None instance).
        raise HTTPException(status_code=404, detail="Decision record not found.")
    key = record.idempotency_key
    # NULL-idempotency_key pre-flight guard (Story 6.1 deferred item, due before
    # go-live / Story 6.3): a decision must carry the stable key persisted at
    # propose. If it is somehow NULL (a data-integrity fault), refuse BEFORE any
    # placement — a pre-fill refusal, never a post-fill crash in ``cosign`` (which
    # asserts the placed key matches the persisted one). Release the claim
    # (cosigning→proposed) so the record is never stranded; the broker is never
    # touched.
    if key is None:
        await release_claim(body.decision_id, scope=scope, session=session)
        raise HTTPException(
            status_code=422,
            detail=(
                "This decision is missing its safety key and can't be placed. "
                "Please start a new recommendation."
            ),
        )
    intent = OrderIntent(
        symbol=body.order_intent.symbol,
        side=body.order_intent.side,
        amount=body.order_intent.amount,
    )
    try:
        outcome = await execute_approved_order(
            intent,
            broker=broker,
            broker_session=broker_session,
            idempotency_key=key,
        )
    except SessionIntegrityError as exc:
        # Session lapsed or provider mismatched at placement time; release the
        # claim (cosigning→proposed) so the decision is retryable, then refuse
        # with the same calm reconnect envelope as the entry gate — broker never
        # touched.
        await release_claim(body.decision_id, scope=scope, session=session)
        raise HTTPException(
            status_code=409, detail=RECONNECT_MESSAGE
        ) from exc
    except OrderScopeError as exc:
        # Rejected before any broker call; release the claim (retryable) and
        # surface through the app envelope.
        await release_claim(body.decision_id, scope=scope, session=session)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OrderNotPlaceableError as exc:
        # A deliberate, calm pre-placement refusal from the adapter (Story 6.3):
        # the dollar amount buys less than one whole share, or the quote was
        # unusable — NO order was placed. Release the claim (retryable) and
        # surface the calm reason, symmetric with the scope refusal above.
        await release_claim(body.decision_id, scope=scope, session=session)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        # ANY other failure before a successful placement (broker timeout,
        # connection error, unexpected bug) — the spec mandates the claim is
        # RELEASED on *any* pre-placement error, not just the two typed refusals.
        # Release (cosigning→proposed) so the decision is never stranded
        # mid-claim, then re-raise the original error (surfaces as 500) with the
        # record left retryable rather than permanently stuck in ``cosigning``.
        await release_claim(body.decision_id, scope=scope, session=session)
        raise

    # Order was actually placed and reconciled: co-sign the referenced record
    # exactly once (cosigning→cosigned, delegated to the sole writer — AD-6).
    cosign(record, order_intent=intent, outcome=outcome, idempotency_key=key)
    await session.commit()
    return _to_approve_response(outcome)


@router.get("/decisions", response_model=DecisionListResponse)
async def list_decisions(
    scope: Scope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
    limit: int = Query(DECISION_PAGE_SIZE, ge=1, le=DECISION_MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
) -> DecisionListResponse:
    """LIST a bounded page of the caller's co-signed decisions (Story 4.10/6.6).

    Read-only, paginated history over the immutable record: delegates to the
    Coach Engine's sole reader
    (:func:`coach.decision_record.list_cosigned_decisions`), which returns this
    user's ``cosigned`` rows ordered by ``co_signed_at`` desc THROUGH the
    fail-closed :class:`~db.repository.ScopedRepository` — scoping, filter,
    ordering, and the ``limit``/``offset`` window all execute in SQL (a foreign
    row is never visible; ``proposed`` rows are excluded). ``limit`` is bounded by
    ``DECISION_MAX_PAGE_SIZE`` (an over-cap request is a 422, never clamped) and
    both bounds have floors (Story 6.6). The additive ``has_more``/``limit``/
    ``offset`` envelope is echoed back so a client can page without guessing. This
    handler never queries the model itself (AD-6) and NEVER re-runs the pipeline
    or recomputes anything (AD-5). No broker/live-session dependency — history
    reads work in degraded mode.
    """
    page = await list_cosigned_decisions(
        scope=scope, session=session, limit=limit, offset=offset
    )
    return DecisionListResponse(
        decisions=[_decision_summary_out(record) for record in page.rows],
        has_more=page.has_more,
        limit=limit,
        offset=offset,
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
