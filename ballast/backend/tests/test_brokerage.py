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
from brokers.factory import (
    OperatorTokenBindError,
    bind_operator_token,
    get_broker,
)
from brokers.port import BrokerPort, BrokerTokens
from db.connection import get_connection
from db.models import BrokerageToken, PortfolioBalance, PortfolioCache
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
        # The broker that minted the URL (fake) — lets the SPA complete the link
        # in-app instead of navigating to the fake adapter's stub authorize page.
        assert body["provider"] == "fake"

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


# --- Story 7.5: re-link clears the two-table projection ----------------------


def _user_id_for(email: str) -> uuid.UUID:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT id FROM "user" WHERE email = %s', (email,))
            (uid_raw,) = cur.fetchone()
    return uuid.UUID(str(uid_raw))


def _seed_stale_projection(owner: uuid.UUID) -> None:
    """Seed a PRIOR account's holdings + balance rows (values distinct from the
    fake adapter's) so a re-link can be proven to CLEAR then repopulate them."""
    import datetime as _dt

    stale_as_of = _dt.datetime(2020, 1, 1, tzinfo=_dt.timezone.utc)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO portfolio_cache "
                "(id, owner_id, symbol, quantity, market_value, cost_basis, cash, as_of) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    str(uuid.uuid4()),
                    str(owner),
                    "STALE",
                    "9",
                    "9999.00",
                    "9000.00",
                    "0",
                    stale_as_of,
                ),
            )
            cur.execute(
                "INSERT INTO portfolio_balance (id, owner_id, cash, as_of) "
                "VALUES (%s, %s, %s, %s)",
                (str(uuid.uuid4()), str(owner), "4242.42", stale_as_of),
            )
        conn.commit()


def _read_projection(owner: uuid.UUID):
    """Return (cache_symbols, balance_cash) for a user, read directly via psycopg."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT symbol FROM portfolio_cache WHERE owner_id = %s",
                (str(owner),),
            )
            symbols = sorted(r[0] for r in cur.fetchall())
            cur.execute(
                "SELECT cash FROM portfolio_balance WHERE owner_id = %s",
                (str(owner),),
            )
            balances = [r[0] for r in cur.fetchall()]
    return symbols, balances


def test_relink_clears_stale_projection_and_repopulates(client):
    """A re-link deletes the PRIOR account's portfolio_cache + portfolio_balance in
    the token-replacement commit, and the fresh import repopulates from the new
    account — the stale rows never survive."""
    from brokers.fake_adapter import FAKE_CASH, FAKE_HOLDINGS

    email = _unique_email()
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        owner = _user_id_for(email)

        # Simulate a prior link's projection.
        _seed_stale_projection(owner)
        symbols, balances = _read_projection(owner)
        assert symbols == ["STALE"]
        assert balances and balances[0] == __import__("decimal").Decimal("4242.42")

        # Re-link (different account, in this test the fake).
        state = client.get("/api/brokerage/authorize", headers=headers).json()["state"]
        cb = client.post(
            "/api/brokerage/callback",
            json={"code": "c", "state": state},
            headers=headers,
        )
        assert cb.status_code == 200, cb.text

        # The stale "STALE"/4242.42 rows are gone; the fresh import populated the
        # fake account's holdings + cash instead.
        symbols, balances = _read_projection(owner)
        assert symbols == sorted(h.symbol for h in FAKE_HOLDINGS)
        assert "STALE" not in symbols
        assert balances and balances[0] == FAKE_CASH
    finally:
        _delete_user(email)


class _FetchFailsAdapter(BrokerPort):
    """Links fine, but the portfolio fetch raises — the re-link projection clear
    must still leave an EMPTY (honest) projection, never the prior account's data."""

    provider = "fake"

    def authorization_url(self, state: str) -> str:
        return FakeBrokerAdapter().authorization_url(state)

    def exchange_code(self, code: str, state: str) -> BrokerTokens:
        return FakeBrokerAdapter().exchange_code(code, state)

    def fetch_portfolio(self):
        raise RuntimeError("simulated broker fetch failure")

    async def place_order(self, order_intent, *, idempotency_key):
        return await FakeBrokerAdapter().place_order(
            order_intent, idempotency_key=idempotency_key
        )

    async def get_order_status(self, idempotency_key):
        return await FakeBrokerAdapter().get_order_status(idempotency_key)

    async def get_order_status_by_ref(self, broker_ref):
        return await FakeBrokerAdapter().get_order_status_by_ref(broker_ref)

    async def cancel_order(self, broker_ref):
        return await FakeBrokerAdapter().cancel_order(broker_ref)

    async def get_quote(self, symbol):
        return await FakeBrokerAdapter().get_quote(symbol)


