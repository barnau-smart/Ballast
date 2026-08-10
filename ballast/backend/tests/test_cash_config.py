"""Story 9.1 — the authenticated cash-configuration endpoints (REAL DB).

Matches the endpoint-test convention (TestClient + register + JWT login, per-test
cleanup). Verifies the honest set-or-decline reserve semantics (never-decided /
declined / set incl. exactly 0), a calm 422 on a negative reserve, parked-symbol
normalization + de-duplication, money on the wire as fixed-point strings, and
per-user isolation (AD-10). Requires the docker Postgres. No email/broker network
is exercised — this only reads/writes the cash config.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from api.app import create_app
from db.connection import get_connection
from fastapi.testclient import TestClient

PASSWORD = "supersecret123"


def _unique_email() -> str:
    return f"cash-cfg-{uuid.uuid4().hex}@example.com"


def _delete_user(email: str) -> None:
    # CASCADE on owner_id cleans the cash_config row too.
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


def test_default_is_never_decided_for_new_user(client):
    """A brand-new user reads a calm default: reserve NEVER-decided (not 0), no
    parked symbols — the honesty crux (AC2)."""
    email = _unique_email()
    try:
        _register(client, email)
        headers = {"Authorization": f"Bearer {_login(client, email)}"}

        r = client.get("/api/cash/config", headers=headers)
        assert r.status_code == 200, r.text
        assert r.json() == {
            "reserve_amount": None,
            "reserve_decided": False,
            "parked_symbols": [],
        }
    finally:
        _delete_user(email)


def test_set_reserve_persists_including_zero(client):
    """Setting a reserve (and exactly 0 — a legitimate explicit set) persists and
    round-trips as a fixed-point string."""
    email = _unique_email()
    try:
        _register(client, email)
        headers = {"Authorization": f"Bearer {_login(client, email)}"}

        r = client.put(
            "/api/cash/config",
            headers=headers,
            json={"reserve_amount": "1500.00", "reserve_decided": True, "parked_symbols": []},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["reserve_decided"] is True
        assert body["reserve_amount"] == "1500.00"
        assert Decimal(str(body["reserve_amount"])) == Decimal("1500.00")

        # A fresh read reflects it.
        r = client.get("/api/cash/config", headers=headers)
        assert r.json()["reserve_amount"] == "1500.00"

        # Exactly 0 is a legitimate explicit set (NOT the same as never-decided).
        r = client.put(
            "/api/cash/config",
            headers=headers,
            json={"reserve_amount": "0", "reserve_decided": True, "parked_symbols": []},
        )
        assert r.status_code == 200, r.text
        assert r.json()["reserve_amount"] == "0.00"
        assert r.json()["reserve_decided"] is True
    finally:
        _delete_user(email)


def test_decline_resolves_to_no_amount_but_decided(client):
    """Declining ("I don't keep one") records decided=True with a null amount."""
    email = _unique_email()
    try:
        _register(client, email)
        headers = {"Authorization": f"Bearer {_login(client, email)}"}

        r = client.put(
            "/api/cash/config",
            headers=headers,
            json={"reserve_amount": None, "reserve_decided": True, "parked_symbols": []},
        )
        assert r.status_code == 200, r.text
        assert r.json() == {
            "reserve_amount": None,
            "reserve_decided": True,
            "parked_symbols": [],
        }
    finally:
        _delete_user(email)


def test_negative_reserve_is_calm_422(client):
    """A negative reserve is a config fault surfaced as a calm 422 (never a 500)."""
    email = _unique_email()
    try:
        _register(client, email)
        headers = {"Authorization": f"Bearer {_login(client, email)}"}

        r = client.put(
            "/api/cash/config",
            headers=headers,
            json={"reserve_amount": "-5.00", "reserve_decided": True, "parked_symbols": []},
        )
        assert r.status_code == 422, r.text

        # And nothing was persisted — still the never-decided default.
        r = client.get("/api/cash/config", headers=headers)
        assert r.json()["reserve_decided"] is False
    finally:
        _delete_user(email)


def test_parked_symbols_normalized_and_deduped(client):
    """Parked symbols are trimmed, upper-cased, and de-duplicated (order-stable)."""
    email = _unique_email()
    try:
        _register(client, email)
        headers = {"Authorization": f"Bearer {_login(client, email)}"}

        r = client.put(
            "/api/cash/config",
            headers=headers,
            json={
                "reserve_amount": None,
                "reserve_decided": False,
                "parked_symbols": ["swvxx", " SWVXX ", "voo", "  ", "VOO"],
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["parked_symbols"] == ["SWVXX", "VOO"]

        r = client.get("/api/cash/config", headers=headers)
        assert r.json()["parked_symbols"] == ["SWVXX", "VOO"]
    finally:
        _delete_user(email)


@pytest.mark.parametrize("bad", ["-5.00", "NaN", "Infinity", "100.999", "1e19"])
def test_invalid_reserve_is_calm_422_not_500(client, bad):
    """Negative, non-finite, over-precision, and out-of-range reserves are calm
    422s (never a raw 500 or a silently-corrupted/rounded value)."""
    email = _unique_email()
    try:
        _register(client, email)
        headers = {"Authorization": f"Bearer {_login(client, email)}"}

        r = client.put(
            "/api/cash/config",
            headers=headers,
            json={"reserve_amount": bad, "reserve_decided": True, "parked_symbols": []},
        )
        assert r.status_code == 422, (bad, r.text)
    finally:
        _delete_user(email)


def test_amount_forces_decided_true(client):
    """Coherence guard: an explicit amount can never persist as never-decided —
    even if the client sends reserve_decided=false, a provided amount sets it."""
    email = _unique_email()
    try:
        _register(client, email)
        headers = {"Authorization": f"Bearer {_login(client, email)}"}

        r = client.put(
            "/api/cash/config",
            headers=headers,
            json={"reserve_amount": "300.00", "reserve_decided": False, "parked_symbols": []},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["reserve_amount"] == "300.00"
        assert body["reserve_decided"] is True  # forced True despite the request
    finally:
        _delete_user(email)


def test_parked_only_write_keeps_reserve_never_decided(client):
    """Tagging a parked symbol with no reserve amount leaves the reserve honestly
    never-decided (so the set-or-decline prompt still shows)."""
    email = _unique_email()
    try:
        _register(client, email)
        headers = {"Authorization": f"Bearer {_login(client, email)}"}

        r = client.put(
            "/api/cash/config",
            headers=headers,
            json={"reserve_amount": None, "reserve_decided": False, "parked_symbols": ["SWVXX"]},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["parked_symbols"] == ["SWVXX"]
        assert body["reserve_decided"] is False
        assert body["reserve_amount"] is None
    finally:
        _delete_user(email)


def test_requires_authentication(client):
    assert client.get("/api/cash/config").status_code == 401


def test_per_user_isolation(client):
    """One user's cash config never leaks to another (fail-closed, AD-10)."""
    email_a = _unique_email()
    email_b = _unique_email()
    try:
        _register(client, email_a)
        _register(client, email_b)
        headers_a = {"Authorization": f"Bearer {_login(client, email_a)}"}
        headers_b = {"Authorization": f"Bearer {_login(client, email_b)}"}

        # A sets a reserve + parked symbol.
        client.put(
            "/api/cash/config",
            headers=headers_a,
            json={"reserve_amount": "999.00", "reserve_decided": True, "parked_symbols": ["SWVXX"]},
        )

        # B is completely unaffected — still the calm never-decided default.
        r = client.get("/api/cash/config", headers=headers_b)
        assert r.json() == {
            "reserve_amount": None,
            "reserve_decided": False,
            "parked_symbols": [],
        }

        # A still sees its own config.
        r = client.get("/api/cash/config", headers=headers_a)
        assert r.json()["reserve_amount"] == "999.00"
        assert r.json()["parked_symbols"] == ["SWVXX"]
    finally:
        _delete_user(email_a)
        _delete_user(email_b)
