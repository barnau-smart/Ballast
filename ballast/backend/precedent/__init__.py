"""The Precedent Engine — the sole, deterministic source of market statistics (AD-3).

Ballast's coach may never fabricate a market fact (FR13); every precedent claim is
computed here, over real ``market_daily`` history, with NO LLM, NO network, NO
randomness, and NO wall-clock read in the matching path. The engine returns claims
as :class:`~precedent.evidence.EvidenceRecord`\\s of the fixed AD-12 shape
``{id, kind, statement, stats, source, as_of}`` — and NEVER an empty list (the
``strategy`` fallback is always available). Epic 4 cites and immutably snapshots
these records by their deterministic ``id``.
"""

from precedent.engine import find_precedent
from precedent.evidence import EvidenceKind, EvidenceRecord
from precedent.missed_growth import MissedGrowthEstimate, estimate_missed_growth

__all__ = [
    "find_precedent",
    "EvidenceRecord",
    "EvidenceKind",
    "estimate_missed_growth",
    "MissedGrowthEstimate",
]