def test_relink_import_failure_leaves_empty_projection(client):
    """When the post-clear import fails on re-link, the projection is EMPTY (the
    honest state) — never the prior account's stale cash/holdings."""
    email = _unique_email()
    app = client.app
    app.dependency_overrides[get_broker] = lambda: _FetchFailsAdapter()
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        owner = _user_id_for(email)

        _seed_stale_projection(owner)

        state = client.get("/api/brokerage/authorize", headers=headers).json()["state"]
        cb = client.post(
            "/api/brokerage/callback",
            json={"code": "c", "state": state},
            headers=headers,
        )
        # Link survives the import failure (no 500).
        assert cb.status_code == 200, cb.text
        assert cb.json()["linked"] is True

        # The projection is EMPTY — the stale rows were cleared in the token
        # commit and the failed import repopulated nothing.
        symbols, balances = _read_projection(owner)
        assert symbols == []
        assert balances == []
    finally:
        app.dependency_overrides.pop(get_broker, None)
        _delete_user(email)


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
    # schwab-py 1.5.x metadata-wrapped envelope: {creation_timestamp, token:{...}}.
    assert set(token) == {"creation_timestamp", "token"}
    inner = token["token"]
    assert inner["access_token"] == "plain-enc-access"
    assert inner["refresh_token"] == "plain-enc-refresh"
    assert inner["token_type"] == "Bearer"
    assert inner["expires_at"] == int(row.expires_at.timestamp())
    assert isinstance(token["creation_timestamp"], int)


# --- Story 7.3: undecryptable-token → calm 409 reconnect at the shared seam ----


def _stub_undecryptable_token_repo(monkeypatch):
    """Wire an offline SchwabAdapter whose ONE stored token cannot be decrypted.

    Mirrors ``test_get_execution_broker_binds_decrypted_schwab_token`` (repo +
    decrypt mocked, no DB, no SDK) but forces ``decrypt_token`` to raise
    ``TokenEncryptionError`` — the exact fault a rotated ``TOKEN_ENCRYPTION_KEY``
    or corrupt ciphertext produces DURING FastAPI dependency resolution.
    """
    from datetime import datetime, timezone
    from types import SimpleNamespace

    import brokers.crypto as crypto_mod
    import db.repository as repo_mod
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

    def _boom(_ct):
        raise TokenEncryptionError(
            "Could not decrypt the stored token (wrong key or tampered data)."
        )

    monkeypatch.setattr(repo_mod, "ScopedRepository", _FakeRepo)
    monkeypatch.setattr(crypto_mod, "decrypt_token", _boom)

    return SchwabAdapter()


@pytest.mark.asyncio
async def test_get_execution_broker_undecryptable_token_is_calm_409(monkeypatch):
    """An undecryptable stored token on the placement seam raises a calm 409.

    The decrypt failure fires DURING dependency resolution (before any handler
    ``try/except``); the shared ``_bind_user_token`` choke point converts it into
    the same ``RECONNECT_MESSAGE`` envelope the entry gate uses — never a raw 500.
    """
    from fastapi import HTTPException

    from api.deps import RECONNECT_MESSAGE
    from brokers.factory import get_execution_broker

    schwab = _stub_undecryptable_token_repo(monkeypatch)
    with pytest.raises(HTTPException) as exc_info:
        await get_execution_broker(scope=object(), session=object(), broker=schwab)
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == RECONNECT_MESSAGE


