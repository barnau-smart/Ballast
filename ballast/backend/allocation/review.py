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
    UNIT_BARE,
    UNIT_MONEY,
    UNIT_PERCENT,
    AllocationNarration,
    check_no_forecast,
    check_no_invented_numbers,
)
from allocation.config import get_config as get_target_config
from allocation.config import resolve as resolve_target_config
from allocation.engine import build_plan, classify_holdings
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
    ASSET_CLASSES,
    ASSET_CLASS_LABEL,
    BONDS,
    CANONICAL_FUND,
    asset_class_for,
)

_ZERO = Decimal("0")
_ONE = Decimal("1")
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

#: The coverage floor (Story 11.1, Epic 11): the classifiable share of the portfolio
#: (holdings mapped to an asset class + cash + parked money-market, ÷ total value) at/above
#: which the review's class-level reasoning is considered adequate. Below it, the review
#: surfaces a calm informational "I can only see part of your portfolio" note and 11.2 /
#: any target-drift check gate on it (never imply completeness over an unclassified sleeve).
#: A locked, auditable strategy constant like :data:`CONCENTRATION_CEILING` — never
#: LLM-touched; ~0.80 means "at least 80% classified".
COVERAGE_MIN: Decimal = Decimal("0.80")

#: The bond-shortfall band (Story 11.2, Epic 11): the classified-sleeve bond weight must
#: fall MORE than this many percentage points (as a fraction) below the chosen model's bond
#: target before the risk-capacity check fires. Locked strategy constant like
#: :data:`CONCENTRATION_CEILING`; 0.15 = 15pp — catches "Conservative 60% bonds held at 25%"
#: while ignoring normal drift. Downside-only: over-bonded / within-band never fires.
BOND_SHORTFALL: Decimal = Decimal("0.15")

#: Finding kinds (the analysis buckets). Stable string keys — wire-friendly.
KIND_CONCENTRATION = "concentration"
KIND_COST = "cost"
KIND_BOND_FLOOR = "bond_floor"

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
    #: BOND_FLOOR only (Story 11.2): the chosen model's bond target as a fraction. For a
    #: bond-floor finding ``weight`` carries the CURRENT classified-sleeve bond fraction and
    #: ``target_weight`` the target — so the narration can honestly state "bonds are X% vs a
    #: Y% target". ``None`` for concentration/cost.
    target_weight: Decimal | None = None


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


@dataclass(frozen=True)
class _AggregatedHolding:
    """One symbol's summed position across the raw cache rows (pure, duck-typed).

    Carries only the fields the detectors read (``symbol``/``quantity``/
    ``market_value``); used to build the aggregated :class:`PortfolioView` fed to the
    detectors so a symbol split across multiple rows is one economic position."""

    symbol: str
    quantity: Decimal
    market_value: Decimal


def _aggregate_by_symbol(view: PortfolioView) -> PortfolioView:
    """Collapse holdings to ONE position per normalized symbol (pure).

    The cached portfolio can hold more than one row for the same ticker — the
    reconcile writer stores one row per broker snapshot position and enforces no
    ``(owner_id, symbol)`` uniqueness. Left un-aggregated, two rows of the same
    over-weight name would each be measured at its own (understated) weight and each
    clear the concentration ceiling, surfacing as SEPARATE trims — a human co-signing
    both oversells the real position. Summing quantity + market_value per symbol
    first makes the detectors see the true single position (correct weight, one
    trim). Cash + as_of pass through unchanged; unsymboled rows are dropped (the
    detectors skip them anyway). Insertion order is preserved for determinism."""
    sums: dict[str, list[Decimal]] = {}  # symbol -> [qty_sum, mv_sum]
    order: list[str] = []
    for h in view.holdings or []:
        symbol = (h.symbol or "").strip().upper()
        if not symbol:
            continue
        quantity = h.quantity if h.quantity is not None else _ZERO
        market_value = h.market_value if h.market_value is not None else _ZERO
        if symbol not in sums:
            sums[symbol] = [_ZERO, _ZERO]
            order.append(symbol)
        sums[symbol][0] += quantity
        sums[symbol][1] += market_value
    holdings = [
        _AggregatedHolding(symbol=s, quantity=sums[s][0], market_value=sums[s][1])
        for s in order
    ]
    return PortfolioView(holdings=holdings, cash=view.cash, as_of=view.as_of)


