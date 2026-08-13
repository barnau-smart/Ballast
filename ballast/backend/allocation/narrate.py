"""The fiduciary-advisor narration layer over the 10-2 deploy plan (Story 10.3).

Story 10-2 hands back a deterministic *"deploy my cash"* :class:`~allocation.engine.Plan`
— concrete BUYs toward the chosen target — but it is silent. This layer adds the
advisor VOICE: it explains the *why / tradeoff / prioritization* as situational
opinion while the engine still owns every number, guarded by three gates so the
AI can neither invent a fact nor forecast the market.

Design guardrails (locked, non-negotiable — mirror :mod:`coach.suggest`):

- **Pure + resilient.** :func:`build_narration_facts`, :func:`allowed_facts`,
  :func:`check_no_invented_numbers`, :func:`check_no_forecast`, and
  :func:`_fallback_narration` are pure functions of the :class:`~allocation.engine.Plan`
  (no I/O, no wall-clock, no RNG). :func:`narrate_plan` mirrors
  :func:`~coach.suggest.narrate_suggestion`: on ANY exception it silently degrades
  to :func:`_fallback_narration` — a narration outage never crashes, never blocks,
  and never surfaces an unvalidated fact.
- **Never invent a fact.** Evidence records (``EvidenceKind.STRATEGY``) are built
  purely from the plan; the LLM narration passes through
  :func:`~coach.validation.validate_recommendation` verbatim (reasoning non-empty,
  ≥1 uncertainty, ≥1 cited engine-provided evidence ID) PLUS
  :func:`check_no_invented_numbers` (every stated figure ∈ the engine allow-set)
  PLUS :func:`check_no_forecast`. Any rejection → the deterministic template.
- **Opinion, not forecast.** The advisor may opine on the user's situation and
  settled principles; the no-forecast gate rejects prediction language. No market
  forecasts, ever.
- **"Nothing to do" is valid.** For every no-action status the narration is a calm
  deterministic ``plan.reason`` passthrough — NO LLM call, no fabricated move.
- **Deterministic reuse → free fake fallback.** ``FakeLLMGateway`` fills
  ``evidence`` with a placeholder (unbacked) ID → :class:`~coach.validation.UnbackedEvidenceError`
  → :func:`_fallback_narration`, so fake/degraded mode yields real templated copy
  exactly as the coach pipeline gets it via ``build_default_plan``.
- **Populate, don't submit.** This layer PLACES NOTHING and writes no
  ``decision_record`` — it only narrates a plan the human co-signs through the
  existing ``/approve`` spine.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from allocation.engine import Plan
from coach.recommendation import recommendation_from_output
from coach.validation import validate_recommendation
from llm.port import LLMGateway, LLMMessage, LLMRequest
from money import format_money
from precedent.evidence import EvidenceKind, EvidenceRecord, make_id
from strategy.target_allocation import (
    ASSET_CLASS_LABEL,
    ASSET_CLASSES,
    BONDS,
)

_ZERO = Decimal("0")
_HUNDRED = Decimal("100")

#: The provenance string on every narration evidence record — the deterministic
#: 10-2 engine, never a market feed (these are the user's own facts).
_EVIDENCE_SOURCE = "allocation-engine"

#: The forecast-word list (named + module-level for tuning). Matched with word
#: boundaries (case-insensitively) against ``reasoning`` + ``action_label`` +
#: ``uncertainties``; ANY hit degrades the narration to the deterministic template.
#: Deliberately focused on prediction language, NOT on plain situational opinion
#: (which the advisor is allowed). This is a best-effort denylist, NOT an exhaustive
#: proof of no-forecast — the failure mode of a miss is bounded by the numeric gate
#: + the calm system prompt; add terms here as real narrations reveal gaps.
FORECAST_TERMS: tuple[str, ...] = (
    "will rise",
    "will fall",
    "will grow",
    "will drop",
    "will climb",
    "will beat",
    "will double",
    "will make",
    "will appreciate",
    "should rise",
    "should grow",
    "should climb",
    "going to",
    "expect",
    "expected",
    "expects",
    "anticipate",
    "forecast",
    "predict",
    "projected",
    "projection",
    "likely to",
    "poised to",
    "set to",
    "tends to",
    "tend to",
    "outperform",
    "beat the market",
    "rally",
    "crash",
    "bull market",
    "bear market",
    "guaranteed return",
    "guaranteed profit",
    "make money",
    "double your",
    "triple your",
    "headed higher",
    "headed lower",
    "next year",
    "over the long run",
    "in the long run",
    "target price",
    # Modal-hedge PREDICTIONS. Directional phrases only (never bare "could"/"may"/
    # "might", which appear in benign coaching like "you may want to consider" and
    # would over-degrade every narration to the template). A miss here is bounded
    # exactly like the rest of this list; add directional combos as gaps surface.
    "could rise",
    "could grow",
    "could climb",
    "could double",
    "could triple",
    "could gain",
    "could go up",
    "could increase",
    "may rise",
    "may grow",
    "may climb",
    "may double",
    "may gain",
    "may increase",
    "might rise",
    "might grow",
    "might climb",
    "might double",
    "might gain",
    "should do well",
    "should outperform",
    "should beat",
    "on track to",
    "on track for",
    "poised for",
)

#: The narration output schema — the ``RECOMMENDATION_OUTPUT_SCHEMA`` MINUS
#: ``order_intent`` (the LLM narrates only; the engine owns the order). All four
#: fields required, ``additionalProperties: false``.
NARRATION_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "action_label": {"type": "string"},
        "reasoning": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "uncertainties": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["action_label", "reasoning", "evidence", "uncertainties"],
    "additionalProperties": False,
}

_NARRATION_SYSTEM = (
    "You are Ballast's calm, honest, fiduciary investing coach. You are handed a "
    "deploy-my-cash plan that has ALREADY been computed — every number is final "
    "and you must NOT change, recompute, or invent one. Your job is to explain, in "
    "plain warm English, the WHY, the TRADEOFF, and the PRIORITIZATION as "
    "situational opinion grounded in settled principles (diversification, "
    "rebalancing toward a chosen target, buying broad low-cost index funds). Cite "
    "ONLY the evidence IDs you are given. State numbers ONLY exactly as provided, "
    "and write every quantity in DIGITS as given (e.g. $3,000.00 or 30%), never "
    "spelled out and never rounded. NEVER forecast or predict the market, name a "
    "hot pick, or promise a return. Always name at least one honest uncertainty."
)

# A money-ish token: an optional leading sign, an optional ``$``, digits with
# optional thousands commas, an optional decimal tail, and an optional trailing
# ``%``. The sign is captured so a flipped value (e.g. ``-5%``) is compared as
# ``-5`` — NOT silently laundered into the unsigned ``5`` and matched against a
# same-magnitude engine weight (a false-ACCEPT the never-invent gate must stop).
_NUMBER_TOKEN_RE = re.compile(r"-?\$?\d[\d,]*(?:\.\d+)?%?")

#: Unit tags for the never-invent gate (Story 10.6). A number token is classified by
#: its surface form — a ``$`` → :data:`UNIT_MONEY`, a trailing ``%`` → :data:`UNIT_PERCENT`,
#: otherwise a bare count → :data:`UNIT_BARE` — and matched ONLY against an allow-set
#: entry of the SAME unit. This closes the value-only laundering hole where a
#: fabricated bare count or dollar figure passed just because its magnitude coincided
#: with a real weight-percent (e.g. "30 companies" when 30% is a target weight).
UNIT_MONEY = "money"
UNIT_PERCENT = "percent"
UNIT_BARE = "bare"


class NarrationValidationError(ValueError):
    """Raised by the never-invent-a-fact / no-forecast gates on a violation.

    A safety NET, not a surfaced error: :func:`narrate_plan` catches it (like any
    exception) and degrades to :func:`_fallback_narration` — so an over-strict
    match is safe (a false positive just yields honest templated copy).
    """


@dataclass(frozen=True)
class AllocationNarration:
    """The advisor narration over a deploy plan — the surfaceable shape.

    ``action_label``/``reasoning`` are the plain-English call + why; ``uncertainties``
    is what is explicitly unknown (≥1); ``evidence`` is the tuple of RESOLVED
    :class:`~precedent.evidence.EvidenceRecord`s the narration cites; ``status`` is
    the originating :class:`~allocation.engine.Plan` status. Frozen — an immutable
    value the API serializes (evidence via ``record.to_dict()``)."""

    action_label: str
    reasoning: str
    uncertainties: tuple[str, ...]
    evidence: tuple[EvidenceRecord, ...]
    status: str


# --- Evidence + allow-set (pure, built from the plan) ------------------------


def _plan_as_of(plan: Plan) -> date:
    """Return the calendar date for the plan's evidence records (pure).

    ``plan.as_of`` is a :class:`~datetime.datetime` (portfolio snapshot time);
    take its ``.date()``. Guard a missing/odd value (``None`` or already a bare
    ``date``) so :func:`make_id`'s ``as_of.isoformat()`` never blows up on the
    resilient path."""
    as_of = plan.as_of
    if isinstance(as_of, datetime):
        return as_of.date()
    if isinstance(as_of, date):
        return as_of
    return date(1970, 1, 1)


def build_narration_facts(plan: Plan) -> tuple[EvidenceRecord, ...]:
    """Build the ``EvidenceKind.STRATEGY`` evidence records for a deploy plan (pure).

    One per-action-item record (statement names the asset-class label + the dollar
    amount; ``stats`` carries the raw ``Decimal``s — the amount + that class's
    current weight, both facts drawn straight from the plan) PLUS one PORTFOLIO-level
    summary record (investable / undeployed cash + each class's current weight —
    the honest current picture). The specific target *weight* is not restated as a
    per-record fact here; the user's own resolved target weights are admitted to the
    allow-set from ``plan.target_weights`` (see :func:`allowed_facts`). Every id is
    content-addressed via :func:`make_id` over ``(kind, symbol, as_of, stats)`` so
    the same plan yields byte-identical ids (load-bearing for the LLM to cite them).
    For any no-action status there is nothing to deploy → an EMPTY tuple."""
    if plan.status != "deploy":
        return ()

    as_of = _plan_as_of(plan)

    records: list[EvidenceRecord] = []
    for item in plan.action_items:
        label = ASSET_CLASS_LABEL.get(item.asset_class, item.asset_class)
        current_weight = plan.current.get(item.asset_class, {}).get("weight", _ZERO)
        stats = {
            "amount": item.amount,
            "current_weight": current_weight,
        }
        records.append(
            EvidenceRecord(
                id=make_id(EvidenceKind.STRATEGY, item.symbol, as_of, stats),
                kind=EvidenceKind.STRATEGY,
                statement=(
                    f"You're underweight {label} versus your target mix — buying "
                    f"{format_money(item.amount)} of {item.symbol} moves you toward "
                    "that balance."
                ),
                stats=stats,
                source=_EVIDENCE_SOURCE,
                as_of=as_of,
            )
        )

    # Portfolio-level summary: investable/undeployed cash + current vs target per
    # class (the honest "where you are vs where you're aiming" picture).
    summary_stats: dict = {
        "investable_cash": plan.investable_cash,
        "undeployed_cash": plan.undeployed_cash,
    }
    for cls in ASSET_CLASSES:
        summary_stats[f"{cls}_current_weight"] = plan.current.get(cls, {}).get(
            "weight", _ZERO
        )
    records.append(
        EvidenceRecord(
            id=make_id(EvidenceKind.STRATEGY, "PORTFOLIO", as_of, summary_stats),
            kind=EvidenceKind.STRATEGY,
            statement=(
                f"You have {format_money(plan.investable_cash)} of investable cash; "
                "this plan puts the underweight classes back toward your chosen "
                "target mix and leaves any leftover cash undeployed."
            ),
            stats=summary_stats,
            source=_EVIDENCE_SOURCE,
            as_of=as_of,
        )
    )
    return tuple(records)


def _add_money(allowed: set[tuple[Decimal, str]], value: Decimal) -> None:
    """Admit a money amount as BOTH a ``$``-prefixed citation (:data:`UNIT_MONEY`)
    and a bare-digits citation (:data:`UNIT_BARE`) (pure, in-place). The deterministic
    fallback renders amounts via ``format_money`` (no ``$``: ``"3000.00"``) while the
    LLM is instructed to write ``"$3,000.00"`` — both are legitimate citations of the
    same engine amount. Admitting the bare form does NOT reopen the weight-percent
    laundering (weight percents are tagged :data:`UNIT_PERCENT`, never bare)."""
    allowed.add((value, UNIT_MONEY))
    allowed.add((value, UNIT_BARE))


def _add_weight_forms(allowed: set[tuple[Decimal, str]], weight: Decimal) -> None:
    """Admit a weight as the fraction (``0.60`` → :data:`UNIT_BARE`) AND the 0–100
    percent (``60`` → :data:`UNIT_PERCENT`) forms the narration might use (pure,
    in-place). Tagging keeps a bare integer that merely equals the percent form from
    matching the percent entry (Story 10.6)."""
    allowed.add((weight, UNIT_BARE))
    allowed.add((weight * _HUNDRED, UNIT_PERCENT))


def allowed_facts(plan: Plan) -> frozenset[tuple[Decimal, str]]:
    """The engine-provided numeric allow-set for :func:`check_no_invented_numbers`.

    Every number the AI may legitimately state, tagged by UNIT as a ``(Decimal,
    unit)`` pair (Story 10.6): each action-item amount + the investable/undeployed
    cash + each current sleeve market value as :data:`UNIT_MONEY`; each current &
    target weight as BOTH the fraction (``Decimal("0.60")`` → :data:`UNIT_BARE`) and
    the 0–100 percent (``Decimal("60")`` → :data:`UNIT_PERCENT`). The target weights
    come from ``plan.target_weights`` — the user's OWN resolved model (never a
    cross-model union): admitting another model's share would let the AI state a
    wrong-but-plausible target as a fact, exactly the false-accept the
    never-invent-a-fact gate exists to stop. The recognized stock/bond SPLIT (sum of
    the equity classes vs the bond weight) is also admitted so the natural framing
    "90% stocks, 10% bonds" is citable rather than needlessly degraded. Pure."""
    allowed: set[tuple[Decimal, str]] = set()

    for item in plan.action_items:
        _add_money(allowed, item.amount)

    _add_money(allowed, plan.investable_cash)
    _add_money(allowed, plan.undeployed_cash)

    for vals in plan.current.values():
        market_value = vals.get("market_value", _ZERO)
        _add_money(allowed, market_value)
        _add_weight_forms(allowed, vals.get("weight", _ZERO))

    # The user's OWN target weights (per class) + the recognized stock/bond split.
    equity_weight = _ZERO
    for cls, weight in plan.target_weights.items():
        _add_weight_forms(allowed, weight)
        if cls != BONDS:
            equity_weight += weight
    if plan.target_weights:
        _add_weight_forms(allowed, equity_weight)
        _add_weight_forms(allowed, plan.target_weights.get(BONDS, _ZERO))

    return frozenset(allowed)


def _classify_number_token(token: str) -> tuple[Decimal, str] | None:
    """Classify a regex-extracted money-ish token as ``(Decimal value, unit)`` or
    ``None`` (Story 10.6).

    The UNIT is read from the surface form: a ``$`` anywhere → :data:`UNIT_MONEY`, a
    trailing ``%`` → :data:`UNIT_PERCENT`, otherwise a bare count → :data:`UNIT_BARE`.
    The value strips ``$``/thousands-commas/trailing-``%`` while PRESERVING a leading
    sign (so ``"-$1,200"`` → ``(-1200, money)``, ``"30%"`` → ``(30, percent)``,
    ``"30"`` → ``(30, bare)``, ``"-5%"`` → ``(-5, percent)`` — a sign-flip is never
    laundered). Returns ``None`` for a bare-sign / pure-punctuation / empty /
    unparseable token so it is ignored (never a false reject on stray text)."""
    raw = token.strip()
    if "$" in raw:
        unit = UNIT_MONEY
    elif raw.endswith("%"):
        unit = UNIT_PERCENT
    else:
        unit = UNIT_BARE
    cleaned = raw.replace(",", "").replace("$", "").rstrip("%")
    if cleaned in ("", "-", "+"):
        return None
    try:
        return (Decimal(cleaned), unit)
    except (InvalidOperation, ValueError):
        return None


def check_no_invented_numbers(
    text: str, allowed: frozenset[tuple[Decimal, str]]
) -> None:
    """Raise :class:`NarrationValidationError` if ``text`` states a non-engine number.

    Regex-extracts money-ish tokens, classifies each as ``(value, unit)`` — money
    (``$``), percent (``%``), or a bare count — and rejects if that PAIR is not in
    ``allowed``. UNIT-AWARE (Story 10.6): a bare count or dollar figure whose
    magnitude merely coincides with a real weight-percent is rejected, closing the
    value-only laundering hole. Bare tokens match ONLY bare/fraction allow entries
    (STRICT), so a fabricated "30 companies" (30 is a target percent, not a bare fact)
    degrades to the honest template. Ignores pure-punctuation / empty matches. Pure —
    a false positive is SAFE (the caller degrades to the honest template)."""
    for match in _NUMBER_TOKEN_RE.findall(text or ""):
        classified = _classify_number_token(match)
        if classified is None:
            continue
        if classified not in allowed:
            raise NarrationValidationError(
                f"Narration stated a number not in the engine-provided set: "
                f"{match!r} ({classified[0]} {classified[1]})."
            )


def check_no_forecast(text: str) -> None:
    """Raise :class:`NarrationValidationError` if ``text`` contains forecast language.

    Case-insensitive, WORD-BOUNDARY match against :data:`FORECAST_TERMS` (so
    ``"expect"`` fires on "expect" but not "expectations", and ``"crash"`` not on
    "crashed"). Best-effort denylist, not exhaustive. Pure — a hit degrades the
    narration to the deterministic template (opinion is allowed; prediction is
    not)."""
    lowered = (text or "").lower()
    for term in FORECAST_TERMS:
        if re.search(r"\b" + re.escape(term) + r"\b", lowered):
            raise NarrationValidationError(
                f"Narration contained forecast language: {term!r}."
            )


# --- Deterministic templated fallback (authored to pass every gate) ----------


def _fallback_narration(
    plan: Plan, evidence: tuple[EvidenceRecord, ...]
) -> AllocationNarration:
    """The deterministic templated narration for a deploy plan (the safety net).

    Authored to PASS all three gates AND the 5 good-lesson tests BY CONSTRUCTION:

    - **principle-not-pick** — frames the move as rebalancing toward the CHOSEN
      target mix by buying the underweight asset CLASS (names the
      :data:`~strategy.target_allocation.ASSET_CLASS_LABEL`), never a stock pick /
      hot / winner.
    - **why-generalizes** — states the settled principle (spreading across broad,
      low-cost index funds), not a one-off tip.
    - **recognized-best-practice** — says "diversification" / "rebalance" / "broad
      index".
    - **teaches-the-tradeoff** — it doesn't try to time the market and leftover cash
      stays put.
    - **facts-not-forecast** — numbers come ONLY from the plan (so
      :func:`check_no_invented_numbers` passes) and there is no prediction language
      (:func:`check_no_forecast` passes).

    Cites EVERY evidence id, carries ≥1 real uncertainty, calm-word-list clean."""
    labels = [
        ASSET_CLASS_LABEL.get(item.asset_class, item.asset_class)
        for item in plan.action_items
    ]
    # A calm "US stocks and Bonds" / "International stocks" join (no oxford drama).
    if len(labels) > 1:
        classes_phrase = ", ".join(labels[:-1]) + " and " + labels[-1]
    else:
        classes_phrase = labels[0] if labels else "your underweight classes"

    buys_phrase = ", ".join(
        f"{format_money(item.amount)} of {item.symbol}" for item in plan.action_items
    )

    action_label = "Put your idle cash to work toward your target mix"
    reasoning = (
        f"Compared with the target mix you chose, you're light on {classes_phrase}, "
        f"so this plan buys {buys_phrase} — the broad, low-cost index funds for "
        "those classes — to move you back toward that balance. This is plain "
        "diversification and rebalancing toward your target, not a bet on any hot "
        "pick or a guess about where the market is headed. The tradeoff: it "
        "doesn't try to time the market, and any cash beyond what closes the gaps "
        f"is left undeployed ({format_money(plan.undeployed_cash)}) rather than "
        "stretched past your target."
    )
    uncertainties = (
        "Markets move, so a fill isn't guaranteed and this isn't a prediction — "
        "it's simply moving your money toward the balance you picked.",
    )
    return AllocationNarration(
        action_label=action_label,
        reasoning=reasoning,
        uncertainties=uncertainties,
        evidence=evidence,
        status=plan.status,
    )


# --- Request composition + orchestration -------------------------------------


def compose_narration_request(
    plan: Plan, evidence: tuple[EvidenceRecord, ...]
) -> LLMRequest:
    """Compose the advisor-persona narration request (pure).

    Feeds the FINISHED plan as facts to explain: each action item (symbol, asset-
    class label, amount), the current-vs-target weights, the investable / undeployed
    cash, and the evidence records as JSON (id + statement + stats). The model emits
    against :data:`NARRATION_OUTPUT_SCHEMA` (the recommendation schema minus
    ``order_intent``). ``hard_reasoning=False`` — this is narration, not a hard
    money-math decision (the engine already did that)."""
    item_lines = []
    for item in plan.action_items:
        label = ASSET_CLASS_LABEL.get(item.asset_class, item.asset_class)
        item_lines.append(
            f"- buy {format_money(item.amount)} of {item.symbol} ({label})"
        )

    weight_lines = []
    for cls in ASSET_CLASSES:
        label = ASSET_CLASS_LABEL.get(cls, cls)
        current_pct = plan.current.get(cls, {}).get("weight", _ZERO) * _HUNDRED
        target_pct = plan.target_weights.get(cls, _ZERO) * _HUNDRED
        weight_lines.append(
            f"- {label}: currently {format_money(current_pct)}% of your classified "
            f"mix, target {format_money(target_pct)}%"
        )

    evidence_json = json.dumps(
        [
            {"id": r.id, "statement": r.statement, "stats": r.to_dict()["stats"]}
            for r in evidence
        ],
        separators=(",", ":"),
    )

    user_content = (
        "The deploy-my-cash plan below is ALREADY computed and final — narrate the "
        "why/tradeoff/prioritization, do not change or invent any number.\n"
        "Action items (the concrete buys toward target):\n"
        + "\n".join(item_lines)
        + "\nCurrent vs target mix:\n"
        + "\n".join(weight_lines)
        + f"\nInvestable cash: {format_money(plan.investable_cash)}\n"
        f"Undeployed (leftover) cash: {format_money(plan.undeployed_cash)}\n"
        "Cite ONLY these evidence IDs, exactly as given:\n"
        + evidence_json
    )
    return LLMRequest(
        messages=(LLMMessage(role="user", content=user_content),),
        output_schema=NARRATION_OUTPUT_SCHEMA,
        system=_NARRATION_SYSTEM,
        hard_reasoning=False,
    )


def narrate_plan(gateway: LLMGateway, plan: Plan) -> AllocationNarration:
    """Narrate a :class:`~allocation.engine.Plan` — the load-bearing safeguard.

    No-action status → a deterministic calm ``plan.reason`` passthrough with NO
    gateway call (empty evidence). Deploy status → the LLM path, gated hard: compose
    the request, map the output to a candidate, run
    :func:`~coach.validation.validate_recommendation` (reasoning / uncertainty /
    cited-evidence), then :func:`check_no_invented_numbers` +
    :func:`check_no_forecast` over ``reasoning + action_label`` AND every
    ``uncertainties`` line (an LLM-authored uncertainty is surfaced to the user too,
    so it must clear both honesty gates). On ANY exception
    (gateway/parse failure, a gate rejection, an unbacked fake-mode ID) it silently
    degrades to :func:`_fallback_narration` — mirroring
    :func:`~coach.suggest.narrate_suggestion`. Never a dead-end, never a surfaced
    unvalidated fact."""
    if plan.status != "deploy":
        return AllocationNarration(
            action_label="Nothing to buy right now",
            reasoning=plan.reason,
            uncertainties=(
                "This can change as your cash, holdings, or target mix change — "
                "there's simply nothing worth doing toward your target right now.",
            ),
            evidence=(),
            status=plan.status,
        )

    evidence = build_narration_facts(plan)
    allowed = allowed_facts(plan)
    try:
        request = compose_narration_request(plan, evidence)
        response = gateway.complete(request)
        candidate = recommendation_from_output(response.output)
        blessed = validate_recommendation(candidate, evidence)
        # Gate the reasoning, the action label, AND every uncertainty line — all
        # three are LLM-authored and surfaced to the user, so a fabricated number or
        # a forecast hiding in an uncertainty must degrade to the template too.
        combined = " ".join(
            (blessed.reasoning, blessed.action_label, *blessed.uncertainties)
        )
        check_no_invented_numbers(combined, allowed)
        check_no_forecast(combined)
        return AllocationNarration(
            action_label=blessed.action_label,
            reasoning=blessed.reasoning,
            uncertainties=blessed.uncertainties,
            evidence=blessed.evidence,
            status="deploy",
        )
    except Exception:
        # Silent, resilient fallback (mirrors narrate_suggestion) — the numbers are
        # the engine's; only the prose degrades to a deterministic, gate-passing
        # template that cites every real evidence id.
        return _fallback_narration(plan, evidence)
