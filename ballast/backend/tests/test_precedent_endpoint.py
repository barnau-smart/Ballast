"""Story 3.3 tests — the recovery-precedent read endpoint.

REAL DB (no mocks), combining two proven conventions:
- ``test_session_status.py``: TestClient(create_app()), register via
  ``POST /api/auth/register``, login via ``POST /api/auth/jwt/login`` → Bearer.
- ``test_precedent.py``: seed the GLOBAL ``market_daily`` table with crafted,
  fully deterministic bars under TEST-prefixed symbols, with per-test cleanup.

We walk the I/O & Edge-Case Matrix over the wire: a seeded qualifying history →
200 ``event-precedent`` with ``windows[]`` + ``source``/``as_of``; a degraded
seed (no-match) → 200 ``strategy`` with ``stats.reason``; and an unauthenticated
request → 401 with no record body. NO network, NO LLM, NO credentials.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from api.app import create_app
from db.connection import get_connection
from fastapi.testclient import TestClient

PASSWORD = "supersecret123"

# TEST-prefixed symbols — safe against the shared global market_daily table.
SYM_QUALIFY = "TEST_PREC_EP_QUALIFY"
SYM_NOMATCH = "TEST_PREC_EP_NOMATCH"
ALL_TEST_SYMBOLS = [SYM_QUALIFY, SYM_NOMATCH]

BASE_DAY = date(2015, 1, 1)


def _clean(symbols: list[str]) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM market_daily WHERE symbol = ANY(%s)", (symbols,))
        conn.commit()


def _insert_series(symbol: str, closes: list[Decimal]) -> None:
    """Insert a deterministic series: one bar/day, adj_close = close = value."""
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
    """A crafted series with ONE clean 8% episode + a current 8% drop.

    Mirrors ``test_precedent._qualify_closes`` so the engine returns a qualifying
    ``event-precedent`` record with exactly one matched window.
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


def _nomatch_closes() -> list[Decimal]:
    """In a drawdown, but NO past episode falls in the current magnitude band.

    A ~2% early dip then a huge current ~40% drop → engine degrades to a
    ``strategy`` record with ``stats.reason == "no_band_match"``.
    """
    return [
        Decimal("100"),
        Decimal("98"),  # ~2% dip
        Decimal("100"),  # recovered
        Decimal("120"),
        Decimal("150"),  # peak 150
        Decimal("120"),
        Decimal("90"),  # current: 40% below 150
    ]


def _unique_email() -> str:
    return f"prec-endpoint-{uuid.uuid4().hex}@example.com"


def _delete_user(email: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM "user" WHERE email = %s', (email,))
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


# --- Qualifying precedent → 200 event-precedent ------------------------------


def test_recovery_returns_event_precedent_for_qualifying_history(client):
    _clean([SYM_QUALIFY])
    _insert_series(SYM_QUALIFY, _qualify_closes())
    email = _unique_email()
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}

        r = client.get(
            "/api/precedent/recovery",
            params={"symbol": SYM_QUALIFY},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        body = r.json()

        # Exactly the AD-12 6-field shape — no field added/removed/renamed.
        assert set(body.keys()) == {
            "id",
            "kind",
            "statement",
            "stats",
            "source",
            "as_of",
        }
        assert body["kind"] == "event-precedent"
        assert body["id"].startswith("ep-")
        assert body["statement"]
        # Always cites source + a real as-of date.
        assert body["source"]
        assert body["as_of"] == "2015-09-26"

        stats = body["stats"]
        # JSON-safe: Decimals as strings, ints preserved.
        assert stats["initial_drawdown_pct"] == "0.0800"
        assert stats["instance_count"] == 1
        assert len(stats["windows"]) == 1
        w = stats["windows"][0]
        assert w["peak_date"] == "2015-01-03"
        assert w["trough_date"] == "2015-01-05"
        assert w["recovered"] is True
    finally:
        _clean([SYM_QUALIFY])
        _delete_user(email)


# --- Degraded / no match → 200 strategy --------------------------------------


def test_recovery_returns_strategy_when_no_band_match(client):
    _clean([SYM_NOMATCH])
    _insert_series(SYM_NOMATCH, _nomatch_closes())
    email = _unique_email()
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}

        r = client.get(
            "/api/precedent/recovery",
            params={"symbol": SYM_NOMATCH},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        body = r.json()

        assert set(body.keys()) == {
            "id",
            "kind",
            "statement",
            "stats",
            "source",
            "as_of",
        }
        assert body["kind"] == "strategy"
        # Strategy fallback carries a machine reason and no windows — never a
        # dead end / empty body.
        assert body["stats"]["reason"] == "no_band_match"
        assert body["stats"]["windows"] == []
        assert body["statement"]
        assert body["source"]
        assert body["as_of"]
    finally:
        _clean([SYM_NOMATCH])
        _delete_user(email)


# --- Unauthenticated → 401 ---------------------------------------------------


def test_recovery_requires_authentication(client):
    r = client.get("/api/precedent/recovery")
    assert r.status_code == 401, r.text
    # No record body leaks to an unauthenticated caller.
    assert "statement" not in r.text
    assert "event-precedent" not in r.text
