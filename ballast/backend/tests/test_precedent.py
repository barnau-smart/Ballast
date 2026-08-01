"""Story 3.2 tests — the deterministic Precedent Engine + AD-12 Evidence Record.

REAL DB (no mocks), mirroring the Story 3.1 style (see ``test_market_ingest.py``):
the autouse table-creation fixture, per-test cleanup via sync psycopg, and
TEST-prefixed symbols so these DB-writing tests never collide with a real ingest
over the shared GLOBAL ``market_daily`` table. NO network, NO LLM, NO credentials.

We insert crafted, fully deterministic ``adj_close`` series with a KNOWN
peak→trough→recovery so every asserted ``Decimal`` stat is pinned, then walk the
I/O & Edge-Case Matrix: qualifying match, no-match → strategy, not-in-drawdown →
strategy, insufficient data → strategy (no crash), determinism (identical id), and
the fixed 6-field shape / ``kind`` / ``Decimal`` assertions.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio

from db.connection import get_connection
from db.models import MarketDaily
from db.session import async_session_maker, engine
from precedent import EvidenceKind, EvidenceRecord, find_precedent

# TEST-prefixed symbols — safe against the shared global table.
SYM_QUALIFY = "TEST_PREC_QUALIFY"
SYM_NOMATCH = "TEST_PREC_NOMATCH"
SYM_ATH = "TEST_PREC_ATH"
SYM_ABSENT = "TEST_PREC_ABSENT"
SYM_ONEBAR = "TEST_PREC_ONEBAR"
SYM_DETERM = "TEST_PREC_DETERM"
SYM_MULTI = "TEST_PREC_MULTI"
SYM_DEFAULT = "TEST_PREC_DEFAULT"

ALL_TEST_SYMBOLS = [
    SYM_QUALIFY,
    SYM_NOMATCH,
    SYM_ATH,
    SYM_ABSENT,
    SYM_ONEBAR,
    SYM_DETERM,
    SYM_MULTI,
    SYM_DEFAULT,
]

BASE_DAY = date(2015, 1, 1)


@pytest_asyncio.fixture(autouse=True)
async def ensure_table():
    """Ensure the market_daily table exists for these real-DB tests."""
    async with engine.begin() as conn:
        await conn.run_sync(MarketDaily.__table__.create, checkfirst=True)
    yield


def _clean(symbols: list[str]) -> None:
    """Remove any rows for these symbols so each test starts clean (global table)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM market_daily WHERE symbol = ANY(%s)", (symbols,))
        conn.commit()


def _insert_series(symbol: str, closes: list[Decimal]) -> None:
    """Insert a deterministic series: one bar/day, adj_close = close = each value.

    OHLC all equal the value (irrelevant to drawdown math, which uses ``adj_close``)
    so every asserted stat is a pure function of the crafted ``closes``.
    """
    ingested_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    with get_connection() as conn:
        with conn.cursor() as cur:
            for i, v in enumerate(closes):
                day = BASE_DAY + timedelta(days=i)
                cur.execute(
                    "INSERT INTO market_daily "
                    "(id, symbol, day, open, high, low, close, adj_close, "
                    " volume, source, ingested_at) "
                    "VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s, %s, "
                    "        %s, %s, %s)",
                    (symbol, day, v, v, v, v, v, 1000, "test", ingested_at),
                )
        conn.commit()


def _qualify_closes() -> list[Decimal]:
    """A crafted series with ONE clean historical 8% episode + a current 8% drop.

    Episode A: peak 100 (day idx2) → trough 92 (idx4, 8% drawdown) → recover 100
    (idx6, 2 trading days from trough). A long monotonic climb follows so episode
    A has a full 252-bar forward window (trough+252 = 350, a 2.8043 return), then a
    fresh 8% drop at the very end is the CURRENT drawdown (peak 360 → 331.20).
    """
    closes = [
        Decimal("90"),
        Decimal("95"),
        Decimal("100"),  # peak A
        Decimal("96"),
        Decimal("92"),  # trough A (8%)
        Decimal("96"),
        Decimal("100"),  # recovery A
    ]
    for i in range(1, 261):
        closes.append(Decimal("100") + Decimal(i))  # 101 .. 360, monotonic
    closes.append(Decimal("355"))
    closes.append(Decimal("331.20"))  # current: 8% below 360
    return closes


