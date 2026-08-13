"""Story 3.4 tests — the missed-growth read endpoint.

REAL DB (no mocks), combining two proven conventions:
- ``test_precedent_endpoint.py``: TestClient(create_app()), register + JWT login,
  crafted deterministic ``market_daily`` bars under TEST-prefixed symbols with
  per-test cleanup.
- ``test_portfolio.py``: seed a ``portfolio_cache`` row via sync psycopg (so it
  works under the TestClient event loop) to supply the user's idle cash.

We walk the I/O & Edge-Case Matrix over the wire: a seeded ``market_daily`` history
+ a ``PortfolioCache`` row (``cash>0``) → 200 with ``forgone_growth`` present and
``source``/``as_of`` set; no cache row → 200 ``no_idle_cash``; unauthenticated →
401 with no figure. NO network, NO LLM, NO credentials.

NOTE: the "figure present" test drives the endpoint's bounded ``?symbol=`` param
with a TEST-prefixed symbol, so it seeds/cleans ONLY that throwaway series and
never touches (or deletes) a real ``VTI`` ingest on the shared global table. The
no-idle-cash and auth tests use the default benchmark but write no market data.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from api.app import create_app
from db.connection import get_connection
from fastapi.testclient import TestClient
from precedent.missed_growth import LOOKBACK_TRADING_DAYS

PASSWORD = "supersecret123"

BASE_DAY = date(2015, 1, 1)

# A throwaway benchmark symbol driven through the endpoint's ?symbol= param, so
# the figure test never seeds/deletes a real VTI ingest on the shared table.
EP_SYMBOL = "TEST_MG_ENDPOINT"


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


def _rising_closes() -> list[Decimal]:
    """253 bars, +14% start→end over the trailing window (first/last bars pinned)."""
    n = LOOKBACK_TRADING_DAYS + 1
    closes = [Decimal("100.00")]
    for i in range(1, n - 1):
        closes.append(Decimal("100.00") + Decimal("14.00") * Decimal(i) / Decimal(n - 1))
    closes.append(Decimal("114.00"))
    return closes


def _unique_email() -> str:
    return f"mg-endpoint-{uuid.uuid4().hex}@example.com"


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


def _insert_cache_row(owner: uuid.UUID, cash: Decimal) -> None:
    """Seed one portfolio_cache row (via sync psycopg) carrying the user's cash."""
    as_of = datetime(2026, 7, 27, tzinfo=timezone.utc)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO portfolio_cache "
                "(id, owner_id, symbol, quantity, market_value, cost_basis, cash, as_of) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    str(uuid.uuid4()),
                    str(owner),
                    "VOO",
                    "1",
                    "100.00",
                    "90.00",
                    str(cash),
                    as_of,
                ),
            )
        conn.commit()


def _insert_balance_row(owner: uuid.UUID, cash: Decimal) -> None:
    """Seed the dedicated portfolio_balance row (Story 6.5) carrying idle cash —
    the AUTHORITATIVE source the view reads (never a holdings row). Sync psycopg so
    it works under the TestClient event loop."""
    as_of = datetime(2026, 7, 27, tzinfo=timezone.utc)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO portfolio_balance (id, owner_id, cash, as_of) "
                "VALUES (%s, %s, %s, %s)",
                (str(uuid.uuid4()), str(owner), str(cash), as_of),
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


@pytest.fixture
def client() -> TestClient:
    with TestClient(create_app()) as c:
        yield c


# --- Idle cash + seeded history → 200 with figure present --------------------


