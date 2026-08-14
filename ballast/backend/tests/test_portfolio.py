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
from brokers.portfolio import debit_cash, get_portfolio, reconcile_portfolio
from db.connection import get_connection
from db.models import BrokerageToken, PortfolioBalance, PortfolioCache
from db.scope import Scope
from db.session import async_session_maker, engine

PASSWORD = "supersecret123"


@pytest_asyncio.fixture(autouse=True)
async def ensure_tables():
    """Ensure the owned tables exist for the repo-level tests."""
    async with engine.begin() as conn:
        await conn.run_sync(BrokerageToken.__table__.create, checkfirst=True)
        await conn.run_sync(PortfolioCache.__table__.create, checkfirst=True)
        # Story 6.5: the dedicated per-user balances source. ``create(checkfirst=True)``
        # is a no-op on an already-existing table, so a carried-over test DB gets the
        # new table without an Alembic migration (matches the create-all lifecycle).
        await conn.run_sync(PortfolioBalance.__table__.create, checkfirst=True)
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


# --- Fixed-point money serialization (Story 6.4, no DB) ----------------------


def test_portfolio_out_serializes_money_fixed_point_no_exponent():
    """`cash` + holding `quantity`/`market_value` render fixed-point (no E+/E-)
    for extreme-large and tiny-fractional values, and round-trip via Decimal(str)."""
    from api.portfolio import CashStatesOut, HoldingOut, PortfolioOut

    big = Decimal("1E29") / Decimal("100")  # str() → "1E+27"
    tiny = Decimal("1E-8")  # str() → "1E-8"
    out = PortfolioOut(
        holdings=[
            HoldingOut(
                symbol="VTI",
                quantity=tiny,
                market_value=big,
                cost_basis=None,
            )
        ],
        cash=big,
        as_of=None,
        cash_states=CashStatesOut(
            ready_to_trade=big,
            parked=Decimal("0"),
            reserved=None,
            reserve_decided=False,
        ),
    )
    dumped = out.model_dump(mode="json")
    holding = dumped["holdings"][0]
    for field, value in (
        ("cash", dumped["cash"]),
        ("quantity", holding["quantity"]),
        ("market_value", holding["market_value"]),
    ):
        assert "E" not in value and "e" not in value, (field, value)
    assert dumped["cash"] == "1000000000000000000000000000"
    assert holding["quantity"] == "0.00000001"
    # Optional None money field stays null (never "None").
    assert holding["cost_basis"] is None
    # Round-trips through the documented Decimal(str(...)) consumer.
    assert Decimal(str(dumped["cash"])) == big
    assert Decimal(str(holding["quantity"])) == tiny


def test_holding_cost_basis_serializes_fixed_point_when_present():
    from api.portfolio import HoldingOut

    tiny = Decimal("1E-8")
    dumped = HoldingOut(
        symbol="VTI",
        quantity=Decimal("1"),
        market_value=Decimal("100.00"),
        cost_basis=tiny,
    ).model_dump(mode="json")
    assert "E" not in dumped["cost_basis"]
    assert dumped["cost_basis"] == "0.00000001"
    assert Decimal(str(dumped["cost_basis"])) == tiny


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


# --- Cash-only mapping (AD-14, Story 6.5) ------------------------------------


@pytest.mark.asyncio
async def test_all_cash_reconcile_reports_true_cash(two_owner_ids):
    """An all-cash account (cash>0, zero holdings) reports its TRUE cash — not 0
    — sourced from the dedicated balance row, with holdings empty (AD-14 closed)."""
    a, _ = two_owner_ids
    snap = _snapshot(offset=timedelta(0), cash=Decimal("500.00"), symbols=[])
    async with async_session_maker() as session:
        await reconcile_portfolio(Scope.for_user(a), session, FakeBrokerAdapter(), snapshot=snap)
    async with async_session_maker() as session:
        view = await get_portfolio(Scope.for_user(a), session)
    assert view.cash == Decimal("500.00")  # true cash, NOT 0
    assert view.holdings == []
    assert view.is_empty
    assert view.as_of == FAKE_AS_OF_BASE