# --- Qualifying precedent ----------------------------------------------------


@pytest.mark.asyncio
async def test_qualifying_precedent_exact_stats_and_windows():
    _clean([SYM_QUALIFY])
    _insert_series(SYM_QUALIFY, _qualify_closes())
    try:
        async with async_session_maker() as session:
            records = await find_precedent(session, SYM_QUALIFY)
        assert len(records) == 1
        rec = records[0]
        assert isinstance(rec, EvidenceRecord)
        assert rec.kind is EvidenceKind.EVENT_PRECEDENT
        assert rec.as_of == date(2015, 9, 26)  # latest day = default as_of

        stats = rec.stats
        # Exact aggregate stats (pinned from the crafted series).
        assert stats["initial_drawdown_pct"] == Decimal("0.0800")
        assert stats["current_velocity"] == Decimal("0.0400")
        assert stats["instance_count"] == 1
        assert stats["recovery_days_median"] == 2
        assert stats["recovery_days_range"] == {"min": 2, "max": 2}
        assert stats["forward_return_1yr_median"] == Decimal("2.8043")

        # Exactly one matched window with pinned per-episode detail.
        assert len(stats["windows"]) == 1
        w = stats["windows"][0]
        assert w["peak_date"] == date(2015, 1, 3)
        assert w["trough_date"] == date(2015, 1, 5)
        assert w["recovery_date"] == date(2015, 1, 7)
        assert w["drawdown_pct"] == Decimal("0.0800")
        assert w["velocity"] == Decimal("0.0400")
        assert w["recovery_days"] == 2
        assert w["recovered"] is True
        assert w["forward_return_1yr"] == Decimal("2.8043")

        # Decimals are Decimal, day counts are int (never float).
        assert isinstance(stats["initial_drawdown_pct"], Decimal)
        assert isinstance(stats["forward_return_1yr_median"], Decimal)
        assert isinstance(stats["recovery_days_median"], int)
        assert isinstance(w["drawdown_pct"], Decimal)
        assert isinstance(w["recovery_days"], int)
    finally:
        _clean([SYM_QUALIFY])


# --- No qualifying precedent → strategy --------------------------------------


@pytest.mark.asyncio
async def test_no_matching_episode_returns_strategy():
    """In a drawdown, but NO past episode falls in the ±2.5pp magnitude band."""
    _clean([SYM_NOMATCH])
    # A tiny early dip (~2%) then a huge current ~40% drop with no comparable past
    # episode in the band [37.5%, 42.5%].
    closes = [
        Decimal("100"),
        Decimal("98"),  # ~2% dip
        Decimal("100"),  # recovered
        Decimal("120"),
        Decimal("150"),  # peak 150
        Decimal("120"),
        Decimal("90"),  # current: 40% below 150
    ]
    _insert_series(SYM_NOMATCH, closes)
    try:
        async with async_session_maker() as session:
            records = await find_precedent(session, SYM_NOMATCH)
        assert len(records) == 1
        rec = records[0]
        assert rec.kind is EvidenceKind.STRATEGY
        assert rec.stats["windows"] == []
    finally:
        _clean([SYM_NOMATCH])


# --- Not in a drawdown (all-time high) → strategy ----------------------------


@pytest.mark.asyncio
async def test_not_in_drawdown_returns_strategy():
    _clean([SYM_ATH])
    # Monotonically rising → as_of is the all-time high, magnitude == 0.
    closes = [Decimal("100") + Decimal(i) for i in range(10)]
    _insert_series(SYM_ATH, closes)
    try:
        async with async_session_maker() as session:
            records = await find_precedent(session, SYM_ATH)
        assert len(records) == 1
        assert records[0].kind is EvidenceKind.STRATEGY
        assert records[0].stats["windows"] == []
    finally:
        _clean([SYM_ATH])


# --- Insufficient data → strategy (no crash) ---------------------------------


@pytest.mark.asyncio
async def test_absent_symbol_returns_strategy_no_crash():
    _clean([SYM_ABSENT])
    async with async_session_maker() as session:
        records = await find_precedent(session, SYM_ABSENT)
    assert len(records) == 1
    assert records[0].kind is EvidenceKind.STRATEGY
    assert records[0].stats["windows"] == []


