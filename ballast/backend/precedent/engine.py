"""The deterministic Precedent Engine (Story 3.2) — AD-3's sole source of market stats.

Given the CURRENT drawdown of a symbol (how far it is below its running peak, and
how fast it fell), this engine finds historically similar drawdown episodes in the
GLOBAL ``market_daily`` store and returns them as ONE aggregate
``event-precedent`` :class:`~precedent.evidence.EvidenceRecord` — or, when nothing
qualifies, the always-available ``strategy`` fallback record. It NEVER returns an
empty list.

Determinism is load-bearing (AD-5): there is NO LLM, NO network, NO randomness, and
NO wall-clock read (``datetime.now`` / ``date.today``) anywhere in the matching
path. ``as_of`` is passed in, or defaults to the LATEST ``day`` present in the
loaded series — never to today. The same ``(symbol, as_of)`` over the same
``market_daily`` rows yields byte-identical records, including an identical ``id``.

Data access reads ONLY the global ``market_daily`` table directly (no ``owner_id``,
NOT via ``ScopedRepository`` — it is global reference data, AD-10) and never makes a
live vendor call. The LLM never computes any of these numbers (AD-3).
"""

from __future__ import annotations

import logging
import statistics
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import MarketDaily
from precedent.evidence import EvidenceKind, EvidenceRecord, make_id

logger = logging.getLogger("ballast.precedent.engine")

# --- Build-time parameters (spine-deferred; tunable module constants) --------

#: Broad index-core proxy used when a caller does not name a symbol.
DEFAULT_BENCHMARK = "VTI"

#: Absolute magnitude band (± 2.5 pp) — the PRIMARY hard filter for matches.
MAGNITUDE_BAND = Decimal("0.025")

#: Forward-return horizon: trading days after a trough (close-to-close).
FORWARD_RETURN_DAYS = 252

#: Exact-zero magnitude means the symbol is at (or above) its running peak — an
#: all-time high, i.e. not in a drawdown at all. (magnitude is floored at 0 in
#: current_drawdown, so <= 0 is equivalent to == 0; there is no tolerance band.)
_NO_DRAWDOWN = Decimal("0")

#: Quantization for percentage / return Decimals (deterministic 4-dp).
_PCT_Q = Decimal("0.0001")

#: Coarser 1-dp grain for the human-readable percentage in ``statement`` (e.g.
#: "~8.0%"). Display-only — it never feeds ``stats`` or the ``id`` hash, so the
#: coach-facing sentence reads cleanly without affecting determinism.
_PCT_DISPLAY_Q = Decimal("0.1")


def _q(value: Decimal) -> Decimal:
    """Quantize a percentage/return Decimal deterministically to 4 decimal places."""
    return value.quantize(_PCT_Q)


# --- Data access -------------------------------------------------------------


async def _load_series(
    session: AsyncSession, symbol: str
) -> list[tuple[date, Decimal]]:
    """Load ``(day, adj_close)`` for ``symbol`` from ``market_daily``, ordered by day.

    Reads the GLOBAL table directly — no ``owner_id``, no ``ScopedRepository`` (it is
    global reference data), and no live vendor call. ``adj_close`` (split/dividend
    adjusted) is the series all drawdown / forward-return math runs on.
    """
    result = await session.execute(
        select(MarketDaily.day, MarketDaily.adj_close)
        .where(MarketDaily.symbol == symbol)
        .order_by(MarketDaily.day)
    )
    return [(row[0], row[1]) for row in result.all()]


# --- Drawdown math -----------------------------------------------------------


