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
from datetime import date
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

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

# --- Story 8.6 honesty/robustness constants (all named for one-line tuning) --
#
# ``SUGGEST_MIN_DISCOUNT_FROM_ASK`` — the falling-market FLOOR. In a fresh
# sell-off the recent-20-day low can be barely below today's ask, so the base
# ``* (1 - SUGGEST_DISCOUNT)`` formula collapses to only ~1% below the market and
# the resting buy sits almost marketable. This guarantees the suggestion is NEVER
# closer than 2% below the live ask. Applied via ``min(base, floor)`` so it ONLY
# tightens the new-lows case and never weakens the deeper discount in rising/flat
# markets.
SUGGEST_MIN_DISCOUNT_FROM_ASK: Decimal = Decimal("0.02")

# --- Data-freshness thresholds (Story 8.6, calendar-day gap, injected as_of) --
#
# ``SUGGEST_STALE_AFTER_DAYS`` — beyond this many calendar days between the
# injected reference date (``as_of``) and the NEWEST stored bar, the suggestion is
# still returned but carries a calm ``stale_data`` note so the user knows the
# price is anchored on delayed data. Chosen at 5 days: a normal weekend + a market
# holiday can legitimately leave the newest daily bar ~4 days old (e.g. asking on
# a Tuesday after a long weekend), so 5 is the first gap that reliably means "the
# feed is actually behind", not just a closed market.
#
# ``SUGGEST_STALE_REFUSE_AFTER_DAYS`` — beyond this HARD cutoff the data is too
# old to price against at all, so we refuse calmly (``OrderScopeError`` → 422)
# rather than anchor a real-money resting order on a stale low. Chosen at 30 days:
# roughly a full trading month of missing bars is unambiguously a broken/paused
# feed, well past any holiday gap, and pricing a live order off a month-old low
# would be dishonest. Between STALE_AFTER (note) and REFUSE_AFTER (refuse) we
# still help the user but tell the truth about the delay.
SUGGEST_STALE_AFTER_DAYS: int = 5
SUGGEST_STALE_REFUSE_AFTER_DAYS: int = 30

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
    # Story 8.6 — backend-computed honesty facts (NEVER computed by the LLM).
    # ``pct_below_ask`` = (ask - limit_price) / ask, quantized to 4 dp (a fraction,
    # e.g. Decimal("0.0200") for 2% below); ``fill_note`` is the calm banded
    # plain-English fill-likelihood copy. ``stale_note`` is a calm delayed-data
    # signal (``None`` when the newest bar is fresh).
    pct_below_ask: Decimal
    fill_note: str
    stale_note: str | None = None


# --- Story 8.6 fill-likelihood bands (deterministic, pure) -------------------
#
# The distance below the live ask that separates a resting buy that may fill soon
# from one that is well below the market. Bands are inclusive on the LOW edge:
# ``pct_below_ask < NEAR`` = near-market; ``< FAR`` = meaningfully-below; else
# far-below. Named so a real-data tuning pass is a one-line change.
FILL_NEAR_MARKET_MAX: Decimal = Decimal("0.02")
FILL_MEANINGFUL_MAX: Decimal = Decimal("0.05")


def fill_likelihood(pct_below_ask: Decimal) -> tuple[str, str]:
    """Return a deterministic ``(band, fill_note)`` for a distance below the ask.

    PURE: a function of ``pct_below_ask`` alone (no network, no LLM, no wall-clock)
    so the same distance always yields the same band + calm copy. ``pct_below_ask``
    is the fraction ``(ask - limit_price) / ask`` (e.g. ``Decimal("0.02")`` for 2%
    below). The copy is in the calm, honest coach voice — it NEVER promises a fill
    and always reminds the user they can cancel.

    - ``< 2%`` below → ``"near-market"``: this rests just below today's price, so it
      may fill soon — but it isn't guaranteed, and you can cancel anytime.
    - ``< 5%`` below → ``"meaningfully-below"``: this rests meaningfully below
      today's price; it may take a while to fill, or may not — you can cancel
      anytime.
    - ``>= 5%`` below → ``"far-below"``: this is well below today's price, so it may
      take a long while to fill, or may never fill — you can cancel anytime.
    """
    if pct_below_ask < FILL_NEAR_MARKET_MAX:
        return (
            "near-market",
            "This rests just below today's price, so it may fill soon — though a "
            "fill is never guaranteed. You can cancel anytime.",
        )
    if pct_below_ask < FILL_MEANINGFUL_MAX:
        return (
            "meaningfully-below",
            "This rests meaningfully below today's price, so it may take a while to "
            "fill — or may not fill at all. You can cancel anytime.",
        )
    return (
        "far-below",
        "This is well below today's price, so it may take a long while to fill, or "
        "may never fill. That's fine — you can cancel anytime.",
    )


