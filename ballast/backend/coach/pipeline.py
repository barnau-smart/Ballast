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
from enum import Enum
from typing import TYPE_CHECKING, Literal, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from coach.recommendation import (
    RECOMMENDATION_OUTPUT_SCHEMA,
    Recommendation,
    recommendation_from_output,
)
from coach.validation import BlessedRecommendation, validate_recommendation
from llm.factory import get_llm_gateway
from llm.port import LLMError, LLMGateway, LLMMessage, LLMRequest
from precedent import EvidenceKind, EvidenceRecord, find_precedent
from precedent.engine import DEFAULT_BENCHMARK

if TYPE_CHECKING:
    # Imported for type hints only. A runtime import here would create a cycle
    # (brokers.portfolio -> brokers.port -> coach.recommendation -> coach ->
    # coach.pipeline), which used to make brokers.portfolio un-importable as an
    # entry point. `from __future__ import annotations` keeps every PortfolioView
    # annotation below a lazy string, so no runtime import is needed.
    from brokers.portfolio import PortfolioView

logger = logging.getLogger("ballast.coach.pipeline")


@dataclass(frozen=True)
class CoachDecision:
    """The user-initiated decision that seeds the pipeline (pull-not-push).

    ``symbol`` is the instrument the user is asking about (defaults to the broad
    benchmark ``VTI``); ``question`` is the free-text ask ("should I invest $X?");
    ``amount`` is the optional contribution size as :class:`~decimal.Decimal`
    (NEVER binary float); ``as_of`` pins the precedent lookup to a calendar date
    (``None`` = latest data day, never the wall clock); ``side`` is the optional
    direction of the action the user is contemplating (``"buy"``/``"sell"``, or
    ``None`` when unstated) — the FR11 self-destructive-move detector reads it.
    Frozen so a decision is an immutable value.

    ``side`` defaults to ``None`` so a pre-4.5 ``CoachDecision`` is fully
    backward-compatible (no side → no warnings, identical behavior).
    """

    symbol: str = DEFAULT_BENCHMARK
    question: str = ""
    amount: Decimal | None = None
    as_of: date | None = None
    side: Literal["buy", "sell"] | None = None


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
    "5. Teach as you go: your reasoning must explain, in plain English, the "
    "principle and mechanics behind the recommended action tied to THIS decision "
    "— why it works, not just what to do (for example, why consistent index "
    "investing compounds over time, or why this is not market timing). Lead with "
    "the immediate why, then layer in the deeper lesson so the reader can keep "
    "reading without being interrupted. Stay patient and warm; never lecture, "
    "and never use jargon, hype, or alarm.\n"
    "6. If the request flags a potentially self-destructive move (for example "
    "selling into a downturn, concentrating too much in one holding, or "
    "committing a lump sum that dwarfs the portfolio), warn honestly and explain "
    "the risk in plain, calm English, then leave the decision to the user. Never "
    "refuse, block, or override the user's choice, and never be alarmist — you "
    "advise, the user decides.\n"
    "When there is no confident special call, the honest recommendation is to "
    "stick to the plan and make the regular contribution."
)


# --- FR11 self-destructive-move detection (deterministic, pure) ---------------

#: Over-concentration ceiling: a single post-trade holding taking more than this
#: share of total portfolio value (holdings market value + cash) is flagged as
#: over-concentrated. 0.40 is a coach heuristic — one position past ~40% of the
#: whole portfolio carries meaningful single-name risk for a beginner whose plan
#: is broad diversification. Tunable; NOT a guarantee. Decimal, never float.
CONCENTRATION_SHARE_CEILING: Decimal = Decimal("0.40")

#: Oversized-lump multiple: a single buy whose amount exceeds this multiple of
#: total portfolio value is flagged as an oversized lump. 0.50 is a coach
#: heuristic — a one-shot contribution larger than half the existing portfolio
#: is a big, lumpy bet whose timing risk is worth naming (spreading it out is
#: often calmer). Tunable; NOT a guarantee. Decimal, never float.
OVERSIZED_LUMP_MULTIPLE: Decimal = Decimal("0.50")