def current_drawdown(
    series: list[tuple[date, Decimal]], as_of: date
) -> dict:
    """Return the current drawdown at ``as_of`` relative to the running peak.

    Walks the series up to and including ``as_of``, tracking the running peak
    (high-water mark). ``magnitude`` = ``(peak - close_at_as_of) / peak`` on
    ``adj_close``; ``velocity`` = ``magnitude`` ÷ trading-days from the peak bar to
    ``as_of`` (0 when the peak IS ``as_of``). Returns ``magnitude``/``velocity`` as
    ``Decimal`` plus the peak date and the ``as_of`` close.
    """
    up_to = [(d, c) for d, c in series if d <= as_of]
    if not up_to:
        return {
            "magnitude": Decimal("0"),
            "velocity": Decimal("0"),
            "peak_date": None,
            "peak_close": None,
            "as_of_close": None,
            "days_from_peak": 0,
        }

    peak_close = up_to[0][1]
    peak_date = up_to[0][0]
    peak_index = 0
    # Use >= (take the LAST bar of a flat high-water plateau) so the current
    # drawdown's peak is chosen by the SAME rule as historical_episodes — keeping
    # velocity and the peak_date exclusion filter consistent between the two paths.
    for i, (d, c) in enumerate(up_to):
        if c >= peak_close:
            peak_close = c
            peak_date = d
            peak_index = i

    as_of_close = up_to[-1][1]
    days_from_peak = (len(up_to) - 1) - peak_index

    if peak_close <= 0:
        magnitude = Decimal("0")
    else:
        magnitude = (peak_close - as_of_close) / peak_close
    if magnitude < 0:
        magnitude = Decimal("0")

    if days_from_peak > 0:
        velocity = magnitude / Decimal(days_from_peak)
    else:
        velocity = Decimal("0")

    return {
        "magnitude": magnitude,
        "velocity": velocity,
        "peak_date": peak_date,
        "peak_close": peak_close,
        "as_of_close": as_of_close,
        "days_from_peak": days_from_peak,
    }


def historical_episodes(series: list[tuple[date, Decimal]]) -> list[dict]:
    """Detect every past peak→trough→recovery-to-peak drawdown episode.

    An episode opens at a running-peak bar as soon as a later bar dips below it; its
    trough is the minimum ``adj_close`` reached before the price recovers back to
    (or above) that peak. ``recovery_days`` is trading days from trough to the
    breakeven bar; episodes that never recover by the end of data have
    ``recovered=False``, ``recovery_days=None`` (excluded from recovery medians but
    still counted). ``forward_return_1yr`` is the close-to-close return over
    :data:`FORWARD_RETURN_DAYS` trading days from the trough, or ``None`` when fewer
    than that many bars follow (excluded from the median). ``magnitude`` and
    ``velocity`` mirror the current-drawdown definitions.
    """
    episodes: list[dict] = []
    n = len(series)
    if n < 2:
        return episodes

    i = 0
    while i < n:
        peak_index = i
        peak_close = series[i][1]
        # Extend the peak while price keeps making new highs.
        j = i + 1
        while j < n and series[j][1] >= peak_close:
            peak_close = series[j][1]
            peak_index = j
            j += 1
        if j >= n:
            break  # peak runs to the end of data — no episode opens after it

        # A drawdown has opened at peak_index. Find the trough (min) until price
        # recovers back to the peak or data ends.
        trough_index = j
        trough_close = series[j][1]
        k = j
        recovered = False
        recovery_index = None
        while k < n:
            c = series[k][1]
            if c < trough_close:
                trough_close = c
                trough_index = k
            if c >= peak_close:
                recovered = True
                recovery_index = k
                break
            k += 1

        magnitude = (peak_close - trough_close) / peak_close if peak_close > 0 else Decimal("0")
        days_peak_to_trough = trough_index - peak_index
        velocity = (
            magnitude / Decimal(days_peak_to_trough)
            if days_peak_to_trough > 0
            else Decimal("0")
        )
        recovery_days = (
            recovery_index - trough_index if recovered and recovery_index is not None else None
        )

        forward_return = None
        fwd_index = trough_index + FORWARD_RETURN_DAYS
        if fwd_index < n:
            base = series[trough_index][1]
            if base > 0:
                forward_return = (series[fwd_index][1] - base) / base

        episodes.append(
            {
                "peak_index": peak_index,
                "trough_index": trough_index,
                "recovery_index": recovery_index,
                "peak_date": series[peak_index][0],
                "trough_date": series[trough_index][0],
                "recovery_date": series[recovery_index][0] if recovered else None,
                "magnitude": magnitude,
                "velocity": velocity,
                "recovery_days": recovery_days,
                "recovered": recovered,
                "forward_return_1yr": forward_return,
            }
        )

        if recovered and recovery_index is not None:
            # Continue scanning from the recovery bar (a new peak search).
            i = recovery_index
        else:
            break  # unrecovered episode runs to end of data — nothing follows
    return episodes