def test_missed_growth_returns_figure_for_idle_cash(client):
    _clean_market([EP_SYMBOL])
    _insert_series(EP_SYMBOL, _rising_closes())
    email = _unique_email()
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}

        owner = _user_id_for(email)
        # Idle cash is now sourced from the dedicated balance row (Story 6.5),
        # not the denormalized holdings-row column; seed both (a holding + the
        # authoritative balance).
        _insert_cache_row(owner, Decimal("25000.00"))
        _insert_balance_row(owner, Decimal("25000.00"))

        r = client.get(
            f"/api/precedent/missed-growth?symbol={EP_SYMBOL}", headers=headers
        )
        assert r.status_code == 200, r.text
        body = r.json()

        # The standalone DTO shape — NOT the AD-12 evidence contract. Story 9.2
        # adds the cash-state/yield-aware fields + reserve_decided (additive).
        assert set(body.keys()) == {
            "idle_cash",
            "benchmark",
            "window_return",
            "window_start",
            "window_end",
            "forgone_growth",
            "trading_days",
            "statement",
            "source",
            "as_of",
            "sufficient",
            "reason",
            "settlement_cash",
            "parked",
            "reserved",
            "reserve_decided",
            "money_market_apy",
            "investable_base",
        }
        # No config → never-decided reserve surfaces as null + reserve_decided=false;
        # no parked holdings, so the figure is the pure-settlement figure.
        assert body["reserved"] is None
        assert body["reserve_decided"] is False
        assert body["parked"] == "0.00"
        assert body["settlement_cash"] == "25000.00"
        assert body["investable_base"] == "25000.00"
        assert body["money_market_apy"] == "0.035"
        assert body["sufficient"] is True
        assert body["reason"] is None
        assert body["benchmark"] == EP_SYMBOL
        assert body["idle_cash"] == "25000.00"
        # +14% window → 25000 × 0.14 = 3500.00 (pinned first/last bars).
        assert body["window_return"] == "0.1400"
        assert body["forgone_growth"] == "3500.00"
        # Always cites source + window.
        assert body["source"]
        assert body["as_of"]
        assert body["window_start"]
        assert body["statement"]
    finally:
        _clean_market([EP_SYMBOL])
        _delete_user(email)


# --- All-cash account (AD-14, Story 6.5) → real idle-cash figure -------------


def test_missed_growth_all_cash_account_yields_real_figure(client):
    """An all-cash account (idle cash in the dedicated balance row, ZERO holdings)
    now yields a real idle-cash figure and no longer returns ``no_idle_cash`` —
    proving the AD-14 cash-only gap is closed end-to-end through get_portfolio."""
    _clean_market([EP_SYMBOL])
    _insert_series(EP_SYMBOL, _rising_closes())
    email = _unique_email()
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}

        owner = _user_id_for(email)
        # Idle cash lives ONLY in the balance row — NO portfolio_cache holdings row.
        _insert_balance_row(owner, Decimal("25000.00"))

        r = client.get(
            f"/api/precedent/missed-growth?symbol={EP_SYMBOL}", headers=headers
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # No longer a confidently-false "no idle cash": a real figure surfaces.
        assert body["reason"] is None
        assert body["idle_cash"] == "25000.00"
        assert body["forgone_growth"] == "3500.00"  # 25000 × 0.14
        assert body["sufficient"] is True
    finally:
        _clean_market([EP_SYMBOL])
        _delete_user(email)


# --- No cache row → 200 no_idle_cash -----------------------------------------


def test_missed_growth_no_cache_row_is_no_idle_cash(client):
    email = _unique_email()
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}

        # No portfolio_cache row seeded → cash reads as 0.
        r = client.get("/api/precedent/missed-growth", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["reason"] == "no_idle_cash"
        assert body["sufficient"] is True
        assert body["forgone_growth"] == "0.00"
        # Never a dead end: still cites the source string.
        assert body["source"]
        assert body["statement"]
    finally:
        _delete_user(email)


# --- Unauthenticated → 401 ---------------------------------------------------


def test_missed_growth_requires_authentication(client):
    r = client.get("/api/precedent/missed-growth")
    assert r.status_code == 401, r.text
    # No figure leaks to an unauthenticated caller.
    assert "forgone_growth" not in r.text
    assert "statement" not in r.text


# --- Story 9.2: cash-state/yield-aware endpoint ------------------------------


def _insert_holding_row(owner: uuid.UUID, symbol: str, market_value: Decimal) -> None:
    """Seed one portfolio_cache holding (sync psycopg) with a chosen market_value —
    used to make a symbol PARKED (via the user's cash_config)."""
    as_of = datetime(2026, 7, 27, tzinfo=timezone.utc)
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
                    str(market_value),
                    "0.00",
                    "0",
                    as_of,
                ),
            )
        conn.commit()


def _put_config(client, headers, *, amount, decided, symbols):
    r = client.put(
        "/api/cash/config",
        headers=headers,
        json={
            "reserve_amount": amount,
            "reserve_decided": decided,
            "parked_symbols": symbols,
        },
    )
    assert r.status_code == 200, r.text


