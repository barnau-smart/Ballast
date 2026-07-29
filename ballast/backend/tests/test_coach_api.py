"""Story 4.6 tests — propose-and-approve execution (FR8/FR9/FR10, AD-7/AD-11).

Locks the full I/O & Edge-Case Matrix structurally and OFFLINE: zero network,
zero credentials, fake adapters (``BROKER_ADAPTER=fake``, ``LLM_ADAPTER=fake``).

Covers:
  (a) approve in-scope, live session → Coach Engine calls place_order exactly
      once and the OrderOutcome is returned
  (b) approve out-of-v1-scope symbol → rejected, broker NEVER called
  (c) approve non-positive amount → rejected, broker NEVER called
  (d) approve on expired/unlinked session → calm 409, no order attempted
  (e) approve unauthenticated → 401, no scope, no broker call
  (f) recommend returns a blessed recommendation (with any order_intent) and
      NEVER places an order (broker never called)
  (g) recommend works while the session is expired (degraded mode)
  (h) recommend surfaces an FR11 warning given a tripping portfolio
  (i) fake place_order determinism
  (j) Schwab place_order stub raises without creds
  (k) sole-execution-path canary — place_order is reachable only through the
      Coach Engine execution owner (the recommend path never calls it)

Requires the docker Postgres (`docker compose up -d db`). Each test uses unique
users and cleans up its own rows. NO real Schwab/Anthropic call is ever made.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import RECONNECT_MESSAGE
from brokers.crypto import encrypt_token
from brokers.factory import get_broker
from brokers.fake_adapter import FAKE_FILL_PRICE, FakeBrokerAdapter
from brokers.port import (
    BrokerPort,
    BrokerTokens,
    OrderOutcome,
    OrderStatus,
    PortfolioSnapshot,
)
from coach.execution import OrderScopeError, execute_approved_order
from coach.recommendation import OrderIntent, OrderSide
from db.connection import get_connection
from db.models import BrokerageToken, MarketDaily, PortfolioCache
from db.session import engine

PASSWORD = "supersecret123"

# A symbol with a seeded drawdown so find_precedent returns an EVENT_PRECEDENT
# (a "live drawdown") — needed for the FR11 over-concentration warning to fire.
SYM = "VTI"  # index-core, so an approve of it is in v1 scope
BASE_DAY = date(2015, 1, 1)


# --- table + fixture setup ---------------------------------------------------


@pytest_asyncio.fixture(autouse=True)
async def ensure_tables():
    """Ensure the owned tables exist (matches the create-all lifecycle)."""
    async with engine.begin() as conn:
        await conn.run_sync(BrokerageToken.__table__.create, checkfirst=True)
        await conn.run_sync(PortfolioCache.__table__.create, checkfirst=True)
        await conn.run_sync(MarketDaily.__table__.create, checkfirst=True)
    yield


@pytest.fixture
def client() -> TestClient:
    with TestClient(create_app()) as c:
        yield c


# --- helpers -----------------------------------------------------------------


def _unique_email() -> str:
    return f"coach-api-test-{uuid.uuid4().hex}@example.com"


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
    """Insert a brokerage token (ciphertext) so the user has a session state."""
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


def _insert_holding_sync(
    owner: uuid.UUID, symbol: str, market_value: str, cash: str = "0"
) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO portfolio_cache "
                "(id, owner_id, symbol, quantity, market_value, cost_basis, cash, as_of) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    str(uuid.uuid4()),
                    str(owner),
                    symbol,
                    "1",
                    market_value,
                    market_value,
                    cash,
                    datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc),
                ),
            )
        conn.commit()


def _clean_market(symbols: list[str]) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM market_daily WHERE symbol = ANY(%s)", (symbols,))
        conn.commit()


def _insert_series(symbol: str, closes: list[Decimal]) -> None:
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


def _live() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=3)


def _expired() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=1)


class _SpyAdapter(BrokerPort):
    """A fake broker that records how many times place_order was called.

    Wraps FakeBrokerAdapter's behavior but counts calls so tests can assert the
    single-execution-path invariant (exactly once, or never) structurally.
    """

    provider = "fake"

    def __init__(self) -> None:
        self.calls: list[tuple[OrderIntent, str]] = []
        self._delegate = FakeBrokerAdapter()

    def authorization_url(self, state: str) -> str:
        return self._delegate.authorization_url(state)

    def exchange_code(self, code: str, state: str) -> BrokerTokens:
        return self._delegate.exchange_code(code, state)

    def fetch_portfolio(self) -> PortfolioSnapshot:
        return self._delegate.fetch_portfolio()

    async def place_order(
        self, order_intent: OrderIntent, *, idempotency_key: str
    ) -> OrderOutcome:
        self.calls.append((order_intent, idempotency_key))
        return await self._delegate.place_order(
            order_intent, idempotency_key=idempotency_key
        )


# =============================================================================
# APPROVE
# =============================================================================


def test_approve_in_scope_places_order_exactly_once(client):
    # (a) authed + live session + in-scope order → Coach Engine places the order
    # exactly once and the OrderOutcome is returned.
    email = _unique_email()
    spy = _SpyAdapter()
    client.app.dependency_overrides[get_broker] = lambda: spy
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        _insert_token_sync(_user_id_for(email), _live())

        resp = client.post(
            "/api/coach/approve",
            json={"order_intent": {"symbol": "VTI", "side": "buy", "amount": "500"}},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "filled"
        # Non-negative qty/price; money as decimal strings, no float. Pin that a
        # fill actually carries a price (guard against a None → Decimal(None)
        # crash masking a real regression).
        assert Decimal(body["filled_qty"]) >= 0
        assert body["avg_price"] is not None
        assert Decimal(body["avg_price"]) >= 0
        assert body["broker_ref"]
        # The Coach Engine (sole caller) placed the order EXACTLY once.
        assert len(spy.calls) == 1
        intent, key = spy.calls[0]
        assert intent.symbol == "VTI"
        assert intent.side is OrderSide.BUY
        assert intent.amount == Decimal("500")
        assert key  # an idempotency key was minted and passed
    finally:
        client.app.dependency_overrides.pop(get_broker, None)
        _delete_user(email)


def test_approve_out_of_scope_symbol_rejected_broker_never_called(client):
    # (b) an out-of-v1-scope symbol → rejected via the envelope; broker untouched.
    email = _unique_email()
    spy = _SpyAdapter()
    client.app.dependency_overrides[get_broker] = lambda: spy
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        _insert_token_sync(_user_id_for(email), _live())

        resp = client.post(
            "/api/coach/approve",
            json={"order_intent": {"symbol": "AAPL", "side": "buy", "amount": "500"}},
            headers=headers,
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["type"]  # error envelope shape
        assert spy.calls == []  # broker NEVER called
    finally:
        client.app.dependency_overrides.pop(get_broker, None)
        _delete_user(email)


def test_approve_non_positive_amount_rejected_broker_never_called(client):
    # (c) a non-positive amount → rejected before any broker call.
    email = _unique_email()
    spy = _SpyAdapter()
    client.app.dependency_overrides[get_broker] = lambda: spy
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        _insert_token_sync(_user_id_for(email), _live())

        resp = client.post(
            "/api/coach/approve",
            json={"order_intent": {"symbol": "VTI", "side": "buy", "amount": "0"}},
            headers=headers,
        )
        assert resp.status_code == 422, resp.text
        assert spy.calls == []
    finally:
        client.app.dependency_overrides.pop(get_broker, None)
        _delete_user(email)


def test_approve_expired_session_returns_409_no_order(client):
    # (d) a non-live (expired) session → calm 409 reconnect; no order attempted.
    email = _unique_email()
    spy = _SpyAdapter()
    client.app.dependency_overrides[get_broker] = lambda: spy
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        _insert_token_sync(_user_id_for(email), _expired())

        resp = client.post(
            "/api/coach/approve",
            json={"order_intent": {"symbol": "VTI", "side": "buy", "amount": "500"}},
            headers=headers,
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["message"] == RECONNECT_MESSAGE
        assert spy.calls == []  # broker NEVER called
    finally:
        client.app.dependency_overrides.pop(get_broker, None)
        _delete_user(email)


def test_approve_unlinked_session_returns_409_no_order(client):
    # (d, unlinked variant) no token row at all → 409; no order attempted.
    email = _unique_email()
    spy = _SpyAdapter()
    client.app.dependency_overrides[get_broker] = lambda: spy
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        # No token inserted → session "unlinked" → not live.

        resp = client.post(
            "/api/coach/approve",
            json={"order_intent": {"symbol": "VTI", "side": "buy", "amount": "500"}},
            headers=headers,
        )
        assert resp.status_code == 409, resp.text
        assert spy.calls == []
    finally:
        client.app.dependency_overrides.pop(get_broker, None)
        _delete_user(email)


def test_approve_unauthenticated_returns_401(client):
    # (e) no token → 401 before the handler; no scope built, no broker call.
    spy = _SpyAdapter()
    client.app.dependency_overrides[get_broker] = lambda: spy
    try:
        resp = client.post(
            "/api/coach/approve",
            json={"order_intent": {"symbol": "VTI", "side": "buy", "amount": "500"}},
        )
        assert resp.status_code == 401, resp.text
        assert spy.calls == []
    finally:
        client.app.dependency_overrides.pop(get_broker, None)


# =============================================================================
# RECOMMEND
# =============================================================================


def test_recommend_returns_blessed_and_never_places(client):
    # (f) recommend returns a blessed recommendation and NEVER places an order.
    email = _unique_email()
    spy = _SpyAdapter()
    # Even if the broker were somehow reachable, the spy proves it is not.
    client.app.dependency_overrides[get_broker] = lambda: spy
    _clean_market([SYM])
    _insert_series(SYM, [Decimal("100"), Decimal("92")])
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        _insert_token_sync(_user_id_for(email), _live())

        resp = client.post(
            "/api/coach/recommend",
            json={"symbol": SYM, "question": "Should I invest now?"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Blessed shape: reasoning + >=1 evidence + >=1 uncertainty.
        assert body["action_label"]
        assert body["reasoning"].strip()
        assert len(body["evidence"]) >= 1
        assert any(u.strip() for u in body["uncertainties"])
        # order_intent key is present (optional; the fake default plan → None).
        assert "order_intent" in body
        # Never placed an order.
        assert spy.calls == []
    finally:
        client.app.dependency_overrides.pop(get_broker, None)
        _clean_market([SYM])
        _delete_user(email)


def test_recommend_works_while_session_expired_degraded(client):
    # (g) recommend works in degraded mode: an EXPIRED session still gets advice.
    email = _unique_email()
    spy = _SpyAdapter()
    client.app.dependency_overrides[get_broker] = lambda: spy
    _clean_market([SYM])
    _insert_series(SYM, [Decimal("100"), Decimal("92")])
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        _insert_token_sync(_user_id_for(email), _expired())

        resp = client.post(
            "/api/coach/recommend",
            json={"symbol": SYM, "question": "Should I invest now?"},
            headers=headers,
        )
        # Advice is NOT gated on a live session (AD-11).
        assert resp.status_code == 200, resp.text
        assert resp.json()["reasoning"].strip()
        assert spy.calls == []
    finally:
        client.app.dependency_overrides.pop(get_broker, None)
        _clean_market([SYM])
        _delete_user(email)


def test_recommend_surfaces_fr11_warning(client):
    # (h) a tripping portfolio (concentrated + a live drawdown) → the FR11
    # over-concentration warning content appears in the returned reasoning.
    email = _unique_email()
    _clean_market([SYM])
    # A drawdown so find_precedent returns an EVENT_PRECEDENT (live drawdown).
    _insert_series(SYM, [Decimal("100"), Decimal("70")])
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        uid = _user_id_for(email)
        # Portfolio: 5000 already in SYM + 1000 cash → total 6000. Buying 5000
        # more of SYM → post 10000/11000 ≈ 0.91 > 0.40 ceiling.
        _insert_holding_sync(uid, SYM, market_value="5000", cash="1000")

        resp = client.post(
            "/api/coach/recommend",
            json={"symbol": SYM, "side": "buy", "amount": "5000"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        text = resp.json()["reasoning"].lower()
        # The FR11 over-concentration warning is surfaced (4.5 behavior), still
        # blessed and not blocked.
        assert "outsized share" in text
    finally:
        _clean_market([SYM])
        _delete_user(email)


# =============================================================================
# ADAPTERS / EXECUTION OWNER (pure, no DB)
# =============================================================================


@pytest.mark.asyncio
async def test_fake_place_order_is_deterministic():
    # (i) same (order_intent, idempotency_key) → equal OrderOutcomes.
    intent = OrderIntent(symbol="VTI", side=OrderSide.BUY, amount=Decimal("500"))
    a = await FakeBrokerAdapter().place_order(intent, idempotency_key="k-123")
    b = await FakeBrokerAdapter().place_order(intent, idempotency_key="k-123")
    assert a == b
    assert a.status is OrderStatus.FILLED
    assert a.filled_qty == Decimal("500") / FAKE_FILL_PRICE
    assert a.avg_price == FAKE_FILL_PRICE
    assert a.filled_qty >= 0 and a.avg_price >= 0
    assert a.broker_ref == "fake-order-k-123"
    # Money is Decimal, never float.
    assert isinstance(a.filled_qty, Decimal)
    assert isinstance(a.avg_price, Decimal)


@pytest.mark.asyncio
async def test_schwab_place_order_stub_raises_without_creds(monkeypatch):
    # (j) The Schwab place_order stub NEVER returns a phantom fill — it always
    # raises SchwabNotConfiguredError. Pinned two ways so a silent-fill
    # regression fails the test:
    #   - creds present at construction → the explicit "not wired yet" raise
    #   - captured cred stripped → _require_configured() raises
    from brokers.schwab_adapter import SchwabAdapter, SchwabNotConfiguredError

    monkeypatch.setenv("SCHWAB_CLIENT_ID", "id")
    monkeypatch.setenv("SCHWAB_CLIENT_SECRET", "secret")
    monkeypatch.setenv("SCHWAB_CALLBACK_URL", "https://example.com/cb")
    adapter = SchwabAdapter()  # constructs (creds present) but network-gated
    intent = OrderIntent(symbol="VTI", side=OrderSide.BUY, amount=Decimal("500"))

    # Creds-present path: the explicit "not wired" stub raise. A silent phantom
    # fill (returning an OrderOutcome) would make the `match` assertion fail.
    with pytest.raises(SchwabNotConfiguredError, match="not wired"):
        await adapter.place_order(intent, idempotency_key="k")

    # Creds-missing path: setenv after construction is a no-op (the adapter read
    # creds at __init__), so strip the CAPTURED attribute to exercise the
    # _require_configured() guard directly.
    adapter._client_id = ""
    with pytest.raises(SchwabNotConfiguredError):
        await adapter.place_order(intent, idempotency_key="k")


@pytest.mark.asyncio
async def test_execution_owner_out_of_scope_never_calls_broker():
    # The execution owner rejects out-of-scope BEFORE the broker call.
    spy = _SpyAdapter()
    intent = OrderIntent(symbol="AAPL", side=OrderSide.BUY, amount=Decimal("500"))
    with pytest.raises(OrderScopeError):
        await execute_approved_order(intent, broker=spy)
    assert spy.calls == []


@pytest.mark.asyncio
async def test_execution_owner_places_exactly_once_and_mints_key():
    # (a, unit level) the sole caller places exactly once and mints a key.
    spy = _SpyAdapter()
    intent = OrderIntent(symbol="VTI", side=OrderSide.BUY, amount=Decimal("500"))
    outcome = await execute_approved_order(intent, broker=spy)
    assert outcome.status is OrderStatus.FILLED
    assert len(spy.calls) == 1
    _, key = spy.calls[0]
    assert key  # minted


def test_sole_execution_path_canary():
    # (k) AD-7: the ONLY code that calls BrokerPort.place_order is the Coach
    # Engine execution owner. Grep the source tree — no other module (API
    # handlers, pipeline, portfolio) calls .place_order(.
    import pathlib

    backend = pathlib.Path(__file__).resolve().parent.parent
    offenders: list[str] = []
    for path in backend.rglob("*.py"):
        # Skip tests, the port ABC definition, and the execution owner itself.
        rel = path.relative_to(backend)
        parts = rel.parts
        if parts and parts[0] in {"tests", ".venv"}:
            continue
        if rel.as_posix() in {
            "brokers/port.py",  # the abstract declaration
            "brokers/fake_adapter.py",  # the implementation
            "brokers/schwab_adapter/adapter.py",  # the implementation
            "coach/execution.py",  # the sole caller
        }:
            continue
        text = path.read_text(encoding="utf-8")
        if ".place_order(" in text:
            offenders.append(rel.as_posix())
    assert offenders == [], (
        "place_order must be called ONLY by coach.execution (AD-7). "
        f"Unexpected callers: {offenders}"
    )


def test_approve_malformed_body_rejected_broker_never_called(client):
    # Review hardening: the handler trusts a client-supplied order_intent, so the
    # request-shape boundary must reject a missing order_intent AND an invalid
    # side WITHOUT ever touching the broker (422 via Pydantic, before execution).
    email = _unique_email()
    spy = _SpyAdapter()
    client.app.dependency_overrides[get_broker] = lambda: spy
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        _insert_token_sync(_user_id_for(email), _live())

        # Missing order_intent entirely.
        r1 = client.post("/api/coach/approve", json={}, headers=headers)
        assert r1.status_code == 422, r1.text

        # An order side outside the closed buy/sell enum.
        r2 = client.post(
            "/api/coach/approve",
            json={"order_intent": {"symbol": "VTI", "side": "hold", "amount": "500"}},
            headers=headers,
        )
        assert r2.status_code == 422, r2.text

        assert spy.calls == []  # broker NEVER called on a malformed body
    finally:
        client.app.dependency_overrides.pop(get_broker, None)
        _delete_user(email)


def test_approve_normalizes_symbol_before_placing(client):
    # Review hardening (symbol/scope agreement): a lowercase/whitespace symbol
    # that passes the case-insensitive scope gate must be CANONICALIZED before it
    # reaches the broker — the gate and the placed order must agree.
    email = _unique_email()
    spy = _SpyAdapter()
    client.app.dependency_overrides[get_broker] = lambda: spy
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        _insert_token_sync(_user_id_for(email), _live())

        resp = client.post(
            "/api/coach/approve",
            json={"order_intent": {"symbol": "  vti  ", "side": "buy", "amount": "500"}},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert len(spy.calls) == 1
        placed_intent, _ = spy.calls[0]
        # The broker received the canonical symbol, not the raw "  vti  ".
        assert placed_intent.symbol == "VTI"
    finally:
        client.app.dependency_overrides.pop(get_broker, None)
        _delete_user(email)