@pytest.mark.asyncio
async def test_cash_heavy_with_holdings_reports_cash_and_holdings(two_owner_ids):
    a, _ = two_owner_ids
    snap = _snapshot(
        offset=timedelta(0), cash=Decimal("1000.00"), symbols=["VTI", "BND"]
    )
    async with async_session_maker() as session:
        await reconcile_portfolio(Scope.for_user(a), session, FakeBrokerAdapter(), snapshot=snap)
    async with async_session_maker() as session:
        view = await get_portfolio(Scope.for_user(a), session)
    assert view.cash == Decimal("1000.00")
    assert {h.symbol for h in view.holdings} == {"VTI", "BND"}
    assert len(view.holdings) == 2


@pytest.mark.asyncio
async def test_stale_cash_only_refetch_does_not_clobber(two_owner_ids):
    """Staleness now protects cash-only accounts too: a stale re-fetch (older
    as_of, different cash) leaves the balance row untouched."""
    a, _ = two_owner_ids
    async with async_session_maker() as session:
        await reconcile_portfolio(
            Scope.for_user(a),
            session,
            FakeBrokerAdapter(),
            snapshot=_snapshot(
                offset=timedelta(hours=1), cash=Decimal("500.00"), symbols=[]
            ),
        )
    # An OLDER cash-only snapshot with different cash must be ignored.
    async with async_session_maker() as session:
        view = await reconcile_portfolio(
            Scope.for_user(a),
            session,
            FakeBrokerAdapter(),
            snapshot=_snapshot(
                offset=timedelta(0), cash=Decimal("999.00"), symbols=[]
            ),
        )
    assert view.cash == Decimal("500.00")  # newer cash truth preserved
    assert view.as_of == FAKE_AS_OF_BASE + timedelta(hours=1)


@pytest.mark.asyncio
async def test_never_imported_reports_zero_cash_and_no_as_of(two_owner_ids):
    a, _ = two_owner_ids
    async with async_session_maker() as session:
        view = await get_portfolio(Scope.for_user(a), session)
    assert view.cash == Decimal("0")
    assert view.as_of is None
    assert view.holdings == []


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


# --- Cash states: the additive three-state summary (Story 9.1) ---------------


def test_read_exposes_default_cash_states_without_breaking_shape(client):
    """A fresh user's `GET /api/portfolio` carries the additive `cash_states`
    block (reserve never-decided → `reserved` null) while the original
    `holdings`/`cash`/`as_of` fields are unchanged."""
    email = _unique_email()
    try:
        _register(client, email)
        headers = {"Authorization": f"Bearer {_login(client, email)}"}

        r = client.get("/api/portfolio", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        # Original fixed shape intact.
        assert body["holdings"] == []
        assert body["as_of"] is None
        assert Decimal(str(body["cash"])) == Decimal("0")
        # Additive cash-state summary present, honest defaults.
        states = body["cash_states"]
        assert Decimal(str(states["ready_to_trade"])) == Decimal("0")
        assert Decimal(str(states["parked"])) == Decimal("0")
        assert states["reserved"] is None  # never-decided → absent, NOT 0
        assert states["reserve_decided"] is False
    finally:
        _delete_user(email)


def test_portfolio_read_does_not_create_cash_config_row(client):
    """`GET /api/portfolio` is read-only (AD-11) — it must NOT write a cash_config
    row for a first-time caller (that would be a write-on-read + a race)."""
    email = _unique_email()
    try:
        _register(client, email)
        headers = {"Authorization": f"Bearer {_login(client, email)}"}
        uid = _user_id_for(email)

        r = client.get("/api/portfolio", headers=headers)
        assert r.status_code == 200, r.text
        # Read still exposes the calm default cash-state summary...
        assert r.json()["cash_states"]["reserve_decided"] is False

        # ...but no config row was persisted by the read.
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM cash_config WHERE owner_id = %s", (str(uid),)
                )
                (count,) = cur.fetchone()
        assert count == 0
    finally:
        _delete_user(email)