@pytest.mark.asyncio
async def test_single_bar_returns_strategy_no_crash():
    _clean([SYM_ONEBAR])
    _insert_series(SYM_ONEBAR, [Decimal("100")])  # < 2 bars
    try:
        async with async_session_maker() as session:
            records = await find_precedent(session, SYM_ONEBAR)
        assert len(records) == 1
        assert records[0].kind is EvidenceKind.STRATEGY
    finally:
        _clean([SYM_ONEBAR])


# --- Determinism -------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_runs_are_byte_identical_including_id():
    _clean([SYM_DETERM])
    _insert_series(SYM_DETERM, _qualify_closes())
    try:
        async with async_session_maker() as session:
            first = await find_precedent(session, SYM_DETERM)
        async with async_session_maker() as session:
            second = await find_precedent(session, SYM_DETERM)
        assert len(first) == len(second) == 1
        # Identical id and byte-identical JSON-safe dicts.
        assert first[0].id == second[0].id
        assert first[0].to_dict() == second[0].to_dict()
        assert first[0].id.startswith("ep-")
    finally:
        _clean([SYM_DETERM])


# --- Fixed AD-12 shape / kind / passing as_of --------------------------------


@pytest.mark.asyncio
async def test_record_has_exactly_the_ad12_shape():
    _clean([SYM_QUALIFY])
    _insert_series(SYM_QUALIFY, _qualify_closes())
    try:
        async with async_session_maker() as session:
            rec = (await find_precedent(session, SYM_QUALIFY))[0]
        d = rec.to_dict()
        assert set(d.keys()) == {"id", "kind", "statement", "stats", "source", "as_of"}
        assert d["kind"] in {"event-precedent", "strategy"}
        # as_of is an ISO-8601 date string in the JSON-safe form.
        assert d["as_of"] == "2015-09-26"
        assert isinstance(d["as_of"], str)
        # JSON-safe: Decimals rendered as strings, ints preserved.
        assert d["stats"]["initial_drawdown_pct"] == "0.0800"
        assert d["stats"]["instance_count"] == 1
    finally:
        _clean([SYM_QUALIFY])


@pytest.mark.asyncio
async def test_explicit_as_of_is_honored_not_wall_clock():
    """Passing as_of mid-series uses that date, not the latest bar / today."""
    _clean([SYM_QUALIFY])
    _insert_series(SYM_QUALIFY, _qualify_closes())
    try:
        # as_of at the all-time-high tail-peak (day idx266 = 360) → no drawdown.
        as_of = BASE_DAY + timedelta(days=266)
        async with async_session_maker() as session:
            records = await find_precedent(session, SYM_QUALIFY, as_of=as_of)
        assert records[0].kind is EvidenceKind.STRATEGY
        assert records[0].as_of == as_of
    finally:
        _clean([SYM_QUALIFY])


# --- Multi-window aggregation (medians/ranges over >1 matched episode) --------


def _multi_episode_closes() -> list[Decimal]:
    """Four clean 8% episodes with recovery_days 2, 4, 6, 8 + a current 8% drop.

    Every episode peaks at 100 and troughs at 92 (exactly 8%), so all four fall in
    the ±2.5pp band around the current 8% drop — exercising the aggregate median /
    range math (including EVEN-count ``median_low``) over more than one window. No
    252-bar forward tail, so ``forward_return_1yr`` is ``None`` for every window and
    the forward-return median is ``None``.
    """
    return [
        # Ep1 recovery_days=2 (peak idx0 → trough idx2 → recover idx4)
        Decimal("100"), Decimal("96"), Decimal("92"), Decimal("96"), Decimal("100"),
        # Ep2 recovery_days=4 (peak idx4 → trough idx8 → recover idx12)
        Decimal("98"), Decimal("96"), Decimal("94"), Decimal("92"),
        Decimal("94"), Decimal("96"), Decimal("98"), Decimal("100"),
        # Ep3 recovery_days=6 (peak idx12 → trough idx15 → recover idx21)
        Decimal("97"), Decimal("94"), Decimal("92"), Decimal("93"),
        Decimal("95"), Decimal("97"), Decimal("98"), Decimal("99"), Decimal("100"),
        # Ep4 recovery_days=8 (peak idx21 → trough idx24 → recover idx32)
        Decimal("97"), Decimal("94"), Decimal("92"), Decimal("93"), Decimal("94"),
        Decimal("95"), Decimal("96"), Decimal("97"), Decimal("98"), Decimal("99"),
        Decimal("100"),
        # Current in-progress 8% drop (peak idx32=100 → idx33=92); excluded as its
        # own precedent because its peak is not strictly before itself.
        Decimal("92"),
    ]