@pytest.mark.asyncio
async def test_get_reading_broker_undecryptable_token_is_calm_409(monkeypatch):
    """Same calm 409 on the READ/refresh seam — the one choke point covers both.

    ``get_reading_broker`` (``/refresh``) delegates to the SAME
    ``_bind_user_token`` as ``get_execution_broker`` (``/approve`` + reconcile),
    so a single decrypt guard fixes all three ledger sites.
    """
    from fastapi import HTTPException

    from api.deps import RECONNECT_MESSAGE
    from brokers.factory import get_reading_broker

    schwab = _stub_undecryptable_token_repo(monkeypatch)
    with pytest.raises(HTTPException) as exc_info:
        await get_reading_broker(scope=object(), session=object(), broker=schwab)
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == RECONNECT_MESSAGE


@pytest.mark.asyncio
async def test_reading_broker_passes_fake_through_no_decrypt():
    """A non-Schwab adapter passes straight through the reading seam untouched.

    No repo/decrypt work happens for the fake/spy — the decrypt guard is only
    ever reachable on the real Schwab path, so the credential-free tested path is
    unaffected.
    """
    from brokers.factory import get_reading_broker

    fake = FakeBrokerAdapter()
    result = await get_reading_broker(scope=None, session=None, broker=fake)
    assert result is fake


# --- Story 6.7: fake adapter durable reconcile-by-ref + sole-caller ------------


@pytest.mark.asyncio
async def test_fake_get_order_status_by_ref_placed_resolves():
    # A placement records the order under its broker_ref, so a durable reconcile
    # keyed on that ref returns the recorded outcome (the cross-request path).
    from decimal import Decimal

    from coach.recommendation import OrderIntent, OrderSide

    adapter = FakeBrokerAdapter()
    intent = OrderIntent(symbol="VTI", side=OrderSide.BUY, amount=Decimal("500"))
    placed = await adapter.place_order(intent, idempotency_key="ref-key")

    resolved = await adapter.get_order_status_by_ref(placed.broker_ref)
    assert resolved == placed  # recorded outcome round-trips by ref


@pytest.mark.asyncio
async def test_fake_get_order_status_by_ref_seed_helper_resolves():
    # The test-seed helper registers the broker_ref → OrderOutcome a LATER reconcile
    # (a fresh adapter that never placed the order) should observe.
    from decimal import Decimal

    from brokers.port import OrderOutcome, OrderStatus

    adapter = FakeBrokerAdapter()
    filled = OrderOutcome(
        status=OrderStatus.FILLED,
        filled_qty=Decimal("2"),
        avg_price=Decimal("100.00"),
        broker_ref="42",
    )
    adapter.seed_order_status_by_ref("42", filled)

    resolved = await adapter.get_order_status_by_ref("42")
    assert resolved == filled


@pytest.mark.asyncio
async def test_fake_get_order_status_by_ref_unknown_is_pending():
    # An unknown / None ref → honest PENDING; the fake never invents a fill and
    # never searches.
    from decimal import Decimal

    from brokers.port import OrderStatus

    adapter = FakeBrokerAdapter()
    for ref in ("never-placed", None):
        outcome = await adapter.get_order_status_by_ref(ref)  # type: ignore[arg-type]
        assert outcome.status is OrderStatus.PENDING
        assert outcome.filled_qty == Decimal("0")
        assert outcome.avg_price is None
        assert outcome.broker_ref is None