def test_read_flags_parked_holdings_and_sums_parked_value(client):
    """A user-tagged money-market holding is flagged `is_parked` and its market
    value rolls up into `cash_states.parked` (derived at read time)."""
    email = _unique_email()
    try:
        _register(client, email)
        headers = {"Authorization": f"Bearer {_login(client, email)}"}

        uid = _user_id_for(email)
        _insert_holding_sync(uid, "VTI")  # genuine holding (market_value 100.00)
        _insert_holding_sync(uid, "SWVXX")  # money-market fund (market_value 100.00)

        # Tag SWVXX (lower-case on the wire — normalization must handle it).
        r = client.put(
            "/api/cash/config",
            headers=headers,
            json={"reserve_amount": None, "reserve_decided": False, "parked_symbols": ["swvxx"]},
        )
        assert r.status_code == 200, r.text

        body = client.get("/api/portfolio", headers=headers).json()
        by_symbol = {h["symbol"]: h["is_parked"] for h in body["holdings"]}
        assert by_symbol == {"VTI": False, "SWVXX": True}
        # Parked value = the tagged holding's market value.
        assert Decimal(str(body["cash_states"]["parked"])) == Decimal("100.00")
    finally:
        _delete_user(email)


def test_read_resolves_reserved_for_set_and_declined(client):
    """`cash_states.reserved` is the amount when set, and 0 when declined."""
    email = _unique_email()
    try:
        _register(client, email)
        headers = {"Authorization": f"Bearer {_login(client, email)}"}

        # Set an explicit reserve.
        client.put(
            "/api/cash/config",
            headers=headers,
            json={"reserve_amount": "250.00", "reserve_decided": True, "parked_symbols": []},
        )
        states = client.get("/api/portfolio", headers=headers).json()["cash_states"]
        assert Decimal(str(states["reserved"])) == Decimal("250.00")
        assert states["reserve_decided"] is True

        # Decline — resolves to 0 (decided, no amount).
        client.put(
            "/api/cash/config",
            headers=headers,
            json={"reserve_amount": None, "reserve_decided": True, "parked_symbols": []},
        )
        states = client.get("/api/portfolio", headers=headers).json()["cash_states"]
        assert states["reserved"] is not None
        assert Decimal(str(states["reserved"])) == Decimal("0")
        assert states["reserve_decided"] is True
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

    async def get_order_status_by_ref(self, broker_ref):
        # Not exercised by this test; the BrokerPort contract now requires it
        # (Story 6.7). Delegate to the fake so the double stays a valid adapter.
        return await FakeBrokerAdapter().get_order_status_by_ref(broker_ref)

    async def cancel_order(self, broker_ref):
        # Not exercised by this test; the BrokerPort contract now requires it
        # (Story 8.2). Delegate to the fake so the double stays a valid adapter.
        return await FakeBrokerAdapter().cancel_order(broker_ref)

    async def get_quote(self, symbol):
        # Not exercised by this test; the BrokerPort contract now requires it
        # (Story 8.4). Delegate to the fake so the double stays a valid adapter.
        return await FakeBrokerAdapter().get_quote(symbol)


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


# --- Blocking read offloaded off the event loop (Story 7.5) ------------------