@pytest.mark.asyncio
async def test_multi_episode_aggregation_median_and_range():
    _clean([SYM_MULTI])
    _insert_series(SYM_MULTI, _multi_episode_closes())
    try:
        async with async_session_maker() as session:
            rec = (await find_precedent(session, SYM_MULTI))[0]
        assert rec.kind is EvidenceKind.EVENT_PRECEDENT
        stats = rec.stats
        assert stats["instance_count"] == 4
        # median_low([2,4,6,8]) == 4 (lower of the two middle values) — even count.
        assert stats["recovery_days_median"] == 4
        assert stats["recovery_days_range"] == {"min": 2, "max": 8}
        # No forward window fits in this short series → median is None.
        assert stats["forward_return_1yr_median"] is None
        # All four matched windows present, each recovered, order-independent set.
        assert len(stats["windows"]) == 4
        assert sorted(w["recovery_days"] for w in stats["windows"]) == [2, 4, 6, 8]
        assert all(w["recovered"] is True for w in stats["windows"])
        assert all(w["forward_return_1yr"] is None for w in stats["windows"])
        assert "4 similar drops" in rec.statement
    finally:
        _clean([SYM_MULTI])


# --- Default as_of is data-derived (latest bar), never the wall clock ----------


@pytest.mark.asyncio
async def test_default_as_of_is_latest_bar_not_today():
    """With as_of=None the record is stamped with the latest DATA day, not today.

    The series' last day is in 2015, so a record whose as_of matches it proves the
    default is derived from the data — a wall-clock read would stamp the current
    date instead (guards the determinism invariant a same-day two-run test cannot).
    """
    _clean([SYM_DEFAULT])
    _insert_series(SYM_DEFAULT, [Decimal("100"), Decimal("92")])  # last day = idx1
    try:
        latest_day = BASE_DAY + timedelta(days=1)  # date(2015, 1, 2)
        async with async_session_maker() as session:
            rec = (await find_precedent(session, SYM_DEFAULT))[0]
        assert rec.as_of == latest_day
        assert rec.as_of != date.today()
    finally:
        _clean([SYM_DEFAULT])


# --- Contract is JSON-serializable and never carries a Python float -----------


