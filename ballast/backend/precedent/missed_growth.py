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
) -> MissedGrowthEstimate:
    """Estimate the growth ``idle_cash`` forgone over a trailing window (AD-1/AD-3).

    Deterministic and honest in both directions:

    - ``idle_cash <= 0`` → a calm ``no_idle_cash`` state, ``forgone_growth`` 0.00.
    - Fewer than ``lookback_days + 1`` bars for ``symbol`` → ``sufficient=False``,
      ``insufficient_history``, ``window_return=None``, ``forgone_growth`` 0.00.
    - Otherwise: ``end`` = last bar at/before ``as_of`` (default: latest bar),
      ``start`` = the bar ``lookback_days`` rows earlier;
      ``window_return = (end_close - start_close) / start_close`` (quantized 4-dp);
      ``forgone_growth = idle_cash × window_return`` (quantized to cents). A rising
      window yields a positive figure (growth missed, green ▲); a falling window a
      negative figure (loss AVOIDED, sky-blue ▼) — never framed as a cost.
    """
    source = _source_str(symbol)
    idle_cash_q = _cents(idle_cash)

    # --- No idle cash → calm informational state (never a dead end) ----------
    if idle_cash_q <= 0:
        return MissedGrowthEstimate(
            idle_cash=_cents(Decimal("0")),
            benchmark=symbol,
            window_return=None,
            window_start=None,
            window_end=None,
            forgone_growth=_cents(Decimal("0")),
            trading_days=lookback_days,
            statement=(
                "You have no idle cash sitting out of the market right now, so "
                "there is no forgone growth to show."
            ),
            source=source,
            as_of=None,
            sufficient=True,
            reason="no_idle_cash",
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
            idle_cash=idle_cash_q,
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
            idle_cash=idle_cash_q,
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
        )

    window_return = _q((end_close - start_close) / start_close)
    forgone_growth = _cents(idle_cash_q * window_return)

    cash_phrase = _format_usd(idle_cash_q)
    amount_phrase = _format_usd(forgone_growth)
    # Frame on the DOLLAR figure's sign so a flat window (or a sub-cent move that
    # rounds to $0.00) reads honestly as "nothing missed" — never a green ▲ +$0.00
    # "growth missed" that overstates a non-event.
    if forgone_growth > 0:
        statement = (
            f"Your ~{cash_phrase} in idle cash has sat out ~{amount_phrase} of "
            f"growth over the past year."
        )
    elif forgone_growth < 0:
        statement = (
            f"Over the past year the market fell, so your ~{cash_phrase} in idle "
            f"cash avoided ~{amount_phrase} of loss."
        )
    else:
        statement = (
            f"Over the past year the market was roughly flat, so your "
            f"~{cash_phrase} in idle cash has not missed measurable growth."
        )

    return MissedGrowthEstimate(
        idle_cash=idle_cash_q,
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
    )
