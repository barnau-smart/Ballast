"""The gap-to-target deploy-my-cash engine (Story 10.2, Epic 10 Allocation Coach).

Answers the beginner's real freeze — *"I have $2k of idle cash, what do I
actually buy?"* — with a **pure, deterministic, cash-only rebalance plan**: group
the user's holdings by asset class, compare to their resolved 10-1 target, compute
investable cash (Epic 9: ``ready_to_trade − reserve``), and produce concrete BUYs
that close the largest gaps *toward* target.

Design guardrails (locked, non-negotiable):

- **Pure/deterministic core.** :func:`classify_holdings` and
  :func:`plan_deployment` do NO I/O, no wall-clock, no RNG — same input yields an
  identical plan, so the math is trivially testable without a DB. Money is
  ``Decimal`` end to end (never binary float).
- **Rebalance toward target, never chase.** Only ever BUY the canonical fund of an
  *underweight* asset class; never deploy past a class's target. Leftover cash is
  left undeployed and reported honestly (``undeployed_cash``) — no chasing.
- **"Nothing to do" is valid.** Already at/above target where cash could help, or
  no investable cash → a calm no-action status. Never manufacture a trade.
- **Honest undecided.** An undecided target (10-1 → ``None``) → ``no_target``; a
  never-decided reserve (Epic 9 ``resolve_reserve`` → ``None``, or no config row at
  all) → ``decide_reserve`` — NEVER silently 0. Parked money-market is a SEPARATE
  holding pool (not part of settlement ``view.cash``), so it is already outside
  investable cash — never subtracted again.
- **Populate, don't submit.** This engine NEVER places an order and NEVER writes a
  ``decision_record``; :func:`build_plan` only *computes* a plan the human co-signs
  through the existing ``/approve`` spine.
- **Unclassified honesty.** Holdings that don't map to exactly one class (VT
  whole-world, single stocks, non-index ETFs) are surfaced in an ``unclassified``
  sleeve but EXCLUDED from the rebalance math — no invented VT split
  (concentration/trim is Story 10-4).
- **Per-user scoped, degraded-safe.** :func:`build_plan` reads the CACHED portfolio
  (``get_portfolio``) + the scoped configs — no live broker session required.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from allocation.config import get_config as get_target_config, resolve as resolve_target_config
from brokers.portfolio import get_portfolio
from cash.config import (
    get_config as get_cash_config,
    normalize_symbols,
    resolve_reserve,
)
from db.scope import Scope
from strategy.target_allocation import ASSET_CLASSES, asset_class_for

#: Drop deploy allocations below this to avoid dust orders (a sub-dollar buy is
#: noise, not a rebalance). If EVERY allocation drops, the plan falls through to a
#: calm ``at_target`` / ``no_cash`` no-action status.
MIN_DEPLOY = Decimal("1.00")

_CENT = Decimal("0.01")
_ZERO = Decimal("0")

# Plan statuses (the honest state machine).
STATUS_DEPLOY = "deploy"
STATUS_AT_TARGET = "at_target"
STATUS_NO_CASH = "no_cash"
STATUS_NO_TARGET = "no_target"
STATUS_DECIDE_RESERVE = "decide_reserve"


# --- Pure data holders -------------------------------------------------------


@dataclass(frozen=True)
class Classification:
    """The result of grouping holdings by asset class (pure).

    ``by_class`` maps each of the three asset classes → its total market value
    (``Decimal("0")`` when the user holds nothing in that class).
    ``unclassified_value`` / ``unclassified_symbols`` capture the sleeve that
    doesn't map to exactly one class (VT, single stocks, non-index ETFs) — surfaced
    honestly but EXCLUDED from the rebalance math.
    """

    by_class: dict[str, Decimal]
    unclassified_value: Decimal
    unclassified_symbols: list[str]


@dataclass(frozen=True)
class ActionItem:
    """One concrete cash-only BUY toward target: the canonical fund of an
    underweight asset class + the dollar amount to deploy."""

    asset_class: str
    symbol: str
    amount: Decimal


@dataclass(frozen=True)
class Deployment:
    """The deterministic result of :func:`plan_deployment` (pure).

    ``action_items`` are the per-class buys (sorted by amount desc);
    ``deployed`` is their exact sum; ``undeployed_cash`` is the investable cash
    left after deploying (leftover when cash exceeds the total positive gap, or
    when there's nothing underweight to add to)."""

    action_items: list[ActionItem]
    deployed: Decimal
    undeployed_cash: Decimal


@dataclass(frozen=True)
class Plan:
    """The full gap-to-target plan returned by :func:`build_plan`.

    ``status`` is one of the five honest states. ``action_items`` /
    ``primary_order`` are populated only for ``deploy``. ``current`` reports each
    class's market value + its share of the classified sleeve. ``target_weights``
    is the caller's RESOLVED per-class target mix (the user's own model, populated
    only for ``deploy``; empty for the no-action statuses) — exposed so a downstream
    narrator can cite the true target without re-resolving it (never a cross-model
    guess). ``reason`` is calm plain-English for the no-action statuses (empty for
    ``deploy``)."""

    status: str
    action_items: list[ActionItem] = field(default_factory=list)
    primary_order: ActionItem | None = None
    current: dict[str, dict[str, Decimal]] = field(default_factory=dict)
    target_weights: dict[str, Decimal] = field(default_factory=dict)
    unclassified_value: Decimal = _ZERO
    unclassified_symbols: list[str] = field(default_factory=list)
    investable_cash: Decimal = _ZERO
    undeployed_cash: Decimal = _ZERO
    # The honest funding split of ``investable_cash`` (Story 10.8 AC5), populated for
    # ``deploy``: ``settlement_cash`` is the part already settled + spendable now;
    # ``from_money_market`` is the part that comes from selling the user's parked
    # money-market fund (which settles first, via the 9-3 liquidation) —
    # ``settlement_cash + from_money_market == investable_cash``. ``reserve`` is the
    # user's PROTECTED cushion (never deployed, never sold). These let the narrator
    # honestly frame "$X is settled cash, $Y comes from selling your money-market;
    # your $Z reserve stays untouched" without inventing a number.
    settlement_cash: Decimal = _ZERO
    from_money_market: Decimal = _ZERO
    reserve: Decimal = _ZERO
    money_market_symbols: list[str] = field(default_factory=list)
    # The broker's account type (Story 10.10), carried from the portfolio view for a
    # gentle margin-account warning on the deploy card. Informational only — never
    # used in the deploy math. Present on every status.
    account_type: str | None = None
    reason: str = ""
    as_of: datetime | None = None


# --- Pure engine (no I/O) ----------------------------------------------------


def classify_holdings(holdings, parked_set: frozenset[str] = frozenset()) -> Classification:
    """Group ``holdings`` by asset class via ``SYMBOL_ASSET_CLASS`` (case-insensitive).

    Pure. Sums ``market_value`` per class; every asset class is present (0 when the
    user holds none of it). A holding whose symbol doesn't map to exactly one class
    (unmapped — VT whole-world, single stocks, non-index ETFs) accrues to the
    ``unclassified`` sleeve (total value + the de-duplicated, upper-cased symbols).

    ``parked_set`` (normalized upper symbols the user declared as parked money-market)
    routes a GENUINE cash-equivalent — a parked holding that is ALSO unclassified
    (``asset_class_for`` is ``None``, e.g. SWVXX) — to NEITHER a class NOR the
    unclassified sleeve: it is deployable CASH (counted once in ``investable`` via
    :func:`_deployable_parked`), not a holding. This closes the Story-10.8 review
    double-report (SWVXX appearing in both the unclassified sleeve AND investable). A
    parked symbol that IS classified (e.g. VTI tagged parked) STAYS in its asset class
    and is NOT treated as cash — so it is never double-counted in ``plan_deployment``'s
    ``base`` (classified) AND ``investable`` (parked); the parked tag is simply ignored
    for an index fund that belongs to a class.
    """
    by_class: dict[str, Decimal] = {cls: _ZERO for cls in ASSET_CLASSES}
    unclassified_value = _ZERO
    unclassified_symbols: list[str] = []
    seen_unclassified: set[str] = set()

    for h in holdings or []:
        symbol = (h.symbol or "").strip().upper()
        value = h.market_value if h.market_value is not None else _ZERO
        cls = asset_class_for(symbol)
        if cls is None:
            if symbol in parked_set:
                # Genuine parked cash-equivalent (unclassified + declared parked) —
                # counted as investable cash, never as an unclassified holding.
                continue
            unclassified_value += value
            if symbol and symbol not in seen_unclassified:
                seen_unclassified.add(symbol)
                unclassified_symbols.append(symbol)
        else:
            by_class[cls] += value

    return Classification(
        by_class=by_class,
        unclassified_value=unclassified_value,
        unclassified_symbols=unclassified_symbols,
    )


def _deployable_parked(
    holdings, parked_set: frozenset[str]
) -> tuple[Decimal, Decimal, str | None]:
    """Return ``(total, largest_value, largest_symbol)`` of the DEPLOYABLE parked cash.

    A holding is deployable parked cash iff its symbol is declared parked AND it is
    unclassified (``asset_class_for`` is ``None``) — i.e. a genuine money-market-style
    fund, NOT an index fund that belongs to an asset class (those stay in their class,
    never double-counted as cash). Mirrors the liquidation filter: an unpriced /
    non-finite / ≤0 ``market_value`` is skipped.

    ``total`` = Σ of all such holdings; ``largest_value`` = the biggest ONE of them and
    ``largest_symbol`` = ITS symbol (``None`` when nothing qualifies). The Story-9.3
    just-in-time liquidation sells ONLY the single largest parked fund, so
    ``largest_value`` caps what one liquidation can free (the deploy plan never promises
    more than a single settle-then-buy can cover), and ``largest_symbol`` is the ONE
    fund the narration/UI may honestly name as being sold — never the whole parked list
    (Story-10.8 review: naming every parked fund when only one is sold is a
    never-invent-a-fact violation). Ties break on the LOWEST symbol, matching the 9-3
    liquidation's ``_largest_parked_holding`` so the named fund is the one actually sold.
    """
    total = _ZERO
    largest = _ZERO
    largest_symbol: str | None = None
    for h in holdings or []:
        symbol = (h.symbol or "").strip().upper()
        if symbol not in parked_set or asset_class_for(symbol) is not None:
            continue
        mv = h.market_value
        if mv is None or not mv.is_finite() or mv <= _ZERO:
            continue
        total += mv
        if mv > largest or (mv == largest and (largest_symbol is None or symbol < largest_symbol)):
            largest = mv
            largest_symbol = symbol
    return total, largest, largest_symbol


def plan_deployment(
    current_by_class: dict[str, Decimal],
    target_weights: dict[str, Decimal],
    funds: dict[str, str],
    investable_cash: Decimal,
) -> Deployment:
    """Deterministic cash-only buy-only water-fill toward target (pure).

    ``base = Σ classified current + investable_cash``. Per class,
    ``gap = weight*base − current``; positive gaps are underweight.
    ``to_deploy = min(investable_cash, Σ positive_gap)`` — never past a class's
    target (never chase). Split ``to_deploy`` proportionally to each positive gap,
    ``quantize(0.01)``, and assign any residual cent to the LARGEST-gap class. Drop
    sub-``MIN_DEPLOY`` dust. The SURVIVING action items sum to ``to_deploy`` MINUS
    any dropped sub-``MIN_DEPLOY`` dust (and minus any sub-cent tail the per-class
    caps can't absorb); that dropped cash is always returned to ``undeployed_cash``
    (``undeployed_cash = investable_cash − Σ deployed``).

    Same input → equal output (a deterministic tiebreak on equal gaps keeps the
    ordering stable). Only ever BUYs — an overweight class is never touched (that
    would be selling, Story 10-4).
    """
    cash = investable_cash if investable_cash > _ZERO else _ZERO
    classified_total = sum(
        (current_by_class.get(cls, _ZERO) for cls in ASSET_CLASSES), _ZERO
    )
    base = classified_total + cash

    # Positive gaps only (underweight classes the cash can add to).
    gaps: list[tuple[str, Decimal]] = []
    for cls in ASSET_CLASSES:
        weight = target_weights.get(cls, _ZERO)
        current = current_by_class.get(cls, _ZERO)
        gap = (weight * base) - current
        if gap > _ZERO:
            gaps.append((cls, gap))

    total_positive_gap = sum((g for _, g in gaps), _ZERO)
    to_deploy = cash if cash < total_positive_gap else total_positive_gap

    if to_deploy <= _ZERO or not gaps:
        return Deployment(action_items=[], deployed=_ZERO, undeployed_cash=cash)

    # Deterministic order: largest gap first, ties broken by the fixed asset-class
    # order (index in ASSET_CLASSES) so the split + residual-cent placement is
    # reproducible.
    class_order = {cls: i for i, cls in enumerate(ASSET_CLASSES)}
    gaps.sort(key=lambda item: (-item[1], class_order[item[0]]))

    # The honest per-class cent-cap: the most we can deploy toward a class WITHOUT
    # exceeding its true (possibly sub-cent) gap is the gap ROUNDED DOWN to cents.
    # Enforcing this everywhere (initial split AND residual reconciliation) keeps a
    # rounding cent from ever nudging an allocation past that class's target.
    gap_cap = {cls: gap.quantize(_CENT, rounding=ROUND_DOWN) for cls, gap in gaps}

    # Proportional split, quantized to cents, never above the cent-cap.
    raw: dict[str, Decimal] = {}
    for cls, gap in gaps:
        share = (to_deploy * gap) / total_positive_gap
        alloc = share.quantize(_CENT)
        if alloc > gap_cap[cls]:
            alloc = gap_cap[cls]
        raw[cls] = alloc

    # Reconcile the quantization residual so Σ alloc == to_deploy exactly (as far as
    # the cent-caps allow). Add (or shave) leftover cents starting at the largest-gap
    # class, never pushing a class past its cent-cap. Any residual the caps can't
    # absorb (to_deploy's sub-cent tail) simply stays undeployed — honest, never a
    # phantom over-target cent.
    allocated = sum(raw.values(), _ZERO)
    residual = (to_deploy - allocated).quantize(_CENT)
    if residual != _ZERO:
        step = _CENT if residual > _ZERO else -_CENT
        remaining = residual
        # Multiple passes in case a single class's headroom can't absorb it all.
        while remaining != _ZERO:
            progressed = False
            for cls, _gap in gaps:
                if remaining == _ZERO:
                    break
                if step > _ZERO and raw[cls] + step <= gap_cap[cls]:
                    raw[cls] += step
                    remaining -= step
                    progressed = True
                elif step < _ZERO and raw[cls] + step >= _ZERO:
                    raw[cls] += step
                    remaining -= step
                    progressed = True
            if not progressed:
                break

    # Build action items, dropping sub-MIN_DEPLOY dust, sorted by amount desc
    # (ties by the fixed class order for determinism).
    items = [
        ActionItem(asset_class=cls, symbol=funds.get(cls, ""), amount=raw[cls])
        for cls, _ in gaps
        if raw[cls] >= MIN_DEPLOY
    ]
    items.sort(key=lambda it: (-it.amount, class_order[it.asset_class]))

    deployed = sum((it.amount for it in items), _ZERO)
    undeployed = cash - deployed
    return Deployment(
        action_items=items, deployed=deployed, undeployed_cash=undeployed
    )


def _current_breakdown(
    by_class: dict[str, Decimal],
) -> dict[str, dict[str, Decimal]]:
    """Per-class market value + its weight within the classified sleeve (pure).

    ``weight`` is the class's share of the total classified value (0 when nothing
    is classified) — the honest "here's where you are today" picture, quantized to
    a stable 4dp so the wire value is deterministic.
    """
    total = sum(by_class.values(), _ZERO)
    out: dict[str, dict[str, Decimal]] = {}
    for cls in ASSET_CLASSES:
        value = by_class.get(cls, _ZERO)
        weight = (value / total).quantize(Decimal("0.0001")) if total > _ZERO else _ZERO
        out[cls] = {"market_value": value, "weight": weight}
    return out


# --- Scoped orchestrator (reads only; never writes) --------------------------


async def build_plan(scope: Scope, session: AsyncSession) -> Plan:
    """Resolve the caller's target + investable cash + holdings, then return a
    deterministic gap-to-target :class:`Plan` (Story 10.2).

    READ-ONLY and degraded-safe: reads the CACHED portfolio (``get_portfolio``)
    and the scoped 10-1 / Epic-9 configs — no live broker session, no writes, no
    order placement. Fail-closed per-user (AD-10): only THIS user's holdings, cash,
    and configs are ever touched.

    The honest state machine (checked in this order):

    1. Undecided target (10-1 → ``None``) → ``no_target``.
    2. Never-decided reserve (no cash config, or ``resolve_reserve`` → ``None``) →
       ``decide_reserve`` (NEVER silently 0).
    3. Investable cash (``settlement view.cash + parked money-market − reserve``;
       parked MM is deployable, reserve out of the total) ≤ 0 → ``no_cash``.
    4. No underweight class the cash can add to (or every allocation is dust) →
       ``at_target``.
    5. Otherwise → ``deploy`` with the per-class buys + the largest-gap
       ``primary_order``.
    """
    view = await get_portfolio(scope, session)
    as_of = view.as_of
    account_type = view.account_type  # Story 10.10 — margin-account warning (info only)

    # (1) Resolved target — undecided → no_target (never a fabricated target).
    target_config = await get_target_config(scope, session)
    resolved = resolve_target_config(target_config)
    # Read the cash config up front so classification is parked-aware: a genuine
    # parked cash-equivalent (declared + unclassified, e.g. SWVXX) is routed to
    # NEITHER a class NOR the unclassified sleeve — it is deployable cash, counted
    # once in ``investable`` (Story-10.8 review: no double-report / double-count).
    cash_config = await get_cash_config(scope, session)
    parked_set = (
        frozenset(normalize_symbols(cash_config.parked_symbols))
        if cash_config is not None
        else frozenset()
    )
    classification = classify_holdings(view.holdings, parked_set)
    current = _current_breakdown(classification.by_class)

    if resolved is None:
        return Plan(
            status=STATUS_NO_TARGET,
            current=current,
            unclassified_value=classification.unclassified_value,
            unclassified_symbols=classification.unclassified_symbols,
            investable_cash=_ZERO,
            undeployed_cash=_ZERO,
            reason=(
                "Pick a target mix first, and I'll show you how to move your cash "
                "toward it."
            ),
            account_type=account_type,
            as_of=as_of,
        )

    target_weights = resolved["weights"]
    funds = resolved["funds"]

    # (2) Reserve honesty — never-decided (no config row OR resolve_reserve None)
    # is decide_reserve, NOT silently 0. A declined reserve resolves to 0 and
    # proceeds normally.
    reserve = resolve_reserve(cash_config) if cash_config is not None else None
    if reserve is None:
        return Plan(
            status=STATUS_DECIDE_RESERVE,
            current=current,
            unclassified_value=classification.unclassified_value,
            unclassified_symbols=classification.unclassified_symbols,
            investable_cash=_ZERO,
            undeployed_cash=_ZERO,
            reason=(
                "Set your cash cushion first — how much you'd like to keep on hand "
                "— and I'll only ever deploy what's above it."
            ),
            account_type=account_type,
            as_of=as_of,
        )

    # (3) Investable cash = settlement + single-fund-liquidatable parked − reserve
    # (Story 10.8, hardened by its 2026-08-13 review; margin-debit clamp aligned in 10.13).
    # The investable base is the RAW settlement balance ``view.cash`` — a negative margin-debit
    # balance correctly REDUCES investable (the debt must be covered before deploying), matching
    # the raw-cash 9-3 liquidation. ``settlement_cash`` (the SPENDABLE figure shown on the wire)
    # is separately clamped ≥ 0 so it never displays negative.
    # Parked money-market — the user's DECLARED, unclassified ``parked_symbols`` (e.g.
    # SWVXX) — is a cash-equivalent they hold instead of idle cash, so it IS deployable.
    # The reserve is a cushion out of the TOTAL available (settlement + parked).
    #
    # SINGLE-FUND CAP (review fix): the Story-9.3 just-in-time liquidation sells only the
    # SINGLE largest parked fund, so the deployable parked contribution is capped at
    # ``largest_parked`` — the plan never promises more cash than one settle-then-buy can
    # actually free. The identity ``investable = settlement + min(largest_parked,
    # parked_total − reserve)`` yields, per the derivation, ``min(cash_on_hand_after_one
    # _liquidation, settlement + parked − reserve)`` — i.e. reserve out of total AND never
    # more than a single liquidation frees. (``min`` term may go negative when reserve
    # exceeds parked, correctly drawing the excess reserve from settlement → possibly
    # ``no_cash``.)
    #
    # EXECUTION SAFETY: counting parked here is ANALYSIS only. A deploy buy beyond
    # settlement is funded at co-sign by liquidating the parked fund first (Story 9.3),
    # and — as of Story 10.9 — ``execute_approved_order`` REFUSES any buy exceeding real
    # settled cash, so a buy ALWAYS places against real cash, never margin.
    # ``settlement_cash`` is what's actually SPENDABLE now — clamped ≥ 0 for display so a
    # margin-debit balance never shows a negative "settled cash" on the wire (Story 10.8 AC5).
    settlement_cash = max(_ZERO, view.cash)
    parked_total, largest_parked, largest_parked_symbol = _deployable_parked(
        view.holdings, parked_set
    )
    if not reserve.is_finite() or reserve < _ZERO:
        reserve = _ZERO
    parked_after_reserve = parked_total - reserve
    liquidatable_parked = (
        largest_parked if largest_parked < parked_after_reserve else parked_after_reserve
    )
    # Story 10.13: the investable base is the RAW settlement balance (``view.cash``), NOT
    # ``max(0, view.cash)`` — so a negative margin-DEBIT balance correctly REDUCES investable
    # by what's owed, matching the raw-cash 9-3 ``plan_liquidation`` (the correct side). A
    # normal account (``view.cash ≥ 0``) is byte-identical (raw == the clamp), so nothing
    # changes there; a margin-debit account no longer over-promises a deploy the liquidation
    # can't cover. A deep debt drives ``investable ≤ 0 → no_cash`` (correct).
    investable = view.cash + liquidatable_parked
    if not investable.is_finite() or investable <= _ZERO:
        return Plan(
            status=STATUS_NO_CASH,
            current=current,
            unclassified_value=classification.unclassified_value,
            unclassified_symbols=classification.unclassified_symbols,
            investable_cash=_ZERO,
            undeployed_cash=_ZERO,
            reason=(
                "There's no investable cash to put to work right now — your "
                "ready-to-trade cash is already at or below your cushion. Nothing "
                "to do here."
            ),
            account_type=account_type,
            as_of=as_of,
        )

    # Honest funding split (Story 10.8 AC5 / 10.13): ``settlement_cash`` (computed above as
    # ``max(0, view.cash)``, never negative) is what's spendable now; the REMAINDER of
    # ``investable`` nets from selling the parked money-market. Invariant preserved:
    # ``settlement_cash + from_money_market == investable``. On a normal account this equals
    # the old ``liquidatable_parked``; on a margin-debit account ``settlement_cash`` is 0 and
    # ``from_money_market`` is the whole (debt-reduced) ``investable`` — honest about the fact
    # that all deployable cash comes from the money-market after the proceeds cover the debit.
    from_money_market = investable - settlement_cash
    # Name ONLY the single fund the 9-3 liquidation actually sells (the largest) —
    # never the whole parked list, which would misstate what's being sold.
    money_market_symbols = (
        [largest_parked_symbol]
        if from_money_market > _ZERO and largest_parked_symbol is not None
        else []
    )

    # (4)/(5) Deterministic cash-only rebalance.
    deployment = plan_deployment(
        classification.by_class, target_weights, funds, investable
    )

    if not deployment.action_items:
        # Nothing underweight the cash could add to (or all allocations were dust):
        # closing the rest would require selling — not done here (Story 10-4).
        return Plan(
            status=STATUS_AT_TARGET,
            current=current,
            unclassified_value=classification.unclassified_value,
            unclassified_symbols=classification.unclassified_symbols,
            investable_cash=investable,
            undeployed_cash=investable,
            reason=(
                "There's nothing worth buying toward your target with the cash "
                "that's free right now — what's investable is too small to move "
                "your mix. Nothing to do here."
            ),
            account_type=account_type,
            as_of=as_of,
        )

    primary = deployment.action_items[0]
    return Plan(
        status=STATUS_DEPLOY,
        action_items=deployment.action_items,
        primary_order=primary,
        current=current,
        target_weights=dict(target_weights),
        unclassified_value=classification.unclassified_value,
        unclassified_symbols=classification.unclassified_symbols,
        investable_cash=investable,
        undeployed_cash=deployment.undeployed_cash,
        settlement_cash=settlement_cash,
        from_money_market=from_money_market,
        reserve=reserve,
        money_market_symbols=money_market_symbols,
        reason="",
        account_type=account_type,
        as_of=as_of,
    )