def find_bond_floor_finding(
    view: PortfolioView,
    target_weights: dict[str, Decimal] | None,
    coverage: Coverage | None,
    cash_config: CashConfig | None,
    deploy_bond_buy: Decimal,
) -> ReviewFinding | None:
    """Detect a material bond shortfall vs the chosen target and size a SELL of overweight
    equity into bonds for the residual cash can't cover (pure, Story 11.2).

    Downside-only + coverage-gated + defer-to-deploy (spec-11-2 D1–D4). Returns ``None``
    when: no target (``target_weights`` None); coverage absent/inadequate (11.1 gate); the
    classified sleeve is empty; the bond weight is within ``BOND_SHORTFALL`` of target or
    over it; the residual after the deploy plan's cash-funded bond buy is dust; or nothing is
    overweight to sell. Weights are measured on the CLASSIFIED sleeve (bonds ÷ Σ classified,
    D1). The SELL is sized as ``min(residual, class_overweight, holding_value)`` — never past
    the residual, the overweight class's target, or the holding's value — then whole-share
    floored (dust dropped). ``switch_to`` = the canonical bond fund to BUY next (narrated,
    like a cost switch). The SELL symbol is an index-core holding, so it can never collide
    with the concentration/cost buckets (both NON-index only) — no cross-bucket dedup needed."""
    if target_weights is None:
        return None
    if coverage is None or not coverage.adequate:
        return None
    target_bond = target_weights.get(BONDS)
    if target_bond is None or not target_bond.is_finite():
        return None

    parked_set: frozenset[str] = frozenset()
    if cash_config is not None:
        parked_set = frozenset(normalize_symbols(cash_config.parked_symbols))
    # Guard non-finite market_value the SAME way Story 11.1's compute_coverage does — drop
    # the unpriced rows BEFORE classifying/aggregating, so a NaN/Inf holding can't poison
    # classified_total / current_bond and size a garbage SELL (11.2 review — Medium; the
    # bond-floor path must inherit the finite-guard 11.1 added). None stays (treated as 0).
    finite_holdings = [
        h
        for h in (view.holdings or [])
        if h.market_value is None or h.market_value.is_finite()
    ]
    view = _aggregate_by_symbol(
        PortfolioView(holdings=finite_holdings, cash=view.cash, as_of=view.as_of)
    )
    by_class = classify_holdings(view.holdings or [], parked_set).by_class
    classified_total = sum((by_class.get(c, _ZERO) for c in ASSET_CLASSES), _ZERO)
    if classified_total <= _ZERO:
        return None

    current_bond = by_class.get(BONDS, _ZERO)
    current_bond_pct = current_bond / classified_total
    if target_bond - current_bond_pct <= BOND_SHORTFALL:
        return None  # within band or over-bonded → downside-only, no finding

    # Base-invariant bond gap (rebalance within the sleeve to hit target), minus the deploy
    # card's cash-funded bond buy → SELL only the residual (D3; safe direction — can only
    # under-size, never oversell, since a cash buy grows the base).
    bond_gap = (target_bond * classified_total - current_bond).quantize(_CENT)
    buy = deploy_bond_buy if deploy_bond_buy.is_finite() and deploy_bond_buy > _ZERO else _ZERO
    residual = (bond_gap - buy).quantize(_CENT)
    if residual <= _ZERO:
        return None  # cash covers it → defer entirely to the deploy card

    # Sell from the MOST-overweight equity class (largest positive gap vs its target).
    best_class: str | None = None
    best_over = _ZERO
    for cls in ASSET_CLASSES:
        if cls == BONDS:
            continue
        over = by_class.get(cls, _ZERO) - (target_weights.get(cls, _ZERO) * classified_total)
        if over > best_over:
            best_over = over
            best_class = cls
    if best_class is None or best_over <= _ZERO:
        return None  # nothing overweight to sell

    # Largest HELD index-core holding in that class is the one we sell.
    sell_symbol: str | None = None
    sell_mv = _ZERO
    sell_qty: Decimal | None = None
    for h in view.holdings or []:
        symbol = (h.symbol or "").strip().upper()
        if not symbol or asset_class_for(symbol) != best_class:
            continue
        mv = h.market_value if h.market_value is not None else _ZERO
        if not mv.is_finite() or mv <= _ZERO:
            continue
        if mv > sell_mv:
            sell_mv, sell_symbol, sell_qty = mv, symbol, h.quantity
    if sell_symbol is None:
        return None

    amount = min(residual, best_over.quantize(_CENT), sell_mv).quantize(_CENT)
    intent = _sell_intent(sell_symbol, amount, sell_mv, sell_qty)
    if intent is None:
        return None  # dust — under one whole share

    return ReviewFinding(
        kind=KIND_BOND_FLOOR,
        symbol=sell_symbol,
        asset_class=best_class,
        order_intent=intent,
        switch_to=CANONICAL_FUND.get(BONDS),  # BND — the bonds to buy next (narrated)
        amount=amount,
        holding_value=sell_mv,
        weight=current_bond_pct,  # CURRENT classified-sleeve bond fraction
        expense_ratio=None,
        cheaper_expense_ratio=None,
        as_of=_as_of_date(view.as_of),
        target_weight=target_bond,
    )


