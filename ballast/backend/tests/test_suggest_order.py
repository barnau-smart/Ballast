"""Story 8.4 tests — AI "suggest & populate the order" (MasterB core vision).

Locks the deterministic resting-limit engine + the ``POST /api/coach/suggest-order``
HTTP surface, entirely OFFLINE: zero network, zero credentials, fake adapters
(``BROKER_ADAPTER=fake`` / ``LLM_ADAPTER=fake``).

Two layers:
  - Engine unit tests (``coach.suggest``): the LOCKED price is pinned against fixed
    ``MarketDaily`` + ask fixtures; real-vs-fake LLM price identity is proven
    WITHOUT a real LLM (a stub gateway returning a different reasoning yields the
    byte-identical number); every calm decline (non-core, no history, insufficient
    cash, unreadable quote) raises ``OrderScopeError``/``OrderNotPlaceableError`` and
    places nothing; the narration fallback survives a crashing gateway.
  - API tests (``/api/coach/suggest-order``): a happy suggest returns fixed-point
    strings + a BUY LIMIT GTC shape and places nothing; a lapsed session is a calm
    409; each decline is a calm 422 ``{error:{type,message}}``.

Requires the docker Postgres (``docker compose up -d db``). Each test uses unique
users/symbols and cleans up its own rows.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import RECONNECT_MESSAGE, require_live_broker_session
from brokers.factory import get_broker
from brokers.fake_adapter import FAKE_FILL_PRICE, FakeBrokerAdapter
from brokers.port import (
    BrokerPort,
    BrokerTokens,
    OrderNotPlaceableError,
    OrderOutcome,
    PortfolioSnapshot,
    Quote,
)
from brokers.session import BrokerageSession
from coach.execution import OrderScopeError, whole_share_quantity
from coach.recommendation import Duration, OrderSide, OrderType
from coach.suggest import (
    SUGGEST_DISCOUNT,
    SUGGEST_LOOKBACK_DAYS,
    SUGGEST_MIN_DISCOUNT_FROM_ASK,
    SUGGEST_STALE_AFTER_DAYS,
    SUGGEST_STALE_REFUSE_AFTER_DAYS,
    compute_suggested_price,
    fill_likelihood,
    narrate_suggestion,
    suggest_resting_order,
)
from db.connection import get_connection
from db.models import (
    BrokerageToken,
    DecisionRecord,
    MarketDaily,
    PortfolioBalance,
    PortfolioCache,
)
from db.scope import Scope
from db.session import async_session_maker, engine
from llm.fake_adapter import FakeLLMGateway
from llm.port import LLMGateway, LLMResponse

PASSWORD = "supersecret123"
BASE_DAY = date(2015, 1, 1)


# --- table + fixture setup ---------------------------------------------------


@pytest_asyncio.fixture(autouse=True)
async def ensure_tables():
    async with engine.begin() as conn:
        await conn.run_sync(BrokerageToken.__table__.create, checkfirst=True)
        await conn.run_sync(PortfolioCache.__table__.create, checkfirst=True)
        await conn.run_sync(PortfolioBalance.__table__.create, checkfirst=True)
        await conn.run_sync(MarketDaily.__table__.create, checkfirst=True)
        await conn.run_sync(DecisionRecord.__table__.create, checkfirst=True)
    yield


# --- fixtures / helpers ------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    with TestClient(create_app()) as c:
        yield c


def _unique_email() -> str:
    return f"suggest-test-{uuid.uuid4().hex}@example.com"


def _unique_symbol() -> str:
    # A per-test symbol keeps MarketDaily rows isolated. It must be index-core to
    # pass the gate, so we monkeypatch is_index_core in the engine tests; the API
    # tests use a real index-core symbol (VTI) with a per-test day offset instead.
    return f"SUG{uuid.uuid4().hex[:6].upper()}"


def _register(client: TestClient, email: str) -> None:
    resp = client.post(
        "/api/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert resp.status_code == 201, resp.text


def _login(client: TestClient, email: str) -> str:
    resp = client.post(
        "/api/auth/jwt/login",
        data={"username": email, "password": PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _user_id_for(email: str) -> uuid.UUID:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT id FROM "user" WHERE email = %s', (email,))
            (uid_raw,) = cur.fetchone()
    return uuid.UUID(str(uid_raw))


def _delete_user(email: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM "user" WHERE email = %s', (email,))
        conn.commit()


def _insert_token_sync(owner: uuid.UUID, expires_at: datetime) -> None:
    from brokers.crypto import encrypt_token

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO brokerage_token "
                "(id, owner_id, provider, access_token, refresh_token, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    str(uuid.uuid4()),
                    str(owner),
                    "fake",
                    encrypt_token("access"),
                    encrypt_token("refresh"),
                    expires_at,
                ),
            )
        conn.commit()


def _insert_balance(owner: uuid.UUID, cash: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO portfolio_balance (id, owner_id, cash, as_of) "
                "VALUES (%s, %s, %s, %s)",
                (
                    str(uuid.uuid4()),
                    str(owner),
                    cash,
                    datetime(2026, 8, 1, tzinfo=timezone.utc),
                ),
            )
        conn.commit()


def _clean_market(symbols: list[str]) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM market_daily WHERE symbol = ANY(%s)", (symbols,))
        conn.commit()


def _insert_series(symbol: str, lows: list[Decimal], *, day0: date = BASE_DAY) -> None:
    """Insert one bar per element; ``low`` is the driver, other OHLC set to low+5."""
    ingested_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    with get_connection() as conn:
        with conn.cursor() as cur:
            for i, low in enumerate(lows):
                day = day0 + timedelta(days=i)
                other = low + Decimal("5")
                cur.execute(
                    "INSERT INTO market_daily "
                    "(id, symbol, day, open, high, low, close, adj_close, "
                    " volume, source, ingested_at) "
                    "VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s, %s, "
                    "        %s, %s, %s)",
                    (symbol, day, other, other, low, other, other, 1000, "test",
                     ingested_at),
                )
        conn.commit()


def _fresh_day0() -> date:
    """A series-start day so the NEWEST of ``SUGGEST_LOOKBACK_DAYS`` bars is fresh.

    Story 8.6: the ``/suggest-order`` endpoint now injects ``as_of=date.today()``
    and flags/refuses stale data. The API tests seed a 20-bar series ending at
    ``day0 + (SUGGEST_LOOKBACK_DAYS - 1)``; anchoring the newest bar to TODAY keeps
    the fixture inside the freshness window. A tiny per-test hour-free day jitter is
    unnecessary (each test cleans + re-inserts VTI), but we keep the newest bar
    exactly today for determinism.
    """
    return date.today() - timedelta(days=SUGGEST_LOOKBACK_DAYS - 1)


def _live() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=3)


def _live_session(provider: str = "fake") -> BrokerageSession:
    return BrokerageSession(state="live", expires_at=_live(), provider=provider)


class _StubGateway(LLMGateway):
    """A gateway that returns a FIXED, distinct reasoning (a stand-in for 'a real

    LLM'). It proves the price is byte-identical regardless of the narration — the
    model never touches the number.
    """

    provider = "stub"

    def complete(self, request) -> LLMResponse:
        return LLMResponse(
            output={"reasoning": "STUB-REASONING: a real model would say more."},
            model="stub-model",
            provider=self.provider,
        )


class _CrashingGateway(LLMGateway):
    """A gateway whose ``complete`` raises — proving the resilient fallback."""

    provider = "crash"

    def complete(self, request) -> LLMResponse:
        raise RuntimeError("gateway down")


class _QuoteAdapter(BrokerPort):
    """A fake broker with a settable ask + a place-order tripwire (Story 8.4).

    ``get_quote`` returns a configurable ask/bid so the price can be pinned against
    a known live ask; ``place_order``/``cancel_order`` raise so the test proves the
    suggest path NEVER places or cancels anything.
    """

    provider = "fake"

    def __init__(self, *, ask: Decimal, unreadable: bool = False) -> None:
        self._ask = ask
        self._unreadable = unreadable
        self._delegate = FakeBrokerAdapter()

    def authorization_url(self, state: str) -> str:
        return self._delegate.authorization_url(state)

    def exchange_code(self, code: str, state: str) -> BrokerTokens:
        return self._delegate.exchange_code(code, state)

    def fetch_portfolio(self) -> PortfolioSnapshot:
        return self._delegate.fetch_portfolio()

    async def place_order(self, order_intent, *, idempotency_key):
        raise AssertionError("suggest must NEVER place an order")

    async def get_order_status(self, idempotency_key):
        raise AssertionError("suggest must NEVER reconcile")

    async def get_order_status_by_ref(self, broker_ref):
        raise AssertionError("suggest must NEVER reconcile by ref")

    async def cancel_order(self, broker_ref):
        raise AssertionError("suggest must NEVER cancel")

    async def get_quote(self, symbol: str) -> Quote:
        if self._unreadable:
            raise OrderNotPlaceableError(
                f"No usable quote for {symbol} right now — no order was placed."
            )
        return Quote(bid=self._ask, ask=self._ask)


# =============================================================================
# PURE FORMULA — pinned, unit-level
# =============================================================================


def test_constants_are_locked():
    assert SUGGEST_LOOKBACK_DAYS == 20
    assert SUGGEST_DISCOUNT == Decimal("0.01")


def test_compute_price_recent_low_below_ask_pinned():
    # recent_low 90 < ask 100 → min(90,100)*0.99 = 89.10, quantized down.
    assert compute_suggested_price(Decimal("90"), Decimal("100")) == Decimal("89.10")


def test_compute_price_recent_low_at_or_above_ask_clamps_to_ask():
    # recent_low 120 >= ask 100 → base = min(120,100)*0.99 = 99.00 (~1% below).
    # Story 8.6: the falling-market floor now tightens this ~1% case to 98.00
    # (2% below the ask) — still strictly < ask.
    price = compute_suggested_price(Decimal("120"), Decimal("100"))
    assert price == Decimal("98.00")
    assert price < Decimal("100")


def test_compute_price_is_always_strictly_below_ask():
    for low in (Decimal("50"), Decimal("100"), Decimal("150.55"), Decimal("0.03")):
        ask = Decimal("100")
        assert compute_suggested_price(low, ask) < ask


def test_compute_price_quantizes_down_two_dp():
    # 99.999 * 0.99 = 98.99901 → ROUND_DOWN 2dp = 98.99.
    assert compute_suggested_price(Decimal("99.999"), Decimal("200")) == Decimal("98.99")


# --- Story 8.6: falling-market floor (pure) ---------------------------------


def test_floor_constant_is_locked():
    assert SUGGEST_MIN_DISCOUNT_FROM_ASK == Decimal("0.02")


def test_floor_only_bites_new_lows_case():
    # NEW-LOWS: recent_low barely below ask (99 < 100). Base = min(99,100)*0.99 =
    # 98.01 — only ~2% below, but the 1% discount off the low alone would be 98.01;
    # the floor = 100*0.98 = 98.00. base(98.01) vs floor(98.00) → floored to 98.00.
    price = compute_suggested_price(Decimal("99"), Decimal("100"))
    assert price == Decimal("98.00")
    # And it is now at least 2% below the ask.
    assert price <= Decimal("100") * (Decimal("1") - SUGGEST_MIN_DISCOUNT_FROM_ASK)


def test_floor_extreme_new_lows_clamps_to_two_percent():
    # recent_low == ask (a fresh low touching today's price): base = 100*0.99 =
    # 99.00 (~1% below). The floor pulls it to 98.00 (2% below).
    price = compute_suggested_price(Decimal("100"), Decimal("100"))
    assert price == Decimal("98.00")


def test_floor_preserves_deeper_discount_in_rising_flat_markets():
    # RISING/FLAT: recent_low well below ask (90 < 100). Base = 89.10 (~11% below),
    # floor = 98.00. min(89.10, 98.00) = 89.10 — the floor NEVER weakens the
    # deeper discount; it only ever lowers the price, never raises it.
    price = compute_suggested_price(Decimal("90"), Decimal("100"))
    assert price == Decimal("89.10")


def test_floored_price_still_strictly_below_ask():
    for low in (Decimal("99.99"), Decimal("100"), Decimal("150"), Decimal("80")):
        ask = Decimal("100")
        assert compute_suggested_price(low, ask) < ask


# --- Story 8.6: fill-likelihood bands (pure) --------------------------------


def test_fill_likelihood_near_market_band():
    band, note = fill_likelihood(Decimal("0.0100"))  # 1% below
    assert band == "near-market"
    assert "may fill soon" in note
    assert "cancel anytime" in note


def test_fill_likelihood_meaningfully_below_band():
    band, note = fill_likelihood(Decimal("0.0300"))  # 3% below
    assert band == "meaningfully-below"
    assert "may take a while" in note
    assert "cancel anytime" in note


def test_fill_likelihood_far_below_band():
    band, note = fill_likelihood(Decimal("0.1000"))  # 10% below
    assert band == "far-below"
    assert "may never fill" in note
    assert "cancel anytime" in note


def test_fill_likelihood_band_edges_are_deterministic():
    # Inclusive-low boundaries: exactly 2% is NO LONGER near-market; exactly 5% is
    # NO LONGER meaningfully-below.
    assert fill_likelihood(Decimal("0.0199"))[0] == "near-market"
    assert fill_likelihood(Decimal("0.0200"))[0] == "meaningfully-below"
    assert fill_likelihood(Decimal("0.0499"))[0] == "meaningfully-below"
    assert fill_likelihood(Decimal("0.0500"))[0] == "far-below"


def test_whole_share_quantity_floors_and_guards():
    assert whole_share_quantity(Decimal("500"), Decimal("99.00")) == 5
    assert whole_share_quantity(Decimal("50"), Decimal("99.00")) == 0
    # Degenerate price → 0 (no raise), so the caller refuses calmly.
    assert whole_share_quantity(Decimal("500"), Decimal("0")) == 0


# =============================================================================
# ENGINE — suggest_resting_order (direct, offline)
# =============================================================================


async def _run_suggest(owner: uuid.UUID, **kwargs):
    scope = Scope.for_user(owner)
    # Story 8.6: `suggest_resting_order` requires an injected reference date. The
    # engine fixtures seed a 20-bar series starting at BASE_DAY (newest bar
    # ~BASE_DAY + 19d); default `as_of` to that newest day so freshness is a no-op
    # unless a test overrides it to exercise the stale path.
    kwargs.setdefault("as_of", BASE_DAY + timedelta(days=SUGGEST_LOOKBACK_DAYS - 1))
    async with async_session_maker() as session:
        return await suggest_resting_order(scope, session, **kwargs)


@pytest.mark.asyncio
async def test_engine_happy_pins_price_amount_shares(client, monkeypatch):
    import coach.suggest as suggest_mod

    monkeypatch.setattr(suggest_mod, "is_index_core", lambda s: True)
    email = _unique_email()
    symbol = _unique_symbol()
    _register(client, email)
    owner = _user_id_for(email)
    try:
        # 20 bars, recent low 90; ample cash.
        _insert_series(symbol, [Decimal("90")] * SUGGEST_LOOKBACK_DAYS)
        _insert_balance(owner, "1000.00")
        adapter = _QuoteAdapter(ask=Decimal("100"))

        suggestion = await _run_suggest(
            owner,
            broker=adapter,
            broker_session=_live_session(),
            gateway=FakeLLMGateway(),
            symbol=symbol,
            target_amount=None,
        )
        # limit = min(90,100)*0.99 = 89.10; shares = floor(1000/89.10) = 11;
        # amount = 11 * 89.10 = 980.10.
        assert suggestion.limit_price == Decimal("89.10")
        assert suggestion.shares == 11
        assert suggestion.amount == Decimal("980.10")
        assert suggestion.side is OrderSide.BUY
        assert suggestion.order_type is OrderType.LIMIT
        assert suggestion.duration is Duration.GTC
        assert suggestion.limit_price < Decimal("100")  # strictly below ask
        assert isinstance(suggestion.reasoning, str) and suggestion.reasoning
    finally:
        _clean_market([symbol])
        _delete_user(email)


@pytest.mark.asyncio
async def test_engine_real_vs_fake_llm_price_identity(client, monkeypatch):
    import coach.suggest as suggest_mod

    monkeypatch.setattr(suggest_mod, "is_index_core", lambda s: True)
    email = _unique_email()
    symbol = _unique_symbol()
    _register(client, email)
    owner = _user_id_for(email)
    try:
        _insert_series(symbol, [Decimal("90")] * SUGGEST_LOOKBACK_DAYS)
        _insert_balance(owner, "1000.00")
        adapter = _QuoteAdapter(ask=Decimal("100"))

        fake = await _run_suggest(
            owner, broker=_QuoteAdapter(ask=Decimal("100")),
            broker_session=_live_session(), gateway=FakeLLMGateway(),
            symbol=symbol, target_amount=None,
        )
        stub = await _run_suggest(
            owner, broker=adapter, broker_session=_live_session(),
            gateway=_StubGateway(), symbol=symbol, target_amount=None,
        )
        # The NUMBERS are byte-identical regardless of which LLM ran; only the
        # reasoning differs (the model never touches the number).
        assert fake.limit_price == stub.limit_price
        assert fake.amount == stub.amount
        assert fake.shares == stub.shares
        assert fake.reasoning != stub.reasoning
        assert "STUB-REASONING" in stub.reasoning
    finally:
        _clean_market([symbol])
        _delete_user(email)


@pytest.mark.asyncio
async def test_engine_narration_crash_falls_back(client, monkeypatch):
    import coach.suggest as suggest_mod

    monkeypatch.setattr(suggest_mod, "is_index_core", lambda s: True)
    email = _unique_email()
    symbol = _unique_symbol()
    _register(client, email)
    owner = _user_id_for(email)
    try:
        _insert_series(symbol, [Decimal("90")] * SUGGEST_LOOKBACK_DAYS)
        _insert_balance(owner, "1000.00")
        suggestion = await _run_suggest(
            owner, broker=_QuoteAdapter(ask=Decimal("100")),
            broker_session=_live_session(), gateway=_CrashingGateway(),
            symbol=symbol, target_amount=None,
        )
        # A crashing gateway never blocks the suggestion; the number is unchanged
        # and a deterministic templated reasoning stands in.
        assert suggestion.limit_price == Decimal("89.10")
        assert suggestion.shares == 11
        assert "$89.10" in suggestion.reasoning
    finally:
        _clean_market([symbol])
        _delete_user(email)


def test_narrate_suggestion_fallback_is_deterministic():
    facts = {
        "symbol": "VTI",
        "limit_price": Decimal("89.10"),
        "recent_low": Decimal("90"),
        "ask": Decimal("100"),
        "shares": 11,
        "amount": Decimal("980.10"),
    }
    out = narrate_suggestion(_CrashingGateway(), facts)
    assert "$89.10" in out and "$100.00" in out and "VTI" in out


@pytest.mark.asyncio
async def test_engine_target_amount_caps_at_cash(client, monkeypatch):
    import coach.suggest as suggest_mod

    monkeypatch.setattr(suggest_mod, "is_index_core", lambda s: True)
    email = _unique_email()
    symbol = _unique_symbol()
    _register(client, email)
    owner = _user_id_for(email)
    try:
        _insert_series(symbol, [Decimal("90")] * SUGGEST_LOOKBACK_DAYS)
        _insert_balance(owner, "300.00")  # only $300 idle
        # A generous target ($5000) is CAPPED at the $300 idle cash.
        suggestion = await _run_suggest(
            owner, broker=_QuoteAdapter(ask=Decimal("100")),
            broker_session=_live_session(), gateway=FakeLLMGateway(),
            symbol=symbol, target_amount=Decimal("5000"),
        )
        # budget = min(5000, 300) = 300; shares = floor(300/89.10) = 3.
        assert suggestion.shares == 3
        assert suggestion.amount == Decimal("267.30")

        # A SMALL target ($100) sizes off the target, not the $300 cash.
        small = await _run_suggest(
            owner, broker=_QuoteAdapter(ask=Decimal("100")),
            broker_session=_live_session(), gateway=FakeLLMGateway(),
            symbol=symbol, target_amount=Decimal("100"),
        )
        assert small.shares == 1  # floor(100/89.10) = 1
    finally:
        _clean_market([symbol])
        _delete_user(email)


@pytest.mark.asyncio
async def test_engine_declines_non_core_symbol(client, monkeypatch):
    import coach.suggest as suggest_mod

    monkeypatch.setattr(suggest_mod, "is_index_core", lambda s: False)
    email = _unique_email()
    _register(client, email)
    owner = _user_id_for(email)
    try:
        with pytest.raises(OrderScopeError) as exc:
            await _run_suggest(
                owner, broker=_QuoteAdapter(ask=Decimal("100")),
                broker_session=_live_session(), gateway=FakeLLMGateway(),
                symbol="TSLA", target_amount=None,
            )
        assert "outside the v1 scope" in str(exc.value)
    finally:
        _delete_user(email)


@pytest.mark.asyncio
async def test_engine_declines_no_history(client, monkeypatch):
    import coach.suggest as suggest_mod

    monkeypatch.setattr(suggest_mod, "is_index_core", lambda s: True)
    email = _unique_email()
    symbol = _unique_symbol()
    _register(client, email)
    owner = _user_id_for(email)
    try:
        _insert_balance(owner, "1000.00")  # cash, but NO market bars
        with pytest.raises(OrderScopeError) as exc:
            await _run_suggest(
                owner, broker=_QuoteAdapter(ask=Decimal("100")),
                broker_session=_live_session(), gateway=FakeLLMGateway(),
                symbol=symbol, target_amount=None,
            )
        assert "recent price history" in str(exc.value)
    finally:
        _delete_user(email)


@pytest.mark.asyncio
async def test_engine_declines_insufficient_cash(client, monkeypatch):
    import coach.suggest as suggest_mod

    monkeypatch.setattr(suggest_mod, "is_index_core", lambda s: True)
    email = _unique_email()
    symbol = _unique_symbol()
    _register(client, email)
    owner = _user_id_for(email)
    try:
        _insert_series(symbol, [Decimal("90")] * SUGGEST_LOOKBACK_DAYS)
        _insert_balance(owner, "10.00")  # < one 89.10 share
        with pytest.raises(OrderScopeError) as exc:
            await _run_suggest(
                owner, broker=_QuoteAdapter(ask=Decimal("100")),
                broker_session=_live_session(), gateway=FakeLLMGateway(),
                symbol=symbol, target_amount=None,
            )
        assert "idle cash" in str(exc.value)
    finally:
        _clean_market([symbol])
        _delete_user(email)


@pytest.mark.asyncio
async def test_engine_declines_unreadable_quote(client, monkeypatch):
    import coach.suggest as suggest_mod

    monkeypatch.setattr(suggest_mod, "is_index_core", lambda s: True)
    email = _unique_email()
    symbol = _unique_symbol()
    _register(client, email)
    owner = _user_id_for(email)
    try:
        _insert_series(symbol, [Decimal("90")] * SUGGEST_LOOKBACK_DAYS)
        _insert_balance(owner, "1000.00")
        with pytest.raises(OrderNotPlaceableError):
            await _run_suggest(
                owner, broker=_QuoteAdapter(ask=Decimal("100"), unreadable=True),
                broker_session=_live_session(), gateway=FakeLLMGateway(),
                symbol=symbol, target_amount=None,
            )
    finally:
        _clean_market([symbol])
        _delete_user(email)


@pytest.mark.asyncio
async def test_engine_uses_only_most_recent_lookback_bars(client, monkeypatch):
    import coach.suggest as suggest_mod

    monkeypatch.setattr(suggest_mod, "is_index_core", lambda s: True)
    email = _unique_email()
    symbol = _unique_symbol()
    _register(client, email)
    owner = _user_id_for(email)
    try:
        # An ANCIENT very-low bar (5) followed by 20 recent bars at low 90. Only
        # the most recent 20 count, so recent_low is 90, NOT 5.
        old = [Decimal("5")]
        recent = [Decimal("90")] * SUGGEST_LOOKBACK_DAYS
        _insert_series(symbol, old + recent)
        _insert_balance(owner, "1000.00")
        suggestion = await _run_suggest(
            owner, broker=_QuoteAdapter(ask=Decimal("100")),
            broker_session=_live_session(), gateway=FakeLLMGateway(),
            symbol=symbol, target_amount=None,
        )
        assert suggestion.limit_price == Decimal("89.10")  # from low 90, not 5
    finally:
        _clean_market([symbol])
        _delete_user(email)


@pytest.mark.asyncio
async def test_engine_skips_non_positive_low_bars(client, monkeypatch):
    import coach.suggest as suggest_mod

    monkeypatch.setattr(suggest_mod, "is_index_core", lambda s: True)
    email = _unique_email()
    symbol = _unique_symbol()
    _register(client, email)
    owner = _user_id_for(email)
    try:
        # A bad ingestion row (low 0) among good bars must NOT drag the min to a
        # degenerate price and nuke a valid symbol — it's skipped; recent_low = 90.
        _insert_series(symbol, [Decimal("0")] + [Decimal("90")] * 5)
        _insert_balance(owner, "1000.00")
        suggestion = await _run_suggest(
            owner, broker=_QuoteAdapter(ask=Decimal("100")),
            broker_session=_live_session(), gateway=FakeLLMGateway(),
            symbol=symbol, target_amount=None,
        )
        assert suggestion.limit_price == Decimal("89.10")  # from low 90, not 0
    finally:
        _clean_market([symbol])
        _delete_user(email)


# =============================================================================
# Story 8.6 — fill-likelihood + freshness on the engine (offline)
# =============================================================================


@pytest.mark.asyncio
async def test_engine_computes_pct_below_ask_and_fill_note(client, monkeypatch):
    import coach.suggest as suggest_mod

    monkeypatch.setattr(suggest_mod, "is_index_core", lambda s: True)
    email = _unique_email()
    symbol = _unique_symbol()
    _register(client, email)
    owner = _user_id_for(email)
    try:
        # recent_low 90, ask 100 → limit 89.10 → pct_below = (100-89.10)/100 =
        # 0.1090 → far-below band.
        _insert_series(symbol, [Decimal("90")] * SUGGEST_LOOKBACK_DAYS)
        _insert_balance(owner, "1000.00")
        suggestion = await _run_suggest(
            owner, broker=_QuoteAdapter(ask=Decimal("100")),
            broker_session=_live_session(), gateway=FakeLLMGateway(),
            symbol=symbol, target_amount=None,
        )
        assert suggestion.pct_below_ask == Decimal("0.1090")
        assert "never fill" in suggestion.fill_note  # far-below copy
        assert suggestion.stale_note is None
    finally:
        _clean_market([symbol])
        _delete_user(email)


@pytest.mark.asyncio
async def test_engine_fill_note_near_market_on_floored_new_lows(client, monkeypatch):
    import coach.suggest as suggest_mod

    monkeypatch.setattr(suggest_mod, "is_index_core", lambda s: True)
    email = _unique_email()
    symbol = _unique_symbol()
    _register(client, email)
    owner = _user_id_for(email)
    try:
        # NEW-LOWS: recent_low == ask (100). Floor pulls limit to 98.00 → exactly
        # 2% below → the boundary lands in the meaningfully-below band (inclusive
        # low), proving the floor guarantees at least a 2% cushion.
        _insert_series(symbol, [Decimal("100")] * SUGGEST_LOOKBACK_DAYS)
        _insert_balance(owner, "10000.00")
        suggestion = await _run_suggest(
            owner, broker=_QuoteAdapter(ask=Decimal("100")),
            broker_session=_live_session(), gateway=FakeLLMGateway(),
            symbol=symbol, target_amount=None,
        )
        assert suggestion.limit_price == Decimal("98.00")
        assert suggestion.pct_below_ask == Decimal("0.0200")
        assert "may take a while" in suggestion.fill_note  # meaningfully-below
    finally:
        _clean_market([symbol])
        _delete_user(email)


@pytest.mark.asyncio
async def test_engine_fresh_data_has_no_stale_note(client, monkeypatch):
    import coach.suggest as suggest_mod

    monkeypatch.setattr(suggest_mod, "is_index_core", lambda s: True)
    email = _unique_email()
    symbol = _unique_symbol()
    _register(client, email)
    owner = _user_id_for(email)
    try:
        _insert_series(symbol, [Decimal("90")] * SUGGEST_LOOKBACK_DAYS)
        _insert_balance(owner, "1000.00")
        newest_day = BASE_DAY + timedelta(days=SUGGEST_LOOKBACK_DAYS - 1)
        # as_of exactly at the stale threshold (not beyond) → still fresh.
        suggestion = await _run_suggest(
            owner, broker=_QuoteAdapter(ask=Decimal("100")),
            broker_session=_live_session(), gateway=FakeLLMGateway(),
            symbol=symbol, target_amount=None,
            as_of=newest_day + timedelta(days=SUGGEST_STALE_AFTER_DAYS),
        )
        assert suggestion.stale_note is None
    finally:
        _clean_market([symbol])
        _delete_user(email)


@pytest.mark.asyncio
async def test_engine_stale_data_attaches_note_keeps_suggestion(client, monkeypatch):
    import coach.suggest as suggest_mod

    monkeypatch.setattr(suggest_mod, "is_index_core", lambda s: True)
    email = _unique_email()
    symbol = _unique_symbol()
    _register(client, email)
    owner = _user_id_for(email)
    try:
        _insert_series(symbol, [Decimal("90")] * SUGGEST_LOOKBACK_DAYS)
        _insert_balance(owner, "1000.00")
        newest_day = BASE_DAY + timedelta(days=SUGGEST_LOOKBACK_DAYS - 1)
        # as_of one day BEYOND the stale threshold → note attached, suggestion kept.
        suggestion = await _run_suggest(
            owner, broker=_QuoteAdapter(ask=Decimal("100")),
            broker_session=_live_session(), gateway=FakeLLMGateway(),
            symbol=symbol, target_amount=None,
            as_of=newest_day + timedelta(days=SUGGEST_STALE_AFTER_DAYS + 1),
        )
        assert suggestion.stale_note is not None
        assert "days old" in suggestion.stale_note
        # The suggestion is STILL computed (kept, not refused).
        assert suggestion.limit_price == Decimal("89.10")
        assert suggestion.shares == 11
    finally:
        _clean_market([symbol])
        _delete_user(email)


@pytest.mark.asyncio
async def test_engine_extremely_stale_data_refuses_calmly(client, monkeypatch):
    import coach.suggest as suggest_mod

    monkeypatch.setattr(suggest_mod, "is_index_core", lambda s: True)
    email = _unique_email()
    symbol = _unique_symbol()
    _register(client, email)
    owner = _user_id_for(email)
    try:
        _insert_series(symbol, [Decimal("90")] * SUGGEST_LOOKBACK_DAYS)
        _insert_balance(owner, "1000.00")
        newest_day = BASE_DAY + timedelta(days=SUGGEST_LOOKBACK_DAYS - 1)
        with pytest.raises(OrderScopeError) as exc:
            await _run_suggest(
                owner, broker=_QuoteAdapter(ask=Decimal("100")),
                broker_session=_live_session(), gateway=FakeLLMGateway(),
                symbol=symbol, target_amount=None,
                as_of=newest_day + timedelta(days=SUGGEST_STALE_REFUSE_AFTER_DAYS + 1),
            )
        assert "too stale" in str(exc.value)
    finally:
        _clean_market([symbol])
        _delete_user(email)


@pytest.mark.asyncio
async def test_engine_real_vs_fake_llm_note_identity(client, monkeypatch):
    import coach.suggest as suggest_mod

    monkeypatch.setattr(suggest_mod, "is_index_core", lambda s: True)
    email = _unique_email()
    symbol = _unique_symbol()
    _register(client, email)
    owner = _user_id_for(email)
    try:
        _insert_series(symbol, [Decimal("90")] * SUGGEST_LOOKBACK_DAYS)
        _insert_balance(owner, "1000.00")
        fake = await _run_suggest(
            owner, broker=_QuoteAdapter(ask=Decimal("100")),
            broker_session=_live_session(), gateway=FakeLLMGateway(),
            symbol=symbol, target_amount=None,
        )
        stub = await _run_suggest(
            owner, broker=_QuoteAdapter(ask=Decimal("100")),
            broker_session=_live_session(), gateway=_StubGateway(),
            symbol=symbol, target_amount=None,
        )
        # The honesty FACTS (pct_below_ask + fill_note + stale_note) are identical
        # regardless of which LLM ran — the model never computes them.
        assert fake.pct_below_ask == stub.pct_below_ask
        assert fake.fill_note == stub.fill_note
        assert fake.stale_note == stub.stale_note
        # Only the narrated prose differs.
        assert fake.reasoning != stub.reasoning
    finally:
        _clean_market([symbol])
        _delete_user(email)


def test_narrate_fallback_includes_fill_note():
    facts = {
        "symbol": "VTI",
        "limit_price": Decimal("89.10"),
        "recent_low": Decimal("90"),
        "ask": Decimal("100"),
        "shares": 11,
        "amount": Decimal("980.10"),
        "pct_below_ask": Decimal("0.1090"),
        "fill_note": "This is well below today's price, so it may never fill.",
        "stale_note": None,
    }
    out = narrate_suggestion(_CrashingGateway(), facts)
    # The deterministic templated fallback carries the fill-likelihood sentence.
    assert "may never fill" in out
    assert "$89.10" in out


# =============================================================================
# API — POST /api/coach/suggest-order
# =============================================================================


def _override_live(client: TestClient) -> None:
    client.app.dependency_overrides[require_live_broker_session] = (
        lambda: _live_session()
    )


def test_api_happy_returns_fixed_point_strings_and_places_nothing(client):
    email = _unique_email()
    symbol = "VTI"  # real index-core symbol
    # Story 8.6: anchor the newest bar within the freshness window of today so
    # the endpoint (as_of=date.today()) does not flag it stale; a tiny per-test
    # day jitter keeps the (VTI, day) unique constraint from colliding.
    day0 = _fresh_day0()
    adapter = _QuoteAdapter(ask=Decimal("100"))
    client.app.dependency_overrides[get_broker] = lambda: adapter
    _override_live(client)
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        owner = _user_id_for(email)
        _insert_token_sync(owner, _live())
        _insert_balance(owner, "1000.00")
        _clean_market([symbol])
        _insert_series(symbol, [Decimal("90")] * SUGGEST_LOOKBACK_DAYS, day0=day0)

        resp = client.post(
            "/api/coach/suggest-order",
            json={"symbol": symbol, "amount": None},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["symbol"] == "VTI"
        assert body["side"] == "buy"
        assert body["order_type"] == "limit"
        assert body["duration"] == "gtc"
        # Fixed-point STRINGS (not Number).
        assert body["limit_price"] == "89.10"
        assert isinstance(body["limit_price"], str)
        assert body["amount"] == "980.10"
        assert isinstance(body["amount"], str)
        assert body["shares"] == 11
        assert isinstance(body["reasoning"], str) and body["reasoning"]
        # strictly below the live ask
        assert Decimal(body["limit_price"]) < Decimal("100")
        # Story 8.6 honesty facts: fixed-point pct string + banded fill note; a
        # fresh fixture carries no stale note (null on the wire).
        assert body["pct_below_ask"] == "0.1090"
        assert isinstance(body["pct_below_ask"], str)
        assert isinstance(body["fill_note"], str) and body["fill_note"]
        assert "never fill" in body["fill_note"]  # far-below band
        assert body["stale_note"] is None
    finally:
        _clean_market([symbol])
        client.app.dependency_overrides.pop(get_broker, None)
        client.app.dependency_overrides.pop(require_live_broker_session, None)
        _delete_user(email)


def test_api_no_amount_sizes_off_cash(client):
    email = _unique_email()
    symbol = "VTI"
    # Story 8.6: anchor the newest bar within the freshness window of today so
    # the endpoint (as_of=date.today()) does not flag it stale; a tiny per-test
    # day jitter keeps the (VTI, day) unique constraint from colliding.
    day0 = _fresh_day0()
    adapter = _QuoteAdapter(ask=Decimal("100"))
    client.app.dependency_overrides[get_broker] = lambda: adapter
    _override_live(client)
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        owner = _user_id_for(email)
        _insert_token_sync(owner, _live())
        _insert_balance(owner, "300.00")
        _clean_market([symbol])
        _insert_series(symbol, [Decimal("90")] * SUGGEST_LOOKBACK_DAYS, day0=day0)

        # No amount key at all → sizes off the $300 idle cash: floor(300/89.10)=3.
        resp = client.post(
            "/api/coach/suggest-order",
            json={"symbol": symbol},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["shares"] == 3
        assert resp.json()["amount"] == "267.30"
    finally:
        _clean_market([symbol])
        client.app.dependency_overrides.pop(get_broker, None)
        client.app.dependency_overrides.pop(require_live_broker_session, None)
        _delete_user(email)


def test_api_non_core_symbol_is_calm_422(client):
    email = _unique_email()
    adapter = _QuoteAdapter(ask=Decimal("100"))
    client.app.dependency_overrides[get_broker] = lambda: adapter
    _override_live(client)
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        _insert_token_sync(_user_id_for(email), _live())
        _insert_balance(_user_id_for(email), "1000.00")

        resp = client.post(
            "/api/coach/suggest-order",
            json={"symbol": "TSLA"},
            headers=headers,
        )
        assert resp.status_code == 422, resp.text
        assert "outside the v1 scope" in resp.json()["error"]["message"]
    finally:
        client.app.dependency_overrides.pop(get_broker, None)
        client.app.dependency_overrides.pop(require_live_broker_session, None)
        _delete_user(email)


def test_api_no_history_is_calm_422(client):
    email = _unique_email()
    symbol = "BND"
    adapter = _QuoteAdapter(ask=Decimal("100"))
    client.app.dependency_overrides[get_broker] = lambda: adapter
    _override_live(client)
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        _insert_token_sync(_user_id_for(email), _live())
        _insert_balance(_user_id_for(email), "1000.00")
        _clean_market([symbol])  # no bars for BND

        resp = client.post(
            "/api/coach/suggest-order",
            json={"symbol": symbol},
            headers=headers,
        )
        assert resp.status_code == 422, resp.text
        assert "recent price history" in resp.json()["error"]["message"]
    finally:
        client.app.dependency_overrides.pop(get_broker, None)
        client.app.dependency_overrides.pop(require_live_broker_session, None)
        _delete_user(email)


def test_api_insufficient_cash_is_calm_422(client):
    email = _unique_email()
    symbol = "VTI"
    # Story 8.6: anchor the newest bar within the freshness window of today so
    # the endpoint (as_of=date.today()) does not flag it stale; a tiny per-test
    # day jitter keeps the (VTI, day) unique constraint from colliding.
    day0 = _fresh_day0()
    adapter = _QuoteAdapter(ask=Decimal("100"))
    client.app.dependency_overrides[get_broker] = lambda: adapter
    _override_live(client)
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        owner = _user_id_for(email)
        _insert_token_sync(owner, _live())
        _insert_balance(owner, "10.00")  # < one share
        _clean_market([symbol])
        _insert_series(symbol, [Decimal("90")] * SUGGEST_LOOKBACK_DAYS, day0=day0)

        resp = client.post(
            "/api/coach/suggest-order",
            json={"symbol": symbol},
            headers=headers,
        )
        assert resp.status_code == 422, resp.text
        assert "idle cash" in resp.json()["error"]["message"]
    finally:
        _clean_market([symbol])
        client.app.dependency_overrides.pop(get_broker, None)
        client.app.dependency_overrides.pop(require_live_broker_session, None)
        _delete_user(email)


def test_api_unreadable_quote_is_calm_422(client):
    email = _unique_email()
    symbol = "VTI"
    # Story 8.6: anchor the newest bar within the freshness window of today so
    # the endpoint (as_of=date.today()) does not flag it stale; a tiny per-test
    # day jitter keeps the (VTI, day) unique constraint from colliding.
    day0 = _fresh_day0()
    adapter = _QuoteAdapter(ask=Decimal("100"), unreadable=True)
    client.app.dependency_overrides[get_broker] = lambda: adapter
    _override_live(client)
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        owner = _user_id_for(email)
        _insert_token_sync(owner, _live())
        _insert_balance(owner, "1000.00")
        _clean_market([symbol])
        _insert_series(symbol, [Decimal("90")] * SUGGEST_LOOKBACK_DAYS, day0=day0)

        resp = client.post(
            "/api/coach/suggest-order",
            json={"symbol": symbol},
            headers=headers,
        )
        assert resp.status_code == 422, resp.text
        assert "No usable quote" in resp.json()["error"]["message"]
    finally:
        _clean_market([symbol])
        client.app.dependency_overrides.pop(get_broker, None)
        client.app.dependency_overrides.pop(require_live_broker_session, None)
        _delete_user(email)


def test_api_lapsed_session_is_409(client):
    # No live broker session (no token, no override) → the entry gate 409s.
    email = _unique_email()
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        # NOTE: no _insert_token_sync and no require_live_broker_session override.
        resp = client.post(
            "/api/coach/suggest-order",
            json={"symbol": "VTI"},
            headers=headers,
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["message"] == RECONNECT_MESSAGE
    finally:
        _delete_user(email)


def test_api_unauthenticated_is_401(client):
    resp = client.post("/api/coach/suggest-order", json={"symbol": "VTI"})
    assert resp.status_code == 401, resp.text
