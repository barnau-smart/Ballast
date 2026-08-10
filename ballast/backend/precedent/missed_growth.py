"""Missed-growth estimate (Story 3.4, FR19) — a deterministic engine function.

Estimates the growth a user's IDLE CASH has forgone (or the loss it has avoided)
over a fixed trailing window, as ``idle_cash × benchmark total return``. Like the
rest of ``precedent/`` this is AD-1/AD-3's sole source of the market number: it is
computed deterministically from the GLOBAL ``market_daily`` store — NO LLM, NO
network, NO randomness, NO wall-clock read (``as_of`` is passed in, or defaults to
the LATEST bar in the loaded series). The same market data + same idle cash always
yield the same estimate; the frontend only formats/renders what this returns.

HONEST IN BOTH DIRECTIONS: when the benchmark rose over the window, the estimate
is positive (growth idle cash missed); when it fell, the estimate is negative and
is stated calmly as a loss idle cash AVOIDED — never framed as a "cost" of holding
cash. ``idle_cash <= 0`` and insufficient history each degrade to a calm
informational state, never a crash or dead end.

This module reuses the engine's ``_load_series`` data-access convention, its
252-row (``FORWARD_RETURN_DAYS``) trailing-window index convention, and its Decimal
quantization helpers — it does NOT touch ``engine.py``'s matching logic, and it
uses its OWN standalone DTO (never the AD-12 ``EvidenceRecord`` contract).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from money import format_money
from precedent.engine import DEFAULT_BENCHMARK, _load_series, _q, _source_str

logger = logging.getLogger("ballast.precedent.missed_growth")

#: Fixed trailing window in market rows (≈ one year), matching the engine's
#: ``FORWARD_RETURN_DAYS`` index convention. Deterministic and legible
#: ("over the past year"). Inherits the same gapless-row assumption already
#: tracked in ``deferred-work.md`` for the engine's index-based horizons — this
#: story does NOT re-solve or duplicate that entry.
LOOKBACK_TRADING_DAYS = 252

#: Named, DISCLOSED placeholder for the money-market yield used to offset parked
#: cash in the yield-aware missed-growth math (Story 9.2). Money-market yields are
#: NOT in the market-data set (Tiingo carries only the 14 index ETFs) and 9-1
#: stores no per-fund yield, so a single disclosed default is the honest v1 source:
#: every statement that uses it states the assumption out loud ("counting your
#: parked money-market cash as already earning about 4% a year"). It is a TUNABLE
#: placeholder — tune it against real money-market data later, and consider a
#: user-editable yield as a later refinement; do NOT hardcode this rate anywhere
#: else (this constant is the single source).
DEFAULT_MONEY_MARKET_APY = Decimal("0.04")

#: Trading days per year — the denominator that prorates an annual APY down to the
#: lookback window (``yield_over_window = apy × lookback_days / 252``).
_TRADING_DAYS_PER_YEAR = Decimal("252")

#: Cent quantization for dollar figures (idle cash, forgone growth).
_CENTS_Q = Decimal("0.01")

#: Shared calm rationale for both insufficient-history exits (kept as one string
#: so the two branches can never drift apart).
_INSUFFICIENT_STATEMENT = (
    "There is not enough market history yet to estimate what idle cash may have "
    "missed over the past year."
)


def _cents(value: Decimal) -> Decimal:
    """Quantize a dollar Decimal to cents (half-up), deterministically."""
    return value.quantize(_CENTS_Q, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class MissedGrowthEstimate:
    """A standalone, JSON-safe estimate of the growth idle cash has forgone.

    NOT an AD-12 ``EvidenceRecord`` (that contract is pinned to two kinds Epic 4
    depends on — see the story's Design Notes). This is a calming, read-only
    view DTO with its own shape.

    Money and returns are ``Decimal`` (NEVER binary float); dates are calendar
    :class:`datetime.date` (ISO-8601 on the wire). ``window_return`` /
    ``window_start`` / ``window_end`` are ``None`` when there is insufficient
    history. ``reason`` is ``None`` in the normal (figure-present) case, else one
    of ``"no_idle_cash"`` | ``"insufficient_history"``.

    Frozen so a computed estimate cannot mutate after the fact.

    Story 9.2 adds cash-state/yield-aware fields (all ADDITIVE; the pre-9.2
    fields keep their meaning): ``settlement_cash`` (ready-to-trade cash),
    ``parked`` (money-market cash), ``reserved`` (the RESOLVED reserve used in
    the calc — ``None`` only when the caller passed it as absent),
    ``money_market_apy`` (the disclosed yield assumption), and
    ``investable_base`` (``cash + parked − reserve``, clamped ≥ 0). ``idle_cash``
    is set to ``investable_base`` so the figure is always computed on genuinely
    investable money. ``reserve_decided`` is NOT on the engine DTO — it is a
    config-layer fact the endpoint adds. ``reason`` gains ``"fully_reserved"``
    (reserve covers all cash → base clamps to 0).
    """

    idle_cash: Decimal
    benchmark: str
    window_return: Decimal | None
    window_start: date | None
    window_end: date | None
    forgone_growth: Decimal
    trading_days: int
    statement: str
    source: str
    as_of: date | None
    sufficient: bool
    reason: str | None
    settlement_cash: Decimal
    parked: Decimal
    reserved: Decimal | None
    money_market_apy: Decimal
    investable_base: Decimal

    def to_dict(self) -> dict:
        """Return a JSON-safe dict (Decimal→fixed-point str, date→ISO, None preserved).

        Money/Decimal fields render via :func:`money.format_money` so extreme or
        tiny values never surface as ``E+``/``E-`` exponent on the wire.
        """
        return {
            "idle_cash": format_money(self.idle_cash),
            "benchmark": self.benchmark,
            "window_return": (
                format_money(self.window_return)
                if self.window_return is not None
                else None
            ),
            "window_start": (
                self.window_start.isoformat() if self.window_start is not None else None
            ),
            "window_end": (
                self.window_end.isoformat() if self.window_end is not None else None
            ),
            "forgone_growth": format_money(self.forgone_growth),
            "trading_days": self.trading_days,
            "statement": self.statement,
            "source": self.source,
            "as_of": self.as_of.isoformat() if self.as_of is not None else None,
            "sufficient": self.sufficient,
            "reason": self.reason,
            "settlement_cash": format_money(self.settlement_cash),
            "parked": format_money(self.parked),
            "reserved": (
                format_money(self.reserved) if self.reserved is not None else None
            ),
            "money_market_apy": format_money(self.money_market_apy),
            "investable_base": format_money(self.investable_base),
        }


def _format_usd(value: Decimal) -> str:
    """Format a (cent-quantized) dollar Decimal as ``$1,234.50`` for the statement."""
    return f"${abs(value):,.2f}"


async def estimate_missed_growth(
    session: AsyncSession,
    idle_cash: Decimal,
    symbol: str = DEFAULT_BENCHMARK,
    lookback_days: int = LOOKBACK_TRADING_DAYS,
    as_of: date | None = None,
    parked: Decimal = Decimal("0"),
    reserved: Decimal = Decimal("0"),
    money_market_apy: Decimal = DEFAULT_MONEY_MARKET_APY,
) -> MissedGrowthEstimate:
    """Estimate the growth investable cash forgone over a trailing window (AD-1/AD-3).

    Cash-state-aware and yield-aware (Story 9.2), deterministic and honest in both
    directions. ``idle_cash`` is the ready-to-trade **settlement cash**; ``parked``
    is money-market cash (earning yield); ``reserved`` is the user's declared
    reserve. The figure is computed on the **investable base** only:

    - Reserve is drawn PARKED-FIRST: ``parked_investable = max(parked − reserved, 0)``,
      then any leftover reserve reduces settlement cash
      (``cash_investable = max(settlement_cash − max(reserved − parked, 0), 0)``).
    - Settlement cash misses the **full** window return; parked money misses only
      ``window_return − yield_over_window`` (it already earns money-market yield),
      where ``yield_over_window = money_market_apy × lookback_days / 252``. The
      parked term stays SIGNED — if the market underperformed the money-market
      yield over the window, parked reduces the figure (that is honest).
    - ``forgone_growth = cash_investable × window_return + parked_investable ×
      (window_return − yield_over_window)`` (quantized to cents).

    Backward-compatible: when ``parked == 0`` and ``reserved == 0`` the investable
    base is just the settlement cash and the yield term vanishes, so a caller
    passing only ``idle_cash`` behaves EXACTLY as pre-9.2.

    Degraded states (never a dead end): investable base ≤ 0 → ``no_idle_cash`` (or
    ``fully_reserved`` when a reserve is what zeroed it); fewer than
    ``lookback_days + 1`` bars → ``insufficient_history``. A rising window yields a
    positive figure (growth missed, green ▲); a falling window a negative figure
    (loss AVOIDED, sky-blue ▼) — never framed as a cost.
    """
    source = _source_str(symbol)
    settlement_cash_q = _cents(idle_cash)
    parked_q = _cents(parked)
    reserved_q = _cents(reserved)

    # Reserve drawn PARKED-FIRST, keeping ~0%-yield settlement cash liquid for
    # trading (money-market still earns yield + is liquid enough for emergencies).
    parked_investable = max(parked_q - reserved_q, Decimal("0"))
    reserve_left = max(reserved_q - parked_q, Decimal("0"))
    cash_investable = max(settlement_cash_q - reserve_left, Decimal("0"))
    investable_base = cash_investable + parked_investable  # == max(cash+parked-reserve, 0)

    # Common tail of the additive cash-state fields for every return below. The
    # engine surfaces the RESOLVED reserve it was handed (the endpoint maps a
    # never-decided config to None → 0 for the calc but reports reserved=null).
    _states = dict(
        settlement_cash=settlement_cash_q,
        parked=parked_q,
        reserved=reserved_q,
        money_market_apy=money_market_apy,
        investable_base=_cents(investable_base),
    )

    # --- No investable cash → calm informational state (never a dead end) ----
    if investable_base <= 0:
        # Distinguish "there was cash but the reserve protects all of it" from
        # "there is simply nothing idle" — the former is a reassurance, not a
        # non-event. A reserve is decisive only when there IS cash to protect.
        fully_reserved = (settlement_cash_q + parked_q) > 0 and reserved_q > 0
        if fully_reserved:
            statement = (
                "Your reserve covers all of your cash right now — nothing is "
                "sitting idle to invest."
            )
            reason = "fully_reserved"
        else:
            statement = (
                "You have no idle cash sitting out of the market right now, so "
                "there is no forgone growth to show."
            )
            reason = "no_idle_cash"
        return MissedGrowthEstimate(
            idle_cash=_cents(Decimal("0")),
            benchmark=symbol,
            window_return=None,
            window_start=None,
            window_end=None,
            forgone_growth=_cents(Decimal("0")),
            trading_days=lookback_days,
            statement=statement,
            source=source,
            as_of=None,
            sufficient=True,
            reason=reason,
            **_states,
        )

    series = await _load_series(session, symbol)

    # Anchor the window end at the last bar at/before as_of (default: latest).
    if as_of is not None:
        window = [(d, c) for d, c in series if d <= as_of]
    else:
        window = list(series)

    resolved_as_of = window[-1][0] if window else as_of

    # --- Insufficient history → calm rationale (never an empty state) --------
    if len(window) < lookback_days + 1:
        logger.info(
            "missed_growth_insufficient_history symbol=%s bars=%d need=%d",
            symbol,
            len(window),
            lookback_days + 1,
        )
        return MissedGrowthEstimate(
            idle_cash=_cents(investable_base),
            benchmark=symbol,
            window_return=None,
            window_start=None,
            window_end=resolved_as_of,
            forgone_growth=_cents(Decimal("0")),
            trading_days=lookback_days,
            statement=_INSUFFICIENT_STATEMENT,
            source=source,
            as_of=resolved_as_of,
            sufficient=False,
            reason="insufficient_history",
            **_states,
        )

    start_day, start_close = window[-(lookback_days + 1)]
    end_day, end_close = window[-1]

    # Guard a non-positive base defensively (real adj_close is > 0); treat as
    # insufficient rather than dividing by zero.
    if start_close <= 0:
        logger.warning(
            "missed_growth_nonpositive_base symbol=%s start_close=%s",
            symbol,
            start_close,
        )
        return MissedGrowthEstimate(
            idle_cash=_cents(investable_base),
            benchmark=symbol,
            window_return=None,
            window_start=None,
            window_end=end_day,
            forgone_growth=_cents(Decimal("0")),
            trading_days=lookback_days,
            statement=_INSUFFICIENT_STATEMENT,
            source=source,
            as_of=end_day,
            sufficient=False,
            reason="insufficient_history",
            **_states,
        )

    window_return = _q((end_close - start_close) / start_close)

    # Prorate the annual money-market APY down to the lookback window (4-dp, the
    # same quantum as window_return). Parked money misses only the SIGNED
    # difference window_return − yield_over_window.
    yield_over_window = _q(
        money_market_apy * Decimal(lookback_days) / _TRADING_DAYS_PER_YEAR
    )
    forgone_growth = _cents(
        cash_investable * window_return
        + parked_investable * (window_return - yield_over_window)
    )

    base_phrase = _format_usd(_cents(investable_base))
    amount_phrase = _format_usd(forgone_growth)

    # A calm reserve clause, appended only when a positive reserve actually
    # protected money (drawn parked-first). Never fabricated.
    reserve_clause = ""
    if reserved_q > 0:
        reserve_clause = (
            f" — and your {_format_usd(reserved_q)} reserve stayed protected, "
            f"just as you set it"
        )

    # DISCLOSE the yield assumption whenever parked money was in the calc, so the
    # number is never a lie by omission.
    yield_clause = ""
    if parked_investable > 0:
        apy_pct = (money_market_apy * Decimal("100")).quantize(Decimal("0.1"))
        yield_clause = (
            f" (counting your parked money-market cash as already earning about "
            f"{apy_pct.normalize():f}% a year)"
        )

    # Any market-DIRECTION wording keys on ``window_return`` (what the market
    # actually did), while the DOLLAR outcome keys on ``forgone_growth``. These
    # can disagree once parked money earns yield: in a modestly rising market
    # (0 < window_return < yield_over_window) parked cash can OUTPACE the market,
    # making forgone_growth negative even though the market rose — so we must
    # never say "the market fell" off the dollar sign alone (that would lie).
    market_fell = window_return < 0
    if forgone_growth > 0:
        # Idle cash missed growth (the market outran any parked yield).
        statement = (
            f"Over the past year, about {base_phrase} of investable cash sat out "
            f"roughly {amount_phrase} of growth{reserve_clause}{yield_clause}."
        )
    elif forgone_growth < 0:
        if market_fell:
            statement = (
                f"Over the past year the market fell, so your {base_phrase} of "
                f"investable cash avoided roughly {amount_phrase} of loss"
                f"{reserve_clause}{yield_clause}."
            )
        else:
            # Market rose (or was flat) but parked money-market yield outpaced it,
            # so investable cash came out ahead — NEVER claim the market fell.
            statement = (
                f"Over the past year your parked cash kept pace with a modestly "
                f"rising market, so your {base_phrase} of investable cash came out "
                f"about {amount_phrase} ahead{reserve_clause}{yield_clause}."
            )
    elif market_fell or window_return > 0:
        # A real market move that netted to $0.00 for investable cash (e.g. parked
        # yield exactly offset a small move) — honestly "kept pace", not "flat".
        statement = (
            f"Over the past year your {base_phrase} of investable cash roughly "
            f"kept pace with the market — no measurable growth missed"
            f"{reserve_clause}{yield_clause}."
        )
    else:
        statement = (
            f"Over the past year the market was roughly flat, so your "
            f"{base_phrase} of investable cash has not missed measurable growth"
            f"{reserve_clause}{yield_clause}."
        )

    return MissedGrowthEstimate(
        idle_cash=_cents(investable_base),
        benchmark=symbol,
        window_return=window_return,
        window_start=start_day,
        window_end=end_day,
        forgone_growth=forgone_growth,
        trading_days=lookback_days,
        statement=statement,
        source=source,
        as_of=end_day,
        sufficient=True,
        reason=None,
        **_states,
    )