def test_sole_caller_of_get_order_status_by_ref():
    # AD-7: the ONLY module that calls BrokerPort.get_order_status_by_ref is the
    # Coach Engine execution owner (coach/execution.py). No API handler, pipeline,
    # or other module may call it — mirroring the place_order/get_order_status
    # canary. The port ABC and the two adapter implementations declare/define it;
    # the sole CALLER is coach/execution.py.
    import pathlib

    backend = pathlib.Path(__file__).resolve().parent.parent
    allowed = {
        "brokers/port.py",  # abstract declaration
        "brokers/fake_adapter.py",  # implementation
        "brokers/schwab_adapter/adapter.py",  # implementation
        "coach/execution.py",  # the sole caller
    }
    offenders: list[str] = []
    for path in backend.rglob("*.py"):
        rel = path.relative_to(backend)
        parts = rel.parts
        if parts and parts[0] in {"tests", ".venv"}:
            continue
        if rel.as_posix() in allowed:
            continue
        if ".get_order_status_by_ref(" in path.read_text(encoding="utf-8"):
            offenders.append(rel.as_posix())
    assert offenders == [], (
        "get_order_status_by_ref must be called ONLY by coach.execution (AD-7). "
        f"Unexpected callers: {offenders}"
    )


# =============================================================================
# Operator token binding for the read-only pre-flight harness (Story 7.6)
#
# bind_operator_token gives the CLI harness (no HTTP request / user scope) the
# single linked operator's decrypted Schwab token, so the four Schwab read seams
# actually drive live instead of failing with "no token bound". These are the
# offline tests for that binder (2026-08-06 follow-up closing the CLI gap).
# =============================================================================