class WarningKind(str, Enum):
    """The closed set of FR11 self-destructive-move signals (v1).

    ``PANIC_SELL`` — selling into a live drawdown; ``OVER_CONCENTRATION`` — a buy
    that would push one holding past the concentration ceiling of portfolio value;
    ``OVERSIZED_LUMP`` — a buy whose amount dwarfs the portfolio. These are honest
    warnings the coach explains; they NEVER block (FR11).
    """

    PANIC_SELL = "panic-sell"
    OVER_CONCENTRATION = "over-concentration"
    OVERSIZED_LUMP = "oversized-lump"


@dataclass(frozen=True)
class MoveWarning:
    """A single deterministic FR11 warning descriptor (frozen, comparable).

    ``kind`` is the :class:`WarningKind`; ``risk`` is a short, plain-English,
    calm sentence naming the risk (embedded verbatim into the composed request's
    ``user_content`` so the LLM can warn, and folded into the code-authored
    default-plan reasoning). ``from_threshold`` marks warnings that fired from a
    numeric heuristic (concentration/lump) so the default plan can add the
    honest "these are heuristics, not guarantees" uncertainty.
    """

    kind: WarningKind
    risk: str
    from_threshold: bool = False


def _total_portfolio_value(portfolio: PortfolioView) -> Decimal:
    """Total portfolio value = sum of holding market values + cash (Decimal)."""
    return sum(
        (h.market_value for h in portfolio.holdings), start=Decimal("0")
    ) + portfolio.cash


def detect_self_destructive_moves(
    decision: CoachDecision,
    retrieved: Sequence[EvidenceRecord],
    portfolio: PortfolioView | None = None,
) -> tuple[MoveWarning, ...]:
    """Detect FR11 self-destructive moves — PURE and deterministic (no I/O).

    Returns a tuple of :class:`MoveWarning` descriptors for:

    - **panic-sell:** ``decision.side == "sell"`` AND a live drawdown is present
      (any retrieved record is an :attr:`~precedent.EvidenceKind.EVENT_PRECEDENT`).
      No portfolio needed.
    - **over-concentration:** ``decision.side == "buy"`` AND, given a
      ``portfolio``, the post-trade share of ``decision.symbol`` would exceed
      :data:`CONCENTRATION_SHARE_CEILING` of total portfolio value. Only when a
      portfolio is provided.
    - **oversized-lump:** ``decision.side == "buy"`` AND ``decision.amount``
      exceeds :data:`OVERSIZED_LUMP_MULTIPLE` of total portfolio value. Only when
      a portfolio is provided and ``amount`` is not ``None``.

    No wall-clock, no randomness, no network: identical
    ``(decision, retrieved, portfolio)`` → identical output. Money is
    :class:`~decimal.Decimal` throughout, never binary float. These are honest
    warnings only — the caller NEVER blocks on them (FR11).
    """
    warnings: list[MoveWarning] = []

    if decision.side == "sell" and any(
        r.kind is EvidenceKind.EVENT_PRECEDENT for r in retrieved
    ):
        warnings.append(
            MoveWarning(
                kind=WarningKind.PANIC_SELL,
                risk=(
                    "Selling into a downturn locks in the loss and takes you out "
                    "of the recovery the record shows tends to follow."
                ),
            )
        )

    if decision.side == "buy" and portfolio is not None:
        total_value = _total_portfolio_value(portfolio)
        if total_value > 0:
            existing_symbol_value = sum(
                (
                    h.market_value
                    for h in portfolio.holdings
                    if h.symbol == decision.symbol
                ),
                start=Decimal("0"),
            )
            amount = decision.amount or Decimal("0")
            post_symbol_value = existing_symbol_value + amount
            post_total_value = total_value + amount
            # Post-trade share of this one holding vs. the whole portfolio.
            if post_symbol_value > CONCENTRATION_SHARE_CEILING * post_total_value:
                warnings.append(
                    MoveWarning(
                        kind=WarningKind.OVER_CONCENTRATION,
                        risk=(
                            f"This buy would leave {decision.symbol} as an "
                            "outsized share of your portfolio, so a single "
                            "holding's swings would drive most of your results."
                        ),
                        from_threshold=True,
                    )
                )

            if (
                decision.amount is not None
                and decision.amount > OVERSIZED_LUMP_MULTIPLE * total_value
            ):
                warnings.append(
                    MoveWarning(
                        kind=WarningKind.OVERSIZED_LUMP,
                        risk=(
                            "This contribution is large next to your current "
                            "portfolio, so investing it all at one moment leans "
                            "heavily on the timing of that single day."
                        ),
                        from_threshold=True,
                    )
                )

    return tuple(warnings)