def compute_suggested_price(recent_low: Decimal, ask: Decimal) -> Decimal:
    """Return the deterministic resting-limit price (LOCKED formula + 8.6 floor).

    Base (Story 8.4): ``quantize_2dp_down( min(recent_low, ask) * (1 - SUGGEST_DISCOUNT) )``.
    Because the discount applies to ``min(recent_low, ask)``, the base is ALWAYS
    strictly below the live ask.

    Story 8.6 FLOOR: in a fresh sell-off the recent low can be barely below the ask
    and the base collapses to only ~1% below the market. So the final price is
    ``min(base, quantize_2dp_down(ask * (1 - SUGGEST_MIN_DISCOUNT_FROM_ASK)))`` —
    the ``min`` means the floor ONLY tightens the new-lows case (it can only lower
    the price) and never weakens the deeper discount in rising/flat markets.
    Quantized with ``ROUND_DOWN`` (a lower buy price only ever favors the user).
    Pure and unit-pinnable: no network, no LLM, no wall-clock.
    """
    anchor = min(recent_low, ask)
    raw = anchor * (Decimal("1") - SUGGEST_DISCOUNT)
    base = raw.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    floor = (ask * (Decimal("1") - SUGGEST_MIN_DISCOUNT_FROM_ASK)).quantize(
        Decimal("0.01"), rounding=ROUND_DOWN
    )
    return min(base, floor)


def _pct_below_ask(limit_price: Decimal, ask: Decimal) -> Decimal:
    """Return the deterministic fraction ``(ask - limit_price) / ask`` (4 dp).

    Pure. Quantized to 4 dp (``ROUND_HALF_UP``) so it is a stable, reproducible
    fraction (e.g. ``Decimal("0.0200")`` for 2% below). Guards a non-positive ask
    (never surfaced by the LOCKED price path, but defense-in-depth) by returning 0.
    """
    if not ask.is_finite() or ask <= 0:
        return Decimal("0.0000")
    return ((ask - limit_price) / ask).quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_UP
    )


async def _recent_low_and_asof(
    session: AsyncSession, symbol: str
) -> tuple[Decimal, date] | None:
    """Return ``(min_low, newest_day)`` over the most recent lookback bars.

    Reads the GLOBAL ``market_daily`` table directly (no ``owner_id``, no
    ``ScopedRepository`` — it is global reference data, per the model's contract),
    newest-first, capped at the lookback window. Skips any non-finite / ``<= 0``
    ``low`` (a bad ingestion row must not drag the min to a degenerate price and
    nuke an otherwise-valid symbol). Returns ``None`` when the symbol has NO usable
    bars (the caller maps that to a calm "not enough recent price history" refusal).

    Story 8.6: also surfaces the newest USABLE bar's ``day`` so the caller can
    gate on data freshness against an injected ``as_of``. Freshness is derived
    from the same bars that price the order — a filtered bad-low row must not set
    the freshness clock either (it wouldn't contribute to ``min_low``, so it must
    not masquerade as the freshest bar). ``low`` is ``Decimal`` (Numeric column),
    never binary float.
    """
    result = await session.execute(
        select(MarketDaily.low, MarketDaily.day)
        .where(MarketDaily.symbol == symbol)
        .order_by(MarketDaily.day.desc())
        .limit(SUGGEST_LOOKBACK_DAYS)
    )
    rows = result.all()
    kept = [
        (row[0], row[1])
        for row in rows
        if row[0] is not None and row[0].is_finite() and row[0] > 0
    ]
    if not kept:
        return None
    # Newest USABLE bar drives freshness (max day among kept rows — independent of
    # SQL row-emission order); min usable low drives the price.
    newest_day = max(day for _low, day in kept)
    return (min(low for low, _day in kept), newest_day)