def find_review(
    view: PortfolioView,
    cash_config: CashConfig | None,
    target_weights: dict[str, Decimal] | None = None,
    coverage: Coverage | None = None,
    deploy_bond_buy: Decimal = _ZERO,
) -> list[ReviewFinding]:
    """Run the analysis detectors and return the ranked findings (pure).

    Ranked by SELL dollar ``amount`` descending, ties broken by ``symbol`` ascending
    (deterministic). "Nothing to fix" → an EMPTY list (the honest, valid output).

    Holdings are first aggregated to one position per symbol (:func:`_aggregate_by_symbol`)
    so a ticker split across multiple cache rows can never surface as two overlapping
    trims (a co-sign-into-oversell hazard).

    A single holding can qualify for BOTH the concentration and cost buckets — a non-index
    high-fee fund held over the ceiling. Surfacing both would double-count one position; the
    cost switch (whole position) subsumes the concentration trim, so we prefer cost and drop
    the redundant trim for that symbol. The Story-11.2 ``bond_floor`` finding (when its inputs
    are supplied) sells an INDEX-CORE holding, which can never collide with the concentration
    or cost symbols (both NON-index only), so it needs no cross-bucket dedup."""
    view = _aggregate_by_symbol(view)
    concentration = find_concentration_findings(view, cash_config)
    cost = find_cost_findings(view, cash_config)
    cost_symbols = {f.symbol for f in cost}
    findings = [f for f in concentration if f.symbol not in cost_symbols] + cost
    bond_floor = find_bond_floor_finding(
        view, target_weights, coverage, cash_config, deploy_bond_buy
    )
    if bond_floor is not None:
        findings.append(bond_floor)
    findings.sort(key=lambda f: (-f.amount, f.symbol))
    return findings


# --- Coverage meta-check (Story 11.1, Epic 11) -------------------------------


@dataclass(frozen=True)
class Coverage:
    """How much of the portfolio the review can actually classify (pure).

    ``coverage`` is the classifiable fraction (0..1): ``1 − unclassified_value / total``,
    where ``total`` is Σ holding market value + cash and ``unclassified_value`` is the
    non-index, NON-parked sleeve (single stocks / niche ETFs). Parked money-market
    (declared cash-equivalents, e.g. SWVXX) is KNOWN, not unclassified — it never inflates
    ``unclassified_value`` (:func:`~allocation.engine.classify_holdings` excludes it).
    ``adequate`` is ``coverage >= COVERAGE_MIN``. Informational only — carries NO order."""

    coverage: Decimal
    adequate: bool
    unclassified_value: Decimal
    unclassified_symbols: list[str]
    total: Decimal


