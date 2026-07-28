"""The recommendation validation gate — the NFR2 structural teeth (FR12/FR13/FR14).

This module is the SOLE producer of a :class:`BlessedRecommendation`. A blessed
object is physically un-surfaceable unless it came through
:func:`validate_recommendation`: the frozen type's ``__post_init__`` requires a
module-private sentinel (:data:`_GATE_KEY`, never exported) that only the gate
holds, so direct construction from any other module raises
:class:`RecommendationValidationError` (mirrors how :mod:`llm.port` makes a
schema-less LLM call un-issuable).

The gate is PURE and SYNCHRONOUS: no I/O, no DB, no network, no LLM, no wall-clock,
no randomness — identical inputs give an equal, frozen blessed object. It rejects
(raising a typed error, never returning) a candidate that is missing reasoning,
missing uncertainties, or citing an evidence ID absent from the retrieved set;
otherwise it resolves each cited ID to its real :class:`~precedent.evidence.EvidenceRecord`
(preserving cite order) — the snapshot 4.9 will persist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from coach.recommendation import OrderIntent, Recommendation
from precedent.evidence import EvidenceRecord


class RecommendationValidationError(ValueError):
    """Base for every gate rejection — a candidate that must not be surfaced.

    Also raised by :class:`BlessedRecommendation.__post_init__` on a direct
    construction attempt (missing the private gate sentinel), the structural
    teeth for NFR2.
    """


class MissingReasoningError(RecommendationValidationError):
    """Raised when a candidate's ``reasoning`` is empty or whitespace-only (FR12)."""


class MissingUncertaintiesError(RecommendationValidationError):
    """Raised when a candidate carries no explicit uncertainties (FR14)."""


class UnbackedEvidenceError(RecommendationValidationError):
    """Raised when evidence is empty OR cites an ID not in the retrieved set (FR13)."""


#: Module-private sentinel; NEVER exported. Only the gate (this module) holds it,
#: so only :func:`validate_recommendation` can construct a blessed object.
_GATE_KEY = object()


@dataclass(frozen=True)
class BlessedRecommendation:
    """A validated, surfaceable recommendation — producible ONLY by the gate.

    Identical to a candidate except ``evidence`` is the tuple of RESOLVED real
    :class:`~precedent.evidence.EvidenceRecord`s (cite order preserved) — the
    snapshot 4.9 persists. ``_gate_key`` is the private construction guard: it is
    excluded from ``repr``/equality and must be :data:`_GATE_KEY`, else
    ``__post_init__`` raises :class:`RecommendationValidationError`. Frozen, so a
    blessed object cannot mutate after the fact.
    """

    action_label: str
    order_intent: OrderIntent | None
    reasoning: str
    evidence: tuple[EvidenceRecord, ...]
    uncertainties: tuple[str, ...]
    _gate_key: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._gate_key is not _GATE_KEY:
            raise RecommendationValidationError(
                "A BlessedRecommendation may be produced only by "
                "validate_recommendation(); direct construction is forbidden (NFR2)."
            )


def validate_recommendation(
    candidate: Recommendation, retrieved: Sequence[EvidenceRecord]
) -> BlessedRecommendation:
    """Bless a candidate or reject it with a typed error (the single trust gate).

    Checks in order: ``reasoning`` must be non-empty and non-whitespace
    (:class:`MissingReasoningError`); ``uncertainties`` must be non-empty
    (:class:`MissingUncertaintiesError`); ``evidence`` must be non-empty AND every
    cited ID present in ``{r.id for r in retrieved}``
    (:class:`UnbackedEvidenceError`). On success, resolves each cited ID to its
    real :class:`~precedent.evidence.EvidenceRecord` preserving cite order and
    returns a frozen :class:`BlessedRecommendation`. Pure and deterministic.
    """
    if not candidate.reasoning or not candidate.reasoning.strip():
        raise MissingReasoningError(
            "A recommendation must carry plain-English reasoning (FR12)."
        )
    # At least one uncertainty with real content — mirror reasoning's strip check so
    # a tuple of blank strings cannot satisfy FR14 (structurally present ≠ explicit).
    if not any(u and u.strip() for u in candidate.uncertainties):
        raise MissingUncertaintiesError(
            "A recommendation must state at least one explicit uncertainty (FR14)."
        )
    if not candidate.evidence:
        raise UnbackedEvidenceError(
            "A recommendation must cite at least one real evidence record; the "
            "strategy default is always available (FR13)."
        )

    by_id = {record.id: record for record in retrieved}
    unbacked = [cited for cited in candidate.evidence if cited not in by_id]
    if unbacked:
        raise UnbackedEvidenceError(
            "A recommendation may cite only IDs present in the retrieved set; "
            f"unbacked cited IDs: {unbacked} (FR13)."
        )

    # Dedupe cited IDs, preserving first-cite order, so a repeated citation cannot
    # inflate the evidence the 4.9 snapshot persists.
    resolved = tuple(by_id[cited] for cited in dict.fromkeys(candidate.evidence))
    return BlessedRecommendation(
        action_label=candidate.action_label,
        order_intent=candidate.order_intent,
        reasoning=candidate.reasoning,
        evidence=resolved,
        uncertainties=candidate.uncertainties,
        _gate_key=_GATE_KEY,
    )
