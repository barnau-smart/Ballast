"""The canonical Recommendation contract — the coach's structured output (AD-2/AD-3).

A :class:`Recommendation` is the UNVALIDATED candidate the LLM composes: exactly
``{action_label, order_intent?, reasoning, evidence[], uncertainties[]}``. Its
``evidence`` is a tuple of CITED evidence-ID strings — the coach cites only IDs it
was handed by the Precedent Engine and never invents a record. Turning a candidate
into a surfaceable, evidence-resolved :class:`~coach.validation.BlessedRecommendation`
is the sole job of the validation gate in :mod:`coach.validation`; this module holds
no validation logic.

All value objects are ``@dataclass(frozen=True)``; money (``amount``) is
``decimal.Decimal`` (NEVER binary float); ``side`` is an :class:`OrderSide` enum.
Style mirrors :mod:`precedent.evidence` and :mod:`llm.port`.

:data:`RECOMMENDATION_OUTPUT_SCHEMA` is the JSON Schema the LLM Gateway will emit
against (a future ``LLMRequest.output_schema``); :func:`recommendation_from_output`
maps a raw output dict onto a candidate TOLERANTLY — it never raises on missing
keys, mapping them to empty fields so the gate stays the single rejection point.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any


class OrderSide(str, Enum):
    """The closed set of order directions for v1 (AD scope: buy/hold + guided sell).

    ``BUY`` — acquire a position; ``SELL`` — reduce/exit (coach-guided rebalancing
    only). No options, shorting, or complex orders in v1.
    """

    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    """The order-execution type (Story 8.1 — order-interface expansion).

    ``MARKET`` fills at the prevailing price (the v1 default); ``LIMIT`` caps the
    price via an explicit ``limit_price`` (Story A supports only MARKETABLE limits
    — they fill immediately). ``STOP``/``STOP_LIMIT`` are forward-compat enum
    members REJECTED at the gate until Story B. String-backed to mirror
    :class:`OrderSide` and serialize cleanly into JSON snapshots.
    """

    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class Session(str, Enum):
    """The trading session an order is valid in (Story 8.1).

    ``REGULAR`` is the standard market session (the v1 default). ``AM``/``PM`` are
    the extended (pre-/post-market) sessions — forward-compat members REJECTED at
    the gate until Story B.
    """

    REGULAR = "regular"
    AM = "am"
    PM = "pm"


class Duration(str, Enum):
    """How long an order stays working (Story 8.1).

    ``DAY`` expires at the close of the trading day (the v1 default). ``GTC``
    (good-till-canceled) rests across sessions — a forward-compat member REJECTED
    at the gate until Story B (resting-order lifecycle).
    """

    DAY = "day"
    GTC = "gtc"


@dataclass(frozen=True)
class OrderIntent:
    """The optional typed executable payload handed to the Broker Port.

    ``symbol`` is the instrument; ``side`` is an :class:`OrderSide`; ``amount`` is
    ``Decimal`` (never float). The order-model fields (Story 8.1) are OPTIONAL and
    DEFAULTED so every existing 3-arg construction (positional or keyword) stays
    valid and existing MARKET flows are byte-for-byte unchanged: ``order_type``
    defaults to :attr:`OrderType.MARKET`, ``session`` to :attr:`Session.REGULAR`,
    ``duration`` to :attr:`Duration.DAY`, and ``limit_price``/``stop_price`` to
    ``None``. Money fields are ``Decimal`` (never float). The field-requirement
    matrix + deferred-feature rejection is enforced at placement time by
    :func:`coach.execution.validate_order_intent`, not here — this is only the
    structural payload shape.
    """

    symbol: str
    side: OrderSide
    amount: Decimal
    order_type: OrderType = OrderType.MARKET
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    session: Session = Session.REGULAR
    duration: Duration = Duration.DAY


@dataclass(frozen=True)
class Recommendation:
    """An UNVALIDATED coach recommendation candidate (AD-2).

    ``action_label`` is the human-readable call; ``reasoning`` is the plain-English
    "why" (also the just-in-time teaching — one field, not a subsystem);
    ``evidence`` is the tuple of CITED evidence-ID strings (resolved to real records
    only by the gate); ``uncertainties`` is what is explicitly unknown;
    ``order_intent`` is the optional typed payload.

    Frozen so a candidate is an immutable value. This object carries NO trust
    guarantees — only :func:`~coach.validation.validate_recommendation` blesses it.
    """

    action_label: str
    reasoning: str
    evidence: tuple[str, ...]
    uncertainties: tuple[str, ...]
    order_intent: OrderIntent | None = None


#: The canonical JSON Schema the LLM emits (a future ``LLMRequest.output_schema``).
#: ``additionalProperties: false`` and the four required fields are the structural
#: contract; ``order_intent`` is the only optional (nested) property.
RECOMMENDATION_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action_label": {"type": "string"},
        "reasoning": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "uncertainties": {"type": "array", "items": {"type": "string"}},
        "order_intent": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "side": {"type": "string", "enum": ["buy", "sell"]},
                "amount": {"type": "string"},
            },
            "required": ["symbol", "side", "amount"],
            "additionalProperties": False,
        },
    },
    "required": ["action_label", "reasoning", "evidence", "uncertainties"],
    "additionalProperties": False,
}


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    """Coerce a raw output value into a tuple of strings, TOLERANTLY.

    A JSON array maps element-wise (each element stringified); a BARE string is
    wrapped as a single element (never iterated char-by-char); anything else —
    missing, ``None``, or a scalar — maps to an empty tuple. Never raises, so a
    malformed shape reaches the gate as an empty field rather than crashing here.
    """
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value)
    return ()


def recommendation_from_output(output: dict) -> Recommendation:
    """Map a raw LLM output dict onto an unvalidated :class:`Recommendation`.

    TOLERANT by design: NO exception is raised here — missing keys map to empty
    fields, a bare-string ``evidence``/``uncertainties`` is wrapped (not
    char-split), and a malformed ``order_intent`` (unknown ``side`` or non-numeric
    ``amount``) drops to ``None``. This keeps the validation gate the SINGLE point
    where trust invariants are enforced (a malformed output becomes a candidate
    the gate then rejects). ``order_intent`` is parsed into an :class:`OrderIntent`
    ONLY when the key is present AND a dict AND its fields are well-formed.
    """
    order_intent = None
    raw_intent = output.get("order_intent")
    if isinstance(raw_intent, dict):
        try:
            # DELIBERATE (Story 8.1): the LLM contract stays MARKET-only. Only
            # symbol/side/amount are parsed from the coach output; the new
            # order-model fields take their MARKET/REGULAR/DAY/None defaults. This
            # is what structurally enforces the locked decision "the LLM coach
            # never sets a limit price" — limit fields are human-entered on the
            # /approve path, never proposable by the model.
            order_intent = OrderIntent(
                symbol=str(raw_intent.get("symbol", "")),
                side=OrderSide(raw_intent.get("side", OrderSide.BUY.value)),
                amount=Decimal(str(raw_intent.get("amount", "0"))),
            )
        except (ValueError, ArithmeticError):
            # Unknown side (ValueError) or non-numeric amount
            # (decimal.InvalidOperation ⊂ ArithmeticError): stay tolerant, drop the
            # malformed intent — order semantics are validated later (4.6), not here.
            order_intent = None
    return Recommendation(
        action_label=output.get("action_label") or "",
        reasoning=output.get("reasoning") or "",
        evidence=_as_str_tuple(output.get("evidence")),
        uncertainties=_as_str_tuple(output.get("uncertainties")),
        order_intent=order_intent,
    )
