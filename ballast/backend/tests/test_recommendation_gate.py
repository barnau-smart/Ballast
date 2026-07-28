"""Story 4.2 tests — the Recommendation object & validation gate (FR12/13/14, NFR2).

These tests are PURE and SYNCHRONOUS: ZERO DB, ZERO network, ZERO credentials, no
LLM, no async. ``EvidenceRecord`` fixtures are built in-memory. They cover every
I/O-matrix row from the spec:
  - valid candidate -> blessed (cited IDs resolved to real records, order preserved,
    all fields carried through)
  - determinism/equality on repeat
  - MissingReasoningError (empty AND whitespace reasoning)
  - MissingUncertaintiesError (empty)
  - UnbackedEvidenceError (fabricated cited ID AND empty evidence)
  - direct BlessedRecommendation construction raises RecommendationValidationError
  - recommendation_from_output round-trip (with & without order_intent) and
    malformed output (missing keys) -> empty fields -> gate rejects
  - RECOMMENDATION_OUTPUT_SCHEMA is a non-empty "object" schema with the four
    required fields
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from coach.recommendation import (
    RECOMMENDATION_OUTPUT_SCHEMA,
    OrderIntent,
    OrderSide,
    Recommendation,
    recommendation_from_output,
)
from coach.validation import (
    BlessedRecommendation,
    MissingReasoningError,
    MissingUncertaintiesError,
    RecommendationValidationError,
    UnbackedEvidenceError,
    validate_recommendation,
)
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


def _retrieved() -> list[EvidenceRecord]:
    return [_strategy_record(), _event_record()]


def _valid_candidate() -> Recommendation:
    return Recommendation(
        action_label="Make your regular contribution",
        reasoning="Drops like this have historically recovered; staying the course beats timing.",
        # Cite in a deliberate (event, strategy) order to prove order preservation.
        evidence=("ep-0002", "strat-0001"),
        uncertainties=("Past precedent does not guarantee this recovery timeline.",),
        order_intent=OrderIntent(symbol="VTI", side=OrderSide.BUY, amount=Decimal("500")),
    )


# --- Valid recommendation -> blessed -----------------------------------------


def test_valid_candidate_is_blessed_with_resolved_evidence():
    candidate = _valid_candidate()
    retrieved = _retrieved()

    blessed = validate_recommendation(candidate, retrieved)

    assert isinstance(blessed, BlessedRecommendation)
    # Evidence resolved to the REAL EvidenceRecords, cite order preserved.
    assert all(isinstance(e, EvidenceRecord) for e in blessed.evidence)
    assert tuple(e.id for e in blessed.evidence) == ("ep-0002", "strat-0001")
    assert blessed.evidence[0] is retrieved[1]  # ep-0002 is the event record
    assert blessed.evidence[1] is retrieved[0]  # strat-0001 is the strategy record
    # All other fields carried through unchanged.
    assert blessed.action_label == candidate.action_label
    assert blessed.reasoning == candidate.reasoning
    assert blessed.uncertainties == candidate.uncertainties
    assert blessed.order_intent == candidate.order_intent


def test_valid_candidate_without_order_intent_is_blessed():
    candidate = Recommendation(
        action_label="Stick to your plan",
        reasoning="No confident special call; the strategy default applies.",
        evidence=("strat-0001",),
        uncertainties=("Markets can stay volatile longer than expected.",),
    )
    blessed = validate_recommendation(candidate, _retrieved())
    assert blessed.order_intent is None
    assert tuple(e.id for e in blessed.evidence) == ("strat-0001",)


# --- Determinism / equality --------------------------------------------------


def test_blessing_is_deterministic_and_equal_on_repeat():
    candidate = _valid_candidate()
    a = validate_recommendation(candidate, _retrieved())
    b = validate_recommendation(candidate, _retrieved())
    assert a == b


# --- Missing reasoning -------------------------------------------------------


def test_empty_reasoning_rejected():
    candidate = Recommendation(
        action_label="x",
        reasoning="",
        evidence=("strat-0001",),
        uncertainties=("unsure",),
    )
    with pytest.raises(MissingReasoningError):
        validate_recommendation(candidate, _retrieved())


def test_whitespace_reasoning_rejected():
    candidate = Recommendation(
        action_label="x",
        reasoning="   \n\t  ",
        evidence=("strat-0001",),
        uncertainties=("unsure",),
    )
    with pytest.raises(MissingReasoningError):
        validate_recommendation(candidate, _retrieved())


# --- Missing uncertainties ---------------------------------------------------


def test_empty_uncertainties_rejected():
    candidate = Recommendation(
        action_label="x",
        reasoning="a real reason",
        evidence=("strat-0001",),
        uncertainties=(),
    )
    with pytest.raises(MissingUncertaintiesError):
        validate_recommendation(candidate, _retrieved())


def test_blank_uncertainties_rejected():
    # A non-empty tuple of only empty/whitespace strings is NOT an explicit
    # uncertainty (FR14) — must be rejected, mirroring reasoning's strip check.
    candidate = Recommendation(
        action_label="x",
        reasoning="a real reason",
        evidence=("strat-0001",),
        uncertainties=("   ", ""),
    )
    with pytest.raises(MissingUncertaintiesError):
        validate_recommendation(candidate, _retrieved())


# --- Unbacked / fabricated evidence ------------------------------------------


def test_fabricated_evidence_id_rejected():
    candidate = Recommendation(
        action_label="x",
        reasoning="a real reason",
        evidence=("ep-DOES-NOT-EXIST",),
        uncertainties=("unsure",),
    )
    with pytest.raises(UnbackedEvidenceError):
        validate_recommendation(candidate, _retrieved())


def test_empty_evidence_rejected():
    candidate = Recommendation(
        action_label="x",
        reasoning="a real reason",
        evidence=(),
        uncertainties=("unsure",),
    )
    with pytest.raises(UnbackedEvidenceError):
        validate_recommendation(candidate, _retrieved())


def test_duplicate_cited_ids_deduped_preserving_order():
    # Repeating a citation must not inflate the resolved (snapshotted) evidence.
    candidate = Recommendation(
        action_label="x",
        reasoning="a real reason",
        evidence=("ep-0002", "ep-0002", "strat-0001", "ep-0002"),
        uncertainties=("unsure",),
    )
    blessed = validate_recommendation(candidate, _retrieved())
    assert tuple(e.id for e in blessed.evidence) == ("ep-0002", "strat-0001")


def test_rejection_errors_subclass_the_base():
    assert issubclass(MissingReasoningError, RecommendationValidationError)
    assert issubclass(MissingUncertaintiesError, RecommendationValidationError)
    assert issubclass(UnbackedEvidenceError, RecommendationValidationError)
    assert issubclass(RecommendationValidationError, ValueError)


# --- Structural direct-construction guard (NFR2 teeth) -----------------------


def test_direct_construction_raises():
    with pytest.raises(RecommendationValidationError):
        BlessedRecommendation(
            action_label="x",
            order_intent=None,
            reasoning="a real reason",
            evidence=(_strategy_record(),),
            uncertainties=("unsure",),
        )


def test_direct_construction_with_wrong_sentinel_raises():
    with pytest.raises(RecommendationValidationError):
        BlessedRecommendation(
            action_label="x",
            order_intent=None,
            reasoning="a real reason",
            evidence=(_strategy_record(),),
            uncertainties=("unsure",),
            _gate_key=object(),
        )


# --- recommendation_from_output round-trip -----------------------------------


def test_from_output_with_order_intent_round_trips_through_gate():
    output = {
        "action_label": "Buy the dip within plan",
        "reasoning": "The precedent supports steady contributions.",
        "evidence": ["ep-0002", "strat-0001"],
        "uncertainties": ["Recovery timing is uncertain."],
        "order_intent": {"symbol": "VTI", "side": "buy", "amount": "500"},
    }
    candidate = recommendation_from_output(output)
    assert isinstance(candidate, Recommendation)
    assert candidate.evidence == ("ep-0002", "strat-0001")
    assert candidate.uncertainties == ("Recovery timing is uncertain.",)
    assert candidate.order_intent == OrderIntent(
        symbol="VTI", side=OrderSide.BUY, amount=Decimal("500")
    )

    blessed = validate_recommendation(candidate, _retrieved())
    assert tuple(e.id for e in blessed.evidence) == ("ep-0002", "strat-0001")


def test_from_output_without_order_intent_maps_to_none():
    output = {
        "action_label": "Stick to your plan",
        "reasoning": "No special call.",
        "evidence": ["strat-0001"],
        "uncertainties": ["Volatility may persist."],
    }
    candidate = recommendation_from_output(output)
    assert candidate.order_intent is None
    blessed = validate_recommendation(candidate, _retrieved())
    assert blessed.order_intent is None


def test_from_output_does_not_parse_non_dict_order_intent():
    output = {
        "action_label": "x",
        "reasoning": "r",
        "evidence": ["strat-0001"],
        "uncertainties": ["u"],
        "order_intent": None,
    }
    assert recommendation_from_output(output).order_intent is None


def test_malformed_output_maps_to_empty_and_gate_rejects():
    # Missing reasoning/uncertainties/evidence entirely -> empty candidate.
    candidate = recommendation_from_output({"action_label": "x"})
    assert candidate.reasoning == ""
    assert candidate.evidence == ()
    assert candidate.uncertainties == ()
    assert candidate.order_intent is None
    # The mapper did not raise; the GATE is the single rejection point.
    with pytest.raises(MissingReasoningError):
        validate_recommendation(candidate, _retrieved())


def test_from_output_never_raises_on_empty_dict():
    candidate = recommendation_from_output({})
    assert candidate == Recommendation(
        action_label="", reasoning="", evidence=(), uncertainties=(), order_intent=None
    )


def test_from_output_malformed_order_intent_drops_to_none_without_raising():
    # Unknown side and non-numeric amount must NOT raise — the mapper stays
    # tolerant and drops the malformed intent so the gate is the single gate.
    bad_side = recommendation_from_output(
        {
            "action_label": "x",
            "reasoning": "r",
            "evidence": ["strat-0001"],
            "uncertainties": ["u"],
            "order_intent": {"symbol": "VTI", "side": "hold", "amount": "500"},
        }
    )
    assert bad_side.order_intent is None
    bad_amount = recommendation_from_output(
        {
            "action_label": "x",
            "reasoning": "r",
            "evidence": ["strat-0001"],
            "uncertainties": ["u"],
            "order_intent": {"symbol": "VTI", "side": "buy", "amount": "not-a-number"},
        }
    )
    assert bad_amount.order_intent is None
    # The recommendation itself still maps and blesses (order semantics are 4.6).
    assert validate_recommendation(bad_side, _retrieved()).order_intent is None


def test_from_output_bare_string_evidence_is_wrapped_not_char_split():
    # A bare string where an array is expected must become a single cited ID,
    # never be iterated character-by-character.
    candidate = recommendation_from_output(
        {
            "action_label": "x",
            "reasoning": "r",
            "evidence": "strat-0001",
            "uncertainties": "u",
        }
    )
    assert candidate.evidence == ("strat-0001",)
    assert candidate.uncertainties == ("u",)


def test_from_output_non_iterable_fields_map_to_empty_without_raising():
    candidate = recommendation_from_output(
        {"action_label": "x", "reasoning": "r", "evidence": 5, "uncertainties": None}
    )
    assert candidate.evidence == ()
    assert candidate.uncertainties == ()


# --- RECOMMENDATION_OUTPUT_SCHEMA shape --------------------------------------


def test_output_schema_is_nonempty_object_with_required_fields():
    schema = RECOMMENDATION_OUTPUT_SCHEMA
    assert schema  # non-empty
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "action_label",
        "reasoning",
        "evidence",
        "uncertainties",
    }
    assert schema["properties"]["evidence"]["type"] == "array"
    assert schema["properties"]["uncertainties"]["type"] == "array"
    # order_intent is present as an optional (not-required) nested object.
    assert "order_intent" not in schema["required"]
    assert schema["properties"]["order_intent"]["type"] == "object"
