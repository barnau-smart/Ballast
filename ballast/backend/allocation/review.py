"""The SELL-side portfolio-review analysis buckets (Story 10.4, Epic 10).

Stories 10-1..10-3 answer *"deploy my idle cash toward my target"* — the BUY half
of a portfolio review. This module adds the SELL half: two deterministic,
read-only **analysis buckets**, each producing a concrete SELL-side action item
that is narrated by the 10-3 advisor safeguards and POPULATED (never submitted):

1. **concentration / single-stock** — a NON-index-core, non-parked holding whose
   weight exceeds the concentration ceiling → a TRIM SELL back toward the ceiling
   (de-speculate into the diversified index core).
2. **cost / fees** — a held fund whose real expense ratio materially exceeds its
   near-identical cheaper same-class canonical index fund → a SWITCH (SELL the
   high-fee holding; the follow-up BUY of the cheaper fund is narrated, the tax
   consequence noted honestly but NOT computed).

Design guardrails (locked, non-negotiable — mirror :mod:`allocation.narrate` and
:mod:`cash.liquidation`):

- **Additive by design — the deploy engine is untouched.** This is a PARALLEL
  SELL-side path that only IMPORTS the 10-3 gate helpers
  (:func:`~allocation.narrate.check_no_invented_numbers` /
  :func:`~allocation.narrate.check_no_forecast`), reused VERBATIM, so the
  never-invent / no-forecast machinery stays single-sourced.
- **Never invent a fact.** Every number a finding's narration states (trim/switch
  amount, holding market value, holding weight, the ceiling, both expense ratios)
  is computed by the deterministic detectors / read from the expense-ratio table
  and is in the finding's numeric allow-set. Fee values come ONLY from
  :mod:`strategy.expense_ratio` (real published figures). The narration passes
  ``validate_recommendation`` PLUS both honesty gates over
  ``reasoning + action_label + *uncertainties``; ANY rejection / parse failure /
  fake-mode unbacked id degrades to :func:`_fallback_review_narration`.
- **Opinion, not forecast.** Narration opines on the situation + settled principles
  (diversify; minimize costs); the no-forecast gate rejects prediction language.
- **"Nothing to fix" is valid.** Zero findings → an empty list with NO LLM call.
- **Populate, don't submit.** Each finding carries a typed SELL/MARKET
  :class:`~coach.recommendation.OrderIntent` the human co-signs through the existing
  ``/approve`` spine; whole-share sized (a trim/switch under one whole share is dust
  → dropped). This layer PLACES NOTHING and writes no ``decision_record``.
- **De-speculate / rebalance, never chase.** Concentration targets single-name risk
  in NON-index holdings only — a broad index-core position over the ceiling is
  asset-class balance handled by the deploy path, never trimmed here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from allocation.narrate import (
    AllocationNarration,
    check_no_forecast,
    check_no_invented_numbers,
)
from brokers.portfolio import PortfolioView, get_portfolio
from cash.config import get_config as get_cash_config, normalize_symbols
from coach.execution import whole_share_quantity
from coach.recommendation import (
    OrderIntent,
    OrderSide,
    OrderType,
    recommendation_from_output,
)
from coach.validation import validate_recommendation
from db.models import CashConfig
from db.scope import Scope
from llm.port import LLMGateway, LLMMessage, LLMRequest
from money import format_money
from precedent.evidence import EvidenceKind, EvidenceRecord, make_id
from strategy.expense_ratio import EXPENSE_RATIO_MATERIAL_DELTA, fund_cost
from strategy.index_core import is_index_core
from strategy.target_allocation import (
    ASSET_CLASS_LABEL,
    CANONICAL_FUND,
    asset_class_for,
)

_ZERO = Decimal("0")
_CENT = Decimal("0.01")
_HUNDRED = Decimal("100")

#: The concentration ceiling: a single NON-index-core holding taking more than this
#: share of total portfolio value (holdings market value + cash) is treated as
#: over-concentrated single-name risk. Mirrors the coach's
#: ``coach.pipeline.CONCENTRATION_SHARE_CEILING`` (0.40) heuristic — one position
#: past ~40% is a lot of eggs in one basket. Mirrored LOCALLY (a bare ``Decimal``
#: constant) rather than imported to avoid coupling this read-only SELL-side path to
#: the heavy coach-pipeline / precedent-engine import graph; keep the two in sync as
#: a deliberate strategy decision.
CONCENTRATION_CEILING: Decimal = Decimal("0.40")

#: Finding kinds (the two analysis buckets). Stable string keys — wire-friendly.
KIND_CONCENTRATION = "concentration"
KIND_COST = "cost"

#: The provenance string on every review evidence record — the deterministic
#: review detectors, never a market feed (these are the user's own facts).
_EVIDENCE_SOURCE = "allocation-review"


class ReviewValidationError(ValueError):
    """Raised by the review honesty gates on a violation — a safety NET, not a
    surfaced error: :func:`narrate_finding` catches it (like any exception) and
    degrades to :func:`_fallback_review_narration`."""


# --- Pure data holder --------------------------------------------------------


@dataclass(frozen=True)
class ReviewFinding:
    """One SELL-side analysis finding (pure).

    ``kind`` is :data:`KIND_CONCENTRATION` or :data:`KIND_COST`. ``symbol`` is the
    holding to SELL; ``asset_class`` its broad class; ``order_intent`` the
    ready-to-approve SELL/MARKET intent (whole-share floored, dust dropped).
    ``switch_to`` is the cheaper canonical fund to BUY next (COST only; ``None`` for
    concentration). ``amount`` is the SELL dollar figure; ``holding_value`` the
    holding's cached market value; ``weight`` its share of total portfolio value
    (fraction). ``expense_ratio`` / ``cheaper_expense_ratio`` are the two real net
    ERs (percentage points, COST only). ``as_of`` is the portfolio snapshot date.
    Frozen — an immutable value ranked + narrated downstream."""

    kind: str
    symbol: str
    asset_class: str
    order_intent: OrderIntent
    switch_to: str | None
    amount: Decimal
    holding_value: Decimal
    weight: Decimal
    expense_ratio: Decimal | None
    cheaper_expense_ratio: Decimal | None
    as_of: date | None


# --- Small pure helpers ------------------------------------------------------


def _as_of_date(view_as_of: datetime | None) -> date | None:
    """Return the calendar date for a finding's evidence, tolerating a bare
    ``date``/``None`` (pure). ``get_portfolio`` yields a tz-aware ``datetime`` (or
    ``None`` when the user never imported)."""
    if isinstance(view_as_of, datetime):
        return view_as_of.date()
    if isinstance(view_as_of, date):
        return view_as_of
    return None


def _total_portfolio_value(view: PortfolioView) -> Decimal:
    """Σ holding market values + cash — the denominator for the weight (pure)."""
    total = view.cash if view.cash is not None else _ZERO
    for h in view.holdings or []:
        if h.market_value is not None and h.market_value.is_finite():
            total += h.market_value
    return total


def _sell_intent(
    symbol: str, amount: Decimal, market_value: Decimal, quantity: Decimal | None
) -> OrderIntent | None:
    """Size a whole-share SELL/MARKET intent for ``amount``, or ``None`` if it's dust.

    Priced off the CACHED ``unit = market_value / quantity`` (no live quote — the
    plan is an honest estimate; live pricing / flooring happens for real only at
    ``/approve``), mirroring :mod:`cash.liquidation`. Uses
    :func:`~coach.execution.whole_share_quantity` so it floors the SAME deterministic
    way the adapters do. A trim/switch that sizes to < 1 whole share is dust →
    ``None`` (dropped). Pure."""
    if (
        quantity is None
        or not quantity.is_finite()
        or quantity <= _ZERO
        or not market_value.is_finite()
        or market_value <= _ZERO
        or amount <= _ZERO
    ):
        return None
    unit = market_value / quantity
    shares = whole_share_quantity(amount, unit)
    if shares < 1:
        return None  # dust — under one whole share
    return OrderIntent(
        symbol=symbol,
        side=OrderSide.SELL,
        amount=amount,
        order_type=OrderType.MARKET,
    )


# --- The two pure detectors --------------------------------------------------


def find_concentration_findings(
    view: PortfolioView, cash_config: CashConfig | None
) -> list[ReviewFinding]:
    """Detect over-concentrated NON-index-core single positions (pure).

    Fires for a holding that is NOT index-core (:func:`~strategy.index_core.is_index_core`
    false), NOT a declared parked money-market symbol, and whose weight —
    ``market_value / (Σ holding market values + cash)`` — exceeds
    :data:`CONCENTRATION_CEILING`. The trim amount is
    ``market_value − ceiling × total`` (bring it back TO the ceiling), quantized to
    cents, then whole-share floored; a sub-one-share trim is dust and dropped. A
    broad index-core position over the ceiling is deliberately LEFT ALONE (that is
    asset-class balance for the deploy/rebalance path, never a single-name trim), as
    is a non-index stock UNDER the ceiling (aware-but-don't-act is deferred). Never
    manufactures a trade."""
    total = _total_portfolio_value(view)
    if total <= _ZERO:
        return []

    parked_set: set[str] = set()
    if cash_config is not None:
        parked_set = set(normalize_symbols(cash_config.parked_symbols))

    as_of = _as_of_date(view.as_of)
    findings: list[ReviewFinding] = []
    for h in view.holdings or []:
        symbol = (h.symbol or "").strip().upper()
        if not symbol:
            continue
        market_value = h.market_value if h.market_value is not None else _ZERO
        if not market_value.is_finite() or market_value <= _ZERO:
            continue
        # De-speculate ONLY: index-core positions + parked money-market are excluded.
        if is_index_core(symbol) or symbol in parked_set:
            continue

        weight = market_value / total
        if weight <= CONCENTRATION_CEILING:
            continue  # under the ceiling → aware-but-don't-act (deferred), no trade

        # Trim back TO the ceiling: sell the excess above (ceiling × total).
        amount = (market_value - (CONCENTRATION_CEILING * total)).quantize(_CENT)
        intent = _sell_intent(symbol, amount, market_value, h.quantity)
        if intent is None:
            continue  # dust — under one whole share, dropped

        # The single-stock's asset class is best-effort (a non-index name usually
        # has no canonical class); surfaced as its label if known, else the symbol.
        asset_class = asset_class_for(symbol) or symbol
        findings.append(
            ReviewFinding(
                kind=KIND_CONCENTRATION,
                symbol=symbol,
                asset_class=asset_class,
                order_intent=intent,
                switch_to=None,
                amount=amount,
                holding_value=market_value,
                weight=weight,
                expense_ratio=None,
                cheaper_expense_ratio=None,
                as_of=as_of,
            )
        )
    return findings


def find_cost_findings(
    view: PortfolioView, cash_config: CashConfig | None
) -> list[ReviewFinding]:
    """Detect high-fee funds with a materially cheaper same-class canonical (pure).

    For a held fund with an expense-ratio-table entry ``(er, asset_class)``, the
    near-identical cheaper index fund is ``CANONICAL_FUND[asset_class]`` and its ER
    is looked up from the same table. Fires a ``cost`` finding when
    ``held_er − canonical_er ≥ EXPENSE_RATIO_MATERIAL_DELTA`` (a beginner-meaningful
    gap that compounds). The SELL leg is the WHOLE position (whole-share floored;
    dust dropped); ``switch_to`` is the canonical cheaper fund (its follow-up BUY is
    narrated, the tax consequence noted honestly but NOT computed). Index-core /
    canonical funds never fire (they ARE the cheap target — the delta is ≤ 0, or the
    fund IS its own canonical). An unknown fund has no ER → no finding (never invent
    a ratio). Parked money-market funds are excluded."""
    parked_set: set[str] = set()
    if cash_config is not None:
        parked_set = set(normalize_symbols(cash_config.parked_symbols))

    total = _total_portfolio_value(view)
    as_of = _as_of_date(view.as_of)
    findings: list[ReviewFinding] = []
    for h in view.holdings or []:
        symbol = (h.symbol or "").strip().upper()
        if not symbol or symbol in parked_set:
            continue
        # Index-core funds ARE the cheap switch target — never flag them (enforce the
        # invariant in code, not merely incidentally via the fee delta).
        if is_index_core(symbol):
            continue
        market_value = h.market_value if h.market_value is not None else _ZERO
        if not market_value.is_finite() or market_value <= _ZERO:
            continue

        held = fund_cost(symbol)
        if held is None:
            continue  # unknown fee → never invent a ratio, no finding

        canonical_symbol = CANONICAL_FUND.get(held.asset_class)
        if not canonical_symbol:
            continue
        # A fund that IS its own canonical (or the same symbol) can't switch to itself.
        if canonical_symbol.strip().upper() == symbol:
            continue
        cheaper = fund_cost(canonical_symbol)
        if cheaper is None:
            continue  # never invent the target's ratio

        if held.expense_ratio - cheaper.expense_ratio < EXPENSE_RATIO_MATERIAL_DELTA:
            continue  # not a material gap

        # SELL the whole position (whole-share floored; dust dropped).
        amount = market_value.quantize(_CENT)
        intent = _sell_intent(symbol, amount, market_value, h.quantity)
        if intent is None:
            continue

        weight = (market_value / total) if total > _ZERO else _ZERO
        findings.append(
            ReviewFinding(
                kind=KIND_COST,
                symbol=symbol,
                asset_class=held.asset_class,
                order_intent=intent,
                switch_to=canonical_symbol.strip().upper(),
                amount=amount,
                holding_value=market_value,
                weight=weight,
                expense_ratio=held.expense_ratio,
                cheaper_expense_ratio=cheaper.expense_ratio,
                as_of=as_of,
            )
        )
    return findings


def find_review(
    view: PortfolioView, cash_config: CashConfig | None
) -> list[ReviewFinding]:
    """Run both detectors and return the ranked findings (pure).

    Ranked by SELL dollar ``amount`` descending, ties broken by ``symbol`` ascending
    (deterministic). "Nothing to fix" → an EMPTY list (the honest, valid output).

    A single holding can qualify for BOTH buckets — a non-index high-fee fund held
    over the ceiling (e.g. an actively-managed fund at 55%). Surfacing both would
    double-count one position (two overlapping SELLs the human could co-sign into an
    oversell). The cost switch sells the WHOLE position and moves it to the cheaper
    same-class core, which subsumes the concentration trim (a partial sell of the
    same holding), so we prefer the cost switch and drop the redundant trim for that
    symbol."""
    concentration = find_concentration_findings(view, cash_config)
    cost = find_cost_findings(view, cash_config)
    cost_symbols = {f.symbol for f in cost}
    findings = [f for f in concentration if f.symbol not in cost_symbols] + cost
    findings.sort(key=lambda f: (-f.amount, f.symbol))
    return findings


# --- Evidence + allow-set (pure, built from a finding) -----------------------


def build_review_facts(finding: ReviewFinding) -> tuple[EvidenceRecord, ...]:
    """Build the ``EvidenceKind.STRATEGY`` evidence record for one finding (pure).

    ONE record per finding: the ``statement`` names the holding + the concrete SELL
    amount (and, for a cost switch, both ERs + the cheaper fund); ``stats`` carries
    the raw ``Decimal``s (the facts the narration may cite). The id is
    content-addressed via :func:`~precedent.evidence.make_id` over
    ``(kind, symbol, as_of, stats)`` so the same finding yields a byte-identical id
    (load-bearing for the LLM to cite it)."""
    as_of = finding.as_of if finding.as_of is not None else date(1970, 1, 1)
    stats: dict = {
        "sell_amount": finding.amount,
        "holding_value": finding.holding_value,
        "weight": finding.weight,
        "ceiling": CONCENTRATION_CEILING,
    }
    if finding.kind == KIND_COST:
        stats["expense_ratio"] = finding.expense_ratio
        stats["cheaper_expense_ratio"] = finding.cheaper_expense_ratio
        statement = (
            f"Your {finding.symbol} charges a {format_money(finding.expense_ratio)}% "
            f"yearly fee versus {format_money(finding.cheaper_expense_ratio)}% for the "
            f"broad {finding.switch_to} index fund — selling "
            f"{format_money(finding.amount)} of {finding.symbol} lets you switch to the "
            "cheaper same-class fund."
        )
    else:
        weight_pct = (finding.weight * _HUNDRED).quantize(_CENT)
        statement = (
            f"Your {finding.symbol} is {format_money(weight_pct)}% of your portfolio — "
            f"trimming {format_money(finding.amount)} brings it back toward the "
            f"{format_money(CONCENTRATION_CEILING * _HUNDRED)}% single-position ceiling "
            "and into your diversified index core."
        )
    record = EvidenceRecord(
        id=make_id(EvidenceKind.STRATEGY, finding.symbol, as_of, stats),
        kind=EvidenceKind.STRATEGY,
        statement=statement,
        stats=stats,
        source=_EVIDENCE_SOURCE,
        as_of=as_of,
    )
    return (record,)


def _add_weight_forms(allowed: set[Decimal], weight: Decimal) -> None:
    """Admit a weight in BOTH the fraction (``0.55``) and 0–100 percent (``55``)
    forms the narration might use (pure, in-place). The percent form is admitted at
    both full precision and cent-quantized so a rounded render (``55.00``) is
    citable too."""
    allowed.add(weight)
    pct = weight * _HUNDRED
    allowed.add(pct)
    allowed.add(pct.quantize(_CENT))


def allowed_review_facts(finding: ReviewFinding) -> frozenset[Decimal]:
    """The engine-provided numeric allow-set for :func:`check_no_invented_numbers`.

    Every number the narration may legitimately state (compared by ``Decimal``
    value): the SELL amount, the holding market value, the holding weight (fraction
    AND percent), the concentration ceiling (fraction AND percent), and — for a cost
    switch — BOTH expense ratios (as percents). The fee values come ONLY from the
    expense-ratio table; a wrong-but-plausible ER the LLM invents is not admitted, so
    the never-invent gate rejects it. Pure."""
    allowed: set[Decimal] = set()
    allowed.add(finding.amount)
    allowed.add(finding.holding_value)
    _add_weight_forms(allowed, finding.weight)
    _add_weight_forms(allowed, CONCENTRATION_CEILING)
    if finding.expense_ratio is not None:
        allowed.add(finding.expense_ratio)
    if finding.cheaper_expense_ratio is not None:
        allowed.add(finding.cheaper_expense_ratio)
    return frozenset(allowed)


# --- Deterministic templated fallback (authored to pass every gate) ----------


def _fallback_review_narration(
    finding: ReviewFinding, evidence: tuple[EvidenceRecord, ...]
) -> AllocationNarration:
    """The deterministic templated narration for one finding (the safety net).

    Authored to PASS ``validate_recommendation`` + both honesty gates AND the 5
    good-lesson tests BY CONSTRUCTION, per kind:

    - **concentration** — principle: a single company is speculative versus a
      diversified index core; tradeoff: you cap the upside of that one name but cut
      single-name risk; recognized best practice: diversification; uncertainty:
      markets move, a fill isn't guaranteed, this isn't a prediction.
    - **cost** — principle: low fund costs compound over decades; tradeoff: selling
      may realize a taxable gain (noted honestly, NOT computed); recognized best
      practice: minimize fund fees; uncertainty: same honest hedge.

    Every number is drawn only from :func:`allowed_review_facts`; calm-word-list
    clean; cites the finding's evidence id; carries ≥1 real uncertainty.

    This is the last-resort safety net (``narrate_finding`` degrades HERE on any
    failure), so it must NEVER raise — a ``cost`` finding always carries non-None
    ERs/``switch_to`` by construction in :func:`find_cost_findings`, but we treat the
    fee branch as cost-shaped only when those fields are actually present so a
    malformed finding degrades to the concentration copy instead of throwing."""
    is_cost = (
        finding.kind == KIND_COST
        and finding.expense_ratio is not None
        and finding.cheaper_expense_ratio is not None
        and finding.switch_to
    )
    if is_cost:
        action_label = "Switch this pricey fund for a cheaper one that holds the same thing"
        reasoning = (
            f"Your {finding.symbol} charges a {format_money(finding.expense_ratio)}% "
            "yearly fee, while the broad "
            f"{finding.switch_to} index fund covering the same asset class charges "
            f"{format_money(finding.cheaper_expense_ratio)}% — a gap that quietly "
            "compounds against you over decades. The principle here is to minimize "
            "fund fees: paying less for the same broad exposure keeps more of your "
            "money working for you. This plan sells "
            f"{format_money(finding.amount)} of {finding.symbol} so you can then buy "
            f"the cheaper {finding.switch_to} — a like-for-like switch, not a bet on "
            "a hot pick. The tradeoff is honest: selling may realize a taxable gain, "
            "and we don't calculate tax here, so weigh that before you co-sign."
        )
        uncertainties = (
            "Markets move, so a fill isn't guaranteed and this isn't a prediction; "
            "and selling may have a tax consequence we don't calculate for you.",
        )
    else:
        weight_pct = format_money((finding.weight * _HUNDRED).quantize(_CENT))
        ceiling_pct = format_money(CONCENTRATION_CEILING * _HUNDRED)
        action_label = "Trim this large single position back toward your diversified core"
        reasoning = (
            f"Your {finding.symbol} has grown to {weight_pct}% of your portfolio, "
            f"past the {ceiling_pct}% single-position ceiling — that is a lot riding "
            "on one company. The principle here is diversification: a single stock is "
            "speculative, while your broad index core spreads the same money across "
            "the whole market. This plan trims "
            f"{format_money(finding.amount)} of {finding.symbol} back toward that "
            "ceiling and into the diversified core — de-speculating, not chasing a "
            "winner. The tradeoff is real: you cap the upside of this one name, but "
            "you cut the single-name risk of holding so much in it."
        )
        uncertainties = (
            "Markets move, so a fill isn't guaranteed and this isn't a prediction — "
            "it's simply bringing one oversized position back toward balance.",
        )
    return AllocationNarration(
        action_label=action_label,
        reasoning=reasoning,
        uncertainties=uncertainties,
        evidence=evidence,
        status=finding.kind,
    )


# --- Request composition + orchestration -------------------------------------

#: The narration output schema — identical shape to the deploy narrator (the
#: recommendation schema MINUS ``order_intent``; the engine owns the order).
REVIEW_NARRATION_OUTPUT_SCHEMA: dict = {
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

_REVIEW_SYSTEM = (
    "You are Ballast's calm, honest, fiduciary investing coach. You are handed a "
    "single portfolio-review finding whose SELL has ALREADY been computed — every "
    "number is final and you must NOT change, recompute, or invent one. Your job is "
    "to explain, in plain warm English, the WHY, the TRADEOFF, and the settled "
    "principle as situational opinion: for an over-concentrated single stock, "
    "diversifying out of single-name risk into the broad index core; for a high-fee "
    "fund, switching to a cheaper same-class index fund because low costs compound. "
    "For a fee switch, note honestly that selling may realize a taxable gain and "
    "that we do NOT calculate tax here — never compute a tax number. Cite ONLY the "
    "evidence IDs you are given. State numbers ONLY exactly as provided, and write "
    "every quantity in DIGITS as given (e.g. $2,000.00 or 0.61%), never spelled out "
    "and never rounded. NEVER forecast or predict the market, name a hot pick, or "
    "promise a return. Always name at least one honest uncertainty."
)


def compose_review_request(
    finding: ReviewFinding, evidence: tuple[EvidenceRecord, ...]
) -> LLMRequest:
    """Compose the advisor-persona narration request for one finding (pure).

    Feeds the FINISHED finding as facts to explain (the SELL amount, holding value +
    weight, the ceiling, and — for a cost switch — both ERs + the cheaper fund) and
    the evidence records as JSON (id + statement + stats). The model emits against
    :data:`REVIEW_NARRATION_OUTPUT_SCHEMA`. ``hard_reasoning=False`` — narration, not
    money-math (the engine already did that)."""
    lines = [
        "The portfolio-review finding below is ALREADY computed and final — narrate "
        "the why/tradeoff/principle, do not change or invent any number.",
        f"- finding kind: {finding.kind}",
        f"- holding to sell: {finding.symbol}",
        f"- sell amount: {format_money(finding.amount)}",
        f"- holding market value: {format_money(finding.holding_value)}",
        f"- single-position ceiling: "
        f"{format_money(CONCENTRATION_CEILING * _HUNDRED)}%",
    ]
    if finding.kind == KIND_CONCENTRATION:
        weight_pct = format_money((finding.weight * _HUNDRED).quantize(_CENT))
        lines.append(f"- this position's weight: {weight_pct}% of the portfolio")
        lines.append(
            "- goal: trim this oversized single stock back toward the ceiling and "
            "into the broad diversified index core (de-speculate, do not chase)"
        )
    else:
        lines.append(f"- switch to (cheaper same-class index fund): {finding.switch_to}")
        lines.append(
            f"- this fund's expense ratio: {format_money(finding.expense_ratio)}%"
        )
        lines.append(
            f"- the cheaper fund's expense ratio: "
            f"{format_money(finding.cheaper_expense_ratio)}%"
        )
        lines.append(
            "- goal: sell this high-fee fund and buy the cheaper same-class index "
            "fund; note honestly that selling may realize a taxable gain and that "
            "tax is NOT calculated here"
        )

    evidence_json = json.dumps(
        [
            {"id": r.id, "statement": r.statement, "stats": r.to_dict()["stats"]}
            for r in evidence
        ],
        separators=(",", ":"),
    )
    user_content = (
        "\n".join(lines)
        + "\nCite ONLY these evidence IDs, exactly as given:\n"
        + evidence_json
    )
    return LLMRequest(
        messages=(LLMMessage(role="user", content=user_content),),
        output_schema=REVIEW_NARRATION_OUTPUT_SCHEMA,
        system=_REVIEW_SYSTEM,
        hard_reasoning=False,
    )


def narrate_finding(gateway: LLMGateway, finding: ReviewFinding) -> AllocationNarration:
    """Narrate one :class:`ReviewFinding` — the load-bearing safeguard.

    Mirrors :func:`allocation.narrate.narrate_plan`'s deploy branch VERBATIM: build
    the evidence + allow-set, compose the request, map the output to a candidate, run
    :func:`~coach.validation.validate_recommendation` (reasoning / uncertainty /
    cited-evidence), then :func:`~allocation.narrate.check_no_invented_numbers` +
    :func:`~allocation.narrate.check_no_forecast` over ``reasoning + action_label``
    AND every ``uncertainties`` line (an LLM-authored uncertainty is surfaced too).
    On ANY exception (gateway/parse failure, a gate rejection, an unbacked fake-mode
    id) it silently degrades to :func:`_fallback_review_narration`. Never a
    dead-end, never a surfaced unvalidated fact."""
    evidence = build_review_facts(finding)
    allowed = allowed_review_facts(finding)
    try:
        request = compose_review_request(finding, evidence)
        response = gateway.complete(request)
        candidate = recommendation_from_output(response.output)
        blessed = validate_recommendation(candidate, evidence)
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
            status=finding.kind,
        )
    except Exception:
        # Silent, resilient fallback (mirrors narrate_plan) — the numbers are the
        # detector's; only the prose degrades to a deterministic, gate-passing
        # template that cites the real evidence id.
        return _fallback_review_narration(finding, evidence)


@dataclass(frozen=True)
class NarratedFinding:
    """A ranked finding paired with its (validated-or-fallback) narration + the one
    STRATEGY evidence record backing it. The API serializes this into the wire
    shape (the SELL order + the narration)."""

    finding: ReviewFinding
    narration: AllocationNarration


# --- Scoped orchestrator (reads only; never writes) --------------------------


async def build_review(
    scope: Scope, session: AsyncSession, gateway: LLMGateway
) -> list[NarratedFinding]:
    """Resolve the caller's holdings + cash config, run the detectors, narrate each
    finding, and return the ranked, narrated findings (Story 10.4).

    READ-ONLY and degraded-safe: reads the CACHED portfolio (``get_portfolio``) +
    the scoped Epic-9 cash config — no live broker session, no writes, no order
    placement. Fail-closed per-user (AD-10): only THIS user's holdings + config are
    ever touched. "Nothing to fix" → an EMPTY list with NO LLM call (the honest,
    valid output — never a fabricated trade). Each returned finding carries a
    SELL/MARKET :class:`~coach.recommendation.OrderIntent` the human co-signs through
    the existing ``/approve`` spine; this places NOTHING."""
    view = await get_portfolio(scope, session)
    cash_config = await get_cash_config(scope, session)
    findings = find_review(view, cash_config)
    # "Nothing to fix" is valid — no findings means NO LLM call.
    return [
        NarratedFinding(finding=f, narration=narrate_finding(gateway, f))
        for f in findings
    ]
