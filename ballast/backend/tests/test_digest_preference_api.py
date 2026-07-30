"""Story 5.1 — the authenticated digest opt-in endpoints (REAL DB).

Matches the endpoint-test convention (TestClient + register + JWT login, per-test
cleanup). Verifies: the digest is OFF by default for a new user; PUT persists the
opt-in and a re-read reflects it; and per-user isolation (AD-10) — one user's
opt-in never shows up for another. Requires the docker Postgres. No email is sent
(these endpoints only read/write the preference).
"""

from __future__ import annotations

import uuid

import pytest

from api.app import create_app
from db.connection import get_connection
from fastapi.testclient import TestClient

PASSWORD = "supersecret123"


def _unique_email() -> str:
    return f"digest-pref-{uuid.uuid4().hex}@example.com"


def _delete_user(email: str) -> None:
    # CASCADE on owner_id cleans the digest_preference row too.
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


def test_default_off_then_opt_in_persists(client):
    email = _unique_email()
    try:
        _register(client, email)
        headers = {"Authorization": f"Bearer {_login(client, email)}"}

        # Off by default for a brand-new user.
        r = client.get("/api/digest/preference", headers=headers)
        assert r.status_code == 200, r.text
        assert r.json() == {"opted_in": False}

        # Opt in — the change persists.
        r = client.put(
            "/api/digest/preference", headers=headers, json={"opted_in": True}
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"opted_in": True}

        # A fresh read reflects the opt-in.
        r = client.get("/api/digest/preference", headers=headers)
        assert r.json() == {"opted_in": True}

        # And it can be turned back off just as easily.
        r = client.put(
            "/api/digest/preference", headers=headers, json={"opted_in": False}
        )
        assert r.json() == {"opted_in": False}
    finally:
        _delete_user(email)


def test_requires_authentication(client):
    r = client.get("/api/digest/preference")
    assert r.status_code == 401


def test_per_user_isolation(client):
    email_a = _unique_email()
    email_b = _unique_email()
    try:
        _register(client, email_a)
        _register(client, email_b)
        headers_a = {"Authorization": f"Bearer {_login(client, email_a)}"}
        headers_b = {"Authorization": f"Bearer {_login(client, email_b)}"}

        # A opts in.
        client.put(
            "/api/digest/preference", headers=headers_a, json={"opted_in": True}
        )

        # B's preference is unaffected — still off (fail-closed per-user, AD-10).
        r = client.get("/api/digest/preference", headers=headers_b)
        assert r.json() == {"opted_in": False}

        # A still sees its own opt-in.
        r = client.get("/api/digest/preference", headers=headers_a)
        assert r.json() == {"opted_in": True}
    finally:
        _delete_user(email_a)
        _delete_user(email_b)
