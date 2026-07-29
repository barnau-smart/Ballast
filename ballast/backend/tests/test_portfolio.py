"""Story 2.3 tests — portfolio import, the single-writer projection, and
reconcile-wins-keyed-on-as_of (AD-14). REAL DB (no mocks) for the cache/reconcile
tests, matching the Epic 1 / 2.1 / 2.2 style.

Requires the docker Postgres (`docker compose up -d db`). Each test uses unique
users and cleans up its own rows. NO real Schwab network is ever exercised — the
FakeBrokerAdapter is the tested path.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from api.app import create_app
from brokers.factory import get_broker
from brokers.fake_adapter import (
    FAKE_AS_OF_BASE,
    FAKE_CASH,
    FAKE_HOLDINGS,
    FakeBrokerAdapter,
)
from brokers.port import BrokerPort, BrokerTokens, PortfolioSnapshot
from brokers.portfolio import get_portfolio, reconcile_portfolio
from db.connection import get_connection
from db.models import BrokerageToken, PortfolioCache
from db.scope import Scope
from db.session import async_session_maker, engine

PASSWORD = "supersecret123"


@pytest_asyncio.fixture(autouse=True)
async def ensure_tables():
    """Ensure the owned tables exist for the repo-level tests."""
    async with engine.begin() as conn:
        await conn.run_sync(BrokerageToken.__table__.create, checkfirst=True)
        await conn.run_sync(PortfolioCache.__table__.create, checkfirst=True)
    yield


# --- helpers -----------------------------------------------------------------


def _unique_email() -> str:
    return f"portfolio-test-{uuid.uuid4().hex}@example.com"


def _make_user_direct() -> uuid.UUID:
    user_id = uuid.uuid4()
    email = f"portfolio-iso-{user_id.hex}@example.com"
    hashed = "x" * 60
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                'INSERT INTO "user" '
                "(id, email, hashed_password, is_active, is_superuser, is_verified) "
                "VALUES (%s, %s, %s, TRUE, FALSE, FALSE)",
                (str(user_id), email, hashed),
            )
        conn.commit()
    return user_id


def _delete_user_by_id(user_id: uuid.UUID) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM "user" WHERE id = %s', (str(user_id),))
        conn.commit()


def _delete_user(email: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM "user" WHERE email = %s', (email,))
        conn.commit()


def _user_id_for(email: str) -> uuid.UUID:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT id FROM "user" WHERE email = %s', (email,))
            (uid_raw,) = cur.fetchone()
    return uuid.UUID(str(uid_raw))


def _insert_holding_sync(owner: uuid.UUID, symbol: str) -> None:
    """Insert one portfolio_cache row via psycopg (sync) for tests running under
    a TestClient event loop — avoids nesting an async loop inside the client's.
    """
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
                    "100.00",
                    "90.00",
                    "0",
                    FAKE_AS_OF_BASE,
                ),
            )
        conn.commit()


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


def _snapshot(*, offset: timedelta, cash: Decimal, symbols: list[str]) -> PortfolioSnapshot:
    from brokers.port import Holding

    return PortfolioSnapshot(
        as_of=FAKE_AS_OF_BASE + offset,
        cash=cash,
        holdings=[
            Holding(
                symbol=s,
                quantity=Decimal("1"),
                market_value=Decimal("100.00"),
                cost_basis=Decimal("90.00"),
            )
            for s in symbols
        ],
    )


@pytest_asyncio.fixture
async def two_owner_ids():
    a = _make_user_direct()
    b = _make_user_direct()
    try:
        yield a, b
    finally:
        _delete_user_by_id(a)
        _delete_user_by_id(b)


@pytest.fixture
def client() -> TestClient:
    with TestClient(create_app()) as c:
        yield c


# --- Fake adapter snapshot ---------------------------------------------------


def test_fake_fetch_portfolio_is_deterministic():
    snap1 = FakeBrokerAdapter().fetch_portfolio()
    snap2 = FakeBrokerAdapter().fetch_portfolio()
    assert snap1 == snap2
    assert snap1.as_of == FAKE_AS_OF_BASE
    assert snap1.cash == FAKE_CASH
    assert [h.symbol for h in snap1.holdings] == [h.symbol for h in FAKE_HOLDINGS]
    # Money is Decimal, never float.
    assert all(isinstance(h.market_value, Decimal) for h in snap1.holdings)
    assert isinstance(snap1.cash, Decimal)


def test_fake_as_of_offset_shifts_snapshot():
    later = FakeBrokerAdapter(as_of_offset=timedelta(hours=1)).fetch_portfolio()
    assert later.as_of == FAKE_AS_OF_BASE + timedelta(hours=1)


# --- Reconcile writes the cache (real DB) ------------------------------------


@pytest.mark.asyncio
async def test_reconcile_writes_cache_matching_snapshot(two_owner_ids):
    a, _ = two_owner_ids
    async with async_session_maker() as session:
        view = await reconcile_portfolio(
            Scope.for_user(a), session, FakeBrokerAdapter()
        )
    assert not view.is_empty
    assert view.cash == FAKE_CASH
    assert view.as_of == FAKE_AS_OF_BASE
    by_symbol = {h.symbol: h for h in view.holdings}
    assert set(by_symbol) == {h.symbol for h in FAKE_HOLDINGS}
    # Exact Decimal round-trip, no float drift.
    vti = by_symbol["VTI"]
    assert vti.quantity == Decimal("10")
    assert vti.market_value == Decimal("2500.00")
    assert vti.cost_basis == Decimal("2000.00")
    assert isinstance(vti.market_value, Decimal)


# --- Reconcile-wins keyed on as_of (AC3) -------------------------------------


@pytest.mark.asyncio
async def test_newer_reconcile_replaces_cache(two_owner_ids):
    a, _ = two_owner_ids
    async with async_session_maker() as session:
        await reconcile_portfolio(
            Scope.for_user(a),
            session,
            FakeBrokerAdapter(),
            snapshot=_snapshot(
                offset=timedelta(0), cash=Decimal("100.00"), symbols=["OLD"]
            ),
        )
    # A strictly-newer snapshot supersedes the cache.
    async with async_session_maker() as session:
        view = await reconcile_portfolio(
            Scope.for_user(a),
            session,
            FakeBrokerAdapter(),
            snapshot=_snapshot(
                offset=timedelta(hours=1), cash=Decimal("200.00"), symbols=["NEW"]
            ),
        )
    assert [h.symbol for h in view.holdings] == ["NEW"]
    assert view.cash == Decimal("200.00")
    assert view.as_of == FAKE_AS_OF_BASE + timedelta(hours=1)


@pytest.mark.asyncio
async def test_stale_reconcile_does_not_clobber_newer_cache(two_owner_ids):
    a, _ = two_owner_ids
    async with async_session_maker() as session:
        await reconcile_portfolio(
            Scope.for_user(a),
            session,
            FakeBrokerAdapter(),
            snapshot=_snapshot(
                offset=timedelta(hours=1), cash=Decimal("200.00"), symbols=["NEW"]
            ),
        )
    # An OLDER snapshot must be ignored — newer truth is preserved.
    async with async_session_maker() as session:
        view = await reconcile_portfolio(
            Scope.for_user(a),
            session,
            FakeBrokerAdapter(),
            snapshot=_snapshot(
                offset=timedelta(0), cash=Decimal("100.00"), symbols=["OLD"]
            ),
        )
    assert [h.symbol for h in view.holdings] == ["NEW"]
    assert view.cash == Decimal("200.00")
    assert view.as_of == FAKE_AS_OF_BASE + timedelta(hours=1)


@pytest.mark.asyncio
async def test_equal_as_of_reconcile_is_noop(two_owner_ids):
    a, _ = two_owner_ids
    snap = _snapshot(offset=timedelta(0), cash=Decimal("100.00"), symbols=["X"])
    async with async_session_maker() as session:
        await reconcile_portfolio(Scope.for_user(a), session, FakeBrokerAdapter(), snapshot=snap)
    # Same as_of → treated as a duplicate; cache untouched.
    async with async_session_maker() as session:
        view = await reconcile_portfolio(
            Scope.for_user(a),
            session,
            FakeBrokerAdapter(),
            snapshot=_snapshot(offset=timedelta(0), cash=Decimal("999.00"), symbols=["Y"]),
        )
    assert [h.symbol for h in view.holdings] == ["X"]
    assert view.cash == Decimal("100.00")


# --- Per-user isolation ------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_is_per_user(two_owner_ids):
    a, b = two_owner_ids
    async with async_session_maker() as session:
        await reconcile_portfolio(Scope.for_user(b), session, FakeBrokerAdapter())
    async with async_session_maker() as session:
        view_a = await get_portfolio(Scope.for_user(a), session)
        view_b = await get_portfolio(Scope.for_user(b), session)
    assert view_a.is_empty  # A never imported — sees nothing of B's
    assert not view_b.is_empty


# --- Read endpoint: read-only, single-writer discipline ----------------------


def test_get_portfolio_endpoint_is_read_only_and_idempotent(client):
    email = _unique_email()
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}

        # Before import: empty, no as_of.
        r0 = client.get("/api/portfolio", headers=headers)
        assert r0.status_code == 200, r0.text
        assert r0.json()["holdings"] == []
        assert r0.json()["as_of"] is None

        # Import via refresh (reconcile from the fake broker).
        r1 = client.post("/api/portfolio/refresh", headers=headers)
        assert r1.status_code == 200, r1.text
        first = r1.json()
        assert len(first["holdings"]) == len(FAKE_HOLDINGS)
        assert first["as_of"] is not None

        # Repeated GETs never mutate the cache (read-only): identical payload.
        r2 = client.get("/api/portfolio", headers=headers)
        r3 = client.get("/api/portfolio", headers=headers)
        assert r2.json() == r3.json()
        assert r2.json()["as_of"] == first["as_of"]
        assert Decimal(str(r2.json()["cash"])) == FAKE_CASH
        # The fake set (VTI/VXUS/BND) is entirely index-core (Story 2.5).
        assert all(h["is_core"] is True for h in first["holdings"])
    finally:
        _delete_user(email)


def test_read_maps_core_vs_non_core(client):
    """`GET /api/portfolio` flags each holding as index-core or not (FR6)."""
    email = _unique_email()
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}

        # Seed a mixed portfolio: VTI (broad index core) + AAPL (not core).
        uid = _user_id_for(email)
        _insert_holding_sync(uid, "VTI")
        _insert_holding_sync(uid, "AAPL")

        r = client.get("/api/portfolio", headers=headers)
        assert r.status_code == 200, r.text
        by_symbol = {h["symbol"]: h["is_core"] for h in r.json()["holdings"]}
        assert by_symbol == {"VTI": True, "AAPL": False}
    finally:
        _delete_user(email)


# --- Import-on-connect -------------------------------------------------------


def test_link_callback_imports_portfolio(client):
    email = _unique_email()
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}

        state = client.get("/api/brokerage/authorize", headers=headers).json()["state"]
        cb = client.post(
            "/api/brokerage/callback",
            json={"code": "c", "state": state},
            headers=headers,
        )
        assert cb.status_code == 200, cb.text

        # Holdings were pulled in on connect (single-writer projection ran).
        r = client.get("/api/portfolio", headers=headers)
        assert r.status_code == 200, r.text
        assert len(r.json()["holdings"]) == len(FAKE_HOLDINGS)
        assert r.json()["as_of"] is not None
    finally:
        _delete_user(email)


class _FetchFailsAdapter(BrokerPort):
    """Links fine, but the portfolio fetch raises — import-on-connect must not
    break the link."""

    provider = "fake"

    def authorization_url(self, state: str) -> str:
        return FakeBrokerAdapter().authorization_url(state)

    def exchange_code(self, code: str, state: str) -> BrokerTokens:
        return FakeBrokerAdapter().exchange_code(code, state)

    def fetch_portfolio(self) -> PortfolioSnapshot:
        raise RuntimeError("simulated broker fetch failure")

    async def place_order(self, order_intent, *, idempotency_key):
        # Not exercised by this test; the BrokerPort contract now requires it
        # (Story 4.6). Delegate to the fake so the double stays a valid adapter.
        return await FakeBrokerAdapter().place_order(
            order_intent, idempotency_key=idempotency_key
        )

    async def get_order_status(self, idempotency_key):
        # Not exercised by this test; the BrokerPort contract now requires it
        # (Story 4.7). Delegate to the fake so the double stays a valid adapter.
        return await FakeBrokerAdapter().get_order_status(idempotency_key)


def test_link_survives_portfolio_fetch_failure(client):
    email = _unique_email()
    app = client.app
    app.dependency_overrides[get_broker] = lambda: _FetchFailsAdapter()
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}

        state = client.get("/api/brokerage/authorize", headers=headers).json()["state"]
        cb = client.post(
            "/api/brokerage/callback",
            json={"code": "c", "state": state},
            headers=headers,
        )
        # The account is still linked despite the import failure (no 500).
        assert cb.status_code == 200, cb.text
        assert cb.json()["linked"] is True
        assert client.get("/api/brokerage/status", headers=headers).json()["state"] == "live"
        # Cache stayed empty; the user can retry the import later.
        assert client.get("/api/portfolio", headers=headers).json()["holdings"] == []
    finally:
        app.dependency_overrides.pop(get_broker, None)
        _delete_user(email)
