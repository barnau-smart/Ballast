"""Recovery-precedent read endpoint (Story 3.3).

A single auth-gated, read-only surface over the Precedent Engine (AD-3). It
calls the engine's public ``find_precedent`` and returns its ``EvidenceRecord``
as JSON in the FIXED AD-12 shape ``{id, kind, statement, stats, source, as_of}``
— verbatim, with no field added, removed, or renamed. The frontend renders this;
it computes no market number (AD-1).

The endpoint is gated to the authenticated active user (same dependency pattern
as ``/api/portfolio`` — ``get_scope``). Precedent is GLOBAL reference data, so
no ``owner_id``/``Scope`` filtering is applied to the query; the gate exists to
require a valid session, not to isolate rows. The API layer NEVER reads
``market_daily`` or any vendor source directly — precedent comes only through
``find_precedent`` (AD-3, AD-6).

Read-only: no LLM, no Recommendation object, no execution, no persistence — that
is Epic 4. This is one endpoint over the existing engine.
"""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_scope
from brokers.portfolio import get_portfolio
from cash.config import get_config, parked_market_value, resolve_reserve
from db.scope import Scope
from db.session import get_async_session
from precedent import estimate_missed_growth, find_precedent
from precedent.engine import DEFAULT_BENCHMARK

router = APIRouter(prefix="/api/precedent", tags=["precedent"])


class RecoveryPrecedentOut(BaseModel):
    """The AD-12 evidence shape, verbatim, as the wire contract.

    Exactly the six top-level fields of ``EvidenceRecord.to_dict()`` — do NOT
    add, remove, or rename a field (a contract change ripples into Epic 4). The
    engine already emits a JSON-safe dict (Decimal→str, date→ISO), so ``stats``
    is an opaque, pass-through object and ``as_of`` an ISO-8601 date string.
    """

    id: str
    kind: str
    statement: str
    stats: dict
    source: str
    as_of: str