def _render_warnings(warnings: Sequence[MoveWarning]) -> str:
    """Render detected warnings as a calm, human-readable risk signal block."""
    lines = [f"- {w.risk}" for w in warnings]
    return "\n".join(lines)


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
    decision: CoachDecision,
    retrieved: Sequence[EvidenceRecord],
    warnings: Sequence[MoveWarning] = (),
) -> LLMRequest:
    """Compose the structured :class:`~llm.port.LLMRequest` (compose stage).

    The user message embeds the decision and each retrieved record (via its
    JSON-safe ``to_dict()``), so the LLM sees exactly the IDs it may cite. The
    request always carries :data:`RECOMMENDATION_OUTPUT_SCHEMA`, the coach-voice
    :data:`COACH_SYSTEM_PROMPT`, and a deterministic ``hard_reasoning`` flag.

    When ``warnings`` are detected (FR11), a calm, human-readable risk-signal
    block is embedded into ``user_content`` so the LLM can warn about the move
    per the system prompt's FR11 rule — the coach advises, never blocks. Pure:
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
    warning_block = (
        "\nPotentially self-destructive-move risks to warn about honestly "
        "(explain the risk calmly, then leave the choice to the user — do NOT "
        "refuse or block):\n"
        f"{_render_warnings(warnings)}\n"
        if warnings
        else ""
    )
    user_content = (
        "A user is asking for a recommendation.\n"
        f"Symbol: {decision.symbol}\n"
        f"Question: {decision.question or '(none given)'}\n"
        f"{amount_line}\n"
        f"{as_of_line}\n"
        f"{warning_block}\n"
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


def build_default_plan(
    retrieved: Sequence[EvidenceRecord],
    warnings: Sequence[MoveWarning] = (),
) -> BlessedRecommendation:
    """Build the deterministic, code-authored default plan and bless it (FR7/AD-4).

    The default plan is NOT LLM-derived: it cites EVERY retrieved evidence ID,
    carries non-empty coach-voice "stick to your plan" reasoning and ≥1 explicit
    uncertainty, sets ``order_intent=None``, and always passes the gate. Pure and
    deterministic: identical retrieved set (and ``warnings``) → equal (frozen)
    blessed object; no I/O, no LLM, no wall-clock, no randomness. This is the
    "never a dead-end" guarantee — always safe to return when the LLM path fails
    or offers no confident special call.

    When ``warnings`` are present (FR11), a calm, honest, coach-voice warning
    LEADS the ``reasoning`` and ``action_label`` — explaining the risk of the
    contemplated move in plain English, then leaving the decision to the user
    (never blocking). If any warning fired from a numeric threshold, an honest
    uncertainty is added noting those thresholds are coach heuristics, not
    guarantees — kept in the uncertainty slot as a genuine unknown, NEVER a
    smuggled benefit claim.
    """
    plan_reasoning = (
        "There's no confident special call here, and that's okay — the honest "
        "move is to stick to your plan and make your regular contribution. "
        "Here's why that works, not just what to do. The principle is simple: "
        "time in the market tends to beat timing it. Nobody, including me, can "
        "reliably predict the short-term swings, so trying to jump in and out "
        "means guessing right twice — when to leave and when to return — and "
        "getting either wrong usually costs more than staying put ever would. "
        "The mechanics are just as steady: making the same regular "
        "contribution on a schedule means you keep buying through both the "
        "high days and the low days, and those steady buys compound quietly "
        "over the years into most of your long-term growth. So the boring, "
        "consistent move — this is deliberately not market timing — is the one "
        "that tends to serve long-term investors best. Reacting to short-term "
        "noise feels productive, but it usually just adds cost and stress "
        "without improving the outcome, which is exactly why staying the "
        "course is the recommendation."
    )
    uncertainties = [
        "Markets can stay volatile longer than anyone expects, and past "
        "patterns never guarantee a future outcome — staying invested "
        "cannot promise a positive return.",
    ]

    action_label = "Stick to your plan: make your regular contribution"

    if warnings:
        # The warning LEADS — name the risk calmly, explain it, then hand the
        # decision back to the user. This never blocks (FR11): the plan is still
        # blessed and order_intent stays None.
        risk_sentences = " ".join(w.risk for w in warnings)
        warning_reasoning = (
            "Before you act, here's an honest word of caution. " + risk_sentences
            + " I'm not telling you not to — this is your call. I just want the "
            "risk in plain view so you can weigh it calmly. With that said, "
            "here's how I'd think about the steadier path. "
        )
        plan_reasoning = warning_reasoning + plan_reasoning
        action_label = "A caution before you act — then, if you like, stick to your plan"
        if any(w.from_threshold for w in warnings):
            uncertainties.append(
                "The thresholds behind this caution are coach heuristics, not "
                "guarantees — they flag a risk worth weighing, but they cannot "
                "tell you what the market will do."
            )

    candidate = Recommendation(
        action_label=action_label,
        reasoning=plan_reasoning,
        evidence=tuple(record.id for record in retrieved),
        uncertainties=tuple(uncertainties),
        order_intent=None,
    )
    return validate_recommendation(candidate, retrieved)


def surface(
    gateway: LLMGateway,
    decision: CoachDecision,
    retrieved: Sequence[EvidenceRecord],
    warnings: Sequence[MoveWarning] = (),
) -> BlessedRecommendation:
    """Return the surfaceable recommendation — the resilience boundary (FR7/AD-4).

    Wraps compose → ``gateway.complete`` → ``recommendation_from_output`` →
    ``validate_recommendation`` in a single ``try``. On ANY ``Exception`` (gateway
    error, unparseable output, or gate rejection — a
    :class:`~coach.validation.RecommendationValidationError` is an ``Exception``)
    it returns :func:`build_default_plan`. The default builder runs OUTSIDE the
    ``try``, so a bug there still surfaces rather than being silently masked.

    Detected FR11 ``warnings`` are threaded into BOTH paths: the risk signal into
    the composed request (LLM path) and the code-authored warning content into
    the default plan (fallback). Warnings never block — any LLM-emitted
    ``order_intent`` still carries through unchanged.
    """
    try:
        response = gateway.complete(compose_request(decision, retrieved, warnings))
        return validate_recommendation(
            recommendation_from_output(response.output), retrieved
        )
    except Exception as exc:
        # KEEP the deliberately-broad net: it MUST still catch
        # RecommendationValidationError (the "valid JSON, fabricated evidence id"
        # arm) as well as every typed LLMError the hardened adapter now raises.
        # Silent degradation of a trust-critical path is unobservable; log a
        # structured warning (no secrets, no raw tokens — just provider + error
        # type, and whether it was a typed gateway failure) so a coach that has
        # quietly stopped making special calls is visible in production. Matches
        # find_precedent's degrade-warning style.
        logger.warning(
            "coach_llm_path_failed_fallback_to_default provider=%s error=%s "
            "llm_gateway_error=%s",
            getattr(gateway, "provider", "unknown"),
            type(exc).__name__,
            isinstance(exc, LLMError),
        )
        return build_default_plan(retrieved, warnings)  # never a dead-end


async def run_coach_pipeline(
    session: AsyncSession,
    decision: CoachDecision,
    *,
    gateway: LLMGateway | None = None,
    portfolio: PortfolioView | None = None,
) -> BlessedRecommendation:
    """Run the full pipeline for a decision and return a blessed recommendation.

    ``retrieve`` via :func:`~precedent.find_precedent` (async), then ``compose →
    validate → surface`` synchronously. ``gateway`` defaults to
    :func:`~llm.factory.get_llm_gateway` (the fake adapter unless configured),
    injectable for tests and the future ask→approve surface (4.6). Always returns
    a :class:`~coach.validation.BlessedRecommendation`; never raises for a missing
    special call.

    ``portfolio`` is an OPTIONAL read-only snapshot the CALLER passes (the
    pipeline never fetches a live portfolio, threads a :class:`~db.scope.Scope`,
    or handles degraded/all-cash — that is Story 4.6's ask→approve concern). When
    provided, it feeds :func:`detect_self_destructive_moves` so FR11 warnings
    surface. Backward compatible: no ``side``/no ``portfolio`` → empty warnings →
    identical behavior to pre-4.5.
    """
    retrieved = tuple(
        await find_precedent(session, symbol=decision.symbol, as_of=decision.as_of)
    )
    warnings = detect_self_destructive_moves(decision, retrieved, portfolio)
    gateway = gateway or get_llm_gateway()
    return surface(gateway, decision, retrieved, warnings)