def compute_coverage(
    view: PortfolioView, cash_config: CashConfig | None
) -> Coverage | None:
    """Compute the classifiable-coverage meta-check (pure, Story 11.1).

    Reuses :func:`~allocation.engine.classify_holdings` (the SAME split the deploy engine
    uses) so "unclassified" never disagrees between the two, and a declared parked
    money-market holding counts as known cash — never as an unseen holding. Returns
    ``None`` when there's nothing to measure (``total <= 0`` — never imported / empty), so
    the caller emits no coverage note and never divides by zero. ``coverage`` is clamped to
    ``[0, 1]`` defensively. Pure arithmetic — never invents a fact, never forecasts."""
    total = _total_portfolio_value(view)
    if total <= _ZERO:
        return None
    parked_set: frozenset[str] = frozenset()
    if cash_config is not None:
        parked_set = frozenset(normalize_symbols(cash_config.parked_symbols))
    # Guard per-holding non-finite market_value the SAME way the denominator does
    # (`_total_portfolio_value` skips non-finite): drop ONLY the unpriced row(s) before
    # classifying, so an unpriced holding is excluded from BOTH sides and can never zero
    # the whole unclassified sleeve and over-report coverage as 100% (Story 11.1 review —
    # Medium). ``None`` stays (``classify_holdings`` treats it as 0, matching the total).
    finite_holdings = [
        h
        for h in (view.holdings or [])
        if h.market_value is None or h.market_value.is_finite()
    ]
    classification = classify_holdings(finite_holdings, parked_set)
    unclassified = classification.unclassified_value
    if unclassified < _ZERO:
        unclassified = _ZERO
    coverage = _ONE - (unclassified / total)
    if coverage < _ZERO:
        coverage = _ZERO
    elif coverage > _ONE:
        coverage = _ONE
    return Coverage(
        coverage=coverage,
        adequate=coverage >= COVERAGE_MIN,
        unclassified_value=unclassified,
        unclassified_symbols=list(classification.unclassified_symbols),
        total=total,
    )