@router.get("/recovery", response_model=RecoveryPrecedentOut)
async def recovery_precedent(
    symbol: str = Query(default=DEFAULT_BENCHMARK, min_length=1, max_length=32),
    drawdown: Decimal | None = Query(default=None, gt=0, le=Decimal("0.90")),
    scope: Scope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> RecoveryPrecedentOut:
    """Return the recovery-precedent evidence record for ``symbol`` (READ-ONLY).

    Delegates entirely to ``find_precedent`` (AD-3), which always returns a
    length-1 list — an ``event-precedent`` record when the current drawdown
    matches a historical band, otherwise the always-valid ``strategy`` fallback
    (AD-4). Never a dead end, never an empty body. The ``scope`` dependency is
    the auth gate (401 for an unauthenticated request); precedent itself is not
    per-user data, so ``scope`` is not used to filter the query.

    ``drawdown`` (optional, Story 3.6) mirrors the contextualizer's hypothetical
    param: when present (``0 < drawdown <= 0.90``, else a calm 422) the record is
    an explicitly-hypothetical precedent centred on that target magnitude rather
    than the live drawdown; when absent the current-conditions behavior is
    unchanged. The top-level ``RecoveryPrecedentOut`` shape is identical either way.
    """
    records = await find_precedent(
        session, symbol=symbol, hypothetical_drawdown=drawdown
    )
    return RecoveryPrecedentOut(**records[0].to_dict())


class ContextualizeIn(BaseModel):
    """Request body for the on-demand headline contextualizer (Story 3.5).

    The ``headline`` is accepted and length-bounded but is deliberately INERT:
    it is never read, parsed, classified, logged, or passed to the engine. It
    exists only so the user story is honest ("a headline I submit") and to give
    a clean seam for a future event-taxonomy enrichment. ``symbol`` mirrors the
    ``/recovery`` bounded global-reference lookup so tests can seed a throwaway
    symbol; the frontend omits ``symbol`` and relies on this server default.

    ``drawdown`` (optional, Story 3.6, FR20) is a HYPOTHETICAL target magnitude —
    "what if it fell about X%?". When present (``0 < drawdown <= 0.90``) it drives
    a hypothetical, explicitly-framed precedent match at that magnitude (never a
    prediction); when absent the current-conditions behavior is unchanged. Unlike
    the inert ``headline``, ``drawdown`` is a NUMBER that legitimately drives the
    match — the headline text still classifies nothing. Out-of-range → calm 422.
    """

    headline: str = Field(min_length=1, max_length=500)
    symbol: str = Field(default=DEFAULT_BENCHMARK, min_length=1, max_length=32)
    drawdown: Decimal | None = Field(default=None, gt=0, le=Decimal("0.90"))


@router.post("/contextualize", response_model=RecoveryPrecedentOut)
async def contextualize(
    body: ContextualizeIn,
    scope: Scope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> RecoveryPrecedentOut:
    """Return drawdown-keyed precedent for a headline the user submits (READ-ONLY).

    Read-only and engine-only (AD-3): the response is obtained exclusively via
    ``find_precedent`` — the API layer never reads ``market_daily`` or a vendor
    source directly, computes no figure (AD-1), and returns the engine's
    ``EvidenceRecord`` verbatim in the AD-12 6-field shape.

    NEVER classifies the event (FR20): the submitted ``body.headline`` is inert
    — it is not read, parsed, or passed to the engine, so the same market state
    with any two different headlines yields a byte-identical record (identical
    ``id`` included). v1 matching is drawdown-band only; event-taxonomy tagging
    is a later enrichment. The ``scope`` dependency is the auth gate (401 for an
    unauthenticated request); precedent is global reference data, so ``scope``
    does not filter the query.

    When ``body.drawdown`` is present the response is an explicitly HYPOTHETICAL
    precedent centred on that target magnitude ("if it fell about X%"), still
    computed entirely by the engine (AD-1/AD-3); it is bounded ``0 < drawdown <=
    0.90`` at the model, so an out-of-range value is a calm 422 (never reaches
    here). The ``drawdown`` number — not the inert headline — drives the match.
    """
    records = await find_precedent(
        session, symbol=body.symbol, hypothetical_drawdown=body.drawdown
    )
    return RecoveryPrecedentOut(**records[0].to_dict())


class MissedGrowthOut(BaseModel):
    """The standalone missed-growth DTO — NOT the AD-12 ``EvidenceRecord``.

    Matches ``MissedGrowthEstimate.to_dict()`` field-for-field, plus the
    config-layer ``reserve_decided`` (Story 9.2) the engine does not carry. It
    deliberately does NOT reuse the shared evidence contract (which is pinned to
    two ``kind`` values Epic 4 depends on — see the story's Design Notes). All
    Decimals arrive as strings and dates as ISO-8601; ``window_return`` /
    ``window_start`` / ``window_end`` / ``as_of`` are ``None`` in the degraded
    states. ``reserved`` is ``None`` when the reserve was never decided.

    Story 9.2 additive fields: ``settlement_cash`` (ready-to-trade cash),
    ``parked`` (money-market cash), ``reserved`` (the resolved reserve, ``None``
    when never-decided), ``reserve_decided``, ``money_market_apy`` (the disclosed
    yield assumption), ``investable_base`` (``cash + parked − reserve``, ≥ 0).
    """

    idle_cash: str
    benchmark: str
    window_return: str | None
    window_start: str | None
    window_end: str | None
    forgone_growth: str
    trading_days: int
    statement: str
    source: str
    as_of: str | None
    sufficient: bool
    reason: str | None
    settlement_cash: str
    parked: str
    reserved: str | None
    reserve_decided: bool
    money_market_apy: str
    investable_base: str


@router.get("/missed-growth", response_model=MissedGrowthOut)
async def missed_growth(
    symbol: str = Query(default=DEFAULT_BENCHMARK, min_length=1, max_length=32),
    scope: Scope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> MissedGrowthOut:
    """Return the forgone-growth estimate for the user's investable cash (READ-ONLY).

    Cash-state-aware + yield-aware (Story 9.2). Reads the authenticated user's
    settlement cash + holdings from the Epic-2 portfolio projection via the
    sanctioned scoped read (``get_portfolio``), and their scoped ``CashConfig``
    READ-ONLY (``get_config`` — never create-on-GET, AD-11). Derives the parked
    (money-market) total via ``parked_market_value`` and the resolved reserve via
    ``resolve_reserve``, then delegates ALL market math to the deterministic engine
    (AD-1/AD-3) — the API layer computes no figure.

    Reserve honesty: a never-decided reserve (``resolve_reserve`` → ``None``) is
    passed to the engine as ``0`` for the calc, but the response surfaces
    ``reserved=null`` + ``reserve_decided=false`` (never a fabricated figure). A
    declined reserve resolves to ``0`` (decided).

    Never a dead end: no investable cash, a fully-covering reserve, and
    insufficient history each return a calm informational body; the ``scope``
    dependency is the auth gate (401 for an unauthenticated request).

    ``symbol`` defaults to the benchmark (``VTI``); it is a bounded global-reference
    lookup (mirrors ``/recovery``), applied to the caller's own cash — the frontend
    always uses the default.
    """
    view = await get_portfolio(scope, session)
    config = await get_config(scope, session)

    parked_total = parked_market_value(view.holdings, config)
    resolved_reserve = resolve_reserve(config) if config is not None else None
    reserve_decided = config.reserve_decided if config is not None else False

    estimate = await estimate_missed_growth(
        session,
        idle_cash=view.cash,
        symbol=symbol,
        parked=parked_total,
        # A never-decided reserve is treated as 0 for the CALC (honest: an unset
        # reserve is legitimately 0); the response still says reserved=null below.
        reserved=resolved_reserve if resolved_reserve is not None else Decimal("0"),
    )

    payload = estimate.to_dict()
    # Surface the config-layer reserve facts the engine can't know: a never-decided
    # reserve reads as null (NEVER a silent 0 — the honesty crux), and the explicit
    # decision flag drives the calm one-time set-or-decline prompt. The engine was
    # handed 0 for a never-decided reserve (for the calc), so overwrite its "0.00"
    # with null here — the ONLY place the null lives.
    if resolved_reserve is None:
        payload["reserved"] = None
    payload["reserve_decided"] = reserve_decided
    return MissedGrowthOut(**payload)
