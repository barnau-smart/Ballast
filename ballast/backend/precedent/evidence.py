"""The AD-12 Evidence Record Contract — the fixed shape Epic 4 cites and snapshots.

Every market claim Ballast's coach makes travels as an :class:`EvidenceRecord` of
EXACTLY the six top-level fields ``{id, kind, statement, stats, source, as_of}``
(AD-12). ``kind`` is one of :class:`EvidenceKind`. The recommendation validator and
the immutable decision snapshot (Epic 4 / AD-5) depend on this shape never
drifting — do NOT add, remove, or rename a top-level field. Per-window drill-down
detail lives INSIDE ``stats.windows`` (``stats`` is an object), so the 6-field
shape stays intact.

Money and percentages are ``decimal.Decimal`` (NEVER binary float); day counts are
``int``; ``as_of`` is a calendar :class:`datetime.date` (ISO-8601 on the wire).

The ``id`` is a DETERMINISTIC content hash (:func:`make_id`) of the record's
identity (``kind``, ``symbol``, ``as_of``, ``stats``). Snapshotting a record into an
immutable decision therefore captures a stable, reproducible id: the same
``(symbol, as_of)`` over the same ``market_daily`` rows yields a byte-identical id,
so a decision never needs the numbers re-derived to be replayed (AD-5).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any

from money import format_money


class EvidenceKind(str, Enum):
    """The closed set of evidence kinds (AD-12).

    ``EVENT_PRECEDENT`` — an aggregate of historically similar drawdown episodes.
    ``STRATEGY`` — the always-available default-plan fallback (no comparable drop,
    or insufficient data). One of these is ALWAYS returned; the engine never
    dead-ends on an empty list.
    """

    EVENT_PRECEDENT = "event-precedent"
    STRATEGY = "strategy"


def _json_safe(value: Any) -> Any:
    """Recursively convert a value into a JSON-serializable, deterministic form.

    ``Decimal`` → its fixed-point string via :func:`money.format_money` (never a
    lossy float, never ``E+``/``E-`` exponent — so extreme/tiny evidence stats
    stay plain on the wire), ``date`` → ISO-8601 string, :class:`EvidenceKind`
    (and any ``Enum``) → its value. Dicts and lists are converted element-wise,
    preserving structure.
    """
    if isinstance(value, Decimal):
        return format_money(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def make_id(kind: EvidenceKind, symbol: str, as_of: date, stats: dict) -> str:
    """Return a deterministic, content-addressed id for an evidence record.

    The id is a SHA-256 over the CANONICAL JSON of the record's identity
    (``kind``, ``symbol``, ``as_of``, ``stats``) — sorted keys, no whitespace,
    JSON-safe values — truncated to 12 hex chars and prefixed ``ep-`` for an
    event-precedent record or ``strat-`` for a strategy record. Identical inputs
    always produce an identical id (load-bearing for AD-5 immutable replay).
    """
    payload = {
        "kind": kind.value,
        "symbol": symbol,
        "as_of": as_of.isoformat(),
        "stats": _json_safe(stats),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    prefix = "ep-" if kind is EvidenceKind.EVENT_PRECEDENT else "strat-"
    return f"{prefix}{digest}"


@dataclass(frozen=True)
class EvidenceRecord:
    """The fixed AD-12 evidence shape — EXACTLY these six fields.

    ``id`` is the deterministic content hash (:func:`make_id`); ``kind`` is an
    :class:`EvidenceKind`; ``statement`` is the human-readable, coach-facing
    sentence; ``stats`` is the object carrying the computed numbers (and, for an
    event-precedent, the per-episode ``windows`` list); ``source`` is a provenance
    string; ``as_of`` is the calendar date the record describes.

    Frozen so a snapshotted record cannot mutate after the fact (AD-5).
    """

    id: str
    kind: EvidenceKind
    statement: str
    stats: dict
    source: str
    as_of: date

    def to_dict(self) -> dict:
        """Return a JSON-safe dict of the record (Decimal→str, date→ISO, enum→value).

        The six top-level keys are preserved verbatim; nested ``stats`` values are
        converted recursively so the result is directly ``json.dumps``-able and
        deterministic.
        """
        return {
            "id": self.id,
            "kind": self.kind.value,
            "statement": self.statement,
            "stats": _json_safe(self.stats),
            "source": self.source,
            "as_of": self.as_of.isoformat(),
        }
