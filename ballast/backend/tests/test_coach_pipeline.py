"""Story 4.3 tests — the Coach pipeline & default-plan fallback (FR7/AD-4).

These tests run with ZERO credentials and ZERO network. The pure/offline tests
build in-memory ``EvidenceRecord`` fixtures and small ``LLMGateway`` stubs; the
single async end-to-end test uses only the local DB (seeded ``market_daily``, no
network, no Anthropic key), mirroring ``tests/test_precedent.py``'s harness.

They walk every row of the spec's I/O & Edge-Case Matrix:
  - LLM valid → the LLM's blessed recommendation is surfaced (default NOT used)
  - No confident call (strategy) → default plan citing the strategy record
  - Gate rejects LLM output → deterministic default plan (never a dead-end)
  - Gateway raises → deterministic default plan
  - Determinism → the default builder yields equal (frozen) blessed objects
  - Hard-reasoning routing → True for an EVENT_PRECEDENT, False for STRATEGY-only
  - End-to-end (offline) → run_coach_pipeline returns a BlessedRecommendation
    (the default plan, since the real FakeLLMGateway cannot cite real IDs)
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio

from coach.pipeline import (
    COACH_SYSTEM_PROMPT,
    CoachDecision,
    build_default_plan,
    compose_request,
    is_hard_reasoning,
    run_coach_pipeline,
    surface,
)
from coach.recommendation import RECOMMENDATION_OUTPUT_SCHEMA, OrderSide
from coach.validation import BlessedRecommendation
from db.connection import get_connection
from db.models import MarketDaily
from db.session import async_session_maker, engine
from llm.fake_adapter import FakeLLMGateway
from llm.port import LLMGateway, LLMResponse
from precedent.evidence import EvidenceKind, EvidenceRecord


# --- In-memory evidence fixtures (no DB) -------------------------------------


def _strategy_record(rid: str = "strat-0001") -> EvidenceRecord:
    return EvidenceRecord(
        id=rid,
        kind=EvidenceKind.STRATEGY,
        statement="Stick to your plan: make the regular contribution.",
        stats={"kind": "default-plan"},
        source="ballast-strategy",
        as_of=date(2026, 7, 28),
    )


def _event_record(rid: str = "ep-0002") -> EvidenceRecord:
    return EvidenceRecord(
        id=rid,
        kind=EvidenceKind.EVENT_PRECEDENT,
        statement="Similar 12% drawdowns recovered within 9 months on average.",
        stats={"drawdown_pct": "12", "median_recovery_days": 270},
        source="market_daily",
        as_of=date(2026, 7, 28),
    )


def _decision() -> CoachDecision:
    return CoachDecision(symbol="VTI", question="Should I keep investing?")


# --- Test gateway stubs ------------------------------------------------------


class _CitingGateway(LLMGateway):
    """A stub that returns a valid output citing a REAL retrieved evidence ID.

    Counts ``complete`` calls (``calls``) so a test can prove the LLM path is the
    primary one, and can optionally emit an ``order_intent`` to prove pass-through.
    """

    provider = "test-citing"

    def __init__(self, cited_id: str, order_intent: dict | None = None):
        self._cited_id = cited_id
        self._order_intent = order_intent
        self.calls = 0

    def complete(self, request):  # noqa: D401 - test stub
        self.calls += 1
        output = {
            "action_label": "Buy the dip within your plan",
            "reasoning": "The retrieved precedent supports staying invested.",
            "evidence": [self._cited_id],
            "uncertainties": ["Recovery timing is genuinely uncertain."],
        }
        if self._order_intent is not None:
            output["order_intent"] = self._order_intent
        return LLMResponse(
            output=output,
            model="test-model",
            provider=self.provider,
        )


class _FabricatingGateway(LLMGateway):
    """A stub whose output cites an ID absent from the retrieved set (gate rejects)."""

    provider = "test-fabricating"

    def complete(self, request):  # noqa: D401 - test stub
        return LLMResponse(
            output={
                "action_label": "x",
                "reasoning": "a plausible-sounding reason",
                "evidence": ["ep-DOES-NOT-EXIST"],
                "uncertainties": ["something"],
            },
            model="test-model",
            provider=self.provider,
        )


class _RaisingGateway(LLMGateway):
    """A stub whose .complete() raises (network/config/parse failure)."""

    provider = "test-raising"

    def complete(self, request):  # noqa: D401 - test stub
        raise RuntimeError("simulated gateway failure")


# --- LLM valid → surface the LLM's recommendation (default NOT used) ----------


def test_llm_valid_output_is_surfaced_not_default():
    retrieved = (_strategy_record(), _event_record())
    gateway = _CitingGateway("ep-0002")
    blessed = surface(gateway, _decision(), retrieved)

    assert isinstance(blessed, BlessedRecommendation)
    # The LLM path is PRIMARY — the gateway was actually consulted, once.
    assert gateway.calls == 1
    # It's the LLM's recommendation, resolved to the real cited record.
    assert blessed.action_label == "Buy the dip within your plan"
    assert tuple(e.id for e in blessed.evidence) == ("ep-0002",)
    assert all(isinstance(e, EvidenceRecord) for e in blessed.evidence)
    # NOT the default plan.
    assert blessed != build_default_plan(retrieved)


def test_llm_order_intent_is_carried_through_to_blessed():
    # The pipeline surfaces an LLM-emitted order_intent unchanged (semantics
    # validation is 4.6's concern, not this story's — carry it through).
    retrieved = (_strategy_record(), _event_record())
    gateway = _CitingGateway(
        "ep-0002",
        order_intent={"symbol": "VTI", "side": "buy", "amount": "500"},
    )
    blessed = surface(gateway, _decision(), retrieved)

    assert blessed.order_intent is not None
    assert blessed.order_intent.symbol == "VTI"
    assert blessed.order_intent.side is OrderSide.BUY
    assert blessed.order_intent.amount == Decimal("500")


# --- No confident call (strategy) → default plan ------------------------------


def test_no_confident_call_returns_strategy_default_plan():
    # Only a STRATEGY record retrieved; the LLM path fails to bless (fabricated ID).
    retrieved = (_strategy_record(),)
    blessed = surface(_FabricatingGateway(), _decision(), retrieved)

    assert blessed == build_default_plan(retrieved)
    assert tuple(e.id for e in blessed.evidence) == ("strat-0001",)
    assert "stick to your plan" in blessed.action_label.lower()
    assert blessed.order_intent is None


# --- Gate rejects LLM output → default plan (never a dead-end) -----------------


def test_gate_rejection_falls_back_to_default_plan():
    retrieved = (_strategy_record(), _event_record())
    blessed = surface(_FabricatingGateway(), _decision(), retrieved)

    assert blessed == build_default_plan(retrieved)
    # Default plan cites EVERY retrieved ID, in retrieved order.
    assert tuple(e.id for e in blessed.evidence) == ("strat-0001", "ep-0002")


# --- Gateway raises → default plan --------------------------------------------


def test_gateway_raise_falls_back_to_default_plan():
    retrieved = (_strategy_record(), _event_record())
    blessed = surface(_RaisingGateway(), _decision(), retrieved)

    assert isinstance(blessed, BlessedRecommendation)
    assert blessed == build_default_plan(retrieved)


# --- Default plan is well-formed ----------------------------------------------


def test_default_plan_blesses_and_cites_all_retrieved_ids():
    retrieved = (_strategy_record(), _event_record())
    blessed = build_default_plan(retrieved)

    assert isinstance(blessed, BlessedRecommendation)
    # Cites every retrieved ID (resolved to real records), order preserved.
    assert tuple(e.id for e in blessed.evidence) == ("strat-0001", "ep-0002")
    assert all(isinstance(e, EvidenceRecord) for e in blessed.evidence)
    # Non-empty coach-voice reasoning and ≥1 explicit uncertainty.
    assert blessed.reasoning.strip()
    assert any(u.strip() for u in blessed.uncertainties)
    assert len(blessed.uncertainties) >= 1
    # order_intent is None.
    assert blessed.order_intent is None


def test_default_plan_works_with_single_strategy_record():
    retrieved = (_strategy_record(),)
    blessed = build_default_plan(retrieved)
    assert tuple(e.id for e in blessed.evidence) == ("strat-0001",)


# --- Determinism --------------------------------------------------------------


def test_default_plan_is_deterministic_and_equal_on_repeat():
    retrieved = (_strategy_record(), _event_record())
    a = build_default_plan(retrieved)
    b = build_default_plan(retrieved)
    assert a == b


# --- Hard-reasoning routing (both tiers) --------------------------------------


def test_is_hard_reasoning_true_when_event_precedent_present():
    assert is_hard_reasoning((_strategy_record(), _event_record())) is True
    assert is_hard_reasoning((_event_record(),)) is True


def test_is_hard_reasoning_false_for_strategy_only():
    assert is_hard_reasoning((_strategy_record(),)) is False
    assert is_hard_reasoning(()) is False


def test_compose_request_routes_hard_reasoning_by_retrieved_kind():
    hard = compose_request(_decision(), (_strategy_record(), _event_record()))
    soft = compose_request(_decision(), (_strategy_record(),))
    assert hard.hard_reasoning is True
    assert soft.hard_reasoning is False


def test_compose_request_carries_schema_prompt_and_evidence_ids():
    retrieved = (_strategy_record(), _event_record())
    req = compose_request(_decision(), retrieved)
    # Always carries the recommendation schema and the coach-owned system prompt.
    assert req.output_schema is RECOMMENDATION_OUTPUT_SCHEMA
    assert req.system == COACH_SYSTEM_PROMPT
    # The user message embeds each retrieved record so the LLM can cite by ID.
    assert len(req.messages) == 1
    content = req.messages[0].content
    assert req.messages[0].role == "user"
    assert "strat-0001" in content
    assert "ep-0002" in content
    # The citation instruction lives in the Coach Engine's prompt, not the gateway.
    assert "cite only" in COACH_SYSTEM_PROMPT.lower()


def test_compose_request_renders_amount_and_as_of_branches():
    retrieved = (_strategy_record(),)
    # Non-None amount + as_of are rendered explicitly (amount as Decimal, ISO date).
    explicit = compose_request(
        CoachDecision(symbol="VTI", amount=Decimal("500"), as_of=date(2026, 7, 28)),
        retrieved,
    ).messages[0].content
    assert "500" in explicit
    assert "2026-07-28" in explicit
    # None amount + None as_of fall to the default phrasings (no crash, no "None").
    default = compose_request(CoachDecision(symbol="VTI"), retrieved).messages[0].content
    assert "not specified" in default
    assert "latest available data" in default


# --- Real FakeLLMGateway falls back to the default plan (placeholder IDs) ------


def test_real_fake_gateway_falls_back_to_default_plan():
    # The FakeLLMGateway emits placeholder evidence IDs that never match a real
    # retrieved ID, so the gate rejects and the default plan is returned.
    retrieved = (_strategy_record(), _event_record())
    blessed = surface(FakeLLMGateway(), _decision(), retrieved)
    assert blessed == build_default_plan(retrieved)


# --- End-to-end (offline, real DB + real find_precedent) ----------------------

SYM_E2E = "TEST_COACH_E2E"
BASE_DAY = date(2015, 1, 1)


@pytest_asyncio.fixture(autouse=True)
async def ensure_table():
    """Ensure the market_daily table exists for the real-DB end-to-end test."""
    async with engine.begin() as conn:
        await conn.run_sync(MarketDaily.__table__.create, checkfirst=True)
    yield


def _clean(symbols: list[str]) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM market_daily WHERE symbol = ANY(%s)", (symbols,))
        conn.commit()


def _insert_series(symbol: str, closes: list[Decimal]) -> None:
    ingested_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    with get_connection() as conn:
        with conn.cursor() as cur:
            for i, v in enumerate(closes):
                day = BASE_DAY + timedelta(days=i)
                cur.execute(
                    "INSERT INTO market_daily "
                    "(id, symbol, day, open, high, low, close, adj_close, "
                    " volume, source, ingested_at) "
                    "VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s, %s, "
                    "        %s, %s, %s)",
                    (symbol, day, v, v, v, v, v, 1000, "test", ingested_at),
                )
        conn.commit()


@pytest.mark.asyncio
async def test_end_to_end_offline_returns_blessed_default_plan():
    """run_coach_pipeline over seeded data returns a BlessedRecommendation offline.

    Uses the real FakeLLMGateway (zero network, zero credentials). The fake cannot
    cite the real retrieved IDs, so the LLM path fails the gate and the pipeline
    returns the strategy-backed default plan — proving never-a-dead-end end to end.
    """
    _clean([SYM_E2E])
    _insert_series(SYM_E2E, [Decimal("100"), Decimal("92")])
    try:
        decision = CoachDecision(symbol=SYM_E2E, question="Should I invest now?")
        async with async_session_maker() as session:
            blessed = await run_coach_pipeline(
                session, decision, gateway=FakeLLMGateway()
            )
        assert isinstance(blessed, BlessedRecommendation)
        # A BlessedRecommendation is always returned, with real resolved evidence.
        assert len(blessed.evidence) >= 1
        assert all(isinstance(e, EvidenceRecord) for e in blessed.evidence)
        assert blessed.reasoning.strip()
        assert any(u.strip() for u in blessed.uncertainties)
        # The fake path yields the default plan (placeholder IDs don't match).
        async with async_session_maker() as session:
            from precedent import find_precedent

            retrieved = tuple(await find_precedent(session, symbol=SYM_E2E))
        assert blessed == build_default_plan(retrieved)
    finally:
        _clean([SYM_E2E])


@pytest.mark.asyncio
async def test_end_to_end_default_gateway_offline():
    """With no injected gateway, run_coach_pipeline uses the configured fake default."""
    _clean([SYM_E2E])
    _insert_series(SYM_E2E, [Decimal("100"), Decimal("92")])
    try:
        decision = CoachDecision(symbol=SYM_E2E)
        async with async_session_maker() as session:
            blessed = await run_coach_pipeline(session, decision)
        assert isinstance(blessed, BlessedRecommendation)
        assert len(blessed.evidence) >= 1
    finally:
        _clean([SYM_E2E])
