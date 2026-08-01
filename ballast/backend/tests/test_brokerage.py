"""Story 2.1 tests — Broker Port, token encryption, encrypted per-user storage,
and the fake OAuth link flow. REAL DB (no mocks) for the storage/flow tests,
matching the Epic 1 test style.

Requires the docker Postgres (`docker compose up -d db`). Each test uses unique
users and cleans up its own rows. NO real Schwab network is ever exercised.
"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from api.app import create_app
from brokers.crypto import (
    TokenEncryptionError,
    decrypt_token,
    encrypt_token,
)
from brokers.fake_adapter import (
    FAKE_ACCESS_TOKEN,
    FAKE_REFRESH_TOKEN,
    FakeBrokerAdapter,
)
from brokers.factory import get_broker
from brokers.port import BrokerPort, BrokerTokens
from db.connection import get_connection
from db.models import BrokerageToken
from db.repository import ScopedRepository
from db.scope import Scope
from db.session import async_session_maker, engine

PASSWORD = "supersecret123"


@pytest_asyncio.fixture(autouse=True)
async def ensure_brokerage_table():
    """Ensure the brokerage_token table exists for the repo-level tests.

    The API-flow tests get it from the app's create-all on TestClient startup,
    but the pure repo tests do not spin up the app, so create it here (idempotent
    — never dropped, matching the app's create-all lifecycle).
    """
    async with engine.begin() as conn:
        await conn.run_sync(BrokerageToken.__table__.create, checkfirst=True)
    yield


# --- helpers -----------------------------------------------------------------


def _unique_email() -> str:
    return f"brokerage-test-{uuid.uuid4().hex}@example.com"


def _delete_user(email: str) -> None:
    # ON DELETE CASCADE removes any brokerage_token rows too.
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM "user" WHERE email = %s', (email,))
        conn.commit()


def _make_user_direct() -> uuid.UUID:
    """Insert a user row directly (for repo-level isolation tests)."""
    user_id = uuid.uuid4()
    email = f"brokerage-iso-{user_id.hex}@example.com"
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


# --- Encryption round-trip ---------------------------------------------------


def test_encrypt_roundtrip_and_ciphertext_differs():
    plaintext = "super-secret-access-token"
    ct = encrypt_token(plaintext)
    assert ct != plaintext  # never store plaintext
    assert plaintext not in ct
    assert decrypt_token(ct) == plaintext


def test_encrypt_is_nondeterministic_but_decrypts():
    # Fernet embeds a random IV, so two ciphertexts differ yet both decrypt.
    a = encrypt_token("x")
    b = encrypt_token("x")
    assert a != b
    assert decrypt_token(a) == "x" == decrypt_token(b)


def test_missing_key_raises(monkeypatch):
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "")
    with pytest.raises(TokenEncryptionError):
        encrypt_token("x")


def test_invalid_key_raises(monkeypatch):
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "not-a-valid-fernet-key")
    with pytest.raises(TokenEncryptionError):
        encrypt_token("x")


def test_decrypt_tampered_raises():
    ct = encrypt_token("hello")
    tampered = ct[:-2] + ("AA" if not ct.endswith("AA") else "BB")
    with pytest.raises(TokenEncryptionError):
        decrypt_token(tampered)


# --- Port / adapters ---------------------------------------------------------


def test_fake_adapter_is_broker_port():
    adapter = FakeBrokerAdapter()
    assert isinstance(adapter, BrokerPort)
    url = adapter.authorization_url("state-xyz")
    assert "state-xyz" in url
    tokens = adapter.exchange_code("any-code", "state-xyz")
    assert isinstance(tokens, BrokerTokens)
    assert tokens.access_token == FAKE_ACCESS_TOKEN
    assert tokens.refresh_token == FAKE_REFRESH_TOKEN
    assert tokens.expires_at.tzinfo is not None  # UTC-aware


def test_factory_defaults_to_fake(monkeypatch):
    monkeypatch.delenv("BROKER_ADAPTER", raising=False)
    assert isinstance(get_broker(), FakeBrokerAdapter)


def test_factory_selects_fake_explicitly(monkeypatch):
    monkeypatch.setenv("BROKER_ADAPTER", "fake")
    assert isinstance(get_broker(), FakeBrokerAdapter)


def test_schwab_import_does_not_crash():
    # Importing the schwab adapter module must never crash (schwab-py is lazy).
    from brokers.schwab_adapter import SchwabAdapter  # noqa: F401


def test_schwab_without_creds_raises_config_error(monkeypatch):
    from brokers.schwab_adapter import SchwabAdapter, SchwabNotConfiguredError

    monkeypatch.setenv("SCHWAB_CLIENT_ID", "")
    monkeypatch.setenv("SCHWAB_CLIENT_SECRET", "")
    monkeypatch.setenv("SCHWAB_CALLBACK_URL", "")
    with pytest.raises(SchwabNotConfiguredError):
        SchwabAdapter()


def test_factory_schwab_without_creds_raises(monkeypatch):
    from brokers.schwab_adapter import SchwabNotConfiguredError

    monkeypatch.setenv("BROKER_ADAPTER", "schwab")
    monkeypatch.setenv("SCHWAB_CLIENT_ID", "")
    monkeypatch.setenv("SCHWAB_CLIENT_SECRET", "")
    monkeypatch.setenv("SCHWAB_CALLBACK_URL", "")
    with pytest.raises(SchwabNotConfiguredError):
        get_broker()


def test_schwab_token_normalization_absolute_and_relative():
    """M1: SchwabAdapter._to_broker_tokens normalizes both an absolute
    ``expires_at`` epoch and a relative ``expires_in`` — the credential-gated
    network path is untestable, but this pure transform is not."""
    from datetime import datetime, timezone

    from brokers.schwab_adapter.adapter import SchwabAdapter

    # Absolute epoch form.
    epoch = 1_900_000_000
    abs_tokens = SchwabAdapter._to_broker_tokens(
        {"access_token": "a", "refresh_token": "r", "expires_at": epoch}
    )
    assert abs_tokens.access_token == "a"
    assert abs_tokens.refresh_token == "r"
    assert abs_tokens.expires_at == datetime.fromtimestamp(epoch, tz=timezone.utc)
    assert abs_tokens.expires_at.tzinfo is not None

    # Relative form: expires_at ~= now + expires_in (UTC-aware).
    rel_tokens = SchwabAdapter._to_broker_tokens(
        {"access_token": "a2", "refresh_token": "r2", "expires_in": 3600}
    )
    now = datetime.now(timezone.utc)
    delta = (rel_tokens.expires_at - now).total_seconds()
    assert 3500 < delta <= 3600
    assert rel_tokens.expires_at.tzinfo is not None


# --- Encrypted per-user storage (real DB) ------------------------------------


@pytest_asyncio.fixture
async def two_owner_ids():
    a = _make_user_direct()
    b = _make_user_direct()
    try:
        yield a, b
    finally:
        _delete_user_by_id(a)
        _delete_user_by_id(b)


@pytest.mark.asyncio
async def test_stored_token_is_ciphertext_and_reads_back(two_owner_ids):
    a, _ = two_owner_ids
    plaintext = FAKE_ACCESS_TOKEN
    async with async_session_maker() as session:
        repo = ScopedRepository(BrokerageToken, Scope.for_user(a), session)
        row = await repo.add(
            provider="fake",
            access_token=encrypt_token(plaintext),
            refresh_token=encrypt_token(FAKE_REFRESH_TOKEN),
            expires_at=FakeBrokerAdapter().exchange_code("c", "s").expires_at,
        )
        await session.commit()
        row_id = row.id

    # Read the RAW column value straight from Postgres: it must be ciphertext.
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT access_token FROM brokerage_token WHERE id = %s",
                (str(row_id),),
            )
            (raw_col,) = cur.fetchone()
    assert raw_col != plaintext, "DB column must NOT equal the plaintext token"
    assert plaintext not in raw_col
    # And it decrypts back to the original plaintext.
    assert decrypt_token(raw_col) == plaintext


@pytest.mark.asyncio
async def test_user_a_cannot_read_user_b_token(two_owner_ids):
    a, b = two_owner_ids
    async with async_session_maker() as session:
        repo_b = ScopedRepository(BrokerageToken, Scope.for_user(b), session)
        b_row = await repo_b.add(
            provider="fake",
            access_token=encrypt_token("b-access"),
            refresh_token=encrypt_token("b-refresh"),
            expires_at=FakeBrokerAdapter().exchange_code("c", "s").expires_at,
        )
        await session.commit()
        b_row_id = b_row.id

    async with async_session_maker() as session:
        repo_a = ScopedRepository(BrokerageToken, Scope.for_user(a), session)
        # A sees no rows and cannot fetch B's specific row.
        assert await repo_a.list() == []
        assert await repo_a.get(b_row_id) is None
        # B can still read its own.
        repo_b = ScopedRepository(BrokerageToken, Scope.for_user(b), session)
        got = await repo_b.get(b_row_id)
        assert got is not None and got.owner_id == b


# --- Full fake link flow via the API (real DB) -------------------------------


def test_full_fake_link_flow(client):
    email = _unique_email()
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}

        # 1. status: not linked yet.
        s0 = client.get("/api/brokerage/status", headers=headers)
        assert s0.status_code == 200, s0.text
        assert s0.json()["linked"] is False
        assert s0.json()["state"] == "unlinked"
        assert s0.json()["provider"] is None

        # 2. authorize: url contains the state; response returns the state.
        a = client.get("/api/brokerage/authorize", headers=headers)
        assert a.status_code == 200, a.text
        body = a.json()
        state = body["state"]
        assert state
        assert state in body["authorization_url"]

        # 3. callback: exchange code + store tokens.
        cb = client.post(
            "/api/brokerage/callback",
            json={"code": "fake-oauth-code", "state": state},
            headers=headers,
        )
        assert cb.status_code == 200, cb.text
        assert cb.json() == {"linked": True}

        # 4. status: now linked; token values never leaked.
        s1 = client.get("/api/brokerage/status", headers=headers)
        assert s1.status_code == 200, s1.text
        assert s1.json()["linked"] is True
        assert s1.json()["provider"] == "fake"
        assert FAKE_ACCESS_TOKEN not in s1.text
        assert FAKE_REFRESH_TOKEN not in s1.text

        # 5. proof at rest: the stored column is ciphertext, not plaintext.
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT bt.access_token FROM brokerage_token bt "
                    'JOIN "user" u ON u.id = bt.owner_id WHERE u.email = %s',
                    (email,),
                )
                (raw_col,) = cur.fetchone()
        assert raw_col != FAKE_ACCESS_TOKEN
        assert decrypt_token(raw_col) == FAKE_ACCESS_TOKEN
    finally:
        _delete_user(email)


def test_callback_rejects_bad_state(client):
    email = _unique_email()
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        cb = client.post(
            "/api/brokerage/callback",
            json={"code": "fake-oauth-code", "state": "forged.deadbeef"},
            headers=headers,
        )
        assert cb.status_code == 400, cb.text
        # No token stored.
        s = client.get("/api/brokerage/status", headers=headers)
        assert s.json()["linked"] is False
    finally:
        _delete_user(email)


def test_state_from_other_user_is_rejected(client):
    """A state minted for user A cannot be used by user B (no cross-user CSRF)."""
    email_a = _unique_email()
    email_b = _unique_email()
    try:
        _register(client, email_a)
        _register(client, email_b)
        token_a = _login(client, email_a)
        token_b = _login(client, email_b)

        a_state = client.get(
            "/api/brokerage/authorize",
            headers={"Authorization": f"Bearer {token_a}"},
        ).json()["state"]

        # B tries to use A's state.
        cb = client.post(
            "/api/brokerage/callback",
            json={"code": "fake-oauth-code", "state": a_state},
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert cb.status_code == 400, cb.text
    finally:
        _delete_user(email_a)
        _delete_user(email_b)


def test_endpoints_require_auth(client):
    assert client.get("/api/brokerage/status").status_code == 401
    assert client.get("/api/brokerage/authorize").status_code == 401
    assert (
        client.post(
            "/api/brokerage/callback", json={"code": "c", "state": "s"}
        ).status_code
        == 401
    )


# --- Story 6.3: get_execution_broker token-binding seam ----------------------


@pytest.mark.asyncio
async def test_get_execution_broker_passes_fake_through():
    """A non-Schwab adapter (the fake/spy) passes straight through untouched.

    This is what keeps the existing ``dependency_overrides[get_broker]`` in the
    coach-api tests working: an injected fake is not a SchwabAdapter, so
    get_execution_broker returns it as-is without any DB / token work.
    """
    from brokers.factory import get_execution_broker

    fake = FakeBrokerAdapter()
    result = await get_execution_broker(scope=None, session=None, broker=fake)
    assert result is fake


@pytest.mark.asyncio
async def test_get_execution_broker_binds_decrypted_schwab_token(monkeypatch):
    """The Schwab path loads the user's token, decrypts it, and binds a
    token_read_func onto a fresh SchwabAdapter (offline — repo + decrypt mocked)."""
    from datetime import datetime, timezone
    from types import SimpleNamespace

    import brokers.crypto as crypto_mod
    import db.repository as repo_mod
    from brokers.factory import get_execution_broker
    from brokers.schwab_adapter import SchwabAdapter

    monkeypatch.setenv("SCHWAB_CLIENT_ID", "id")
    monkeypatch.setenv("SCHWAB_CLIENT_SECRET", "secret")
    monkeypatch.setenv("SCHWAB_CALLBACK_URL", "https://example.com/cb")

    row = SimpleNamespace(
        access_token="enc-access",
        refresh_token="enc-refresh",
        expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )

    class _FakeRepo:
        def __init__(self, model, scope, session):
            pass

        async def list(self):
            return [row]

    monkeypatch.setattr(repo_mod, "ScopedRepository", _FakeRepo)
    monkeypatch.setattr(crypto_mod, "decrypt_token", lambda ct: "plain-" + ct)

    schwab = SchwabAdapter()
    bound = await get_execution_broker(
        scope=object(), session=object(), broker=schwab
    )

    assert isinstance(bound, SchwabAdapter)
    token = bound._token_read_func()
    assert token["access_token"] == "plain-enc-access"
    assert token["refresh_token"] == "plain-enc-refresh"
    assert token["token_type"] == "Bearer"
    assert token["expires_at"] == int(row.expires_at.timestamp())
