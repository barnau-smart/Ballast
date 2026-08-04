"""Integration tests for POST /api/auth/register.

These hit a REAL database (no mocks) per AC1 — they require the docker Postgres
(`docker compose up -d db`) to be running and reachable at the default
DATABASE_URL. They assert the negative properties that matter for security:
- the stored password is a HASH, not the submitted plaintext (and the hasher
  verifies it),
- the user has a UUID primary key,
- the API response never leaks the password or its hash,
- a duplicate email is rejected in plain language and creates no second row.

Each test uses a unique email and cleans up its own rows.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from db.connection import get_connection

# Story 8.5: this file's whole purpose is auth-persistence SECURITY — it asserts
# the stored password is a valid pwdlib/Argon2 hash the real helper verifies. Opt
# out of the test-only fast hasher (tests/conftest.py) so these run production
# Argon2; the fast hasher would defeat the very property under test.
pytestmark = pytest.mark.real_hasher


def _unique_email() -> str:
    return f"reg-test-{uuid.uuid4().hex}@example.com"


def _fetch_hashed_password(email: str) -> tuple[str, str] | None:
    """Return (id, hashed_password) for the given email, or None.

    Uses the sync psycopg connection (the real DB) so the assertions stay off
    the app's async event loop.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                'SELECT id, hashed_password FROM "user" WHERE email = %s',
                (email,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return str(row[0]), row[1]


def _count_users(email: str) -> int:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT COUNT(*) FROM "user" WHERE email = %s', (email,))
            return cur.fetchone()[0]


def _delete_user(email: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM "user" WHERE email = %s', (email,))
        conn.commit()


@pytest.fixture
def client() -> TestClient:
    # TestClient runs the app's startup (create-all) so the `user` table exists.
    with TestClient(create_app()) as c:
        yield c


def test_register_new_email_persists_hashed_password(client: TestClient) -> None:
    """A new email registers, persists a row with a UUID PK, and the stored
    password is a hash the verifier accepts — never the plaintext."""
    email = _unique_email()
    password = "supersecret123"
    try:
        resp = client.post(
            "/api/auth/register",
            json={"email": email, "password": password},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()

        # UUID primary key returned and parseable.
        assert "id" in body
        uuid.UUID(body["id"])
        assert body["email"] == email

        # Persisted row exists with a UUID PK and a hashed password.
        stored = _fetch_hashed_password(email)
        assert stored is not None
        stored_id, hashed_password = stored
        uuid.UUID(stored_id)  # UUID PK persisted
        assert hashed_password != password, "password stored in plaintext!"

        # The FastAPI-Users password helper verifies the stored hash.
        from fastapi_users.password import PasswordHelper

        helper = PasswordHelper()
        verified, _ = helper.verify_and_update(password, hashed_password)
        assert verified is True
        # A wrong password must not verify.
        wrong, _ = helper.verify_and_update("not-the-password", hashed_password)
        assert wrong is False
    finally:
        _delete_user(email)


def test_register_response_never_contains_password_or_hash(
    client: TestClient,
) -> None:
    """The register response body must not leak the password or its hash."""
    email = _unique_email()
    password = "supersecret123"
    try:
        resp = client.post(
            "/api/auth/register",
            json={"email": email, "password": password},
        )
        assert resp.status_code == 201, resp.text
        raw = resp.text
        body = resp.json()

        assert password not in raw
        assert "password" not in body
        assert "hashed_password" not in body

        stored = _fetch_hashed_password(email)
        assert stored is not None
        _, hashed_password = stored
        assert hashed_password not in raw
    finally:
        _delete_user(email)


@pytest.mark.parametrize("password", ["", "a", "short7!"])
def test_register_short_password_rejected_plain_language(
    client: TestClient, password: str
) -> None:
    """Passwords under the 8-char floor are rejected with a 4xx plain-language
    message and create NO user row (no empty/weak-password accounts)."""
    email = _unique_email()
    try:
        resp = client.post(
            "/api/auth/register",
            json={"email": email, "password": password},
        )
        assert 400 <= resp.status_code < 500, resp.text
        body = resp.json()
        # Warm, plain reason surfaced via the canonical error envelope.
        assert "error" in body
        assert "8 characters" in body["error"]["message"]
        # Internal machine code never leaked.
        assert "REGISTER_INVALID_PASSWORD" not in resp.text
        # No account was created.
        assert _count_users(email) == 0
    finally:
        _delete_user(email)


def test_register_minimum_length_password_succeeds(client: TestClient) -> None:
    """A password exactly at the 8-char floor is accepted."""
    email = _unique_email()
    try:
        resp = client.post(
            "/api/auth/register",
            json={"email": email, "password": "12345678"},
        )
        assert resp.status_code == 201, resp.text
        assert _count_users(email) == 1
    finally:
        _delete_user(email)


def test_register_duplicate_email_rejected_plain_language(
    client: TestClient,
) -> None:
    """A duplicate email is rejected with a 4xx plain-language message and
    creates no second row. The internal machine code is never leaked."""
    email = _unique_email()
    password = "supersecret123"
    try:
        first = client.post(
            "/api/auth/register",
            json={"email": email, "password": password},
        )
        assert first.status_code == 201, first.text

        dup = client.post(
            "/api/auth/register",
            json={"email": email, "password": password},
        )
        assert 400 <= dup.status_code < 500
        body = dup.json()

        # Canonical error envelope with warm, jargon-free copy.
        assert body["error"]["message"] == (
            "An account with that email already exists. Try logging in instead."
        )
        # No leaked internal code / stack trace.
        assert "REGISTER_USER_ALREADY_EXISTS" not in dup.text
        assert "Traceback" not in dup.text

        # No second row created.
        assert _count_users(email) == 1
    finally:
        _delete_user(email)
