"""Story 2.2 tests — brokerage session status, the degraded-mode gate, and
resume-in-place ``return_to`` validation. REAL DB (no mocks) for the status /
gate tests, matching the Epic 1 / Story 2.1 style.

Requires the docker Postgres (`docker compose up -d db`). Each test uses unique
users and cleans up its own rows. NO real Schwab network is ever exercised.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from fastapi import Depends
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import (
    RECONNECT_MESSAGE,
    get_scope,
    require_live_broker_session,
)
from api.brokerage import sanitize_return_to
from brokers.crypto import encrypt_token
from brokers.session import BrokerageSession, compute_state, get_brokerage_session
from db.connection import get_connection
from db.models import BrokerageToken
from db.repository import ScopedRepository
from db.scope import Scope
from db.session import async_session_maker, engine

PASSWORD = "supersecret123"


@pytest_asyncio.fixture(autouse=True)
async def ensure_brokerage_table():
    """Ensure the brokerage_token table exists for the repo-level tests."""
    async with engine.begin() as conn:
        await conn.run_sync(BrokerageToken.__table__.create, checkfirst=True)
    yield


# --- helpers -----------------------------------------------------------------


def _unique_email() -> str:
    return f"session-test-{uuid.uuid4().hex}@example.com"


def _delete_user(email: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM "user" WHERE email = %s', (email,))
        conn.commit()


def _make_user_direct() -> uuid.UUID:
    user_id = uuid.uuid4()
    email = f"session-iso-{user_id.hex}@example.com"
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


async def _insert_token(owner: uuid.UUID, expires_at: datetime) -> None:
    """Insert a brokerage token for ``owner`` via the fail-closed scoped repo."""
    async with async_session_maker() as session:
        repo = ScopedRepository(BrokerageToken, Scope.for_user(owner), session)
        await repo.add(
            provider="fake",
            access_token=encrypt_token("access"),
            refresh_token=encrypt_token("refresh"),
            expires_at=expires_at,
        )
        await session.commit()


def _user_id_for(email: str) -> uuid.UUID:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT id FROM "user" WHERE email = %s', (email,))
            (uid_raw,) = cur.fetchone()
    return uuid.UUID(str(uid_raw))


def _insert_token_sync(owner: uuid.UUID, expires_at: datetime) -> None:
    """Insert a brokerage token via psycopg (sync) for tests running under a
    TestClient event loop. Token columns hold ciphertext (encrypt is pure/sync);
    the app reads it back through the same scoped repo path.
    """
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


@pytest_asyncio.fixture
async def two_owner_ids():
    a = _make_user_direct()
    b = _make_user_direct()
    try:
        yield a, b
    finally:
        _delete_user_by_id(a)
        _delete_user_by_id(b)


# --- Pure state computation --------------------------------------------------


def test_compute_state_unlinked_when_no_token():
    assert compute_state(None) == "unlinked"


def test_compute_state_live_when_future():
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    assert compute_state(future) == "live"


def test_compute_state_expired_when_past():
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    assert compute_state(past) == "expired"


def test_compute_state_treats_naive_as_utc():
    past_naive = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(tzinfo=None)
    assert compute_state(past_naive) == "expired"


# --- Session state via scoped repo (real DB) ---------------------------------


@pytest.mark.asyncio
async def test_session_unlinked_live_expired(two_owner_ids):
    a, _ = two_owner_ids
    async with async_session_maker() as session:
        s = await get_brokerage_session(Scope.for_user(a), session)
    assert s == BrokerageSession(state="unlinked")
    assert s.is_live is False

    future = datetime.now(timezone.utc) + timedelta(days=1)
    await _insert_token(a, future)
    async with async_session_maker() as session:
        s = await get_brokerage_session(Scope.for_user(a), session)
    assert s.state == "live"
    assert s.is_live is True
    assert s.provider == "fake"
    assert s.expires_at is not None


@pytest.mark.asyncio
async def test_session_expired_with_past_expiry(two_owner_ids):
    a, _ = two_owner_ids
    past = datetime.now(timezone.utc) - timedelta(days=1)
    await _insert_token(a, past)
    async with async_session_maker() as session:
        s = await get_brokerage_session(Scope.for_user(a), session)
    assert s.state == "expired"
    assert s.is_live is False


@pytest.mark.asyncio
async def test_session_is_per_user(two_owner_ids):
    """A's status must NEVER reflect B's token (fail-closed scoping)."""
    a, b = two_owner_ids
    await _insert_token(b, datetime.now(timezone.utc) + timedelta(days=1))
    async with async_session_maker() as session:
        s_a = await get_brokerage_session(Scope.for_user(a), session)
        s_b = await get_brokerage_session(Scope.for_user(b), session)
    assert s_a.state == "unlinked"  # A has no token of its own
    assert s_b.state == "live"


# --- Status endpoint over the wire (real DB) ---------------------------------


def test_status_endpoint_states(client):
    email = _unique_email()
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}

        # Unlinked.
        r = client.get("/api/brokerage/status", headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "unlinked"
        assert r.json()["linked"] is False
        assert r.json()["expires_at"] is None

        # Live: run the fake link flow.
        state = client.get("/api/brokerage/authorize", headers=headers).json()[
            "state"
        ]
        cb = client.post(
            "/api/brokerage/callback",
            json={"code": "c", "state": state},
            headers=headers,
        )
        assert cb.status_code == 200, cb.text
        r = client.get("/api/brokerage/status", headers=headers)
        assert r.json()["state"] == "live"
        assert r.json()["linked"] is True
        assert r.json()["expires_at"] is not None
        # Never a token value.
        assert "access" not in r.text and "refresh" not in r.text
    finally:
        _delete_user(email)


def test_status_endpoint_reports_expired(client):
    """Insert an expired token for the user, confirm status='expired'."""
    email = _unique_email()
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}

        # Insert an expired token for this user (sync path, no event loop).
        _insert_token_sync(
            _user_id_for(email), datetime.now(timezone.utc) - timedelta(days=1)
        )

        r = client.get("/api/brokerage/status", headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "expired"
        assert r.json()["linked"] is True
    finally:
        _delete_user(email)


# --- The degraded-mode gate --------------------------------------------------


def _gate_app():
    """A tiny app exposing endpoints that exercise the gate directly.

    ``/exec`` is guarded by ``require_live_broker_session`` (execution-class);
    ``/read`` depends only on ``get_scope`` (read/coach) and is NEVER gated.
    Marked clearly as a test-only harness for the Story 2.2 gate.
    """
    app = create_app()

    @app.post("/api/_test/exec")
    async def _exec(
        broker_session: BrokerageSession = Depends(require_live_broker_session),
    ):
        return {"placed": True, "state": broker_session.state}

    @app.get("/api/_test/read")
    async def _read(scope: Scope = Depends(get_scope)):
        # A read/coach-style endpoint: works regardless of session state.
        return {"ok": True}

    return app


@pytest.fixture
def gate_client() -> TestClient:
    with TestClient(_gate_app()) as c:
        yield c


def test_gate_allows_when_live(gate_client):
    email = _unique_email()
    try:
        _register(gate_client, email)
        token = _login(gate_client, email)
        headers = {"Authorization": f"Bearer {token}"}
        # Link (fake) → live.
        state = gate_client.get(
            "/api/brokerage/authorize", headers=headers
        ).json()["state"]
        gate_client.post(
            "/api/brokerage/callback",
            json={"code": "c", "state": state},
            headers=headers,
        )
        r = gate_client.post("/api/_test/exec", headers=headers)
        assert r.status_code == 200, r.text
        assert r.json() == {"placed": True, "state": "live"}
    finally:
        _delete_user(email)


def test_gate_blocks_when_unlinked_with_plain_message(gate_client):
    email = _unique_email()
    try:
        _register(gate_client, email)
        token = _login(gate_client, email)
        headers = {"Authorization": f"Bearer {token}"}
        r = gate_client.post("/api/_test/exec", headers=headers)
        assert r.status_code == 409, r.text
        # Plain-language message via the error envelope; no error dump.
        assert r.json()["error"]["message"] == RECONNECT_MESSAGE
        # Read/coach still works while unlinked (degraded mode).
        assert gate_client.get("/api/_test/read", headers=headers).status_code == 200
    finally:
        _delete_user(email)


def test_gate_blocks_when_expired_but_read_works(gate_client):
    email = _unique_email()
    try:
        _register(gate_client, email)
        token = _login(gate_client, email)
        headers = {"Authorization": f"Bearer {token}"}
        _insert_token_sync(
            _user_id_for(email), datetime.now(timezone.utc) - timedelta(days=1)
        )
        # Execution blocked with the calm message...
        r = gate_client.post("/api/_test/exec", headers=headers)
        assert r.status_code == 409, r.text
        assert r.json()["error"]["message"] == RECONNECT_MESSAGE
        # ...but read/coach stays usable in degraded mode (AD-11).
        assert gate_client.get("/api/_test/read", headers=headers).status_code == 200
    finally:
        _delete_user(email)


# --- Re-auth restores live (fake) --------------------------------------------


def test_reauth_restores_live_after_expiry(client):
    email = _unique_email()
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        _insert_token_sync(
            _user_id_for(email), datetime.now(timezone.utc) - timedelta(days=1)
        )
        assert client.get("/api/brokerage/status", headers=headers).json()[
            "state"
        ] == "expired"

        # Re-auth reuses the 2.1 link flow; callback overwrites the old token.
        state = client.get("/api/brokerage/authorize", headers=headers).json()[
            "state"
        ]
        cb = client.post(
            "/api/brokerage/callback",
            json={"code": "c", "state": state},
            headers=headers,
        )
        assert cb.status_code == 200, cb.text
        assert client.get("/api/brokerage/status", headers=headers).json()[
            "state"
        ] == "live"
    finally:
        _delete_user(email)


# --- return_to allowlist (resume in place, no open redirect) ------------------


@pytest.mark.parametrize(
    "path", ["/", "/dashboard", "/coach", "/decisions", "/settings", "/onboarding"]
)
def test_sanitize_return_to_accepts_in_app_paths(path):
    assert sanitize_return_to(path) == path


@pytest.mark.parametrize(
    "bad",
    [
        "https://evil.com",
        "http://evil.com/coach",
        "//evil.com",
        "/../../etc/passwd",
        "coach",  # not absolute in-app path
        "/unknown-route",
        "javascript:alert(1)",
        "\\evil.com",
        None,
        "",
    ],
)
def test_sanitize_return_to_rejects_external_or_unknown(bad):
    assert sanitize_return_to(bad) is None


def test_authorize_echoes_valid_return_to_and_drops_external(client):
    email = _unique_email()
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}

        ok = client.get(
            "/api/brokerage/authorize", params={"return_to": "/coach"}, headers=headers
        )
        assert ok.json()["return_to"] == "/coach"

        evil = client.get(
            "/api/brokerage/authorize",
            params={"return_to": "https://evil.com"},
            headers=headers,
        )
        assert evil.json()["return_to"] is None
    finally:
        _delete_user(email)