def _fallback_reasoning(
    *,
    symbol: str,
    limit_price: Decimal,
    ask: Decimal,
    fill_note: str = "",
) -> str:
    """A deterministic, calm templated reasoning string (gateway-outage fallback).

    Mirrors ``build_default_plan`` resilience: if the LLM narration fails for ANY
    reason, the suggestion is NEVER blocked — this plain sentence stands in. It
    states the already-computed facts honestly (numbers unchanged). Story 8.6:
    appends the deterministic fill-likelihood sentence so the templated fallback
    carries the same honesty as the LLM narration.
    """
    base = (
        f"This rests a buy for {symbol} at ${limit_price:.2f} — a touch below its "
        f"recent low and below today's ask of ${ask:.2f}. It waits patiently at "
        "that price until it's reached, or until you cancel it. No rush, no chase."
    )
    if fill_note:
        return f"{base} {fill_note}"
    return base


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
    fill_note = str(facts.get("fill_note", ""))
    stale_note = facts.get("stale_note")
    # Backend-owned display math lives OUTSIDE the try: it is pure and must never be
    # mistaken for a gateway/LLM failure (an arithmetic bug should fail loudly, not
    # silently degrade to the fallback). Percent for the model to weave in as a FACT
    # (already computed by the backend — the model never derives it); ROUND_HALF_UP
    # to stay consistent with the serialized ``pct_below_ask``.
    pct = facts.get("pct_below_ask", Decimal("0"))
    pct_display = (pct * Decimal("100")).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP
    )
    stale_line = f"\n- data freshness: {stale_note}" if stale_note else ""
    try:
        user_content = (
            "The order below is ALREADY computed and final — narrate it, do not "
            "change any number.\n"
            f"- symbol: {symbol}\n"
            f"- side: buy (resting limit, good-till-canceled)\n"
            f"- limit price: ${facts['limit_price']:.2f}\n"
            f"- recent 20-day low: ${facts['recent_low']:.2f}\n"
            f"- today's ask: ${facts['ask']:.2f}\n"
            f"- distance below ask: {pct_display}%\n"
            f"- fill likelihood (say this honestly): {fill_note}\n"
            f"- shares: {facts['shares']}\n"
            f"- total cost: ${facts['amount']:.2f}"
            f"{stale_line}"
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
        # unchanged; only the prose degrades to a deterministic template (which
        # still carries the deterministic fill-likelihood sentence, Story 8.6).
        return _fallback_reasoning(
            symbol=symbol, limit_price=limit_price, ask=ask, fill_note=fill_note
        )


async def suggest_resting_order(
    scope: Scope,
    session: AsyncSession,
    *,
    broker: BrokerPort,
    broker_session: BrokerageSession,
    gateway: LLMGateway,
    symbol: str,
    target_amount: Decimal | None,
    as_of: date,
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

    low_and_asof = await _recent_low_and_asof(session, normalized_symbol)
    if low_and_asof is None:
        raise OrderScopeError(
            "There isn't enough recent price history to suggest a resting order "
            f"for {normalized_symbol} yet."
        )
    recent_low, newest_day = low_and_asof

    # Data-freshness gate (Story 8.6): compare the injected reference date against
    # the newest stored bar. Beyond the HARD cutoff we refuse calmly rather than
    # anchor a live order on stale data; between the note and refuse thresholds we
    # still suggest but attach a calm delayed-data note. ``as_of`` is injected (the
    # endpoint passes ``date.today()``, tests a fixed date) so the pricing path
    # never reads the wall clock and stays deterministic.
    # Never let the gap go negative: a bar dated after ``as_of`` (a benign
    # timezone/clock skew, or a corrupt future-dated row) is not "stale" and must
    # not read as negative — clamp to 0 so the thresholds below behave sanely.
    staleness_days = max((as_of - newest_day).days, 0)
    stale_note: str | None = None
    if staleness_days > SUGGEST_STALE_REFUSE_AFTER_DAYS:
        raise OrderScopeError(
            f"The most recent price data for {normalized_symbol} is "
            f"{staleness_days} days old — too stale to suggest a resting order "
            "against right now. Nothing was suggested."
        )
    if staleness_days > SUGGEST_STALE_AFTER_DAYS:
        stale_note = (
            f"Heads up: the newest price data for {normalized_symbol} is "
            f"{staleness_days} days old, so this suggestion is based on delayed "
            "data. Double-check today's price before you approve."
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

    # Story 8.6 — deterministic fill-likelihood, computed by the backend and fed to
    # the LLM as a FACT (the model never derives it). Same bars+ask ⇒ same numbers.
    # ``pct_below_ask`` is the 4dp-quantized value for the wire; the BAND is chosen
    # from the exact unrounded fraction so display rounding can never nudge a value
    # across a band edge (ask is guaranteed positive here — checked above).
    pct_below_ask = _pct_below_ask(limit_price, ask)
    exact_pct_below_ask = (ask - limit_price) / ask
    _band, fill_note = fill_likelihood(exact_pct_below_ask)

    reasoning = narrate_suggestion(
        gateway,
        {
            "symbol": normalized_symbol,
            "limit_price": limit_price,
            "recent_low": recent_low,
            "ask": ask,
            "shares": shares,
            "amount": amount,
            "pct_below_ask": pct_below_ask,
            "fill_note": fill_note,
            "stale_note": stale_note,
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
        pct_below_ask=pct_below_ask,
        fill_note=fill_note,
        stale_note=stale_note,
    )
