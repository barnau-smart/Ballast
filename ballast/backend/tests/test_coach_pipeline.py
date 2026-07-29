"""Story 4.3 + 4.4 tests — the Coach pipeline, default-plan fallback (FR7/AD-4),
and just-in-time teaching in the single ``reasoning`` field (FR18).

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

Story 4.4 (FR18) rows — teaching lives in the one ``reasoning`` field:
  - Default-plan reasoning teaches the principle + mechanics (not a bare directive)
  - COACH_SYSTEM_PROMPT carries the FR18 teaching directive for the LLM path
  - LLM teaching reasoning is surfaced verbatim (no post-processing added by 4.4)
  - AC3 canary → no new Recommendation field and no schema change under FR18

Story 4.5 (FR11) rows — self-destructive-move warnings that NEVER block, still
one ``reasoning`` field, no new schema field / gate rule:
  - Panic-sell (sell into a live drawdown) → default plan warns, still blesses,
    cites all IDs, order_intent=None, ≥1 honest uncertainty
  - Over-concentration (buy past the concentration ceiling of a PortfolioView) →
    warning content in reasoning
  - Oversized-lump (buy amount dwarfs the portfolio) → warning content in reasoning
  - No rash move (plain buy, small/no amount, no/diversified portfolio) → NO
    warning content (pre-4.5 behavior unchanged)
  - Never-block canary → a detected move + a valid order_intent still yields a
    blessed recommendation with order_intent intact and unchanged
  - Prompt/request → COACH_SYSTEM_PROMPT carries the FR11 directive; a detected
    warning surfaces as a risk signal in compose_request(...).user_content
  - Determinism → equal warnings + equal frozen default plans for identical inputs
  - No-new-field/no-schema-change canary
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio

from coach.pipeline import (
    COACH_SYSTEM_PROMPT,
    CONCENTRATION_SHARE_CEILING,
    OVERSIZED_LUMP_MULTIPLE,
    CoachDecision,
    WarningKind,
    build_default_plan,
    compose_request,
    detect_self_destructive_moves,
    is_hard_reasoning,
    run_coach_pipeline,
    surface,
)
from coach.recommendation import RECOMMENDATION_OUTPUT_SCHEMA, OrderSide
from coach.validation import BlessedRecommendation
from brokers.portfolio import PortfolioView
from db.connection import get_connection
from db.models import MarketDaily, PortfolioCache
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


# --- In-memory portfolio fixtures (no DB; unsaved ORM instances) -------------


def _holding(symbol: str, market_value: Decimal) -> PortfolioCache:
    """An unsaved PortfolioCache row for offline concentration/lump math."""
    return PortfolioCache(
        symbol=symbol,
        quantity=Decimal("1"),
        market_value=market_value,
        cost_basis=market_value,
        cash=Decimal("0"),
        as_of=datetime(2026, 7, 28, tzinfo=timezone.utc),
    )


def _portfolio(
    holdings: list[PortfolioCache], cash: Decimal = Decimal("0")
) -> PortfolioView:
    return PortfolioView(
        holdings=holdings,
        cash=cash,
        as_of=datetime(2026, 7, 28, tzinfo=timezone.utc),
    )


def _diversified_portfolio() -> PortfolioView:
    """A well-diversified portfolio: no single holding near the ceiling."""
    return _portfolio(
        [
            _holding("VTI", Decimal("2000")),
            _holding("BND", Decimal("2000")),
            _holding("VXUS", Decimal("2000")),
        ],
        cash=Decimal("2000"),
    )


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


# --- FR18: just-in-time teaching (one reasoning field) ------------------------


def test_default_plan_reasoning_teaches_principle_and_mechanics():
    # FR18: the default-plan reasoning must TEACH the principle + mechanics of
    # staying the course (the "why it works"), not merely say "stick to your plan".
    retrieved = (_strategy_record(), _event_record())
    blessed = build_default_plan(retrieved)
    text = blessed.reasoning.lower()

    # It still blesses and stays well-formed.
    assert isinstance(blessed, BlessedRecommendation)
    # References the mechanics: time-in-market vs timing, and steady contributions.
    assert "time in the market" in text
    assert "timing" in text
    assert "market timing" in text
    assert "compound" in text
    assert "contribution" in text
    # Materially longer than a bare directive (it teaches, it doesn't just direct).
    assert len(blessed.reasoning) > len(blessed.action_label) * 4
    assert len(blessed.reasoning) > 400
    # Still cites every retrieved ID, order preserved, with no order intent.
    assert tuple(e.id for e in blessed.evidence) == ("strat-0001", "ep-0002")
    assert blessed.order_intent is None
    # Preserves ≥1 non-blank explicit uncertainty.
    assert len(blessed.uncertainties) >= 1
    assert any(u.strip() for u in blessed.uncertainties)


def test_compose_request_system_prompt_carries_fr18_teaching_directive():
    # FR18: the LLM-path teaching directive lives in the composed system prompt.
    retrieved = (_strategy_record(), _event_record())
    system = compose_request(_decision(), retrieved).system
    lowered = system.lower()
    assert "principle and mechanics" in lowered
    assert "not market timing" in lowered
    # It directs teaching tied to THIS decision, layered (immediate why first).
    assert "this decision" in lowered
    assert "lead with the immediate why" in lowered


def test_llm_teaching_reasoning_is_surfaced_verbatim_not_default():
    # A stub emitting teaching-shaped reasoning citing a REAL retrieved ID is
    # surfaced (not the default), with its reasoning carried through verbatim.
    retrieved = (_strategy_record(), _event_record())
    teaching = (
        "Staying invested here fits your plan. The principle is that consistent "
        "index investing compounds over time, and this is not market timing — "
        "you keep buying through the dips, which is where much of the long-run "
        "return comes from."
    )

    class _TeachingGateway(LLMGateway):
        provider = "test-teaching"

        def complete(self, request):  # noqa: D401 - test stub
            return LLMResponse(
                output={
                    "action_label": "Keep to your plan and stay invested",
                    "reasoning": teaching,
                    "evidence": ["ep-0002"],
                    "uncertainties": ["Recovery timing is genuinely uncertain."],
                },
                model="test-model",
                provider=self.provider,
            )

    blessed = surface(_TeachingGateway(), _decision(), retrieved)
    assert isinstance(blessed, BlessedRecommendation)
    # It's the LLM's recommendation, not the default plan.
    assert blessed != build_default_plan(retrieved)
    assert blessed.action_label == "Keep to your plan and stay invested"
    # Teaching reasoning carried through verbatim.
    assert blessed.reasoning == teaching
    assert tuple(e.id for e in blessed.evidence) == ("ep-0002",)


def test_teaching_adds_no_new_field_or_schema_change():
    # AC3 canary: FR18 teaching lives in the single existing ``reasoning`` field.
    # Story 4.4 must add NO new Recommendation field and NO schema change — this
    # pins that central negative guarantee so a future FR18-branded widening fails.
    from coach.recommendation import (
        RECOMMENDATION_OUTPUT_SCHEMA,
        Recommendation,
    )

    assert set(RECOMMENDATION_OUTPUT_SCHEMA["required"]) == {
        "action_label",
        "reasoning",
        "evidence",
        "uncertainties",
    }
    assert set(Recommendation.__dataclass_fields__) == {
        "action_label",
        "reasoning",
        "evidence",
        "uncertainties",
        "order_intent",
    }


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


# --- FR11: self-destructive-move warnings (never block, one reasoning field) --


def test_panic_sell_default_plan_warns_still_blesses_cites_all():
    # (a) side="sell" while a live drawdown (event-precedent) is present → the
    # default plan warns about selling into a downturn, still blesses, cites all
    # IDs, order_intent=None, ≥1 non-blank uncertainty. Never blocked.
    retrieved = (_strategy_record(), _event_record())
    decision = CoachDecision(symbol="VTI", question="Should I sell?", side="sell")
    warnings = detect_self_destructive_moves(decision, retrieved)
    assert any(w.kind is WarningKind.PANIC_SELL for w in warnings)

    blessed = build_default_plan(retrieved, warnings)
    text = blessed.reasoning.lower()
    assert isinstance(blessed, BlessedRecommendation)
    # Warns honestly about selling into a downturn and explains the risk.
    assert "selling into a downturn" in text
    assert "locks in the loss" in text
    # Still blessed, cites every retrieved ID (order preserved), no order intent.
    assert tuple(e.id for e in blessed.evidence) == ("strat-0001", "ep-0002")
    assert blessed.order_intent is None
    assert len(blessed.uncertainties) >= 1
    assert any(u.strip() for u in blessed.uncertainties)


def test_panic_sell_needs_no_portfolio_and_requires_live_drawdown():
    # Panic-sell fires from side="sell" + an event-precedent, no portfolio needed;
    # a sell with only a strategy record (no live drawdown) does NOT warn.
    sell = CoachDecision(symbol="VTI", side="sell")
    assert any(
        w.kind is WarningKind.PANIC_SELL
        for w in detect_self_destructive_moves(sell, (_strategy_record(), _event_record()))
    )
    assert detect_self_destructive_moves(sell, (_strategy_record(),)) == ()


def test_over_concentration_produces_warning_content():
    # (b) A buy that pushes this symbol past the concentration ceiling of total
    # portfolio value → over-concentration warning content in the reasoning.
    # Portfolio: 5000 in VTI already, 1000 cash → total 6000. Buy 5000 more of VTI
    # → post 10000 / 11000 ≈ 0.91 > 0.40 ceiling.
    portfolio = _portfolio([_holding("VTI", Decimal("5000"))], cash=Decimal("1000"))
    decision = CoachDecision(symbol="VTI", side="buy", amount=Decimal("5000"))
    retrieved = (_strategy_record(),)
    warnings = detect_self_destructive_moves(decision, retrieved, portfolio)
    assert any(w.kind is WarningKind.OVER_CONCENTRATION for w in warnings)

    blessed = build_default_plan(retrieved, warnings)
    text = blessed.reasoning.lower()
    assert "vti" in text
    assert "outsized share" in text
    assert blessed.order_intent is None
    # A numeric threshold fired → the honest heuristic uncertainty is present.
    assert any("heuristic" in u.lower() for u in blessed.uncertainties)


def test_oversized_lump_produces_warning_content():
    # (c) A buy whose amount dwarfs the portfolio value → oversized-lump warning.
    # Portfolio total = 1000; buy 2000 (> 0.50 * 1000). Use a diversified symbol so
    # concentration doesn't dominate the assertion (both may fire, that's fine).
    portfolio = _portfolio(
        [_holding("BND", Decimal("800"))], cash=Decimal("200")
    )
    decision = CoachDecision(symbol="VTI", side="buy", amount=Decimal("2000"))
    retrieved = (_strategy_record(),)
    warnings = detect_self_destructive_moves(decision, retrieved, portfolio)
    assert any(w.kind is WarningKind.OVERSIZED_LUMP for w in warnings)

    blessed = build_default_plan(retrieved, warnings)
    text = blessed.reasoning.lower()
    assert "large next to your current" in text
    assert blessed.order_intent is None
    assert any("heuristic" in u.lower() for u in blessed.uncertainties)


def test_no_rash_move_adds_no_warning_content():
    # (d) A plain, sensible buy (small amount, diversified portfolio) adds NO
    # warning content — behavior identical to pre-4.5.
    portfolio = _diversified_portfolio()
    decision = CoachDecision(symbol="VTI", side="buy", amount=Decimal("100"))
    retrieved = (_strategy_record(), _event_record())
    warnings = detect_self_destructive_moves(decision, retrieved, portfolio)
    assert warnings == ()
    # The default plan is byte-for-byte the pre-4.5 (no-warning) plan.
    assert build_default_plan(retrieved, warnings) == build_default_plan(retrieved)


def test_no_side_and_no_portfolio_yields_no_warnings_backward_compatible():
    # Backward compatibility: a CoachDecision with no side / no portfolio yields
    # no warnings and the same default plan as before 4.5.
    retrieved = (_strategy_record(), _event_record())
    warnings = detect_self_destructive_moves(_decision(), retrieved, None)
    assert warnings == ()
    assert build_default_plan(retrieved, warnings) == build_default_plan(retrieved)


def test_over_concentration_only_when_portfolio_provided():
    # over-concentration / oversized-lump need a portfolio; without one, no warning.
    decision = CoachDecision(symbol="VTI", side="buy", amount=Decimal("100000"))
    assert detect_self_destructive_moves(decision, (_strategy_record(),), None) == ()


def test_never_block_canary_order_intent_carried_through_on_detected_move():
    # (e) A detected self-destructive move + a gateway emitting a valid order_intent
    # STILL yields a blessed recommendation with the order_intent intact and
    # unchanged — a warning never refuses, blocks, or strips the intent (FR11).
    retrieved = (_strategy_record(), _event_record())
    decision = CoachDecision(symbol="VTI", question="Sell now?", side="sell")
    warnings = detect_self_destructive_moves(decision, retrieved)
    assert warnings  # a panic-sell was detected

    gateway = _CitingGateway(
        "ep-0002",
        order_intent={"symbol": "VTI", "side": "sell", "amount": "500"},
    )
    blessed = surface(gateway, decision, retrieved, warnings)

    assert isinstance(blessed, BlessedRecommendation)
    # Not blocked: the LLM path was consulted and its recommendation surfaced.
    assert gateway.calls == 1
    assert blessed.action_label == "Buy the dip within your plan"
    # order_intent carried through intact and unchanged.
    assert blessed.order_intent is not None
    assert blessed.order_intent.symbol == "VTI"
    assert blessed.order_intent.side is OrderSide.SELL
    assert blessed.order_intent.amount == Decimal("500")


def test_compose_request_carries_fr11_directive_and_risk_signal():
    # (f) The system prompt carries the FR11 "warn but never block" directive, and
    # a detected warning surfaces as a risk signal in compose_request.user_content.
    retrieved = (_strategy_record(), _event_record())
    system = compose_request(_decision(), retrieved).system.lower()
    assert "self-destructive" in system
    assert "never refuse, block" in system
    assert "you advise, the user decides" in system

    decision = CoachDecision(symbol="VTI", side="sell")
    warnings = detect_self_destructive_moves(decision, retrieved)
    content = compose_request(decision, retrieved, warnings).messages[0].content
    assert "self-destructive-move risks" in content.lower()
    assert "selling into a downturn locks in the loss" in content.lower()
    # No warnings → no risk-signal block (pre-4.5 request shape unchanged).
    plain = compose_request(_decision(), retrieved).messages[0].content
    assert "self-destructive-move risks" not in plain.lower()


def test_detect_and_default_plan_are_deterministic_on_repeat():
    # (g) Determinism: equal warnings for identical inputs, and two default plans
    # built from the same warnings are equal (frozen).
    portfolio = _portfolio([_holding("VTI", Decimal("5000"))], cash=Decimal("1000"))
    decision = CoachDecision(symbol="VTI", side="buy", amount=Decimal("5000"))
    retrieved = (_strategy_record(),)
    w1 = detect_self_destructive_moves(decision, retrieved, portfolio)
    w2 = detect_self_destructive_moves(decision, retrieved, portfolio)
    assert w1 == w2
    assert build_default_plan(retrieved, w1) == build_default_plan(retrieved, w2)


def test_warning_uncertainty_is_not_a_smuggled_benefit_claim():
    # The heuristic uncertainty states a genuine unknown only — no favorable
    # benefit claim smuggled into the FR14 uncertainty slot.
    portfolio = _portfolio([_holding("VTI", Decimal("5000"))], cash=Decimal("1000"))
    decision = CoachDecision(symbol="VTI", side="buy", amount=Decimal("5000"))
    retrieved = (_strategy_record(),)
    warnings = detect_self_destructive_moves(decision, retrieved, portfolio)
    blessed = build_default_plan(retrieved, warnings)
    heuristic = next(u for u in blessed.uncertainties if "heuristic" in u.lower())
    low = heuristic.lower()
    assert "not guarantees" in low
    assert "cannot tell you what the market will do" in low


def test_fr11_adds_no_new_field_or_schema_change():
    # (h) No-new-field/no-schema-change canary: FR11 warnings live in the single
    # existing ``reasoning`` field. Pin the schema required set and the
    # Recommendation fields so a future FR11-branded widening fails loudly.
    from coach.recommendation import RECOMMENDATION_OUTPUT_SCHEMA, Recommendation

    assert set(RECOMMENDATION_OUTPUT_SCHEMA["required"]) == {
        "action_label",
        "reasoning",
        "evidence",
        "uncertainties",
    }
    assert set(Recommendation.__dataclass_fields__) == {
        "action_label",
        "reasoning",
        "evidence",
        "uncertainties",
        "order_intent",
    }


def test_threshold_constants_are_decimal_not_float():
    # Money/ratio thresholds are Decimal, never binary float (consistency AD).
    assert isinstance(CONCENTRATION_SHARE_CEILING, Decimal)
    assert isinstance(OVERSIZED_LUMP_MULTIPLE, Decimal)


@pytest.mark.asyncio
async def test_run_coach_pipeline_threads_portfolio_into_fr11_warning():
    # The public-entrypoint seam (what Story 4.6 will call): run_coach_pipeline
    # must thread the optional portfolio snapshot through detection into the
    # surfaced recommendation. A concentrated buy + the offline fake gateway
    # (which falls to the default plan) surfaces the over-concentration warning
    # end to end; the same call with NO portfolio surfaces no warning — proving
    # the portfolio→detect→surface wiring is real, both directions.
    _clean([SYM_E2E])
    _insert_series(SYM_E2E, [Decimal("100"), Decimal("92")])
    try:
        portfolio = _portfolio(
            [_holding(SYM_E2E, Decimal("5000"))], cash=Decimal("1000")
        )
        decision = CoachDecision(symbol=SYM_E2E, side="buy", amount=Decimal("5000"))
        async with async_session_maker() as session:
            blessed = await run_coach_pipeline(
                session, decision, gateway=FakeLLMGateway(), portfolio=portfolio
            )
        assert isinstance(blessed, BlessedRecommendation)
        assert "outsized share" in blessed.reasoning.lower()
        assert blessed.order_intent is None
        # Same entrypoint, no portfolio → no threshold warning surfaced.
        async with async_session_maker() as session:
            plain = await run_coach_pipeline(
                session, decision, gateway=FakeLLMGateway()
            )
        assert "outsized share" not in plain.reasoning.lower()
    finally:
        _clean([SYM_E2E])