# --- Matching + ranking ------------------------------------------------------


def _match_and_rank(
    episodes: list[dict], current_magnitude: Decimal, current_velocity: Decimal
) -> list[dict]:
    """Band-filter episodes to the current magnitude, then rank the matches.

    Hard filter: ``|episode.magnitude - current_magnitude| <= MAGNITUDE_BAND``.
    Deterministic rank key: ``|Δmagnitude|`` then ``|Δvelocity|`` (velocity is a
    secondary tiebreaker, not a hard cut), then episode order for full stability.
    """
    matches = [
        e
        for e in episodes
        if abs(e["magnitude"] - current_magnitude) <= MAGNITUDE_BAND
    ]
    matches.sort(
        key=lambda e: (
            abs(e["magnitude"] - current_magnitude),
            abs(e["velocity"] - current_velocity),
            e["peak_index"],
        )
    )
    return matches


# --- Record builders ---------------------------------------------------------


def _source_str(symbol: str) -> str:
    """Provenance string for the record's ``source`` field."""
    return f"{symbol} daily close (market_daily)"


def _build_strategy_record(
    symbol: str, as_of: date, statement: str, reason: str
) -> EvidenceRecord:
    """Build the always-available ``strategy`` fallback record (empty windows).

    ``stats.reason`` (``insufficient_data`` | ``all_time_high`` | ``no_band_match``)
    makes the fallback situation machine-distinguishable — and gives each reason a
    distinct deterministic ``id`` — without a downstream consumer having to parse the
    English ``statement``.
    """
    stats: dict = {"reason": reason, "windows": []}
    return EvidenceRecord(
        id=make_id(EvidenceKind.STRATEGY, symbol, as_of, stats),
        kind=EvidenceKind.STRATEGY,
        statement=statement,
        stats=stats,
        source=_source_str(symbol),
        as_of=as_of,
    )


def _build_event_precedent_record(
    symbol: str,
    as_of: date,
    current: dict,
    matches: list[dict],
) -> EvidenceRecord:
    """Build the ONE aggregate ``event-precedent`` record from the matched windows.

    Aggregate ``stats`` carry the current drawdown context plus medians/ranges over
    the matched episodes; per-episode detail lives in ``stats.windows`` (keeping the
    fixed 6-field shape). ``recovery_days_*`` use only recovered episodes;
    ``forward_return_1yr_median`` uses only episodes with a full forward window.
    Integer-day medians use :func:`statistics.median_low` to stay ``int``.
    """
    windows = []
    recovery_days_list: list[int] = []
    forward_returns: list[Decimal] = []
    for e in matches:
        window = {
            "peak_date": e["peak_date"],
            "trough_date": e["trough_date"],
            "recovery_date": e["recovery_date"],
            "drawdown_pct": _q(e["magnitude"]),
            "velocity": _q(e["velocity"]),
            "recovery_days": e["recovery_days"],
            "recovered": e["recovered"],
            "forward_return_1yr": (
                _q(e["forward_return_1yr"])
                if e["forward_return_1yr"] is not None
                else None
            ),
        }
        windows.append(window)
        if e["recovered"] and e["recovery_days"] is not None:
            recovery_days_list.append(e["recovery_days"])
        if e["forward_return_1yr"] is not None:
            forward_returns.append(e["forward_return_1yr"])

    instance_count = len(matches)

    if recovery_days_list:
        recovery_days_median: int | None = int(statistics.median_low(recovery_days_list))
        recovery_days_range: dict | None = {
            "min": int(min(recovery_days_list)),
            "max": int(max(recovery_days_list)),
        }
    else:
        recovery_days_median = None
        recovery_days_range = None

    if forward_returns:
        forward_return_1yr_median: Decimal | None = _q(statistics.median_low(forward_returns))
    else:
        forward_return_1yr_median = None

    stats = {
        "initial_drawdown_pct": _q(current["magnitude"]),
        "current_velocity": _q(current["velocity"]),
        "instance_count": instance_count,
        "recovery_days_median": recovery_days_median,
        "recovery_days_range": recovery_days_range,
        "forward_return_1yr_median": forward_return_1yr_median,
        "windows": windows,
    }

    pct_display = (current["magnitude"] * Decimal("100")).quantize(_PCT_DISPLAY_Q)
    if recovery_days_median is not None:
        statement = (
            f"{symbol} is ~{pct_display}% below its recent peak. In {instance_count} "
            f"similar drops, it recovered to breakeven in a median of "
            f"{recovery_days_median} trading days."
        )
    else:
        statement = (
            f"{symbol} is ~{pct_display}% below its recent peak. {instance_count} "
            f"similar drops occurred historically; none has fully recovered within "
            f"the available data."
        )

    return EvidenceRecord(
        id=make_id(EvidenceKind.EVENT_PRECEDENT, symbol, as_of, stats),
        kind=EvidenceKind.EVENT_PRECEDENT,
        statement=statement,
        stats=stats,
        source=_source_str(symbol),
        as_of=as_of,
    )