@pytest.mark.asyncio
async def test_reconcile_offloads_fetch_off_event_loop(two_owner_ids, monkeypatch):
    """When no snapshot is passed, ``reconcile_portfolio`` obtains the snapshot via
    ``anyio.to_thread.run_sync`` (NOT a direct in-loop call), and still writes both
    tables correctly on the event loop.

    Asserts two things: (1) ``anyio.to_thread.run_sync`` was invoked with
    ``broker.fetch_portfolio`` — the blocking network read crosses to a worker
    thread; and (2) ``fetch_portfolio`` actually ran off the main event-loop
    thread.
    """
    import threading

    import anyio.to_thread
    import brokers.portfolio as portfolio_mod

    a, _ = two_owner_ids
    main_thread = threading.get_ident()
    ran_on: dict[str, int] = {}

    class _ThreadRecordingAdapter(FakeBrokerAdapter):
        def fetch_portfolio(self) -> PortfolioSnapshot:
            ran_on["fetch"] = threading.get_ident()
            return super().fetch_portfolio()

    broker = _ThreadRecordingAdapter()

    real_run_sync = anyio.to_thread.run_sync
    calls: list[object] = []

    async def _spy_run_sync(func, *args, **kwargs):
        calls.append(func)
        return await real_run_sync(func, *args, **kwargs)

    monkeypatch.setattr(portfolio_mod.anyio.to_thread, "run_sync", _spy_run_sync)

    async with async_session_maker() as session:
        view = await reconcile_portfolio(Scope.for_user(a), session, broker)

    # (1) The read was routed through anyio.to_thread.run_sync, with the pure
    # network read (fetch_portfolio) as the offloaded callable.
    assert broker.fetch_portfolio in calls
    # (2) It actually executed off the main event-loop thread.
    assert ran_on["fetch"] != main_thread
    # Both tables were still written correctly on the event loop.
    assert not view.is_empty
    assert view.cash == FAKE_CASH
    assert view.as_of == FAKE_AS_OF_BASE
    async with async_session_maker() as session:
        reread = await get_portfolio(Scope.for_user(a), session)
    assert reread.cash == FAKE_CASH
    assert {h.symbol for h in reread.holdings} == {
        h.symbol for h in FAKE_HOLDINGS
    }


@pytest.mark.asyncio
async def test_reconcile_with_injected_snapshot_does_not_offload(
    two_owner_ids, monkeypatch
):
    """An injected snapshot (tests) is used directly — no offload call is made."""
    import brokers.portfolio as portfolio_mod

    a, _ = two_owner_ids
    calls: list[object] = []

    async def _spy_run_sync(func, *args, **kwargs):
        calls.append(func)
        raise AssertionError("run_sync must not be called when a snapshot is passed")

    monkeypatch.setattr(portfolio_mod.anyio.to_thread, "run_sync", _spy_run_sync)

    snap = _snapshot(offset=timedelta(0), cash=Decimal("100.00"), symbols=["X"])
    async with async_session_maker() as session:
        view = await reconcile_portfolio(
            Scope.for_user(a), session, FakeBrokerAdapter(), snapshot=snap
        )
    assert calls == []
    assert view.cash == Decimal("100.00")
    assert [h.symbol for h in view.holdings] == ["X"]


class _MultiAccountRefusesAdapter(_FetchFailsAdapter):
    """A reading adapter that refuses a multi-account read (Story 7.5)."""

    def fetch_portfolio(self) -> PortfolioSnapshot:
        from brokers.schwab_adapter import SchwabAccountSelectionError

        raise SchwabAccountSelectionError(
            "This Schwab login exposes more than one account; set "
            "SCHWAB_ACCOUNT_ID to choose which one to trade. No order was placed."
        )


def test_refresh_multi_account_selection_is_calm_422_not_500(client):
    # A multi-account login with no SCHWAB_ACCOUNT_ID surfaces on the READ path as
    # a calm 422 config fault (never a raw 500 — the spec's I/O matrix promise).
    from brokers.factory import get_reading_broker

    email = _unique_email()
    app = client.app
    app.dependency_overrides[get_reading_broker] = lambda: _MultiAccountRefusesAdapter()
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}

        r = client.post("/api/portfolio/refresh", headers=headers)
        assert r.status_code == 422, r.text
        assert "SCHWAB_ACCOUNT_ID" in r.json()["error"]["message"]
    finally:
        app.dependency_overrides.pop(get_reading_broker, None)
        _delete_user(email)


# --- Story 10.10: account-type (margin) detect + persist + view --------------