def _clear_all_brokerage_tokens() -> None:
    """Empty the brokerage_token table for a deterministic global-query start.

    bind_operator_token reads across ALL owners (it expects exactly one linked
    account), so these tests control the whole table. The suite is serial and
    every other test cleans up its own tokens, so clearing here is safe.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM brokerage_token")
        conn.commit()


async def _insert_token(owner: uuid.UUID, *, access: str, refresh: str) -> None:
    from datetime import datetime, timezone

    async with async_session_maker() as session:
        repo = ScopedRepository(BrokerageToken, Scope.for_user(owner), session)
        await repo.add(
            provider="schwab",
            access_token=access,
            refresh_token=refresh,
            expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        )
        await session.commit()


@pytest.fixture
def _schwab_creds(monkeypatch):
    """Set SCHWAB_* so a SchwabAdapter constructs (it refuses without creds).

    Scoped to the tests that request it — NOT file-wide, since other tests in
    this module assert the missing-creds refusal.
    """
    monkeypatch.setenv("SCHWAB_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("SCHWAB_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("SCHWAB_CALLBACK_URL", "https://127.0.0.1/callback")


@pytest.mark.asyncio
async def test_bind_operator_token_fake_adapter_passthrough():
    # A non-Schwab adapter is returned untouched — a BROKER_ADAPTER=fake run needs
    # no DB rows and never touches the token path.
    fake = FakeBrokerAdapter()
    async with async_session_maker() as session:
        result = await bind_operator_token(fake, session)
    assert result is fake


@pytest.mark.asyncio
async def test_bind_operator_token_no_linked_account_raises(_schwab_creds):
    from brokers.schwab_adapter import SchwabAdapter

    _clear_all_brokerage_tokens()
    async with async_session_maker() as session:
        with pytest.raises(OperatorTokenBindError, match="No linked Schwab account"):
            await bind_operator_token(SchwabAdapter(), session)


@pytest.mark.asyncio
async def test_bind_operator_token_single_owner_binds_decrypted_token(
    two_owner_ids, _schwab_creds
):
    from brokers.schwab_adapter import SchwabAdapter

    a, _ = two_owner_ids
    _clear_all_brokerage_tokens()
    await _insert_token(
        a, access=encrypt_token("live-access"), refresh=encrypt_token("live-refresh")
    )

    async with async_session_maker() as session:
        bound = await bind_operator_token(SchwabAdapter(), session)

    # A token-bound Schwab adapter whose accessor yields the reconstructed,
    # DECRYPTED schwab-py token envelope (never ciphertext, never logged).
    assert isinstance(bound, SchwabAdapter)
    token = bound._token_read_func()
    # schwab-py 1.5.x metadata-wrapped envelope.
    assert set(token) == {"creation_timestamp", "token"}
    inner = token["token"]
    assert inner["access_token"] == "live-access"
    assert inner["refresh_token"] == "live-refresh"
    assert inner["token_type"] == "Bearer"
    assert isinstance(inner["expires_at"], int)
    assert isinstance(token["creation_timestamp"], int)


@pytest.mark.asyncio
async def test_bind_operator_token_multiple_owners_raises(two_owner_ids, _schwab_creds):
    from brokers.schwab_adapter import SchwabAdapter

    a, b = two_owner_ids
    _clear_all_brokerage_tokens()
    await _insert_token(
        a, access=encrypt_token("a-access"), refresh=encrypt_token("a-refresh")
    )
    await _insert_token(
        b, access=encrypt_token("b-access"), refresh=encrypt_token("b-refresh")
    )

    async with async_session_maker() as session:
        with pytest.raises(OperatorTokenBindError, match="single dedicated operator"):
            await bind_operator_token(SchwabAdapter(), session)


@pytest.mark.asyncio
async def test_bind_operator_token_undecryptable_raises(two_owner_ids, _schwab_creds):
    from brokers.schwab_adapter import SchwabAdapter

    a, _ = two_owner_ids
    _clear_all_brokerage_tokens()
    # A row whose ciphertext cannot be decrypted (rotated key / corrupt data).
    await _insert_token(a, access="not-valid-fernet-ciphertext", refresh="also-bad")

    async with async_session_maker() as session:
        with pytest.raises(OperatorTokenBindError, match="could not be decrypted"):
            await bind_operator_token(SchwabAdapter(), session)


def test_token_dict_is_schwab_py_metadata_wrapped():
    """Regression: the reconstructed token must be schwab-py's metadata-wrapped
    shape ``{creation_timestamp, token:{...}}`` (Story 7.6 go-live pre-flight
    2026-08-07 caught schwab-py 1.5.x rejecting a bare token with "The token
    format has changed"). A bare oauth token breaks the live trading-client build.
    """
    from datetime import datetime, timezone

    from brokers.factory import (
        _SCHWAB_ACCESS_TOKEN_TTL_SECONDS,
        _token_dict_from_broker_tokens,
    )

    expires_at = datetime(2030, 1, 1, tzinfo=timezone.utc)
    wrapped = _token_dict_from_broker_tokens(
        BrokerTokens(access_token="acc", refresh_token="ref", expires_at=expires_at)
    )
    # Top level is the metadata wrapper schwab-py's TokenMetadata.from_loaded_token
    # requires — NOT a bare oauth token.
    assert set(wrapped) == {"creation_timestamp", "token"}
    epoch = int(expires_at.timestamp())
    # Mint time reconstructed as expiry minus the access-token TTL.
    assert wrapped["creation_timestamp"] == epoch - _SCHWAB_ACCESS_TOKEN_TTL_SECONDS
    inner = wrapped["token"]
    assert inner == {
        "access_token": "acc",
        "refresh_token": "ref",
        "token_type": "Bearer",
        "expires_at": epoch,
    }


def test_to_broker_tokens_unwraps_schwab_py_metadata():
    """schwab-py 1.5.x hands ``_to_broker_tokens`` a metadata-WRAPPED token
    ({creation_timestamp, token:{...}}) on the exchange-capture path; it must
    unwrap, else access/refresh come back EMPTY and Schwab rejects the call as
    ``token_invalid`` (go-live pre-flight, 2026-08-07). A bare token is unchanged.
    """
    from datetime import datetime, timezone

    from brokers.schwab_adapter.adapter import SchwabAdapter

    epoch = 1_900_000_000
    wrapped = {
        "creation_timestamp": epoch - 1800,
        "token": {
            "access_token": "acc",
            "refresh_token": "ref",
            "expires_at": epoch,
        },
    }
    t = SchwabAdapter._to_broker_tokens(wrapped)
    assert t.access_token == "acc"
    assert t.refresh_token == "ref"
    assert t.expires_at == datetime.fromtimestamp(epoch, tz=timezone.utc)

    # A bare oauth token (no wrapper) still maps directly.
    bare = SchwabAdapter._to_broker_tokens(
        {"access_token": "a", "refresh_token": "r", "expires_at": epoch}
    )
    assert bare.access_token == "a" and bare.refresh_token == "r"
