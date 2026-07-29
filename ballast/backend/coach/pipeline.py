"""The Coach Engine pipeline — ``retrieve → compose → validate → surface`` (FR7/AD-4).

This module is the single orchestrator that turns a user :class:`CoachDecision`
into a blessed, surfaceable recommendation. It wires together the three owners
built in Epic 3/4 without any module bypassing another:

- **retrieve:** evidence comes only from the Precedent Engine's
  :func:`~precedent.find_precedent` (always ≥1 record — the ``strategy`` default
  is always available, so retrieval never dead-ends).
- **compose:** prompt assembly and the "cite only the evidence IDs I hand you,
  never invent a number" instruction live HERE (the Coach Engine), never in the
  Gateway. The composed :class:`~llm.port.LLMRequest` always carries
  :data:`~coach.recommendation.RECOMMENDATION_OUTPUT_SCHEMA` and a deterministic
  ``hard_reasoning`` flag routed purely from the retrieved set.
- **validate:** blessing goes only through
  :func:`~coach.validation.validate_recommendation` — the sole producer of a
  :class:`~coach.validation.BlessedRecommendation`.
- **surface:** the LLM's blessed recommendation when it validates against the
  retrieved evidence; otherwise the deterministic, code-authored **default plan**.

**Never a dead-end (FR7/AD-4):** :func:`surface` wraps compose→complete→map→
validate in a single ``try``; on ANY exception (gateway error, unparseable
output, or gate rejection — including the fake adapter's placeholder IDs) it
returns :func:`build_default_plan`, which runs OUTSIDE the ``try`` so a bug in
the default builder still surfaces rather than being masked. A
:class:`~coach.validation.BlessedRecommendation` is therefore ALWAYS returned.

``LLMGateway.complete`` is synchronous, so :func:`surface` is a plain function;
:func:`run_coach_pipeline` is ``async`` only because ``find_precedent`` is.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from coach.recommendation import (
    RECOMMENDATION_OUTPUT_SCHEMA,
    Recommendation,
    recommendation_from_output,
)
from coach.validation import BlessedRecommendation, validate_recommendation
from llm.factory import get_llm_gateway
from llm.port import LLMGateway, LLMMessage, LLMRequest
from precedent import EvidenceKind, EvidenceRecord, find_precedent
from precedent.engine import DEFAULT_BENCHMARK

logger = logging.getLogger("ballast.coach.pipeline")


@dataclass(frozen=True)
class CoachDecision:
    """The user-initiated decision that seeds the pipeline (pull-not-push).

    ``symbol`` is the instrument the user is asking about (defaults to the broad
    benchmark ``VTI``); ``question`` is the free-text ask ("should I invest $X?");
    ``amount`` is the optional contribution size as :class:`~decimal.Decimal`
    (NEVER binary float); ``as_of`` pins the precedent lookup to a calendar date
    (``None`` = latest data day, never the wall clock). Frozen so a decision is an
    immutable value.
    """

    symbol: str = DEFAULT_BENCHMARK
    question: str = ""
    amount: Decimal | None = None
    as_of: date | None = None


#: The coach-voice system prompt (owned by the Coach Engine, not the Gateway).
#: Encodes the trust invariants as instructions AND the reviewable coach voice:
#: patient, warm, honest, plain-spoken, explicit about uncertainty; never hype,
#: condescending, or alarmist.
COACH_SYSTEM_PROMPT = (
    "You are Ballast, a patient, warm, and honest investing coach for a "
    "self-aware beginner. Speak plainly, without jargon, hype, condescension, or "
    "alarm. Your job is to help the user make a calm, informed decision and to be "
    "explicit about what is uncertain.\n\n"
    "Strict rules you must follow:\n"
    "1. Cite ONLY the evidence IDs provided to you in the retrieved evidence. "
    "Never invent an evidence ID and never cite one you were not handed.\n"
    "2. Never invent, compute, or recall a market number. Every factual or "
    "precedent claim must come from the retrieved evidence you were given.\n"
    "3. Always state at least one explicit uncertainty — what you do not know or "
    "cannot promise. Past precedent never guarantees a future outcome.\n"
    "4. Always give plain-English reasoning for your recommendation; never a "
    "black box.\n"
    "When there is no confident special call, the honest recommendation is to "
    "stick to the plan and make the regular contribution."
)


def is_hard_reasoning(retrieved: Sequence[EvidenceRecord]) -> bool:
    """Route the model tier PURELY from the retrieved set — no wall-clock, no RNG.

    Returns ``True`` iff any retrieved record is an
    :attr:`~precedent.EvidenceKind.EVENT_PRECEDENT` (a tactical special call
    warranting the hard-reasoning tier); ``False`` for the always-available
    ``STRATEGY`` default. Deterministic: identical retrieved set → identical flag.
    """
    return any(r.kind is EvidenceKind.EVENT_PRECEDENT for r in retrieved)


def _render_evidence(retrieved: Sequence[EvidenceRecord]) -> str:
    """Render the retrieved evidence as JSON-safe lines the LLM may cite by ID."""
    return "\n".join(
        json.dumps(record.to_dict(), sort_keys=True) for record in retrieved
    )


def compose_request(
    decision: CoachDecision, retrieved: Sequence[EvidenceRecord]
) -> LLMRequest:
    """Compose the structured :class:`~llm.port.LLMRequest` (compose stage).

    The user message embeds the decision and each retrieved record (via its
    JSON-safe ``to_dict()``), so the LLM sees exactly the IDs it may cite. The
    request always carries :data:`RECOMMENDATION_OUTPUT_SCHEMA`, the coach-voice
    :data:`COACH_SYSTEM_PROMPT`, and a deterministic ``hard_reasoning`` flag. Pure:
    no I/O, no LLM call, no wall-clock.
    """
    amount_line = (
        f"Amount under consideration: {decision.amount}"
        if decision.amount is not None
        else "Amount under consideration: not specified"
    )
    as_of_line = (
        f"As of: {decision.as_of.isoformat()}"
        if decision.as_of is not None
        else "As of: latest available data"
    )
    user_content = (
        "A user is asking for a recommendation.\n"
        f"Symbol: {decision.symbol}\n"
        f"Question: {decision.question or '(none given)'}\n"
        f"{amount_line}\n"
        f"{as_of_line}\n\n"
        "Retrieved evidence (cite only these IDs, never invent one):\n"
        f"{_render_evidence(retrieved)}\n\n"
        "Produce a recommendation as structured output conforming to the schema. "
        "Cite the relevant evidence IDs, give plain-English reasoning, and state "
        "at least one explicit uncertainty."
    )
    return LLMRequest(
        messages=(LLMMessage(role="user", content=user_content),),
        output_schema=RECOMMENDATION_OUTPUT_SCHEMA,
        system=COACH_SYSTEM_PROMPT,
        hard_reasoning=is_hard_reasoning(retrieved),
    )


def build_default_plan(retrieved: Sequence[EvidenceRecord]) -> BlessedRecommendation:
    """Build the deterministic, code-authored default plan and bless it (FR7/AD-4).

    The default plan is NOT LLM-derived: it cites EVERY retrieved evidence ID,
    carries non-empty coach-voice "stick to your plan" reasoning and ≥1 explicit
    uncertainty, sets ``order_intent=None``, and always passes the gate. Pure and
    deterministic: identical retrieved set → equal (frozen) blessed object; no
    I/O, no LLM, no wall-clock, no randomness. This is the "never a dead-end"
    guarantee — always safe to return when the LLM path fails or offers no
    confident special call.
    """
    candidate = Recommendation(
        action_label="Stick to your plan: make your regular contribution",
        reasoning=(
            "There's no confident special call here, and that's okay. The steady, "
            "boring move — keeping to your plan and making your regular "
            "contribution — is what tends to serve long-term investors best. "
            "Reacting to short-term noise usually costs more than it helps, so the "
            "honest recommendation is to stay the course."
        ),
        evidence=tuple(record.id for record in retrieved),
        uncertainties=(
            "Markets can stay volatile longer than anyone expects, and past "
            "patterns never guarantee a future outcome.",
        ),
        order_intent=None,
    )
    return validate_recommendation(candidate, retrieved)


def surface(
    gateway: LLMGateway,
    decision: CoachDecision,
    retrieved: Sequence[EvidenceRecord],
) -> BlessedRecommendation:
    """Return the surfaceable recommendation — the resilience boundary (FR7/AD-4).

    Wraps compose → ``gateway.complete`` → ``recommendation_from_output`` →
    ``validate_recommendation`` in a single ``try``. On ANY ``Exception`` (gateway
    error, unparseable output, or gate rejection — a
    :class:`~coach.validation.RecommendationValidationError` is an ``Exception``)
    it returns :func:`build_default_plan`. The default builder runs OUTSIDE the
    ``try``, so a bug there still surfaces rather than being silently masked.
    """
    try:
        response = gateway.complete(compose_request(decision, retrieved))
        return validate_recommendation(
            recommendation_from_output(response.output), retrieved
        )
    except Exception as exc:
        # Silent degradation of a trust-critical path is unobservable; log a
        # structured warning (no secrets, no raw tokens — just provider + error
        # type) so a coach that has quietly stopped making special calls is
        # visible in production. Matches find_precedent's degrade-warning style.
        logger.warning(
            "coach_llm_path_failed_fallback_to_default provider=%s error=%s",
            getattr(gateway, "provider", "unknown"),
            type(exc).__name__,
        )
        return build_default_plan(retrieved)  # never a dead-end


async def run_coach_pipeline(
    session: AsyncSession,
    decision: CoachDecision,
    *,
    gateway: LLMGateway | None = None,
) -> BlessedRecommendation:
    """Run the full pipeline for a decision and return a blessed recommendation.

    ``retrieve`` via :func:`~precedent.find_precedent` (async), then ``compose →
    validate → surface`` synchronously. ``gateway`` defaults to
    :func:`~llm.factory.get_llm_gateway` (the fake adapter unless configured),
    injectable for tests and the future ask→approve surface (4.6). Always returns
    a :class:`~coach.validation.BlessedRecommendation`; never raises for a missing
    special call.
    """
    retrieved = tuple(
        await find_precedent(session, symbol=decision.symbol, as_of=decision.as_of)
    )
    gateway = gateway or get_llm_gateway()
    return surface(gateway, decision, retrieved)