@pytest.mark.asyncio
async def test_reconcile_persists_account_type_and_view_exposes_it(two_owner_ids):
    a, _b = two_owner_ids
    snap = PortfolioSnapshot(
        as_of=FAKE_AS_OF_BASE, cash=Decimal("100.00"), holdings=[], account_type="MARGIN"
    )
    async with async_session_maker() as session:
        await reconcile_portfolio(Scope.for_user(a), session, FakeBrokerAdapter(), snapshot=snap)
        view = await get_portfolio(Scope.for_user(a), session)
    assert view.account_type == "MARGIN"
    assert view.cash == Decimal("100.00")  # cash unchanged by the account-type read


@pytest.mark.asyncio
async def test_reconcile_none_account_type_stays_none(two_owner_ids):
    a, _b = two_owner_ids
    snap = PortfolioSnapshot(
        as_of=FAKE_AS_OF_BASE, cash=Decimal("100.00"), holdings=[], account_type=None
    )
    async with async_session_maker() as session:
        await reconcile_portfolio(Scope.for_user(a), session, FakeBrokerAdapter(), snapshot=snap)
        view = await get_portfolio(Scope.for_user(a), session)
    assert view.account_type is None  # no type → no warning (fail-open)


@pytest.mark.asyncio
async def test_never_imported_has_no_account_type(two_owner_ids):
    a, _b = two_owner_ids
    async with async_session_maker() as session:
        view = await get_portfolio(Scope.for_user(a), session)
    assert view.account_type is None


@pytest.mark.asyncio
async def test_newer_reconcile_updates_account_type(two_owner_ids):
    a, _b = two_owner_ids
    async with async_session_maker() as session:
        await reconcile_portfolio(
            Scope.for_user(a),
            session,
            FakeBrokerAdapter(),
            snapshot=PortfolioSnapshot(
                as_of=FAKE_AS_OF_BASE, cash=Decimal("100.00"), holdings=[], account_type="CASH"
            ),
        )
        await reconcile_portfolio(
            Scope.for_user(a),
            session,
            FakeBrokerAdapter(),
            snapshot=PortfolioSnapshot(
                as_of=FAKE_AS_OF_BASE + timedelta(hours=1),
                cash=Decimal("200.00"),
                holdings=[],
                account_type="MARGIN",
            ),
        )
        view = await get_portfolio(Scope.for_user(a), session)
    assert view.account_type == "MARGIN"


def test_fake_adapter_reports_non_margin_account():
    """The fake broker never trips the margin warning (real-broker-only by design)."""
    assert FakeBrokerAdapter().fetch_portfolio().account_type == "CASH"


# --- Story 10.12: debit_cash (keep settled cash fresh across sequential buys) --


async def _reconcile_cash(uid, cash: str):
    async with async_session_maker() as session:
        await reconcile_portfolio(
            Scope.for_user(uid),
            session,
            FakeBrokerAdapter(),
            snapshot=PortfolioSnapshot(
                as_of=FAKE_AS_OF_BASE, cash=Decimal(cash), holdings=[]
            ),
        )


@pytest.mark.asyncio
async def test_debit_cash_reduces_cached_cash(two_owner_ids):
    a, _b = two_owner_ids
    await _reconcile_cash(a, "750.00")
    async with async_session_maker() as session:
        did = await debit_cash(Scope.for_user(a), session, Decimal("500.00"))
    assert did is True
    async with async_session_maker() as session:
        view = await get_portfolio(Scope.for_user(a), session)
    assert view.cash == Decimal("250.00")


@pytest.mark.asyncio
async def test_debit_cash_clamps_at_zero(two_owner_ids):
    a, _b = two_owner_ids
    await _reconcile_cash(a, "100.00")
    async with async_session_maker() as session:
        await debit_cash(Scope.for_user(a), session, Decimal("500.00"))
    async with async_session_maker() as session:
        view = await get_portfolio(Scope.for_user(a), session)
    assert view.cash == Decimal("0.00")  # never negative