def coverage_message(cov: Coverage) -> str:
    """The deterministic, calm informational line for a LOW-coverage portfolio (pure).

    Authored to be honest + calm (no forecast, no FOMO, no nudge) and to state ONLY numbers
    the detector computed (never-invent): the classifiable percent and the unclassified
    dollar amount + symbols. NO LLM call — this is templated by construction."""
    pct = format_money((cov.coverage * _HUNDRED).quantize(_CENT))
    syms = ", ".join(cov.unclassified_symbols) if cov.unclassified_symbols else "some holdings"
    return (
        f"I can categorize about {pct}% of your portfolio into stocks and bonds. The rest — "
        f"${format_money(cov.unclassified_value.quantize(_CENT))} in {syms} — is in individual stocks and "
        "specialty funds I don't classify, so when I describe your mix, keep in mind I'm only "
        "describing the part I can see."
    )


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
            f"${format_money(finding.amount)} of {finding.symbol} lets you switch to the "
            "cheaper same-class fund."
        )
    elif finding.kind == KIND_BOND_FLOOR:
        stats["target_weight"] = finding.target_weight
        cur_pct = (finding.weight * _HUNDRED).quantize(_CENT)
        tgt_pct = ((finding.target_weight or _ZERO) * _HUNDRED).quantize(_CENT)
        statement = (
            f"Your bonds are {format_money(cur_pct)}% of your invested mix versus your "
            f"{format_money(tgt_pct)}% target — selling ${format_money(finding.amount)} of "
            f"{finding.symbol} into {finding.switch_to} moves you toward the risk level you chose."
        )
    else:
        weight_pct = (finding.weight * _HUNDRED).quantize(_CENT)
        statement = (
            f"Your {finding.symbol} is {format_money(weight_pct)}% of your portfolio — "
            f"trimming ${format_money(finding.amount)} brings it back toward the "
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


def _add_money(allowed: set[tuple[Decimal, str]], value: Decimal) -> None:
    """Admit a money amount as :data:`UNIT_MONEY` ONLY (pure, in-place). Mirrors
    :func:`allocation.narrate._add_money`: amounts are cited WITH a ``$`` in both the
    fallback and the LLM narration, so a bare integer equal to a real amount is not
    admitted — closing the money-magnitude laundering axis (Story 10.6)."""
    allowed.add((value, UNIT_MONEY))


def _add_weight_forms(allowed: set[tuple[Decimal, str]], weight: Decimal) -> None:
    """Admit a weight as the fraction (``0.55`` → :data:`UNIT_BARE`) AND the 0–100
    percent (``55`` → :data:`UNIT_PERCENT`) forms the narration might use (pure,
    in-place). The percent form is admitted at both full precision and cent-quantized
    so a rounded render (``55.00%``) is citable too (Story 10.6 unit-tagged)."""
    allowed.add((weight, UNIT_BARE))
    pct = weight * _HUNDRED
    allowed.add((pct, UNIT_PERCENT))
    allowed.add((pct.quantize(_CENT), UNIT_PERCENT))


def allowed_review_facts(finding: ReviewFinding) -> frozenset[tuple[Decimal, str]]:
    """The engine-provided numeric allow-set for :func:`check_no_invented_numbers`.

    Every number the narration may legitimately state, tagged by UNIT as a
    ``(Decimal, unit)`` pair (Story 10.6): the SELL amount + the holding market value
    as :data:`UNIT_MONEY`; the holding weight and the concentration ceiling as BOTH
    the fraction (:data:`UNIT_BARE`) and percent (:data:`UNIT_PERCENT`); and — for a
    cost switch — BOTH expense ratios as percents (cited "0.61%"). The fee values come
    ONLY from the expense-ratio table; a wrong-but-plausible ER the LLM invents is not
    admitted, so the never-invent gate rejects it. Pure."""
    allowed: set[tuple[Decimal, str]] = set()
    _add_money(allowed, finding.amount)
    _add_money(allowed, finding.holding_value)
    _add_weight_forms(allowed, finding.weight)
    _add_weight_forms(allowed, CONCENTRATION_CEILING)
    if finding.expense_ratio is not None:
        allowed.add((finding.expense_ratio, UNIT_PERCENT))
    if finding.cheaper_expense_ratio is not None:
        allowed.add((finding.cheaper_expense_ratio, UNIT_PERCENT))
    # BOND_FLOOR (Story 11.2): admit the target bond weight AND the shortfall (target −
    # current), both fraction+percent, so the narration may state "bonds are X% vs a Y%
    # target, Z below it". ``weight`` (current bond) is already admitted above.
    if finding.target_weight is not None:
        _add_weight_forms(allowed, finding.target_weight)
        _add_weight_forms(allowed, finding.target_weight - finding.weight)
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
    is_bond_floor = (
        finding.kind == KIND_BOND_FLOOR
        and finding.target_weight is not None
        and finding.switch_to
    )
    is_cost = (
        finding.kind == KIND_COST
        and finding.expense_ratio is not None
        and finding.cheaper_expense_ratio is not None
        and finding.switch_to
    )
    if is_bond_floor:
        cur_pct = format_money((finding.weight * _HUNDRED).quantize(_CENT))
        tgt_pct = format_money(((finding.target_weight or _ZERO) * _HUNDRED).quantize(_CENT))
        action_label = "Add to your bonds to match the risk level you chose"
        reasoning = (
            f"Your bonds are {cur_pct}% of your invested mix, while the plan you chose aims "
            f"for {tgt_pct}% — you are holding more in stocks than your risk level calls for. "
            "The principle here is risk capacity: bonds cushion the ride, so a portfolio that "
            "matches your chosen plan holds you steadier when markets fall. This is a two-step "
            f"move: step one is to sell ${format_money(finding.amount)} of {finding.symbol} "
            f"from your overweight stocks now; step two, the follow-up buy of the broad "
            f"{finding.switch_to} bond fund, is queued and linked to this sell so it is ready "
            "to review the moment the cash settles — you are never left stranded in cash. It "
            "is a rebalance toward your own plan, not a bet on where markets go next. The "
            "tradeoff is honest: more in bonds means a bit less upside in a strong market, in "
            "exchange for a steadier ride; and selling may realize a taxable gain, which we do "
            "not calculate here, so weigh that before you co-sign."
        )
        uncertainties = (
            "Markets move, so a fill isn't guaranteed and this isn't a prediction — it's "
            "simply bringing your bonds up to the plan you chose; and selling may have a tax "
            "consequence we don't calculate for you.",
        )
    elif is_cost:
        action_label = "Switch this pricey fund for a cheaper one that holds the same thing"
        reasoning = (
            f"Your {finding.symbol} charges a {format_money(finding.expense_ratio)}% "
            "yearly fee, while the broad "
            f"{finding.switch_to} index fund covering the same asset class charges "
            f"{format_money(finding.cheaper_expense_ratio)}% — a gap that quietly "
            "compounds against you over decades. The principle here is to minimize "
            "fund fees: paying less for the same broad exposure keeps more of your "
            "money working for you. This is a two-step switch: step one is to sell "
            f"${format_money(finding.amount)} of {finding.symbol} now; step two, the "
            f"follow-up buy of the cheaper {finding.switch_to}, is queued and linked "
            "to this sell so it is ready for you to review the moment the cash "
            "settles — you are never left stranded in cash if you step away. It is a "
            "like-for-like switch, not a bet on a hot pick. The tradeoff is honest: "
            "selling may realize a taxable gain, and we don't calculate tax here, so "
            "weigh that before you co-sign."
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
            f"${format_money(finding.amount)} of {finding.symbol} back toward that "
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
    "fund, switching to a cheaper same-class index fund because low costs compound; "
    "for a bond shortfall, selling some overweight stock into a broad bond fund to "
    "bring bonds up to the RISK LEVEL THE USER CHOSE (a rebalance toward their own "
    "plan, never a market call — bonds cushion drawdowns). "
    "For a fee switch, frame it as a LINKED TWO-STEP switch: step one sells the "
    "high-fee fund now; step two, the follow-up buy of the cheaper fund, is queued "
    "and linked to that sell so it is ready to review once the cash settles — the "
    "user is never stranded in cash. Note honestly that selling may realize a "
    "taxable gain and that we do NOT calculate tax here — never compute a tax "
    "number. Cite ONLY the "
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
        f"- sell amount: ${format_money(finding.amount)}",
        f"- holding market value: ${format_money(finding.holding_value)}",
    ]
    if finding.kind == KIND_CONCENTRATION:
        weight_pct = format_money((finding.weight * _HUNDRED).quantize(_CENT))
        lines.append(
            f"- single-position ceiling: {format_money(CONCENTRATION_CEILING * _HUNDRED)}%"
        )
        lines.append(f"- this position's weight: {weight_pct}% of the portfolio")
        lines.append(
            "- goal: trim this oversized single stock back toward the ceiling and "
            "into the broad diversified index core (de-speculate, do not chase)"
        )
    elif finding.kind == KIND_BOND_FLOOR:
        cur_pct = format_money((finding.weight * _HUNDRED).quantize(_CENT))
        tgt_pct = format_money(((finding.target_weight or _ZERO) * _HUNDRED).quantize(_CENT))
        lines.append(f"- current bonds: {cur_pct}% of your invested mix")
        lines.append(f"- your chosen target bonds: {tgt_pct}%")
        lines.append(f"- buy next (canonical broad bond fund): {finding.switch_to}")
        lines.append(
            "- goal: sell this overweight stock holding and buy the broad bond fund to "
            "bring bonds up to the risk level the user CHOSE — a rebalance toward their "
            "own plan, NOT a market call; note honestly that selling may realize a "
            "taxable gain and that tax is NOT calculated here"
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
    # Story 11.2 inputs for the bond-floor check: the resolved target weights (same resolver
    # the deploy engine uses — never a cross-model guess), the 11.1 coverage gate, and the
    # deploy plan's cash-funded bond buy (so bond-floor SELLs only the residual cash can't
    # cover — D3). Compute the deploy plan ONLY when a bond-floor finding is even possible
    # (target chosen + coverage adequate), to avoid the extra read/work otherwise.
    resolved = resolve_target_config(await get_target_config(scope, session))
    target_weights = resolved["weights"] if resolved else None
    coverage = compute_coverage(view, cash_config)
    deploy_bond_buy = _ZERO
    if target_weights is not None and coverage is not None and coverage.adequate:
        plan = await build_plan(scope, session)
        deploy_bond_buy = sum(
            (it.amount for it in plan.action_items if it.asset_class == BONDS), _ZERO
        )
    findings = find_review(view, cash_config, target_weights, coverage, deploy_bond_buy)
    # "Nothing to fix" is valid — no findings means NO LLM call.
    return [
        NarratedFinding(finding=f, narration=narrate_finding(gateway, f))
        for f in findings
    ]


async def build_coverage(scope: Scope, session: AsyncSession) -> Coverage | None:
    """Resolve the caller's cached portfolio + cash config and compute coverage (Story 11.1).

    READ-ONLY and fail-closed per-user (AD-10): reads only THIS user's cached holdings +
    scoped cash config — no live broker session, no writes, no LLM. Returns ``None`` when
    nothing is imported (``total <= 0``). The ``adequate`` flag on the result is the signal
    Story 11.2 (bond-floor) / any target-drift check hard-gate on before trusting the
    class-level mix."""
    view = await get_portfolio(scope, session)
    cash_config = await get_cash_config(scope, session)
    return compute_coverage(view, cash_config)
