"""The just-in-time liquidation planner (Story 9.3, Epic 9 Cash Intelligence).

The deterministic money-math + LLM-narration owner for the buy-step liquidation
surface. When a beginner's decided buy exceeds their instantly-spendable
``ready_to_trade`` cash, this computes a pre-filled money-market **SELL** for the
shortfall (drawn only from parked funds ABOVE the declared reserve — the reserve
is never liquidated) and hands the FINISHED numbers to the LLM only to narrate in
plain English. The model NEVER does money-math; the backend owns
``sell_amount``/``est_shares`` entirely. Mirrors the 8.4 suggest-and-populate DNA
(:mod:`coach.suggest`).

Two invariants make the plan safe and testable:

- **The backend owns the number, and there is NO live-quote dependency in the
  plan step.** :func:`plan_liquidation` is a pure, deterministic function of the
  user's cached ``PortfolioView`` (holdings + ``ready_to_trade`` cash) and their
  :class:`~db.models.CashConfig`. Parked holdings are priced off their CACHED
  ``unit = market_value / quantity`` (money-market NAV is stable), never a live
  quote — the plan is an honest estimate, not a promise; live pricing /
  whole-share flooring happens for real only at ``/approve`` placement (the
  adapter sizes off the live ask), exactly as buys already work. Every figure
  carries ``as_of`` so a stale price can't make a calm-looking number silently
  wrong.
- **Narration is downstream of, and blind to, the math.** The plan is computed
  BEFORE the gateway is touched; :func:`narrate_liquidation` receives the
  finished numbers as FACTS to explain and returns only prose. A gateway outage
  degrades to a deterministic templated sentence (mirroring
  :func:`coach.suggest.narrate_suggestion`) — it never crashes and never blocks
  the plan.

v1 sells the SINGLE largest-value parked holding only — never chains multiple
funds. Partial coverage is surfaced honestly (``coverable=False`` with a "covers
$Y of $X" figure); a durable pending buy is recorded by the endpoint regardless
so the intent survives.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from brokers.portfolio import get_portfolio
from cash.config import (
    get_config,
    normalize_symbols,
    parked_market_value,
    resolve_reserve,
)
from coach.execution import whole_share_quantity
from coach.recommendation import OrderIntent, OrderSide, OrderType
from db.scope import Scope
from llm.port import LLMGateway, LLMMessage, LLMRequest

#: The tiny narration-only output schema for the LLM (Story 9.3). Deliberately
#: SEPARATE from the recommend/suggest schemas — liquidation is its own surface
#: and the model returns ONLY prose.
LIQUIDATION_NARRATION_SCHEMA: dict = {
    "type": "object",
    "properties": {"reasoning": {"type": "string"}},
    "required": ["reasoning"],
    "additionalProperties": False,
}

_LIQUIDATION_SYSTEM = (
    "You are Ballast's calm, honest investing coach. A beginner wants to buy a "
    "broad index fund, but some of the money they'd use is in a money-market fund "
    "that must be sold and settled first. A money-market SELL has ALREADY been "
    "computed for them — the numbers are final and you must NOT change, recompute, "
    "or second-guess them. Explain in one short, warm, plain-English paragraph "
    "what this sell does and why it's a normal, unhurried step: it frees up cash "
    "for the buy they chose, their protected reserve is untouched, and nothing is "
    "placed until they review and submit it themselves. Never hype, never rush, "
    "never alarm. Return only the reasoning."
)


@dataclass(frozen=True)
class LiquidationPlan:
    """A deterministic just-in-time liquidation plan + its narration (Story 9.3).

    All money fields are ``Decimal`` (the API serializes them to fixed-point
    strings on the wire). ``needs_liquidation`` is ``True`` iff the decided buy's
    ``amount`` exceeds ``ready_to_trade``. When it does, ``shortfall`` is the
    positive gap; ``coverable`` is ``True`` iff the parked value ABOVE the reserve
    covers the whole shortfall.

    When there is something to sell (parked value above the reserve > 0), the plan
    pre-fills a money-market SELL of the SINGLE largest-value parked holding:
    ``sell_symbol``, ``sell_amount`` (the shortfall, or the whole available parked
    value of that holding when it can't cover the shortfall), ``est_shares``
    (whole-share estimate off the CACHED unit price), and ``sell_order_intent``
    (a ready-to-approve :class:`~coach.recommendation.OrderIntent`). When nothing
    is liquidatable those sell fields are ``None`` (the pending buy still records).

    ``reserved`` is the RESOLVED reserve (amount / 0 / ``None`` never-decided) and
    ``reserve_decided`` drives honest reserve framing. ``as_of`` is the portfolio
    snapshot timestamp (data-freshness, surfaced on every figure). ``reasoning``
    is the LLM's prose (or a deterministic templated fallback). This engine PLACES
    NOTHING — the human runs the unchanged ``/approve`` co-sign path.
    """

    needs_liquidation: bool
    coverable: bool
    ready_to_trade: Decimal
    shortfall: Decimal
    sell_symbol: str | None
    sell_amount: Decimal | None
    est_shares: int | None
    sell_order_intent: OrderIntent | None
    reserved: Decimal | None
    reserve_decided: bool
    as_of: datetime | None
    reasoning: str


def _largest_parked_holding(holdings, parked_set: set[str], *, exclude_symbol: str = ""):
    """Return the single largest-``market_value`` parked holding, or ``None``.

    PURE: selects deterministically over the passed-in holdings — a holding is
    parked when its normalized symbol is in ``parked_set`` (reuse
    :func:`~cash.config.normalize_symbols` so the compare rule matches how symbols
    were stored). Ties break on symbol (ascending) so the choice is reproducible.
    A holding with a non-positive/non-finite ``market_value`` or ``quantity`` is
    skipped (it can't be sensibly priced/sold). ``None`` when nothing qualifies.

    ``exclude_symbol`` (already normalized upper) is never selected — so the
    planner never proposes selling the very fund the user is trying to buy (a user
    can tag their buy target as parked; the index-core BUY gate only runs later at
    /approve, not here).
    """
    candidates = [
        h
        for h in holdings
        if h.symbol
        and (h.symbol or "").strip().upper() != exclude_symbol
        and any(sym in parked_set for sym in normalize_symbols([h.symbol]))
        and h.market_value is not None
        and h.market_value.is_finite()
        and h.market_value > 0
        and h.quantity is not None
        and h.quantity.is_finite()
        and h.quantity > 0
    ]
    if not candidates:
        return None
    # Largest market_value wins; symbol ascending breaks ties (deterministic).
    return max(candidates, key=lambda h: (h.market_value, _neg_symbol(h.symbol)))


def _neg_symbol(symbol: str) -> str:
    """A tie-break key so that, for equal ``market_value``, the LOWEST symbol wins.

    ``max`` picks the largest; inverting each character's ordinal makes the
    alphabetically-first symbol sort largest, so ties resolve to the lowest symbol
    deterministically (no locale/None surprises).
    """
    return "".join(chr(0x10FFFF - ord(c)) for c in symbol.strip().upper())


def _fallback_reasoning(
    *,
    sell_symbol: str | None,
    sell_amount: Decimal | None,
    coverable: bool,
    shortfall: Decimal,
    reserved: Decimal | None,
) -> str:
    """A deterministic, calm templated reasoning string (gateway-outage fallback).

    Mirrors :func:`coach.suggest._fallback_reasoning` resilience: if the LLM
    narration fails for ANY reason, the plan is NEVER blocked — this plain
    sentence stands in. It states the already-computed facts honestly (numbers
    unchanged) and stays calm/never-alarmist. Covers the three honest shapes:
    a coverable sell, a partial-coverage sell, and nothing-to-liquidate.
    """
    reserve_clause = ""
    if reserved is not None and reserved > 0:
        reserve_clause = (
            f" Your ${reserved:.2f} reserve stays protected — it's never sold."
        )
    if sell_symbol is None or sell_amount is None:
        return (
            "You don't have any money-market funds to sell right now, so there's "
            f"nothing to free up toward the ${shortfall:.2f} you're short. This "
            "will resume on its own once enough cash settles — nothing to do for "
            "now." + reserve_clause
        )
    if coverable:
        return (
            f"This sells about ${sell_amount:.2f} of your {sell_symbol} "
            "money-market fund to free up the cash for the buy you chose. Nothing "
            "is placed until you review and submit it yourself." + reserve_clause
        )
    return (
        f"This sells about ${sell_amount:.2f} of your {sell_symbol} money-market "
        f"fund — that covers ${sell_amount:.2f} of the ${shortfall:.2f} you're "
        "short. The rest can be freed up later; nothing is placed until you "
        "review and submit it yourself." + reserve_clause
    )


def narrate_liquidation(gateway: LLMGateway, facts: dict) -> str:
    """Ask the LLM to NARRATE the already-computed liquidation numbers (Story 9.3).

    Composes an :class:`~llm.port.LLMRequest` feeding the FINISHED numbers as
    facts (never asking the model to compute anything) against the tiny
    :data:`LIQUIDATION_NARRATION_SCHEMA`, and returns ``output["reasoning"]``.
    Downstream of and blind to the math — the plan is computed BEFORE this call.

    RESILIENT: on ANY exception (a gateway transport/parse failure, a missing
    ``reasoning`` key, a non-string value) it returns a deterministic templated
    fallback — a narration outage never crashes and never blocks the plan
    (mirrors :func:`coach.suggest.narrate_suggestion`).
    """
    sell_symbol = facts.get("sell_symbol")
    sell_amount = facts.get("sell_amount")
    coverable = bool(facts.get("coverable"))
    shortfall = facts.get("shortfall", Decimal("0"))
    reserved = facts.get("reserved")
    try:
        lines = [
            "The money-market sell below is ALREADY computed and final — narrate "
            "it, do not change any number.",
            f"- shortfall to cover: ${shortfall:.2f}",
        ]
        if sell_symbol is not None and sell_amount is not None:
            lines.append(f"- sell (money-market fund): {sell_symbol}")
            lines.append(f"- sell amount: ${sell_amount:.2f}")
            lines.append(
                f"- fully covers the shortfall: {'yes' if coverable else 'no'}"
            )
            if not coverable:
                lines.append(
                    f"- this covers ${sell_amount:.2f} of ${shortfall:.2f}; the "
                    "rest can be freed up later"
                )
        else:
            lines.append(
                "- there is nothing to liquidate (no money-market funds above the "
                "reserve); this will resume when cash settles"
            )
        if reserved is not None and reserved > 0:
            lines.append(
                f"- protected reserve (NEVER sold): ${reserved:.2f}"
            )
        lines.append("- data freshness: prices are as of the last portfolio sync")
        request = LLMRequest(
            messages=(LLMMessage(role="user", content="\n".join(lines)),),
            output_schema=LIQUIDATION_NARRATION_SCHEMA,
            system=_LIQUIDATION_SYSTEM,
        )
        response = gateway.complete(request)
        reasoning = response.output.get("reasoning")
        if not isinstance(reasoning, str) or not reasoning.strip():
            raise ValueError("empty or non-string reasoning")
        return reasoning
    except Exception:
        return _fallback_reasoning(
            sell_symbol=sell_symbol,
            sell_amount=sell_amount,
            coverable=coverable,
            shortfall=shortfall,
            reserved=reserved,
        )


async def plan_liquidation(
    scope: Scope,
    session: AsyncSession,
    *,
    buy_symbol: str,
    buy_amount: Decimal,
    buy_intent: OrderIntent,
    gateway: LLMGateway,
) -> LiquidationPlan:
    """Compute a deterministic just-in-time liquidation plan + narrate it (Story 9.3).

    The orchestration owner (money-math lives here, out of the API layer). READS
    ONLY — never places, never writes; NO live quote:

    1. Read the user's cached ``PortfolioView`` (``ready_to_trade`` cash + holdings)
       and their :class:`~db.models.CashConfig`, both READ-ONLY through the scoped
       repo (a user only ever touches their OWN rows, AD-10).
    2. If ``buy_amount <= ready_to_trade`` there is no shortfall —
       ``needs_liquidation=False``; the buy proceeds normally (no sell, no pending
       buy). ``coverable`` is ``True`` (nothing to cover), sell fields ``None``.
    3. Otherwise the ``shortfall`` is ``buy_amount - ready_to_trade``. Compute the
       reserve-aware available parked value:
       ``available_parked = max(parked_market_value - resolve_reserve, 0)`` — the
       declared reserve is NEVER liquidated. ``coverable = available_parked >=
       shortfall``.
    4. Select the SINGLE largest-value parked holding (v1 never chains funds). If
       one exists, size the SELL: ``sell_amount = min(shortfall, its available
       market value above the reserve headroom)`` — but never less than nothing —
       priced off the CACHED ``unit = market_value / quantity`` for the whole-share
       estimate (:func:`~coach.execution.whole_share_quantity`). Build a
       ready-to-approve SELL :class:`OrderIntent` (amount-based, MARKET) exactly as
       buys are sized. If there is no parked holding to sell (all reserved / none),
       the sell fields stay ``None`` (a calm "nothing to liquidate yet").
    5. Narrate the FINISHED numbers via :func:`narrate_liquidation` (LLM prose or
       a resilient fallback).

    PLACES NOTHING — no ``place_order``, no ``decision_record``. The endpoint
    records the durable pending buy and (when coverable) the proposed SELL; the
    human executes via the unchanged ``/approve`` flow.
    """
    normalized_buy_symbol = (buy_symbol or "").strip().upper()

    view = await get_portfolio(scope, session)
    config = await get_config(scope, session)
    ready_to_trade = view.cash
    as_of = view.as_of

    reserved = resolve_reserve(config) if config is not None else None
    reserve_decided = config.reserve_decided if config is not None else False

    # No shortfall: the buy is fully covered by ready-to-trade cash.
    if not buy_amount.is_finite() or buy_amount <= ready_to_trade:
        reasoning = _fallback_reasoning(
            sell_symbol=None,
            sell_amount=None,
            coverable=True,
            shortfall=Decimal("0.00"),
            reserved=reserved,
        )
        return LiquidationPlan(
            needs_liquidation=False,
            coverable=True,
            ready_to_trade=ready_to_trade,
            shortfall=Decimal("0.00"),
            sell_symbol=None,
            sell_amount=None,
            est_shares=None,
            sell_order_intent=None,
            reserved=reserved,
            reserve_decided=reserve_decided,
            as_of=as_of,
            reasoning=reasoning,
        )

    shortfall = (buy_amount - ready_to_trade).quantize(Decimal("0.01"))

    # Reserve-aware available parked value (the reserve is NEVER liquidated).
    total_parked = parked_market_value(view.holdings, config)
    reserve_floor = reserved if reserved is not None else Decimal("0")
    available_parked = total_parked - reserve_floor
    if available_parked < 0:
        available_parked = Decimal("0")
    available_parked = available_parked.quantize(Decimal("0.01"))

    parked_set = (
        set(normalize_symbols(config.parked_symbols)) if config is not None else set()
    )
    holding = _largest_parked_holding(
        view.holdings, parked_set, exclude_symbol=normalized_buy_symbol
    )

    # v1 sells the SINGLE largest parked holding only (never chains funds). The
    # honest sellable capacity is THAT one holding capped by the reserve-aware
    # available parked value (the protected reserve is never sold). ``coverable``
    # reflects what this ONE sell can actually free up — NEVER the aggregate across
    # funds — so a "fully covered" figure can never overclaim (hard honesty
    # constraint): with several parked funds where the sum covers but no single one
    # does, this is honestly ``coverable=False`` with a "covers $Y of $X" sell.
    sellable = Decimal("0")
    if holding is not None and available_parked > 0:
        sellable = min(holding.market_value, available_parked).quantize(
            Decimal("0.01")
        )
    coverable = sellable >= shortfall

    sell_symbol: str | None = None
    sell_amount: Decimal | None = None
    est_shares: int | None = None
    sell_order_intent: OrderIntent | None = None

    if sellable > 0:
        sell_symbol = holding.symbol.strip().upper()
        # Sell at most the shortfall, and at most this single holding's reserve-aware
        # sellable capacity (so the protected reserve is never sold).
        raw_sell = min(shortfall, sellable)
        sell_amount = raw_sell.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        if sell_amount <= 0:
            # Degenerate (headroom rounded to nothing) — treat as nothing to sell.
            sell_symbol = None
            sell_amount = None
        else:
            # Cached unit price (money-market NAV is stable) — NO live quote.
            unit = holding.market_value / holding.quantity
            est_shares = whole_share_quantity(sell_amount, unit)
            sell_order_intent = OrderIntent(
                symbol=sell_symbol,
                side=OrderSide.SELL,
                amount=sell_amount,
                order_type=OrderType.MARKET,
            )

    reasoning = narrate_liquidation(
        gateway,
        {
            "sell_symbol": sell_symbol,
            "sell_amount": sell_amount,
            "coverable": coverable,
            "shortfall": shortfall,
            "reserved": reserved,
        },
    )

    return LiquidationPlan(
        needs_liquidation=True,
        coverable=coverable,
        ready_to_trade=ready_to_trade,
        shortfall=shortfall,
        sell_symbol=sell_symbol,
        sell_amount=sell_amount,
        est_shares=est_shares,
        sell_order_intent=sell_order_intent,
        reserved=reserved,
        reserve_decided=reserve_decided,
        as_of=as_of,
        reasoning=reasoning,
    )