@pytest.mark.asyncio
async def test_debit_cash_no_balance_row_is_noop(two_owner_ids):
    a, _b = two_owner_ids  # never reconciled → no balance row
    async with async_session_maker() as session:
        did = await debit_cash(Scope.for_user(a), session, Decimal("500.00"))
    assert did is False  # nothing to debit; the existing approve harness relies on this


@pytest.mark.asyncio
async def test_debit_cash_ignores_nonpositive_amount(two_owner_ids):
    a, _b = two_owner_ids
    await _reconcile_cash(a, "750.00")
    async with async_session_maker() as session:
        assert await debit_cash(Scope.for_user(a), session, Decimal("0")) is False
        assert await debit_cash(Scope.for_user(a), session, Decimal("-5")) is False
    async with async_session_maker() as session:
        view = await get_portfolio(Scope.for_user(a), session)
    assert view.cash == Decimal("750.00")  # unchanged


@pytest.mark.asyncio
async def test_debit_cash_is_per_user_scoped(two_owner_ids):
    a, b = two_owner_ids
    await _reconcile_cash(a, "750.00")
    await _reconcile_cash(b, "750.00")
    async with async_session_maker() as session:
        await debit_cash(Scope.for_user(a), session, Decimal("500.00"))
    async with async_session_maker() as session:
        assert (await get_portfolio(Scope.for_user(a), session)).cash == Decimal("250.00")
        assert (await get_portfolio(Scope.for_user(b), session)).cash == Decimal("750.00")


@pytest.mark.asyncio
async def test_reconcile_overwrites_a_prior_debit_with_broker_truth(two_owner_ids):
    """AC5 — the debit is a between-refreshes approximation; a newer authoritative
    reconcile still wins and overwrites the debited cash with broker truth."""
    a, _b = two_owner_ids
    await _reconcile_cash(a, "750.00")
    async with async_session_maker() as session:
        await debit_cash(Scope.for_user(a), session, Decimal("500.00"))
    async with async_session_maker() as session:
        assert (await get_portfolio(Scope.for_user(a), session)).cash == Decimal("250.00")
    # A newer broker snapshot (later as_of) reconciles and overwrites the debit.
    async with async_session_maker() as session:
        await reconcile_portfolio(
            Scope.for_user(a),
            session,
            FakeBrokerAdapter(),
            snapshot=PortfolioSnapshot(
                as_of=FAKE_AS_OF_BASE + timedelta(hours=1),
                cash=Decimal("900.00"),
                holdings=[],
            ),
        )
    async with async_session_maker() as session:
        assert (await get_portfolio(Scope.for_user(a), session)).cash == Decimal("900.00")


# --- Demo portfolio flag (safe team demo — fake data only) -------------------


def test_fake_demo_portfolio_flag_returns_demo_holdings(monkeypatch):
    """DEMO_PORTFOLIO=1 → the richer presentation portfolio (all-US index core + an
    over-concentrated single stock + a high-fee active fund + an under-ceiling stock
    + a few thousand cash), so a team demo lights up the deploy AND SELL-side review
    stories on FAKE data."""
    from brokers.fake_adapter import DEMO_CASH
    monkeypatch.setenv("DEMO_PORTFOLIO", "1")
    snap = FakeBrokerAdapter().fetch_portfolio()
    symbols = {h.symbol for h in snap.holdings}
    assert symbols == {"VTI", "NVDA", "AGTHX", "AAPL"}  # index core + unclassified sleeve
    assert "VXUS" not in symbols and "BND" not in symbols  # underweight → deploy buys them
    assert snap.cash == DEMO_CASH == Decimal("4000.00")
    assert snap.account_type == "CASH"                  # never a margin warning in demo


def test_fake_default_portfolio_unchanged_without_flag():
    """Default (no flag) is the small 3-fund portfolio — tests/normal runs unaffected."""
    snap = FakeBrokerAdapter().fetch_portfolio()
    assert {h.symbol for h in snap.holdings} == {"VTI", "VXUS", "BND"}
    assert snap.cash == FAKE_CASH == Decimal("750.25")