def test_missed_growth_parked_and_reserve_raise_figure_honestly(client):
    """Parked money-market holdings + a set reserve produce a yield-aware figure
    computed on cash+parked−reserve (reserve parked-first), disclose the apy, and
    surface the resolved reserve — never the pre-reserve amount."""
    _clean_market([EP_SYMBOL])
    _insert_series(EP_SYMBOL, _rising_closes())
    email = _unique_email()
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}

        owner = _user_id_for(email)
        # 5000 settlement cash + 5000 parked (SWVXX) + 2000 reserve.
        _insert_balance_row(owner, Decimal("5000.00"))
        _insert_holding_row(owner, "SWVXX", Decimal("5000.00"))
        _put_config(
            client, headers, amount="2000.00", decided=True, symbols=["swvxx"]
        )

        r = client.get(
            f"/api/precedent/missed-growth?symbol={EP_SYMBOL}", headers=headers
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["reason"] is None
        assert body["settlement_cash"] == "5000.00"
        assert body["parked"] == "5000.00"
        assert body["reserved"] == "2000.00"
        assert body["reserve_decided"] is True
        assert body["investable_base"] == "8000.00"
        # parked drawn-first → parked_inv=3000, cash_inv=5000;
        # 5000·0.14 + 3000·(0.14−0.035) = 700 + 315 = 1015.00.
        assert body["forgone_growth"] == "1015.00"
        # Discloses the yield assumption; surfaces the reserve in the statement.
        assert "money-market" in body["statement"].lower()
        assert body["money_market_apy"] == "0.035"
        assert "$2,000.00" in body["statement"]
    finally:
        _clean_market([EP_SYMBOL])
        _delete_user(email)


def test_missed_growth_reserve_declined_resolves_zero(client):
    """A declined reserve resolves to 0 (decided): reserved="0.00",
    reserve_decided=true, and the figure is on the full cash."""
    _clean_market([EP_SYMBOL])
    _insert_series(EP_SYMBOL, _rising_closes())
    email = _unique_email()
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}

        owner = _user_id_for(email)
        _insert_balance_row(owner, Decimal("25000.00"))
        _put_config(client, headers, amount=None, decided=True, symbols=[])

        r = client.get(
            f"/api/precedent/missed-growth?symbol={EP_SYMBOL}", headers=headers
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["reserved"] == "0.00"
        assert body["reserve_decided"] is True
        # No reserve drawn → figure on the full 25000.
        assert body["forgone_growth"] == "3500.00"
        assert body["investable_base"] == "25000.00"
    finally:
        _clean_market([EP_SYMBOL])
        _delete_user(email)


def test_missed_growth_reserve_never_decided_surfaces_null(client):
    """A never-decided reserve surfaces reserved=null + reserve_decided=false, is
    treated as 0 for the calc, and stays calm (no fabricated reserve figure)."""
    _clean_market([EP_SYMBOL])
    _insert_series(EP_SYMBOL, _rising_closes())
    email = _unique_email()
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}

        owner = _user_id_for(email)
        _insert_balance_row(owner, Decimal("25000.00"))
        # No cash_config PUT at all → never-decided.

        r = client.get(
            f"/api/precedent/missed-growth?symbol={EP_SYMBOL}", headers=headers
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["reserved"] is None
        assert body["reserve_decided"] is False
        # Reserve treated as 0 → full figure; no reserve clause in the copy.
        assert body["forgone_growth"] == "3500.00"
        assert "reserve" not in body["statement"].lower()
    finally:
        _clean_market([EP_SYMBOL])
        _delete_user(email)


def test_missed_growth_fully_reserved_when_reserve_covers_all(client):
    """A reserve larger than cash+parked → reason=fully_reserved, forgone 0.00,
    calm reserve-aware statement."""
    _clean_market([EP_SYMBOL])
    _insert_series(EP_SYMBOL, _rising_closes())
    email = _unique_email()
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}

        owner = _user_id_for(email)
        _insert_balance_row(owner, Decimal("1000.00"))
        _insert_holding_row(owner, "SWVXX", Decimal("1000.00"))
        _put_config(
            client, headers, amount="5000.00", decided=True, symbols=["swvxx"]
        )

        r = client.get(
            f"/api/precedent/missed-growth?symbol={EP_SYMBOL}", headers=headers
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["reason"] == "fully_reserved"
        assert body["forgone_growth"] == "0.00"
        assert body["investable_base"] == "0.00"
        assert "reserve covers all" in body["statement"].lower()
    finally:
        _clean_market([EP_SYMBOL])
        _delete_user(email)
