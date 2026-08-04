"""The Coach Engine's "suggest & populate the order" engine (Story 8.4, MasterB core).

The deterministic money-math + LLM-narration owner for the optional
``POST /api/coach/suggest-order`` button. A beginner should not have to invent a
"buy near the low and wait" resting-limit price by hand — this computes a sensible
resting BUY LIMIT (GTC) order and hands the FINISHED numbers to the LLM only to
narrate in plain English. The model NEVER does money-math and NEVER sets the
price; the backend owns ``limit_price``/``amount``/``shares`` entirely.

Two invariants make the suggestion safe and testable:

- **The backend owns the number.** :func:`compute_suggested_price` is a pure
  function of the stored :class:`~db.models.MarketDaily` bars + the live ask —
  fully reproducible with NO network and NO LLM. Same symbol + same bars + same
  ask ⇒ byte-identical price regardless of which LLM adapter runs.
- **Narration is downstream of, and blind to, the math.** The price is computed
  BEFORE the gateway is touched; :func:`narrate_suggestion` receives the finished
  numbers as FACTS to explain and returns only prose. A gateway outage degrades to
  a deterministic templated sentence (mirroring ``build_default_plan`` resilience)
  — it never crashes and never blocks the suggestion.

Every calm decline (non-core symbol, no price history, insufficient idle cash,
unreadable quote) raises :class:`~coach.execution.OrderScopeError` (or lets the
adapter's :class:`~brokers.port.OrderNotPlaceableError` surface), which the API
layer maps to a calm HTTP 422 ``{error:{type,message}}`` — never a 500, never a
phantom order (this engine touches no ``place_order``/``decision_record`` path).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brokers.port import BrokerPort, OrderNotPlaceableError
from brokers.portfolio import get_portfolio
from brokers.session import BrokerageSession
from coach.execution import OrderScopeError, whole_share_quantity
from coach.recommendation import Duration, OrderSide, OrderType
from db.models import MarketDaily
from db.scope import Scope
from llm.port import LLMGateway, LLMMessage, LLMRequest
from strategy.index_core import is_index_core

# --- Locked deterministic pricing formula (this pass locks the params) -------
#
# ``recent_low`` = min ``low`` over the most recent ``SUGGEST_LOOKBACK_DAYS``
# MarketDaily bars for the symbol; ``limit_price`` = quantize_2dp_down(
# min(recent_low, ask) * (1 - SUGGEST_DISCOUNT) ). Discounting ``min(recent_low,
# ask)`` guarantees ``limit_price < ask`` in EVERY branch, so the suggestion is
# always a genuine resting buy (never accidentally marketable). Both constants
# are named so a later real-data tuning pass is a one-line change.
SUGGEST_LOOKBACK_DAYS: int = 20
SUGGEST_DISCOUNT: Decimal = Decimal("0.01")

#: The tiny narration-only output schema for the LLM (Story 8.4). Deliberately
#: SEPARATE from ``RECOMMENDATION_OUTPUT_SCHEMA`` — the ``/recommend`` path stays
#: MARKET-only; suggest-order is its own surface and the model returns ONLY prose.
SUGGEST_NARRATION_SCHEMA: dict = {
    "type": "object",
    "properties": {"reasoning": {"type": "string"}},
    "required": ["reasoning"],
    "additionalProperties": False,
}

_SUGGEST_SYSTEM = (
    "You are Ballast's calm, honest investing coach. You are handed an order that "
    "has ALREADY been computed for the user — the numbers are final and you must "
    "NOT change, recompute, or second-guess them. Explain in one short, warm, "
    "plain-English paragraph WHY this resting buy-limit is a sensible, unhurried "
    "way to add to a broad index core. Never hype, never rush the user, never "
    "promise a fill. Return only the reasoning."
)


@dataclass(frozen=True)
class SuggestedOrder:
    """A computed resting BUY LIMIT (GTC) suggestion + its narration (Story 8.4).

    ``limit_price``/``amount`` are ``Decimal`` (the API serializes them to
    fixed-point strings on the wire); ``shares`` is the whole-share count. ``side``
    is always :attr:`~coach.recommendation.OrderSide.BUY`, ``order_type`` always
    :attr:`~coach.recommendation.OrderType.LIMIT`, ``duration`` always
    :attr:`~coach.recommendation.Duration.GTC`. ``reasoning`` is the LLM's prose
    (or a deterministic templated fallback). This engine PLACES NOTHING — the
    human runs the unchanged ``/approve`` co-sign path to actually execute.
    """

    symbol: str
    side: OrderSide
    order_type: OrderType
    limit_price: Decimal
    duration: Duration
    amount: Decimal
    shares: int
    reasoning: str


def compute_suggested_price(recent_low: Decimal, ask: Decimal) -> Decimal:
    """Return the deterministic resting-limit price (LOCKED formula, Story 8.4).

    ``limit_price = quantize_2dp_down( min(recent_low, ask) * (1 - SUGGEST_DISCOUNT) )``.
    Because the discount applies to ``min(recent_low, ask)``, the result is ALWAYS
    strictly below the live ask — a genuine resting buy that never fills
    immediately. Quantized with ``ROUND_DOWN`` to 2 dp (a lower buy price only ever
    favors the user). Pure and unit-pinnable: no network, no LLM, no wall-clock.
    """
    anchor = min(recent_low, ask)
    raw = anchor * (Decimal("1") - SUGGEST_DISCOUNT)
    return raw.quantize(Decimal("0.01"), rounding=ROUND_DOWN)


async def _recent_low(session: AsyncSession, symbol: str) -> Decimal | None:
    """Return the min ``low`` over the most recent ``SUGGEST_LOOKBACK_DAYS`` bars.

    Reads the GLOBAL ``market_daily`` table directly (no ``owner_id``, no
    ``ScopedRepository`` — it is global reference data, per the model's contract),
    newest-first, capped at the lookback window. Skips any non-finite / ``<= 0``
    ``low`` (a bad ingestion row must not drag the min to a degenerate price and
    nuke an otherwise-valid symbol). Returns ``None`` when the symbol has NO usable
    bars (the caller maps that to a calm "not enough recent price history" refusal).
    ``low`` is ``Decimal`` (Numeric column), never binary float.
    """
    result = await session.execute(
        select(MarketDaily.low)
        .where(MarketDaily.symbol == symbol)
        .order_by(MarketDaily.day.desc())
        .limit(SUGGEST_LOOKBACK_DAYS)
    )
    lows = [
        row[0]
        for row in result.all()
        if row[0] is not None and row[0].is_finite() and row[0] > 0
    ]
    if not lows:
        return None
    return min(lows)


def _fallback_reasoning(*, symbol: str, limit_price: Decimal, ask: Decimal) -> str:
    """A deterministic, calm templated reasoning string (gateway-outage fallback).

    Mirrors ``build_default_plan`` resilience: if the LLM narration fails for ANY
    reason, the suggestion is NEVER blocked — this plain sentence stands in. It
    states the already-computed facts honestly (numbers unchanged).
    """
    return (
        f"This rests a buy for {symbol} at ${limit_price:.2f} — a touch below its "
        f"recent low and below today's ask of ${ask:.2f}. It waits patiently at "
        "that price until it's reached, or until you cancel it. No rush, no chase."
    )


def narrate_suggestion(
    gateway: LLMGateway,
    facts: dict,
) -> str:
    """Ask the LLM to NARRATE the already-computed numbers as prose (Story 8.4).

    Composes an :class:`~llm.port.LLMRequest` feeding the FINISHED numbers as facts
    (never asking the model to compute anything) against the tiny
    :data:`SUGGEST_NARRATION_SCHEMA`, and returns ``output["reasoning"]``. This is
    downstream of and blind to the math — the price was computed BEFORE this call,
    which is why real-vs-fake LLM price identity is trivially true.

    RESILIENT: on ANY exception (a gateway transport/parse failure, a missing
    ``reasoning`` key, a non-string value) it returns a deterministic templated
    fallback — a narration outage never crashes and never blocks the suggestion.
    """
    symbol = str(facts.get("symbol", ""))
    limit_price = facts.get("limit_price", Decimal("0"))
    ask = facts.get("ask", Decimal("0"))
    try:
        user_content = (
            "The order below is ALREADY computed and final — narrate it, do not "
            "change any number.\n"
            f"- symbol: {symbol}\n"
            f"- side: buy (resting limit, good-till-canceled)\n"
            f"- limit price: ${facts['limit_price']:.2f}\n"
            f"- recent 20-day low: ${facts['recent_low']:.2f}\n"
            f"- today's ask: ${facts['ask']:.2f}\n"
            f"- shares: {facts['shares']}\n"
            f"- total cost: ${facts['amount']:.2f}"
        )
        request = LLMRequest(
            messages=(LLMMessage(role="user", content=user_content),),
            output_schema=SUGGEST_NARRATION_SCHEMA,
            system=_SUGGEST_SYSTEM,
        )
        response = gateway.complete(request)
        reasoning = response.output.get("reasoning")
        if not isinstance(reasoning, str) or not reasoning.strip():
            raise ValueError("empty or non-string reasoning")
        return reasoning
    except Exception:
        # Silent, resilient fallback (mirrors build_default_plan) — the number is
        # unchanged; only the prose degrades to a deterministic template.
        return _fallback_reasoning(symbol=symbol, limit_price=limit_price, ask=ask)


async def suggest_resting_order(
    scope: Scope,
    session: AsyncSession,
    *,
    broker: BrokerPort,
    broker_session: BrokerageSession,
    gateway: LLMGateway,
    symbol: str,
    target_amount: Decimal | None,
) -> SuggestedOrder:
    """Compute a deterministic resting BUY LIMIT (GTC) suggestion + narrate it.

    The orchestration owner (money-math lives here, out of the API layer):

    1. Gate on :func:`~strategy.index_core.is_index_core` — a non-core symbol is a
       calm :class:`~coach.execution.OrderScopeError` (verbatim engine copy).
    2. Load the recent 20-day lows from ``market_daily``; ``< 1`` bar refuses
       calmly ("not enough recent price history …").
    3. Read the live ask via ``broker.get_quote`` (an unusable quote surfaces the
       adapter's :class:`~brokers.port.OrderNotPlaceableError`, mapped to a calm
       422 by the endpoint) — computed BEFORE the gateway is touched.
    4. Compute ``limit_price`` via the LOCKED :func:`compute_suggested_price`
       (always strictly ``< ask``).
    5. Size whole shares off the budget (the request's ``target_amount`` if
       ``> 0``, else available cash, capped at available cash) via
       :func:`~coach.execution.whole_share_quantity`; ``< 1`` share refuses calmly.
    6. Narrate the FINISHED numbers via :func:`narrate_suggestion` (LLM prose or a
       resilient fallback).

    PLACES NOTHING — no ``place_order``, no ``decision_record``. The human executes
    via the unchanged ``/approve`` flow.
    """
    normalized_symbol = (symbol or "").strip().upper()
    if not is_index_core(normalized_symbol):
        raise OrderScopeError(
            "This order is outside the v1 scope. Ballast v1 can only suggest "
            "orders in broad index funds and ETFs."
        )

    recent_low = await _recent_low(session, normalized_symbol)
    if recent_low is None:
        raise OrderScopeError(
            "There isn't enough recent price history to suggest a resting order "
            f"for {normalized_symbol} yet."
        )

    # Live ask (a calm OrderNotPlaceableError on an unusable quote — the endpoint
    # maps it to a 422). Computed BEFORE the gateway is touched.
    quote = await broker.get_quote(normalized_symbol)
    ask = quote.ask

    limit_price = compute_suggested_price(recent_low, ask)
    # Defense-in-depth: the LOCKED formula always yields a positive price for a
    # positive ask; but never let a degenerate quote produce a non-positive limit.
    if not limit_price.is_finite() or limit_price <= 0:
        raise OrderNotPlaceableError(
            f"Couldn't read a live price for {normalized_symbol} right now — no "
            "order was suggested."
        )

    portfolio = await get_portfolio(scope, session)
    available_cash = portfolio.cash

    # Budget: the request's optional target if > 0, else available cash; ALWAYS
    # capped at available cash (never suggest spending more than the idle cash).
    if target_amount is not None and target_amount.is_finite() and target_amount > 0:
        budget = min(target_amount, available_cash)
    else:
        budget = available_cash

    shares = whole_share_quantity(budget, limit_price)
    if shares < 1:
        raise OrderScopeError(
            "There isn't enough idle cash for a whole share at that resting price "
            "right now — nothing was suggested."
        )

    # Exact whole-share cost, quantized 2dp (shares is an int, limit_price is 2dp).
    amount = (Decimal(shares) * limit_price).quantize(
        Decimal("0.01"), rounding=ROUND_DOWN
    )

    reasoning = narrate_suggestion(
        gateway,
        {
            "symbol": normalized_symbol,
            "limit_price": limit_price,
            "recent_low": recent_low,
            "ask": ask,
            "shares": shares,
            "amount": amount,
        },
    )

    return SuggestedOrder(
        symbol=normalized_symbol,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        limit_price=limit_price,
        duration=Duration.GTC,
        amount=amount,
        shares=shares,
        reasoning=reasoning,
    )
