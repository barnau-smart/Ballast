"""Integration tests for Story 1.3: login, session, protected route, logout.

These hit a REAL database (no mocks) per AC1 — they require the docker Postgres
(`docker compose up -d db`) running at the default DATABASE_URL. Each test uses
a unique email and cleans up its own row.

What they assert:
- register -> login (OAuth2 form) returns a bearer token,
- that token grants GET /api/users/me (200 + correct user, never the hash),
- no token -> 401,
- wrong password AND unknown email both return the SAME generic 4xx message
  (no user enumeration),
- logout returns success and the honest client contract holds (no token -> 401).

JWT is stateless: we deliberately do NOT assert server-side revocation of an
already-issued token after logout — that would require a denylist (out of scope
for v1). See the story Dev Notes.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from db.connection import get_connection

PASSWORD = "supersecret123"


def _unique_email() -> str:
    return f"login-test-{uuid.uuid4().hex}@example.com"


def _delete_user(email: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM "user" WHERE email = %s', (email,))
        conn.commit()


def _register(client: TestClient, email: str, password: str = PASSWORD) -> None:
    resp = client.post(
        "/api/auth/register",
        json={"email": email, "password": password},
    )
    assert resp.status_code == 201, resp.text


def _login(client: TestClient, email: str, password: str):
    # FastAPI-Users login is an OAuth2 password form: username=email + password.
    return client.post(
        "/api/auth/jwt/login",
        data={"username": email, "password": password},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


@pytest.fixture
def client() -> TestClient:
    with TestClient(create_app()) as c:
        yield c


def test_login_correct_credentials_returns_token(client: TestClient) -> None:
    """Valid credentials return a bearer access token."""
    email = _unique_email()
    try:
        _register(client, email)
        resp = _login(client, email, PASSWORD)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("token_type") == "bearer"
        assert isinstance(body.get("access_token"), str)
        assert body["access_token"]
    finally:
        _delete_user(email)


def test_token_grants_access_to_users_me(client: TestClient) -> None:
    """The issued token reaches GET /api/users/me and returns the right user;
    the response never contains the password hash."""
    email = _unique_email()
    try:
        _register(client, email)
        token = _login(client, email, PASSWORD).json()["access_token"]

        resp = client.get(
            "/api/users/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["email"] == email
        uuid.UUID(body["id"])
        assert body["is_active"] is True
        # Never leak credential material.
        assert "hashed_password" not in resp.text
        assert "password" not in body
    finally:
        _delete_user(email)


def test_users_me_without_token_is_401(client: TestClient) -> None:
    """An unauthenticated request to the protected route is rejected 401."""
    resp = client.get("/api/users/me")
    assert resp.status_code == 401, resp.text


def test_users_me_with_garbage_token_is_401(client: TestClient) -> None:
    """A malformed/invalid bearer token is rejected 401 (not 500)."""
    resp = client.get(
        "/api/users/me",
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert resp.status_code == 401, resp.text


def test_wrong_password_generic_message_no_enumeration(client: TestClient) -> None:
    """A wrong password is rejected with a generic 4xx message that does not
    reveal whether the account exists."""
    email = _unique_email()
    try:
        _register(client, email)
        resp = _login(client, email, "totally-wrong-password")
        assert 400 <= resp.status_code < 500, resp.text
        body = resp.json()
        assert body["error"]["message"] == (
            "That email or password doesn't match. Please try again."
        )
        # Internal machine code never leaked.
        assert "LOGIN_BAD_CREDENTIALS" not in resp.text
    finally:
        _delete_user(email)


def test_unknown_email_returns_same_generic_message(client: TestClient) -> None:
    """An unknown email returns the SAME generic message as a wrong password —
    proving no user enumeration on login."""
    known = _unique_email()
    unknown = _unique_email()
    try:
        _register(client, known)

        wrong_pw = _login(client, known, "totally-wrong-password")
        no_such = _login(client, unknown, PASSWORD)

        assert 400 <= wrong_pw.status_code < 500
        assert 400 <= no_such.status_code < 500
        # Identical status AND identical body — indistinguishable to a caller.
        assert wrong_pw.status_code == no_such.status_code
        assert wrong_pw.json() == no_such.json()
        assert no_such.json()["error"]["message"] == (
            "That email or password doesn't match. Please try again."
        )
    finally:
        _delete_user(known)


def test_logout_succeeds_and_client_contract_holds(client: TestClient) -> None:
    """Logout returns success; the honest client contract is that a request
    with no token is 401 and the login flow still works afterward.

    JWT is stateless — we do NOT assert the already-issued token is
    server-rejected after logout (that would need a denylist, out of scope).
    """
    email = _unique_email()
    try:
        _register(client, email)
        token = _login(client, email, PASSWORD).json()["access_token"]

        logout = client.post(
            "/api/auth/jwt/logout",
            headers={"Authorization": f"Bearer {token}"},
        )
        # FastAPI-Users returns 204 No Content for a successful logout.
        assert logout.status_code in (200, 204), logout.text

        # Client contract: with the token discarded, no-token requests are 401.
        assert client.get("/api/users/me").status_code == 401

        # And the user can log in again cleanly.
        again = _login(client, email, PASSWORD)
        assert again.status_code == 200, again.text
        assert again.json()["access_token"]
    finally:
        _delete_user(email)