# --- Public entry point ------------------------------------------------------


async def find_precedent(
    session: AsyncSession,
    symbol: str = DEFAULT_BENCHMARK,
    as_of: date | None = None,
) -> list[EvidenceRecord]:
    """Find drawdown precedents for ``symbol`` as of ``as_of`` (default: latest bar).

    Returns a length-1 ``list[EvidenceRecord]`` in v1 — ONE aggregate
    ``event-precedent`` record when ≥ 1 historical episode matches the current
    drawdown's magnitude band, otherwise the ``strategy`` fallback. NEVER an empty
    list. Fully deterministic: ``as_of`` defaults to the latest ``day`` in the
    loaded series (never today), and identical inputs yield an identical ``id``.

    Insufficient data (symbol absent, or < 2 bars) logs a structured warning and
    degrades to the ``strategy`` record — it never crashes.
    """
    series = await _load_series(session, symbol)

    if len(series) < 2:
        logger.warning(
            "precedent_insufficient_data symbol=%s bars=%d",
            symbol,
            len(series),
        )
        # With no bars AND no caller-supplied as_of there is no data day to stamp,
        # yet the record must still be returned (never dead-end). date.min is a
        # fixed, deterministic sentinel — NOT date.today(): reading the wall clock
        # here would break the no-wall-clock determinism invariant. It is a valid
        # ISO-8601 date, so the AD-12 shape holds; callers with an empty table
        # should pass an explicit as_of.
        resolved_as_of = as_of if as_of is not None else (
            series[-1][0] if series else date.min
        )
        return [
            _build_strategy_record(
                symbol,
                resolved_as_of,
                f"Not enough price history for {symbol} to find a comparable "
                "precedent. Stay the course with your plan.",
                reason="insufficient_data",
            )
        ]

    resolved_as_of = as_of if as_of is not None else series[-1][0]
    current = current_drawdown(series, resolved_as_of)

    # Not in a drawdown (at its all-time high) → strategy fallback.
    if current["magnitude"] <= _NO_DRAWDOWN:
        return [
            _build_strategy_record(
                symbol,
                resolved_as_of,
                f"{symbol} is at or near its all-time high — no comparable drop to "
                "cite. Stay the course with your plan.",
                reason="all_time_high",
            )
        ]

    episodes = historical_episodes(series)
    # Exclude the CURRENT, in-progress drawdown itself (its own peak occurs at or
    # after the current-drawdown peak): it is the drop we are finding precedents
    # FOR, not a past precedent. Everything strictly before it is genuine history.
    current_peak_date = current["peak_date"]
    past_episodes = [
        e for e in episodes if e["peak_date"] < current_peak_date
    ]
    matches = _match_and_rank(
        past_episodes, current["magnitude"], current["velocity"]
    )

    if not matches:
        return [
            _build_strategy_record(
                symbol,
                resolved_as_of,
                f"No historical drawdown of comparable size to {symbol}'s current "
                "drop was found. Stay the course with your plan.",
                reason="no_band_match",
            )
        ]

    return [_build_event_precedent_record(symbol, resolved_as_of, current, matches)]
