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
from datetime import date
from decimal import Decimal, InvalidOperation

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import get_settings
from api.deps import RECONNECT_MESSAGE, get_scope, require_live_broker_session
from brokers.factory import get_execution_broker
from brokers.port import BrokerPort, OrderNotPlaceableError, OrderOutcome
from brokers.schwab_adapter import (
    SchwabAccountSelectionError,
    SchwabNotConfiguredError,
)
from brokers.portfolio import get_portfolio
from brokers.session import BrokerageSession
from coach.decision_record import (
    claim_for_cosign,
    cosign,
    effective_outcome_status,
    list_cosigned_decisions,
    load_decision,
    lock_decision,
    persist_broker_ref,
    record_proposal,
    record_reconciliation,
    release_claim,
)
from coach.execution import (
    OrderNotSupportedError,
    OrderScopeError,
    SessionIntegrityError,
    cancel_pending_decision,
    execute_approved_order,
    reconcile_pending_decision,
)
from coach.pipeline import CoachDecision, run_coach_pipeline
from coach.suggest import suggest_resting_order
from coach.recommendation import (
    Duration,
    OrderIntent,
    OrderSide,
    OrderType,
    Session,
)
from coach.validation import BlessedRecommendation
from db.scope import Scope
from db.session import get_async_session
from llm.factory import get_llm_gateway
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

    ``amount``/``limit_price``/``stop_price`` are accepted as Decimals (Pydantic
    parses a JSON string/number to ``Decimal``) and stay ``Decimal`` end to end —
    never binary float. The order-model fields (Story 8.1) are optional and
    defaulted so a plain ``{symbol, side, amount}`` body is still a valid MARKET
    order; ``order_type``/``session``/``duration`` are the human-entered overrides
    on the ``/approve`` path (the LLM coach never proposes them).
    """

    symbol: str
    side: OrderSide
    amount: Decimal
    order_type: OrderType = OrderType.MARKET
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    session: Session = Session.REGULAR
    duration: Duration = Duration.DAY

    @model_validator(mode="after")
    def _validate_order_matrix(self) -> "OrderIntentIn":
        """Fast price-shape check for the two SUPPORTED order types (Story 8.1).

        A malformed MARKET (carrying a limit/stop price) or LIMIT (missing a
        positive, finite ``limit_price`` / carrying a ``stop_price``) is a 422
        HERE, before the engine.

        Deferred features (``stop``/``stop_limit`` type, ``am``/``pm`` session,
        ``gtc`` duration) are DELIBERATELY NOT rejected here — they pass the
        boundary and are refused by the authoritative engine gate
        (:func:`coach.execution.validate_order_intent` →
        :class:`~coach.execution.OrderNotSupportedError`), which ``approve`` maps
        to a calm 422 carrying the explicit "not supported in this version"
        message. Rejecting them here would raise ``ValueError`` →
        ``RequestValidationError``, which ``app.py`` flattens to the generic
        "Request validation failed", swallowing that explicit copy (Story 8.1
        review, 2026-08-04). ``stop``/``stop_limit`` therefore skip the
        price-shape check below (the engine refuses the *type* before any
        shape).
        """
        if self.order_type == OrderType.MARKET:
            if self.limit_price is not None or self.stop_price is not None:
                raise ValueError(
                    "A market order can't carry a limit or stop price."
                )
        elif self.order_type == OrderType.LIMIT:
            if self.stop_price is not None:
                raise ValueError("A limit order can't carry a stop price.")
            if (
                self.limit_price is None
                or not self.limit_price.is_finite()
                or self.limit_price <= 0
            ):
                raise ValueError(
                    "A limit order needs a limit price greater than zero."
                )
        # STOP / STOP_LIMIT: not shape-validated here; the engine gate refuses
        # the type with the explicit deferred-feature message.
        return self


class OrderIntentOut(BaseModel):
    """A serialized ``order_intent`` — money fields as decimal STRINGS on the wire.

    The order-model fields (Story 8.1) are **null-when-default on the wire**: a
    MARKET intent serializes to ``{symbol, side, amount, order_type: null,
    limit_price: null, stop_price: null, session: null, duration: null}`` and a
    LIMIT intent carries ``order_type: "limit"`` + ``limit_price`` (the rest
    null). This differs from the PERSISTED snapshot
    (:func:`~coach.decision_record._order_intent_json`), which OMITS the default
    keys entirely — the wire carries explicit nulls, the snapshot carries none.
    (Additive-null only: the Coach never proposes a limit, so ``/recommend``
    output is always the MARKET case; existing consumers see extra ``null`` keys,
    never a changed value.) The schema is kept forward-compatible.
    """

    symbol: str
    side: OrderSide
    amount: str
    order_type: OrderType | None = None
    limit_price: str | None = None
    stop_price: str | None = None
    session: Session | None = None
    duration: Duration | None = None


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
    # ADDITIVE durable-reconciliation fields (Story 6.7), passed through as stored.
    # The immutable ``recommendation_snapshot``/``cosign_snapshot`` above are
    # UNCHANGED, so verbatim replay of them stays byte-identical; these two carry
    # the broker's LATER truth (null until a reconcile runs).
    reconciliation_snapshot: dict | None = None
    reconciled_at: str | None = None


class ReconcileResponse(BaseModel):
    """The result of a durable cross-request reconcile (Story 6.7), money as strings.

    ``status``/``filled_qty``/``avg_price``/``broker_ref`` are the truthful reconciled
    :class:`~brokers.port.OrderOutcome`. ``needs_reconfirmation`` is ``True`` when
    the true outcome could NOT be positively confirmed (no ``broker_ref``, or the
    read is still ``pending``/``timeout``) — a calm signal that a human should
    decide; NEVER an auto-resolve and NEVER a re-place.
    """

    status: str
    filled_qty: str
    avg_price: str | None = None
    broker_ref: str | None = None
    needs_reconfirmation: bool


class CancelResponse(BaseModel):
    """The result of a cancel on a cosigned resting order (Story 8.2), money as strings.

    ``status``/``filled_qty``/``avg_price``/``broker_ref`` are the truthful
    post-cancel :class:`~brokers.port.OrderOutcome` — ``rejected`` on a confirmed
    cancel (cancel reuses ``REJECTED``; there is no ``cancelled`` member). Returned
    at 200 whenever the broker was actually called; a refused cancel (already
    settled / partially filled / no order id) is a calm 422 envelope, never this
    body.

    ``needs_reconfirmation`` (Story 8.2, mirroring
    :class:`ReconcileResponse`) is ``True`` whenever the post-cancel read-back is
    anything other than a confirmed terminal ``rejected`` — a transport
    ``timeout`` or a still-``pending`` (cancel not yet applied) order, AND ALSO
    the race-window case where the order actually ``filled``/``partial``-filled
    just before the DELETE applied (``cancel_order`` reads back the true broker
    state through ``_map_order``, exactly as the reconcile path does, so a cancel
    that loses the race truthfully surfaces the fill rather than a phantom
    cancel). In every case the outcome is the honest latest-known truth, is
    persisted, and stays reconcilable; the response is still 200. ``False`` only
    on a confirmed terminal ``rejected`` cancel.
    """

    status: str
    filled_qty: str
    avg_price: str | None = None
    broker_ref: str | None = None
    needs_reconfirmation: bool = False


class SuggestOrderRequest(BaseModel):
    """The user-initiated "suggest & populate the order" request (Story 8.4).

    ``symbol`` is the instrument to size a resting BUY LIMIT for; ``amount`` is an
    OPTIONAL target dollar budget (``Decimal``, never float) — when present and
    ``> 0`` the engine sizes off ``min(amount, available_cash)``, otherwise off the
    user's available idle cash. The endpoint COMPUTES the price/amount/shares
    deterministically; the LLM only narrates them.
    """

    symbol: str
    amount: Decimal | None = None


class SuggestOrderResponse(BaseModel):
    """A computed resting BUY LIMIT (GTC) suggestion, money as strings (Story 8.4).

    ``limit_price``/``amount`` are fixed-point decimal STRINGS on the wire (never
    binary float), via the same ``_money_str`` path as ``/approve``. ``side`` is
    always ``"buy"``, ``order_type`` always ``"limit"``, ``duration`` always
    ``"gtc"`` — a genuine resting buy the frontend populates into the frozen 8.3
    controls. ``shares`` is the whole-share count. ``reasoning`` is the LLM's plain
    prose (or a resilient templated fallback). Nothing executes: the human runs the
    unchanged ``/approve`` co-sign path.
    """

    symbol: str
    side: OrderSide
    order_type: OrderType
    limit_price: str
    duration: Duration
    amount: str
    shares: int
    reasoning: str
    # Story 8.6 — backend-computed honesty facts, serialized as fixed-point
    # strings (never float). ``pct_below_ask`` is the fraction ``(ask -
    # limit_price)/ask`` (e.g. ``"0.0200"``); ``fill_note`` is the calm banded
    # fill-likelihood copy; ``stale_note`` is a calm delayed-data signal (``null``
    # when the newest bar is fresh).
    pct_below_ask: str
    fill_note: str
    stale_note: str | None = None


# --- Serialization helpers ---------------------------------------------------


def _order_intent_out(intent: OrderIntent | None) -> OrderIntentOut | None:
    if intent is None:
        return None
    # Null-when-default on the wire (Story 8.1): defaulted fields serialize as
    # explicit ``null`` (the persisted snapshot OMITS them — see OrderIntentOut).
    return OrderIntentOut(
        symbol=intent.symbol,
        side=intent.side,
        amount=format_money(intent.amount),
        order_type=(
            None if intent.order_type == OrderType.MARKET else intent.order_type
        ),
        limit_price=(
            None if intent.limit_price is None else format_money(intent.limit_price)
        ),
        stop_price=(
            None if intent.stop_price is None else format_money(intent.stop_price)
        ),
        session=None if intent.session == Session.REGULAR else intent.session,
        duration=None if intent.duration == Duration.DAY else intent.duration,
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


def _pct_str(value: Decimal) -> str:
    """Serialize a wire percentage/fraction as fixed-point (never binary float).

    Story 8.6: ``pct_below_ask`` is a ``Decimal`` fraction (e.g.
    ``Decimal("0.0200")``); this renders it as a plain fixed-point string
    (``"0.0200"``) via ``format(Decimal, "f")`` so the frontend receives an exact
    string and no ``E+`` notation, symmetric with :func:`_money_str`.
    """
    return format(value, "f")


def _placed_order_matches_proposal(order_intent, snap_intent: dict) -> bool:
    """True iff the submitted order matches the co-signed proposal (Story 9.3).

    Compares the GUARDRAIL-critical fields only — symbol (normalized), side, and
    amount (by Decimal value, so ``"500"`` == ``"500.00"``). The human-entered
    order-shape controls (order_type/limit_price/session/duration, Story 8.1) are
    intentionally NOT compared. A missing/malformed snapshot amount fails closed
    (returns ``False`` → the caller refuses). Only called when the proposal
    carried a concrete ``order_intent`` (the deterministic 8.4/9.3 flows); the
    human-supplied coach flow snapshots ``None`` and never reaches here.
    """
    submitted_side = getattr(order_intent.side, "value", order_intent.side)
    if (order_intent.symbol or "").strip().upper() != (
        snap_intent.get("symbol") or ""
    ).strip().upper():
        return False
    if submitted_side != snap_intent.get("side"):
        return False
    try:
        return order_intent.amount == Decimal(str(snap_intent.get("amount")))
    except (InvalidOperation, TypeError, ValueError):
        return False


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
    return DecisionSummaryOut(
        decision_id=str(record.id),
        action_label=snapshot.get("action_label", ""),
        symbol=order_intent.get("symbol"),
        co_signed_at=record.co_signed_at.isoformat(),
        # The NEWEST-known truth (Story 6.7): the durable reconciliation status
        # when present, else the original co-sign status. The immutable snapshots
        # are untouched, so this layering is a projection only.
        outcome_status=effective_outcome_status(record),
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
    # Story 9.3 hardening: when the PROPOSED decision carried a concrete order
    # (the deterministic 8.4-suggest / 9.3-liquidation flows snapshot one; the
    # human-supplied coach flow snapshots ``None``), the placed order MUST match
    # that co-signed proposal on the guardrail-critical fields — symbol, side,
    # amount — so a co-signed decision can't be repurposed to place a DIFFERENT
    # order (e.g. an arbitrary-amount SELL of a different symbol, bypassing the
    # index-core scope gate the SELL widening relaxed). The order-shape controls
    # (order_type/limit_price/session/duration) are human-entered at /approve
    # (Story 8.1) and are intentionally NOT reconciled. When the proposal carried
    # no order_intent, the pre-existing human-supplied contract is unchanged.
    _snap_intent = (record.recommendation_snapshot or {}).get("order_intent")
    if _snap_intent is not None and not _placed_order_matches_proposal(
        body.order_intent, _snap_intent
    ):
        # Release the claim (cosigning→proposed) so nothing is stranded, then
        # refuse calmly BEFORE any broker call — symmetric with the scope arm.
        await release_claim(body.decision_id, scope=scope, session=session)
        raise HTTPException(
            status_code=422,
            detail=(
                "This order doesn't match the recommendation you co-signed. "
                "Please start a new recommendation."
            ),
        )
    # Carry ALL the order-model fields (Story 8.1) so the limit price actually
    # reaches the adapter and the cosign snapshot — not just symbol/side/amount.
    intent = OrderIntent(
        symbol=body.order_intent.symbol,
        side=body.order_intent.side,
        amount=body.order_intent.amount,
        order_type=body.order_intent.order_type,
        limit_price=body.order_intent.limit_price,
        stop_price=body.order_intent.stop_price,
        session=body.order_intent.session,
        duration=body.order_intent.duration,
    )
    try:
        outcome = await execute_approved_order(
            intent,
            broker=broker,
            broker_session=broker_session,
            idempotency_key=key,
            # Story 9.3: pass the user scope + session so the SELL scope gate can
            # (read-only) widen to the user's declared parked money-market symbols
            # (a BUY stays index-core-only). Fail-closed when absent.
            scope=scope,
            session=session,
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
    except OrderNotSupportedError as exc:
        # A deferred order feature (Story 8.1): stop/stop_limit type, am/pm
        # session, or gtc duration — rejected BEFORE any broker call. Since
        # ``OrderNotSupportedError(ValueError)`` is caught by no existing typed
        # arm, this MUST sit above the trailing ``except Exception`` (which would
        # release + re-raise → 500). Release the claim (retryable) and surface a
        # calm 422 "not supported in this version", symmetric with the scope arm.
        await release_claim(body.decision_id, scope=scope, session=session)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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
    except SchwabAccountSelectionError as exc:
        # An ambiguous / non-matching multi-account selection (Story 7.5): the
        # adapter refuses BEFORE any broker call (no order placed). Symmetric with
        # the ``OrderNotPlaceableError`` branch above — release the claim
        # (cosigning→proposed) so the decision is retryable, then surface a calm
        # 422 with the adapter's clear reason, never a raw 500.
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

    # Recoverable placement (Story 7.2): persist ``broker_ref`` DURABLY in its own
    # commit the instant placement returns and BEFORE the cosign, so the queryable
    # reference is durable independent of the cosign/commit below. If that later
    # cosign/commit fails, the row stays ``cosigning`` but carries ``broker_ref``
    # (recoverable by the 6.7 reconcile or the reclaimer) instead of a NULL-ref
    # zombie. We do NOT release the claim on a post-placement failure — a live
    # order may exist, so releasing (which permits a re-place) would risk a
    # double-place; recoverability comes from the durable ref, not from releasing.
    if outcome.broker_ref is not None:
        await persist_broker_ref(
            body.decision_id, outcome.broker_ref, scope=scope, session=session
        )

    # Order was actually placed and reconciled: co-sign the referenced record
    # exactly once (cosigning→cosigned, delegated to the sole writer — AD-6).
    # ``cosign`` re-writes ``broker_ref`` idempotently (same value already
    # persisted above).
    #
    # Re-load so the cosign acts on the CURRENT DB state, not the pre-network
    # in-memory instance (``expire_on_commit`` is off + the ``persist_broker_ref``
    # Core UPDATE bypassed this instance, so ``record.status`` would otherwise be a
    # stale ``cosigning``). If a concurrent forward-recovery (the reclaimer) already
    # moved the row out of ``cosigning``, do NOT re-complete a stale instance — the
    # order was placed, so surface the honest outcome without a spurious second
    # cosign (and never release: a live order may exist).
    record = await load_decision(body.decision_id, scope=scope, session=session)
    if record is None or record.status != "cosigning":
        return _to_approve_response(outcome)
    cosign(record, order_intent=intent, outcome=outcome, idempotency_key=key)
    await session.commit()
    return _to_approve_response(outcome)


@router.post("/suggest-order", response_model=SuggestOrderResponse)
async def suggest_order(
    body: SuggestOrderRequest,
    scope: Scope = Depends(get_scope),
    broker_session: BrokerageSession = Depends(require_live_broker_session),
    broker: BrokerPort = Depends(get_execution_broker),
    session: AsyncSession = Depends(get_async_session),
) -> SuggestOrderResponse:
    """SUGGEST & POPULATE a resting BUY LIMIT (GTC) order (Story 8.4, MasterB core).

    The backend DETERMINISTICALLY computes a resting buy-limit price (a touch below
    the recent 20-day low, always strictly below the live ask), sizes a whole-share
    dollar amount from the user's real idle cash, and asks the LLM gateway to
    NARRATE that already-computed number in plain English — the model never does
    money-math and never sets the price. Mirrors the ``/approve`` DI (``get_scope``
    + ``require_live_broker_session`` + ``get_execution_broker`` +
    ``get_async_session``): only reached on an authenticated user AND a live
    brokerage session (:func:`require_live_broker_session` → calm 409 reconnect
    otherwise, no suggestion attempted).

    PLACES NOTHING — it touches no ``place_order``/``decision_record`` path. The
    human still executes via the unchanged ``/approve`` co-sign flow. Every calm
    decline (non-core symbol, no price history, insufficient idle cash) is an
    :class:`~coach.execution.OrderScopeError` → calm 422; an unreadable live quote
    is the adapter's :class:`~brokers.port.OrderNotPlaceableError` → calm 422; a
    lapsed session is the existing 409. Money crosses the wire as fixed-point
    strings. The LLM gateway is resolved from config (fake unless configured);
    narration failure degrades to a deterministic templated reasoning, never a 500.
    """
    gateway = get_llm_gateway()
    try:
        suggestion = await suggest_resting_order(
            scope,
            session,
            broker=broker,
            broker_session=broker_session,
            gateway=gateway,
            symbol=body.symbol,
            target_amount=body.amount,
            # Inject the wall-clock reference date HERE (out of the pure pricing
            # path) so freshness is deterministic and testable — Story 8.6.
            as_of=date.today(),
        )
    except OrderNotSupportedError as exc:
        # Symmetric with /approve: a deferred order feature refusal → calm 422.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OrderScopeError as exc:
        # A calm decline (non-core symbol, no history, insufficient idle cash) →
        # 422 through the app error envelope, never a 500, never a phantom order.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OrderNotPlaceableError as exc:
        # An unreadable live quote (missing/non-positive ask) surfaced by
        # ``broker.get_quote`` → the same calm 422 as an out-of-scope order.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SchwabNotConfiguredError as exc:
        # A config/auth fault surfaced by the quote read at client build — map it
        # to the SAME calm 409 reconnect envelope as a lapsed session (distinct
        # from a calm decline), symmetric with /reconcile and /cancel.
        raise HTTPException(status_code=409, detail=RECONNECT_MESSAGE) from exc

    return SuggestOrderResponse(
        symbol=suggestion.symbol,
        side=suggestion.side,
        order_type=suggestion.order_type,
        limit_price=_money_str(suggestion.limit_price),
        duration=suggestion.duration,
        amount=_money_str(suggestion.amount),
        shares=suggestion.shares,
        reasoning=suggestion.reasoning,
        pct_below_ask=_pct_str(suggestion.pct_below_ask),
        fill_note=suggestion.fill_note,
        stale_note=suggestion.stale_note,
    )


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
        reconciliation_snapshot=record.reconciliation_snapshot,
        reconciled_at=(
            None if record.reconciled_at is None else record.reconciled_at.isoformat()
        ),
    )


@router.post(
    "/decisions/{decision_id}/reconcile", response_model=ReconcileResponse
)
async def reconcile_decision(
    decision_id: UUID,
    scope: Scope = Depends(get_scope),
    broker_session: BrokerageSession = Depends(require_live_broker_session),
    broker: BrokerPort = Depends(get_execution_broker),
    session: AsyncSession = Depends(get_async_session),
) -> ReconcileResponse:
    """RECONCILE a cosigned decision's ambiguous placement durably (Story 6.7).

    The explicit, cross-request recovery for a placement surfaced
    ``pending``/``timeout`` in an earlier request (Story 6.3): it reads the true
    :class:`~brokers.port.OrderOutcome` by the persisted queryable ``broker_ref``
    and persists it ADDITIVELY, so an order stranded ``pending`` can finally be
    resolved. READ-ONLY — it NEVER calls ``place_order`` and never re-places.

    Only reached on an authenticated user AND a live brokerage session
    (:func:`require_live_broker_session` → calm 409 reconnect otherwise). Loads the
    record through the sole reader (:func:`load_decision`, per-user scoped — a
    foreign/unknown id is invisible → 404). A record not yet ``cosigned`` has no
    placed order to reconcile → calm 422. Otherwise it delegates to the Coach
    Engine's :func:`reconcile_pending_decision` (the SOLE caller of
    ``get_order_status_by_ref``, AD-7), which re-asserts placement-time integrity
    (mapped HERE to the same calm 409 ``RECONNECT_MESSAGE`` as ``approve`` on a
    :class:`SessionIntegrityError`). When the reconcile actually READ the broker
    (``reconciled=True``) the true outcome is persisted via
    :func:`record_reconciliation` (additive; the immutable snapshots are never
    mutated) and committed. The response surfaces the honest outcome + the
    ``needs_reconfirmation`` signal (True when unconfirmable — no ``broker_ref``, or
    still ``pending``/``timeout``). Money crosses the wire as fixed-point strings.
    """
    record = await load_decision(decision_id, scope=scope, session=session)
    if record is None:
        # Unknown or foreign decision_id → invisible under this user's scope.
        raise HTTPException(status_code=404, detail="Decision record not found.")
    if record.status != "cosigned":
        # A proposed/cosigning record placed no order — nothing to reconcile yet.
        raise HTTPException(
            status_code=422,
            detail="This decision has no placed order to reconcile yet.",
        )

    try:
        result = await reconcile_pending_decision(
            record, broker=broker, broker_session=broker_session
        )
    except SessionIntegrityError as exc:
        # Session lapsed or provider mismatched at reconcile time — the same calm
        # reconnect envelope as the entry gate; the broker was never touched.
        raise HTTPException(status_code=409, detail=RECONNECT_MESSAGE) from exc
    except SchwabNotConfiguredError as exc:
        # A DETERMINISTIC config/auth fault surfaced by the READ-ONLY reconcile
        # read (``get_order_status_by_ref`` now lets ``SchwabNotConfiguredError``
        # propagate rather than laundering it into TIMEOUT). Map it to the SAME
        # calm 409 reconnect envelope — DISTINCT from a transport blip, which
        # still flows through below as a normal 200 ``timeout``/``needs_reconfirmation``
        # result with the ``broker_ref`` preserved (so the order stays
        # reconcilable). Nothing is persisted here: the fault precedes
        # ``record_reconciliation``, so no local money truth is touched.
        raise HTTPException(status_code=409, detail=RECONNECT_MESSAGE) from exc

    if result.reconciled:
        # The broker was actually read (UNLOCKED, above — never hold a row lock
        # across the network round-trip). Now serialize the LOCAL persist: re-load
        # the row UNDER A LOCK (Story 7.2) so two concurrent reconciles of this
        # decision serialize — the second blocks on the ``FOR UPDATE`` until the
        # first commits, then ``record_reconciliation``'s in-writer monotonic
        # terminal guard sees the just-committed state and no-ops rather than
        # regressing a terminal money truth. Persist the reconciled outcome
        # ADDITIVELY on the LOCKED instance (immutable snapshots untouched) via the
        # sole writer (AD-6), then commit (releasing the lock).
        locked = await lock_decision(decision_id, scope=scope, session=session)
        if locked is not None:
            record_reconciliation(locked, outcome=result.outcome)
            await session.commit()

    outcome = result.outcome
    return ReconcileResponse(
        status=outcome.status.value,
        filled_qty=format_money(outcome.filled_qty),
        avg_price=(
            None if outcome.avg_price is None else format_money(outcome.avg_price)
        ),
        broker_ref=outcome.broker_ref,
        needs_reconfirmation=result.needs_reconfirmation,
    )


@router.post("/decisions/{decision_id}/cancel", response_model=CancelResponse)
async def cancel_decision(
    decision_id: UUID,
    scope: Scope = Depends(get_scope),
    broker_session: BrokerageSession = Depends(require_live_broker_session),
    broker: BrokerPort = Depends(get_execution_broker),
    session: AsyncSession = Depends(get_async_session),
) -> CancelResponse:
    """CANCEL a cosigned resting order by its persisted ``broker_ref`` (Story 8.2).

    The HTTP boundary for the resting-order lifecycle's cancel verb, mirroring
    :func:`reconcile_decision`. A resting limit that co-signed ``pending`` can be
    affirmatively cancelled: the broker cancels it, it maps to ``rejected`` (cancel
    reuses ``REJECTED`` — there is no ``cancelled`` member), and it is not
    re-placeable.

    Only reached on an authenticated user AND a live brokerage session
    (:func:`require_live_broker_session` → calm 409 reconnect otherwise). Loads the
    record through the sole reader (:func:`load_decision`, per-user scoped — a
    foreign/unknown id is invisible → 404). A record not yet ``cosigned`` has no
    placed order to cancel → calm 422. Otherwise it delegates to the Coach Engine's
    :func:`cancel_pending_decision` (the SOLE caller of ``cancel_order``, AD-7),
    which re-asserts placement-time integrity (mapped HERE to the same calm 409
    ``RECONNECT_MESSAGE`` as ``approve``/``reconcile`` on a
    :class:`SessionIntegrityError`).

    A cancel that is REFUSED without touching the broker — the order is already
    terminal/settled (``filled``/``rejected``, which also makes a second cancel
    idempotent) or carries no ``broker_ref`` — is a calm 422, NEVER a 500. When the
    broker WAS called (``cancelled=True``) the ``rejected`` outcome is persisted via
    :func:`record_reconciliation` under ``lock_decision`` (additive; the immutable
    snapshots are never mutated) and committed. Money crosses the wire as
    fixed-point strings.
    """
    record = await load_decision(decision_id, scope=scope, session=session)
    if record is None:
        # Unknown or foreign decision_id → invisible under this user's scope.
        raise HTTPException(status_code=404, detail="Decision record not found.")
    if record.status != "cosigned":
        # A proposed/cosigning record placed no order — nothing to cancel yet.
        raise HTTPException(
            status_code=422,
            detail="This decision has no placed order to cancel yet.",
        )

    try:
        result = await cancel_pending_decision(
            record, broker=broker, broker_session=broker_session
        )
    except SessionIntegrityError as exc:
        # Session lapsed or provider mismatched at cancel time — the same calm
        # reconnect envelope as the entry gate; the broker was never touched.
        raise HTTPException(status_code=409, detail=RECONNECT_MESSAGE) from exc
    except SchwabNotConfiguredError as exc:
        # A DETERMINISTIC config/auth fault surfaced by ``cancel_order`` at client
        # build — map it to the SAME calm 409 reconnect envelope, DISTINCT from a
        # transport blip. Nothing is persisted (the fault precedes the persist).
        raise HTTPException(status_code=409, detail=RECONNECT_MESSAGE) from exc

    if result.refused:
        # Already settled / already partially filled / no order id — a calm 422
        # (never a 500); the broker was never touched. Idempotent: a second cancel
        # sees the first's terminal rejected and lands here too. A ``partial`` order
        # is refused because cancelling it would erase its real filled shares (B1
        # scope).
        raise HTTPException(
            status_code=422,
            detail=(
                "This order can no longer be cancelled — it is already settled or "
                "partially filled."
            ),
        )

    # The broker WAS called (cancelled=True): persist the outcome under a row lock
    # (Story 7.2) via the sole writer (AD-6), advancing pending → rejected (or
    # persisting the honest indeterminate timeout/pending latest-known truth), then
    # commit. Mirrors the reconcile persist exactly.
    locked = await lock_decision(decision_id, scope=scope, session=session)
    if locked is not None:
        record_reconciliation(locked, outcome=result.outcome)
        await session.commit()

    outcome = result.outcome
    # Honesty signal (Story 8.2, mirrors the reconcile endpoint): a cancel is a
    # CLEAN success only when the broker positively confirmed a terminal
    # ``rejected`` (canceled) state. A ``timeout`` (transport blip after the DELETE)
    # or a still-``pending`` read-back means the order's true state is unknown /
    # still working — flag it so the response doesn't claim a clean cancel. The
    # outcome is persisted above and stays reconcilable.
    needs_reconfirmation = outcome.status.value != "rejected"
    return CancelResponse(
        status=outcome.status.value,
        filled_qty=format_money(outcome.filled_qty),
        avg_price=(
            None if outcome.avg_price is None else format_money(outcome.avg_price)
        ),
        broker_ref=outcome.broker_ref,
        needs_reconfirmation=needs_reconfirmation,
    )