def _assert_no_float(obj) -> None:
    """Recursively assert no ``float`` appears anywhere (money must be Decimal)."""
    if isinstance(obj, bool):
        return
    assert not isinstance(obj, float), f"unexpected float: {obj!r}"
    if isinstance(obj, dict):
        for v in obj.values():
            _assert_no_float(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _assert_no_float(v)


def test_evidence_stats_decimals_are_fixed_point_no_exponent():
    """Story 6.4: extreme-large/tiny `Decimal`s in `EvidenceRecord.stats` serialize
    fixed-point (no E+/E-) through `_json_safe`, including nested in lists."""
    big = Decimal("1E29") / Decimal("100")  # str() → "1E+27"
    tiny = Decimal("1E-8")  # str() → "1E-8"
    rec = EvidenceRecord(
        id="deadbeef",
        kind=EvidenceKind.EVENT_PRECEDENT,
        statement="",
        stats={
            "big": big,
            "tiny": tiny,
            "windows": [{"drop": big}, {"drop": tiny}],
        },
        source="test",
        as_of=BASE_DAY,
    )
    d = rec.to_dict()
    stats = d["stats"]
    assert stats["big"] == "1000000000000000000000000000"
    assert stats["tiny"] == "0.00000001"
    assert stats["windows"][0]["drop"] == "1000000000000000000000000000"
    assert stats["windows"][1]["drop"] == "0.00000001"
    # No exponent anywhere, and round-trips via Decimal(str(...)).
    assert "E" not in json.dumps(stats) and "e" not in json.dumps(stats)
    assert Decimal(stats["big"]) == big
    assert Decimal(stats["tiny"]) == tiny


@pytest.mark.asyncio
async def test_record_is_json_safe_and_never_float():
    _clean([SYM_QUALIFY])
    _insert_series(SYM_QUALIFY, _qualify_closes())
    try:
        async with async_session_maker() as session:
            rec = (await find_precedent(session, SYM_QUALIFY))[0]
        # Raw stats must never contain a Python float (Decimal for money/percent).
        _assert_no_float(rec.stats)
        # to_dict() must round-trip through json.dumps without raising.
        d = rec.to_dict()
        encoded = json.dumps(d)
        assert json.loads(encoded)["id"] == rec.id
    finally:
        _clean([SYM_QUALIFY])


# --- Determinism does not depend on input Decimal precision -------------------


@pytest.mark.asyncio
async def test_id_is_stable_across_input_decimal_precision():
    """Same values written with different trailing-zero precision → identical id.

    ``market_daily.adj_close`` is ``Numeric(20,8)`` and the engine quantizes before
    hashing, so ``Decimal("331.20")`` and ``Decimal("331.20000000")`` must yield a
    byte-identical record incl. ``id``. Pins the load-bearing AD-5 replay invariant
    against a future change that let raw round-trip precision leak into ``make_id``
    (the two-run determinism test uses byte-identical inserts and cannot catch it).
    """
    _clean([SYM_DETERM])
    _insert_series(SYM_DETERM, _qualify_closes())
    try:
        async with async_session_maker() as session:
            coarse = (await find_precedent(session, SYM_DETERM))[0]
    finally:
        _clean([SYM_DETERM])

    # Same numeric series, but every value carries extra trailing-zero precision.
    padded = [c + Decimal("0.00000000") for c in _qualify_closes()]
    _insert_series(SYM_DETERM, padded)
    try:
        async with async_session_maker() as session:
            padded_rec = (await find_precedent(session, SYM_DETERM))[0]
        assert padded_rec.id == coarse.id
        assert padded_rec.to_dict() == coarse.to_dict()
    finally:
        _clean([SYM_DETERM])


# --- Forward-return median over MORE THAN ONE window --------------------------


def _multi_forward_closes() -> list[Decimal]:
    """Two 8% episodes that BOTH have a full 252-bar forward window + a current drop.

    Ep1 (peak 100 idx0 → trough 92 idx1 → recover 100 idx2) and Ep2 (peak 100 idx2 →
    trough 92 idx3 → recover 100 idx4) are each 8% and each gain 252 following bars
    from the long climb, so ``forward_return_1yr`` is a DISTINCT ``Decimal`` for BOTH
    — exercising ``median_low`` over more than one forward value (a path the single-
    forward qualify test and the all-``None``-forward multi-episode test never hit).
    A fresh 8% drop at the tail is the current drawdown (excluded as its own
    precedent because its peak is not strictly before itself).
    """
    closes = [
        Decimal("100"),  # peak1 idx0
        Decimal("92"),   # trough1 idx1 (8%)
        Decimal("100"),  # recovery1 idx2 == peak2
        Decimal("92"),   # trough2 idx3 (8%)
        Decimal("100"),  # recovery2 idx4
    ]
    # Long monotonic climb so both troughs get >= 252 forward bars (idx5..264).
    for v in range(101, 361):  # 101 .. 360 → 260 bars, running peak ends at 360
        closes.append(Decimal(v))
    closes.append(Decimal("355"))
    closes.append(Decimal("331.20"))  # current: 8% below 360
    return closes


@pytest.mark.asyncio
async def test_forward_return_median_over_multiple_windows_is_decimal():
    _clean([SYM_MULTI])
    _insert_series(SYM_MULTI, _multi_forward_closes())
    try:
        async with async_session_maker() as session:
            rec = (await find_precedent(session, SYM_MULTI))[0]
        stats = rec.stats
        assert rec.kind is EvidenceKind.EVENT_PRECEDENT
        assert stats["instance_count"] == 2
        fwds = [w["forward_return_1yr"] for w in stats["windows"]]
        # Both windows carry a Decimal forward return, and they are DISTINCT.
        assert all(isinstance(f, Decimal) for f in fwds)
        assert len(fwds) == 2 and fwds[0] != fwds[1]
        median = stats["forward_return_1yr_median"]
        # Aggregate median over >1 value stays Decimal (never None, never float)…
        assert isinstance(median, Decimal)
        # …and median_low of an even (2) set is the lower value.
        assert median == min(fwds)
    finally:
        _clean([SYM_MULTI])
