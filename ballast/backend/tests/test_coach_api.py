"""Story 4.6 tests — propose-and-approve execution (FR8/FR9/FR10, AD-7/AD-11).

Locks the full I/O & Edge-Case Matrix structurally and OFFLINE: zero network,
zero credentials, fake adapters (``BROKER_ADAPTER=fake``, ``LLM_ADAPTER=fake``).

Covers:
  (a) approve in-scope, live session → Coach Engine calls place_order exactly
      once and the OrderOutcome is returned
  (b) approve out-of-v1-scope symbol → rejected, broker NEVER called
  (c) approve non-positive amount → rejected, broker NEVER called
  (d) approve on expired/unlinked session → calm 409, no order attempted
  (e) approve unauthenticated → 401, no scope, no broker call
  (f) recommend returns a blessed recommendation (with any order_intent) and
      NEVER places an order (broker never called)
  (g) recommend works while the session is expired (degraded mode)
  (h) recommend surfaces an FR11 warning given a tripping portfolio
  (i) fake place_order determinism
  (j) Schwab place_order stub raises without creds
  (k) sole-execution-path canary — place_order is reachable only through the
      Coach Engine execution owner (the recommend path never calls it)

Requires the docker Postgres (`docker compose up -d db`). Each test uses unique
users and cleans up its own rows. NO real Schwab/Anthropic call is ever made.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import RECONNECT_MESSAGE, require_live_broker_session
from brokers.crypto import encrypt_token
from brokers.factory import get_broker
from brokers.fake_adapter import FAKE_FILL_PRICE, FakeBrokerAdapter
from brokers.port import (
    BrokerPort,
    BrokerTokens,
    OrderNotPlaceableError,
    OrderOutcome,
    OrderStatus,
    PortfolioSnapshot,
)
from brokers.session import BrokerageSession
from api.coach import IN_PROGRESS_MESSAGE
from coach.decision_record import claim_for_cosign, release_claim
from coach.execution import (
    OrderScopeError,
    SessionIntegrityError,
    execute_approved_order,
)
from coach.recommendation import OrderIntent, OrderSide
from db.connection import get_connection
from db.models import BrokerageToken, DecisionRecord, MarketDaily, PortfolioCache
from db.scope import Scope
from db.session import async_session_maker, engine

PASSWORD = "supersecret123"

# A symbol with a seeded drawdown so find_precedent returns an EVENT_PRECEDENT
# (a "live drawdown") — needed for the FR11 over-concentration warning to fire.
SYM = "VTI"  # index-core, so an approve of it is in v1 scope
BASE_DAY = date(2015, 1, 1)


# --- table + fixture setup ---------------------------------------------------


@pytest_asyncio.fixture(autouse=True)
async def ensure_tables():
    """Ensure the owned tables exist (matches the create-all lifecycle).

    ``create(checkfirst=True)`` is a no-op on an ALREADY-existing table, so a test
    DB carried over from before Story 6.1 would lack the new unique index on
    ``decision_record.idempotency_key`` (a fresh ``create_all`` would build it).
    Reconcile that explicitly with ``CREATE UNIQUE INDEX IF NOT EXISTS`` so the
    test DB matches a fresh schema without an Alembic migration.
    """
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.run_sync(BrokerageToken.__table__.create, checkfirst=True)
        await conn.run_sync(PortfolioCache.__table__.create, checkfirst=True)
        await conn.run_sync(MarketDaily.__table__.create, checkfirst=True)
        await conn.run_sync(DecisionRecord.__table__.create, checkfirst=True)
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "uq_decision_record_idempotency_key "
                "ON decision_record (idempotency_key)"
            )
        )
        # Story 6.3 hoists broker_ref into a queryable column; reconcile a
        # carried-over test DB the same way (create_all won't ALTER an existing
        # table). Harmless/no-op once the column exists.
        await conn.execute(
            text(
                "ALTER TABLE decision_record "
                "ADD COLUMN IF NOT EXISTS broker_ref VARCHAR(64)"
            )
        )
    yield


@pytest.fixture
def client() -> TestClient:
    with TestClient(create_app()) as c:
        yield c


# --- helpers -----------------------------------------------------------------


def _unique_email() -> str:
    return f"coach-api-test-{uuid.uuid4().hex}@example.com"


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


def _recommend_decision_id(client: TestClient, headers: dict) -> str:
    """POST a minimal /recommend and return the persisted ``decision_id`` (4.9).

    Every /approve now requires a real decision_id from a prior /recommend; this
    threads one in with a minimal body (the fake pipeline always blesses a
    default plan, so no market seeding is needed).
    """
    resp = client.post("/api/coach/recommend", json={}, headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["decision_id"]
    return body["decision_id"]


def _decision_rows(owner: uuid.UUID) -> list[dict]:
    """Read this owner's decision_record rows (sync psycopg) for assertions."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, schema_version, recommendation_snapshot, status, "
                "created_at, co_signed_at, idempotency_key, cosign_snapshot "
                "FROM decision_record WHERE owner_id = %s",
                (str(owner),),
            )
            cols = [c.name for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def _user_id_for(email: str) -> uuid.UUID:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT id FROM "user" WHERE email = %s', (email,))
            (uid_raw,) = cur.fetchone()
    return uuid.UUID(str(uid_raw))


def _delete_user(email: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM "user" WHERE email = %s', (email,))
        conn.commit()


def _insert_token_sync(owner: uuid.UUID, expires_at: datetime) -> None:
    """Insert a brokerage token (ciphertext) so the user has a session state."""
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


def _insert_holding_sync(
    owner: uuid.UUID, symbol: str, market_value: str, cash: str = "0"
) -> None:
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
                    market_value,
                    market_value,
                    cash,
                    datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc),
                ),
            )
        conn.commit()


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


def _live() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=3)


def _expired() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=1)


def _live_session(provider: str = "fake") -> BrokerageSession:
    """A live, provider-matched brokerage session for direct execution-owner calls.

    Placement-time integrity (Story 4.8) requires the owner be handed a live
    session whose ``provider`` matches the placing adapter. The fake adapters used
    in these tests expose ``provider = "fake"``, so the default matches.
    """
    return BrokerageSession(state="live", expires_at=_live(), provider=provider)


class _SpyAdapter(BrokerPort):
    """A fake broker that records how many times place_order was called.

    Wraps FakeBrokerAdapter's behavior but counts calls so tests can assert the
    single-execution-path invariant (exactly once, or never) structurally. As of
    Story 4.7 it also records ``get_order_status`` calls so the reconciliation
    invariants (same-key reuse, called only when indeterminate) can be asserted.
    """

    provider = "fake"

    def __init__(self) -> None:
        self.calls: list[tuple[OrderIntent, str]] = []
        self.status_calls: list[str] = []
        self._delegate = FakeBrokerAdapter()

    def authorization_url(self, state: str) -> str:
        return self._delegate.authorization_url(state)

    def exchange_code(self, code: str, state: str) -> BrokerTokens:
        return self._delegate.exchange_code(code, state)

    def fetch_portfolio(self) -> PortfolioSnapshot:
        return self._delegate.fetch_portfolio()

    async def place_order(
        self, order_intent: OrderIntent, *, idempotency_key: str
    ) -> OrderOutcome:
        self.calls.append((order_intent, idempotency_key))
        return await self._delegate.place_order(
            order_intent, idempotency_key=idempotency_key
        )

    async def get_order_status(self, idempotency_key: str) -> OrderOutcome:
        self.status_calls.append(idempotency_key)
        return await self._delegate.get_order_status(idempotency_key)


class _ScriptedAdapter(BrokerPort):
    """A broker double that returns a SCRIPTED placement/reconciliation outcome.

    Story 4.7 needs non-``FILLED`` statuses that the default FakeBrokerAdapter
    never produces (its default placement is always ``filled``). This double lets
    a test inject a specific ``place_order`` outcome and, independently, a
    specific ``get_order_status`` outcome — while recording call args so the
    reconciliation invariants (place_order at most once, same-key reuse,
    get_order_status called only when indeterminate) are asserted structurally.
    Fully offline/deterministic; injected via the ``get_broker`` DI override.
    """

    provider = "fake"

    def __init__(
        self,
        *,
        placement: OrderOutcome,
        reconciled: OrderOutcome | None = None,
    ) -> None:
        self._placement = placement
        self._reconciled = reconciled
        self.calls: list[tuple[OrderIntent, str]] = []
        self.status_calls: list[str] = []
        self._delegate = FakeBrokerAdapter()

    def authorization_url(self, state: str) -> str:
        return self._delegate.authorization_url(state)

    def exchange_code(self, code: str, state: str) -> BrokerTokens:
        return self._delegate.exchange_code(code, state)

    def fetch_portfolio(self) -> PortfolioSnapshot:
        return self._delegate.fetch_portfolio()

    async def place_order(
        self, order_intent: OrderIntent, *, idempotency_key: str
    ) -> OrderOutcome:
        self.calls.append((order_intent, idempotency_key))
        return self._placement

    async def get_order_status(self, idempotency_key: str) -> OrderOutcome:
        self.status_calls.append(idempotency_key)
        if self._reconciled is None:
            raise AssertionError(
                "get_order_status was called but no reconciled outcome was "
                "scripted — the placement should have been definitive."
            )
        return self._reconciled


# =============================================================================
# APPROVE
# =============================================================================


def test_approve_in_scope_places_order_exactly_once(client):
    # (a) authed + live session + in-scope order → Coach Engine places the order
    # exactly once and the OrderOutcome is returned.
    email = _unique_email()
    spy = _SpyAdapter()
    client.app.dependency_overrides[get_broker] = lambda: spy
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        _insert_token_sync(_user_id_for(email), _live())
        decision_id = _recommend_decision_id(client, headers)

        resp = client.post(
            "/api/coach/approve",
            json={
                "decision_id": decision_id,
                "order_intent": {"symbol": "VTI", "side": "buy", "amount": "500"},
            },
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "filled"
        # Non-negative qty/price; money as decimal strings, no float. Pin that a
        # fill actually carries a price (guard against a None → Decimal(None)
        # crash masking a real regression).
        assert Decimal(body["filled_qty"]) >= 0
        assert body["avg_price"] is not None
        assert Decimal(body["avg_price"]) >= 0
        assert body["broker_ref"]
        # The Coach Engine (sole caller) placed the order EXACTLY once.
        assert len(spy.calls) == 1
        intent, key = spy.calls[0]
        assert intent.symbol == "VTI"
        assert intent.side is OrderSide.BUY
        assert intent.amount == Decimal("500")
        assert key  # an idempotency key was minted and passed
    finally:
        client.app.dependency_overrides.pop(get_broker, None)
        _delete_user(email)


def test_approve_out_of_scope_symbol_rejected_broker_never_called(client):
    # (b) an out-of-v1-scope symbol → rejected via the envelope; broker untouched.
    email = _unique_email()
    spy = _SpyAdapter()
    client.app.dependency_overrides[get_broker] = lambda: spy
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        _insert_token_sync(_user_id_for(email), _live())
        decision_id = _recommend_decision_id(client, headers)

        resp = client.post(
            "/api/coach/approve",
            json={
                "decision_id": decision_id,
                "order_intent": {"symbol": "AAPL", "side": "buy", "amount": "500"},
            },
            headers=headers,
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["type"]  # error envelope shape
        assert spy.calls == []  # broker NEVER called
        # A refusal (422) leaves the referenced record PROPOSED (no co-sign).
        rows = _decision_rows(_user_id_for(email))
        assert len(rows) == 1 and rows[0]["status"] == "proposed"
    finally:
        client.app.dependency_overrides.pop(get_broker, None)
        _delete_user(email)


def test_approve_non_positive_amount_rejected_broker_never_called(client):
    # (c) a non-positive amount → rejected before any broker call.
    email = _unique_email()
    spy = _SpyAdapter()
    client.app.dependency_overrides[get_broker] = lambda: spy
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        _insert_token_sync(_user_id_for(email), _live())
        decision_id = _recommend_decision_id(client, headers)

        resp = client.post(
            "/api/coach/approve",
            json={
                "decision_id": decision_id,
                "order_intent": {"symbol": "VTI", "side": "buy", "amount": "0"},
            },
            headers=headers,
        )
        assert resp.status_code == 422, resp.text
        assert spy.calls == []
    finally:
        client.app.dependency_overrides.pop(get_broker, None)
        _delete_user(email)


def test_approve_expired_session_returns_409_no_order(client):
    # (d) a non-live (expired) session → calm 409 reconnect; no order attempted.
    email = _unique_email()
    spy = _SpyAdapter()
    client.app.dependency_overrides[get_broker] = lambda: spy
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        _insert_token_sync(_user_id_for(email), _expired())
        # Recommend works in degraded mode (AD-11) even on an expired session, so
        # a proposed record + decision_id exist before the approve entry gate.
        decision_id = _recommend_decision_id(client, headers)

        resp = client.post(
            "/api/coach/approve",
            json={
                "decision_id": decision_id,
                "order_intent": {"symbol": "VTI", "side": "buy", "amount": "500"},
            },
            headers=headers,
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["message"] == RECONNECT_MESSAGE
        assert spy.calls == []  # broker NEVER called
        # The entry-gate 409 leaves the referenced record PROPOSED (no co-sign).
        rows = _decision_rows(_user_id_for(email))
        assert len(rows) == 1 and rows[0]["status"] == "proposed"
    finally:
        client.app.dependency_overrides.pop(get_broker, None)
        _delete_user(email)


def test_approve_unlinked_session_returns_409_no_order(client):
    # (d, unlinked variant) no token row at all → 409; no order attempted.
    email = _unique_email()
    spy = _SpyAdapter()
    client.app.dependency_overrides[get_broker] = lambda: spy
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        # No token inserted → session "unlinked" → not live. Recommend still works
        # (degraded, no session required) so a decision_id exists.
        decision_id = _recommend_decision_id(client, headers)

        resp = client.post(
            "/api/coach/approve",
            json={
                "decision_id": decision_id,
                "order_intent": {"symbol": "VTI", "side": "buy", "amount": "500"},
            },
            headers=headers,
        )
        assert resp.status_code == 409, resp.text
        assert spy.calls == []
    finally:
        client.app.dependency_overrides.pop(get_broker, None)
        _delete_user(email)


def test_approve_provider_mismatch_returns_409_no_order(client):
    # (c, 4.8) The entry gate PASSES with a live session, but that session's
    # provider ("schwab") disagrees with the placing adapter ("fake"). The
    # execution owner's placement-time integrity check must refuse: calm 409
    # RECONNECT_MESSAGE, broker never touched. Override require_live_broker_session
    # directly (approve() depends on it by name) so the entry gate returns a
    # live-but-mismatched session; the fake spy proves the broker stays untouched.
    email = _unique_email()
    spy = _SpyAdapter()  # provider == "fake"
    client.app.dependency_overrides[get_broker] = lambda: spy
    client.app.dependency_overrides[require_live_broker_session] = (
        lambda: _live_session(provider="schwab")
    )
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        _insert_token_sync(_user_id_for(email), _live())
        decision_id = _recommend_decision_id(client, headers)

        resp = client.post(
            "/api/coach/approve",
            json={
                "decision_id": decision_id,
                "order_intent": {"symbol": "VTI", "side": "buy", "amount": "500"},
            },
            headers=headers,
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["message"] == RECONNECT_MESSAGE
        assert spy.calls == []  # broker NEVER called on a provider mismatch
        assert spy.status_calls == []
        # The integrity 409 leaves the referenced record PROPOSED (no co-sign).
        rows = _decision_rows(_user_id_for(email))
        assert len(rows) == 1 and rows[0]["status"] == "proposed"
    finally:
        client.app.dependency_overrides.pop(get_broker, None)
        client.app.dependency_overrides.pop(require_live_broker_session, None)
        _delete_user(email)


def test_approve_live_matched_session_places_once_and_reconciles(client):
    # (d, 4.8 regression) The happy path is unregressed by the integrity gate: a
    # live, provider-matched session + in-scope intent still places EXACTLY once
    # and reconciles per 4.7. A scripted `timeout` placement reconciles to filled,
    # proving both the single-place and the 4.7 reconciliation survive 4.8.
    email = _unique_email()
    placement = OrderOutcome(
        status=OrderStatus.TIMEOUT, filled_qty=Decimal("0"), avg_price=None
    )
    reconciled = OrderOutcome(
        status=OrderStatus.FILLED,
        filled_qty=Decimal("5"),
        avg_price=Decimal("100.00"),
        broker_ref="scripted-ref",
    )
    adapter = _ScriptedAdapter(placement=placement, reconciled=reconciled)
    client.app.dependency_overrides[get_broker] = lambda: adapter
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        _insert_token_sync(_user_id_for(email), _live())
        decision_id = _recommend_decision_id(client, headers)

        resp = client.post(
            "/api/coach/approve",
            json={
                "decision_id": decision_id,
                "order_intent": {"symbol": "VTI", "side": "buy", "amount": "500"},
            },
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "filled"  # reconciled true state (4.7)
        assert len(adapter.calls) == 1  # placed EXACTLY once (no double-place)
        assert len(adapter.status_calls) == 1  # reconciled once
    finally:
        client.app.dependency_overrides.pop(get_broker, None)
        _delete_user(email)


class _NotPlaceableAdapter(BrokerPort):
    """A broker double whose place_order refuses via OrderNotPlaceableError (6.3).

    Models the adapter's sub-minimum / unusable-quote calm refusal so the API's
    claim-release + calm-422 handling can be asserted without the real SDK.
    """

    provider = "fake"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._delegate = FakeBrokerAdapter()

    def authorization_url(self, state: str) -> str:
        return self._delegate.authorization_url(state)

    def exchange_code(self, code: str, state: str) -> BrokerTokens:
        return self._delegate.exchange_code(code, state)

    def fetch_portfolio(self) -> PortfolioSnapshot:
        return self._delegate.fetch_portfolio()

    async def place_order(
        self, order_intent: OrderIntent, *, idempotency_key: str
    ) -> OrderOutcome:
        self.calls.append(idempotency_key)
        raise OrderNotPlaceableError(
            "$100.00 buys less than one whole share of VTI at about $500.00 — "
            "no order was placed."
        )

    async def get_order_status(self, idempotency_key: str) -> OrderOutcome:
        raise AssertionError("get_order_status must not be reached on a refusal")


def _broker_ref_for(owner: uuid.UUID) -> str | None:
    """Read this owner's decision_record.broker_ref column (Story 6.3)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT broker_ref FROM decision_record WHERE owner_id = %s",
                (str(owner),),
            )
            (ref,) = cur.fetchone()
    return ref


def _null_idempotency_key(owner: uuid.UUID) -> None:
    """Force the owner's decision_record idempotency_key to NULL (6.1 guard test)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE decision_record SET idempotency_key = NULL "
                "WHERE owner_id = %s",
                (str(owner),),
            )
        conn.commit()


def test_approve_null_idempotency_key_refuses_broker_untouched(client):
    # (6.1 pre-flight guard, due at 6.3) A decision whose persisted
    # idempotency_key is NULL is refused calmly (422) BEFORE any placement, the
    # claim is released (retryable → back to proposed), and the broker is never
    # touched — a post-fill cosign crash is converted to a pre-fill refusal.
    email = _unique_email()
    spy = _SpyAdapter()
    client.app.dependency_overrides[get_broker] = lambda: spy
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        uid = _user_id_for(email)
        _insert_token_sync(uid, _live())
        decision_id = _recommend_decision_id(client, headers)
        _null_idempotency_key(uid)

        resp = client.post(
            "/api/coach/approve",
            json={
                "decision_id": decision_id,
                "order_intent": {"symbol": "VTI", "side": "buy", "amount": "500"},
            },
            headers=headers,
        )
        assert resp.status_code == 422, resp.text
        assert spy.calls == []  # broker NEVER touched
        # Claim released → the record is retryable (proposed), not stranded.
        rows = _decision_rows(uid)
        assert len(rows) == 1 and rows[0]["status"] == "proposed"
    finally:
        client.app.dependency_overrides.pop(get_broker, None)
        _delete_user(email)


def test_approve_not_placeable_refusal_releases_claim(client):
    # (6.3) A sub-minimum / unusable-quote refusal (OrderNotPlaceableError) from
    # the adapter surfaces as a calm 422 and RELEASES the claim (retryable),
    # symmetric with the out-of-scope refusal — no order placed.
    email = _unique_email()
    adapter = _NotPlaceableAdapter()
    client.app.dependency_overrides[get_broker] = lambda: adapter
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        uid = _user_id_for(email)
        _insert_token_sync(uid, _live())
        decision_id = _recommend_decision_id(client, headers)

        resp = client.post(
            "/api/coach/approve",
            json={
                "decision_id": decision_id,
                "order_intent": {"symbol": "VTI", "side": "buy", "amount": "500"},
            },
            headers=headers,
        )
        assert resp.status_code == 422, resp.text
        assert len(adapter.calls) == 1  # the adapter refused at placement time
        rows = _decision_rows(uid)
        assert len(rows) == 1 and rows[0]["status"] == "proposed"  # released
    finally:
        client.app.dependency_overrides.pop(get_broker, None)
        _delete_user(email)


def test_approve_persists_broker_ref_column(client):
    # (6.3) The reconciled OrderOutcome's broker_ref is hoisted into the queryable
    # decision_record.broker_ref column (not only the cosign_snapshot JSON) so a
    # later explicit reconcile (Story 6.7) can find the order.
    email = _unique_email()
    placement = OrderOutcome(
        status=OrderStatus.FILLED,
        filled_qty=Decimal("5"),
        avg_price=Decimal("100.00"),
        broker_ref="schwab-order-42",
    )
    adapter = _ScriptedAdapter(placement=placement)
    client.app.dependency_overrides[get_broker] = lambda: adapter
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        uid = _user_id_for(email)
        _insert_token_sync(uid, _live())
        decision_id = _recommend_decision_id(client, headers)

        resp = client.post(
            "/api/coach/approve",
            json={
                "decision_id": decision_id,
                "order_intent": {"symbol": "VTI", "side": "buy", "amount": "500"},
            },
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert _broker_ref_for(uid) == "schwab-order-42"
    finally:
        client.app.dependency_overrides.pop(get_broker, None)
        _delete_user(email)


def test_approve_unauthenticated_returns_401(client):
    # (e) no token → 401 before the handler; no scope built, no broker call.
    spy = _SpyAdapter()
    client.app.dependency_overrides[get_broker] = lambda: spy
    try:
        resp = client.post(
            "/api/coach/approve",
            json={
                "decision_id": str(uuid.uuid4()),
                "order_intent": {"symbol": "VTI", "side": "buy", "amount": "500"},
            },
        )
        assert resp.status_code == 401, resp.text
        assert spy.calls == []
    finally:
        client.app.dependency_overrides.pop(get_broker, None)


# =============================================================================
# RECOMMEND
# =============================================================================


def test_recommend_returns_blessed_and_never_places(client):
    # (f) recommend returns a blessed recommendation and NEVER places an order.
    email = _unique_email()
    spy = _SpyAdapter()
    # Even if the broker were somehow reachable, the spy proves it is not.
    client.app.dependency_overrides[get_broker] = lambda: spy
    _clean_market([SYM])
    _insert_series(SYM, [Decimal("100"), Decimal("92")])
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        _insert_token_sync(_user_id_for(email), _live())

        resp = client.post(
            "/api/coach/recommend",
            json={"symbol": SYM, "question": "Should I invest now?"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Blessed shape: reasoning + >=1 evidence + >=1 uncertainty.
        assert body["action_label"]
        assert body["reasoning"].strip()
        assert len(body["evidence"]) >= 1
        assert any(u.strip() for u in body["uncertainties"])
        # order_intent key is present (optional; the fake default plan → None).
        assert "order_intent" in body
        # Never placed an order.
        assert spy.calls == []
    finally:
        client.app.dependency_overrides.pop(get_broker, None)
        _clean_market([SYM])
        _delete_user(email)


def test_recommend_order_intent_amount_is_fixed_point():
    # (6.4) The /recommend order_intent.amount serializer renders an extreme
    # amount as plain fixed-point (no E+/E-) — the 4.7-ledger bug (str(Decimal)
    # → "1E+27") is closed, and the wire string round-trips via Decimal(str(...)).
    from api.coach import _order_intent_out

    extreme = Decimal("1E29") / Decimal("100")
    intent = OrderIntent(symbol="VTI", side=OrderSide.BUY, amount=extreme)
    out = _order_intent_out(intent)
    assert out is not None
    dumped = out.model_dump(mode="json")
    assert "E" not in dumped["amount"]
    assert dumped["amount"] == "1000000000000000000000000000"
    assert Decimal(dumped["amount"]) == extreme
    # None intent stays None (never "None").
    assert _order_intent_out(None) is None


def test_recommend_works_while_session_expired_degraded(client):
    # (g) recommend works in degraded mode: an EXPIRED session still gets advice.
    email = _unique_email()
    spy = _SpyAdapter()
    client.app.dependency_overrides[get_broker] = lambda: spy
    _clean_market([SYM])
    _insert_series(SYM, [Decimal("100"), Decimal("92")])
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        _insert_token_sync(_user_id_for(email), _expired())

        resp = client.post(
            "/api/coach/recommend",
            json={"symbol": SYM, "question": "Should I invest now?"},
            headers=headers,
        )
        # Advice is NOT gated on a live session (AD-11).
        assert resp.status_code == 200, resp.text
        assert resp.json()["reasoning"].strip()
        assert spy.calls == []
    finally:
        client.app.dependency_overrides.pop(get_broker, None)
        _clean_market([SYM])
        _delete_user(email)


def test_recommend_surfaces_fr11_warning(client):
    # (h) a tripping portfolio (concentrated + a live drawdown) → the FR11
    # over-concentration warning content appears in the returned reasoning.
    email = _unique_email()
    _clean_market([SYM])
    # A drawdown so find_precedent returns an EVENT_PRECEDENT (live drawdown).
    _insert_series(SYM, [Decimal("100"), Decimal("70")])
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        uid = _user_id_for(email)
        # Portfolio: 5000 already in SYM + 1000 cash → total 6000. Buying 5000
        # more of SYM → post 10000/11000 ≈ 0.91 > 0.40 ceiling.
        _insert_holding_sync(uid, SYM, market_value="5000", cash="1000")

        resp = client.post(
            "/api/coach/recommend",
            json={"symbol": SYM, "side": "buy", "amount": "5000"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        text = resp.json()["reasoning"].lower()
        # The FR11 over-concentration warning is surfaced (4.5 behavior), still
        # blessed and not blocked.
        assert "outsized share" in text
    finally:
        _clean_market([SYM])
        _delete_user(email)


# =============================================================================
# ADAPTERS / EXECUTION OWNER (pure, no DB)
# =============================================================================


@pytest.mark.asyncio
async def test_fake_place_order_is_deterministic():
    # (i) same (order_intent, idempotency_key) → equal OrderOutcomes.
    intent = OrderIntent(symbol="VTI", side=OrderSide.BUY, amount=Decimal("500"))
    a = await FakeBrokerAdapter().place_order(intent, idempotency_key="k-123")
    b = await FakeBrokerAdapter().place_order(intent, idempotency_key="k-123")
    assert a == b
    assert a.status is OrderStatus.FILLED
    assert a.filled_qty == Decimal("500") / FAKE_FILL_PRICE
    assert a.avg_price == FAKE_FILL_PRICE
    assert a.filled_qty >= 0 and a.avg_price >= 0
    assert a.broker_ref == "fake-order-k-123"
    # Money is Decimal, never float.
    assert isinstance(a.filled_qty, Decimal)
    assert isinstance(a.avg_price, Decimal)


@pytest.mark.asyncio
async def test_schwab_place_order_without_token_raises_no_phantom_fill(monkeypatch):
    # (j) The Schwab place_order (now wired, Story 6.3) NEVER returns a phantom
    # fill when it has no linked/decrypted user token — it refuses loudly with
    # SchwabNotConfiguredError rather than building a tokenless client. Pinned two
    # ways so a silent-fill regression fails the test:
    #   - creds present but NO token bound → the token-required refusal
    #   - captured cred stripped → _require_configured() raises
    from brokers.schwab_adapter import SchwabAdapter, SchwabNotConfiguredError

    monkeypatch.setenv("SCHWAB_CLIENT_ID", "id")
    monkeypatch.setenv("SCHWAB_CLIENT_SECRET", "secret")
    monkeypatch.setenv("SCHWAB_CALLBACK_URL", "https://example.com/cb")
    adapter = SchwabAdapter()  # creds present, but NO token_read_func bound
    intent = OrderIntent(symbol="VTI", side=OrderSide.BUY, amount=Decimal("500"))

    # No token bound: refuses (a config error), never a silent OrderOutcome fill.
    with pytest.raises(SchwabNotConfiguredError):
        await adapter.place_order(intent, idempotency_key="k")

    # Creds-missing path: setenv after construction is a no-op (the adapter read
    # creds at __init__), so strip the CAPTURED attribute to exercise the
    # _require_configured() guard directly.
    adapter._client_id = ""
    with pytest.raises(SchwabNotConfiguredError):
        await adapter.place_order(intent, idempotency_key="k")


@pytest.mark.asyncio
async def test_execution_owner_out_of_scope_never_calls_broker():
    # The execution owner rejects out-of-scope BEFORE the broker call.
    spy = _SpyAdapter()
    intent = OrderIntent(symbol="AAPL", side=OrderSide.BUY, amount=Decimal("500"))
    with pytest.raises(OrderScopeError):
        await execute_approved_order(
            intent, broker=spy, broker_session=_live_session()
        )
    assert spy.calls == []


@pytest.mark.asyncio
async def test_execution_owner_places_exactly_once_and_mints_key():
    # (a, unit level) the sole caller places exactly once and mints a key.
    spy = _SpyAdapter()
    intent = OrderIntent(symbol="VTI", side=OrderSide.BUY, amount=Decimal("500"))
    outcome = await execute_approved_order(
        intent, broker=spy, broker_session=_live_session()
    )
    assert outcome.status is OrderStatus.FILLED
    assert len(spy.calls) == 1
    _, key = spy.calls[0]
    assert key  # minted


@pytest.mark.asyncio
async def test_execution_owner_non_live_session_raises_broker_untouched():
    # (a, 4.8 unit) a non-live (expired) broker_session → SessionIntegrityError
    # BEFORE any broker call; neither place_order nor get_order_status is reached.
    spy = _SpyAdapter()
    intent = OrderIntent(symbol="VTI", side=OrderSide.BUY, amount=Decimal("500"))
    session = BrokerageSession(
        state="expired", expires_at=_expired(), provider="fake"
    )
    with pytest.raises(SessionIntegrityError):
        await execute_approved_order(intent, broker=spy, broker_session=session)
    assert spy.calls == []
    assert spy.status_calls == []


@pytest.mark.asyncio
async def test_execution_owner_provider_mismatch_raises_broker_untouched():
    # (b, 4.8 unit) a LIVE session whose provider ("schwab") disagrees with the
    # placing adapter ("fake") → SessionIntegrityError; broker never touched.
    spy = _SpyAdapter()  # provider == "fake"
    intent = OrderIntent(symbol="VTI", side=OrderSide.BUY, amount=Decimal("500"))
    session = _live_session(provider="schwab")
    with pytest.raises(SessionIntegrityError):
        await execute_approved_order(intent, broker=spy, broker_session=session)
    assert spy.calls == []
    assert spy.status_calls == []


@pytest.mark.asyncio
async def test_execution_owner_provider_match_is_case_whitespace_insensitive():
    # (review patch, 4.8 unit) provider agreement is compared normalized: a live
    # session whose stored provider differs only by case/whitespace (" FAKE ")
    # from the adapter's ("fake") still MATCHES and places once — so a benign
    # stored-provider casing drift never false-refuses a legitimate live session.
    spy = _SpyAdapter()  # provider == "fake"
    intent = OrderIntent(symbol="VTI", side=OrderSide.BUY, amount=Decimal("500"))
    session = _live_session(provider=" FAKE ")
    outcome = await execute_approved_order(
        intent, broker=spy, broker_session=session
    )
    assert outcome.status is OrderStatus.FILLED
    assert len(spy.calls) == 1  # normalized match → placed exactly once


@pytest.mark.asyncio
async def test_execution_owner_integrity_before_scope():
    # (e, 4.8 unit) integrity-before-scope: a non-live session AND an out-of-scope
    # symbol → the integrity check fails FIRST (SessionIntegrityError, not
    # OrderScopeError); the scope check is never reached and the broker untouched.
    spy = _SpyAdapter()
    intent = OrderIntent(symbol="AAPL", side=OrderSide.BUY, amount=Decimal("500"))
    session = BrokerageSession(
        state="expired", expires_at=_expired(), provider="fake"
    )
    with pytest.raises(SessionIntegrityError):
        await execute_approved_order(intent, broker=spy, broker_session=session)
    assert spy.calls == []
    assert spy.status_calls == []


@pytest.mark.parametrize("broker_method", [".place_order(", ".get_order_status("])
def test_sole_execution_path_canary(broker_method):
    # (k) AD-7: the ONLY code that calls BrokerPort.place_order OR
    # BrokerPort.get_order_status is the Coach Engine execution owner. Grep the
    # source tree — no other module (API handlers, pipeline, portfolio) may call
    # either. Story 4.7 extends the 4.6 canary to the reconciliation read so it
    # cannot leak into the API handler or elsewhere.
    import pathlib

    backend = pathlib.Path(__file__).resolve().parent.parent
    offenders: list[str] = []
    for path in backend.rglob("*.py"):
        # Skip tests, the port ABC definition, and the execution owner itself.
        rel = path.relative_to(backend)
        parts = rel.parts
        if parts and parts[0] in {"tests", ".venv"}:
            continue
        if rel.as_posix() in {
            "brokers/port.py",  # the abstract declaration
            "brokers/fake_adapter.py",  # the implementation
            "brokers/schwab_adapter/adapter.py",  # the implementation
            "coach/execution.py",  # the sole caller
        }:
            continue
        text = path.read_text(encoding="utf-8")
        if broker_method in text:
            offenders.append(rel.as_posix())
    assert offenders == [], (
        f"{broker_method.strip('.(')} must be called ONLY by coach.execution "
        f"(AD-7). Unexpected callers: {offenders}"
    )


def test_approve_malformed_body_rejected_broker_never_called(client):
    # Review hardening: the handler trusts a client-supplied order_intent, so the
    # request-shape boundary must reject a missing order_intent AND an invalid
    # side WITHOUT ever touching the broker (422 via Pydantic, before execution).
    email = _unique_email()
    spy = _SpyAdapter()
    client.app.dependency_overrides[get_broker] = lambda: spy
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        _insert_token_sync(_user_id_for(email), _live())
        decision_id = _recommend_decision_id(client, headers)

        # Missing order_intent (and decision_id) entirely.
        r1 = client.post("/api/coach/approve", json={}, headers=headers)
        assert r1.status_code == 422, r1.text

        # An order side outside the closed buy/sell enum (with a real decision_id
        # so the failure is the side enum, not the missing decision_id).
        r2 = client.post(
            "/api/coach/approve",
            json={
                "decision_id": decision_id,
                "order_intent": {"symbol": "VTI", "side": "hold", "amount": "500"},
            },
            headers=headers,
        )
        assert r2.status_code == 422, r2.text

        assert spy.calls == []  # broker NEVER called on a malformed body
    finally:
        client.app.dependency_overrides.pop(get_broker, None)
        _delete_user(email)


def test_approve_normalizes_symbol_before_placing(client):
    # Review hardening (symbol/scope agreement): a lowercase/whitespace symbol
    # that passes the case-insensitive scope gate must be CANONICALIZED before it
    # reaches the broker — the gate and the placed order must agree.
    email = _unique_email()
    spy = _SpyAdapter()
    client.app.dependency_overrides[get_broker] = lambda: spy
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        _insert_token_sync(_user_id_for(email), _live())
        decision_id = _recommend_decision_id(client, headers)

        resp = client.post(
            "/api/coach/approve",
            json={
                "decision_id": decision_id,
                "order_intent": {"symbol": "  vti  ", "side": "buy", "amount": "500"},
            },
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert len(spy.calls) == 1
        placed_intent, _ = spy.calls[0]
        # The broker received the canonical symbol, not the raw "  vti  ".
        assert placed_intent.symbol == "VTI"
    finally:
        client.app.dependency_overrides.pop(get_broker, None)
        _delete_user(email)


# =============================================================================
# STORY 4.7 — OUTCOMES & RECONCILIATION
# =============================================================================


def _approve_with_scripted(client: TestClient, adapter: BrokerPort):
    """Register+login+link a live session, then POST an in-scope approve through
    the injected ``adapter``. Returns the raw response. Caller cleans up."""
    email = _unique_email()
    client.app.dependency_overrides[get_broker] = lambda: adapter
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        _insert_token_sync(_user_id_for(email), _live())
        decision_id = _recommend_decision_id(client, headers)
        resp = client.post(
            "/api/coach/approve",
            json={
                "decision_id": decision_id,
                "order_intent": {"symbol": "VTI", "side": "buy", "amount": "500"},
            },
            headers=headers,
        )
        return resp
    finally:
        client.app.dependency_overrides.pop(get_broker, None)
        _delete_user(email)


@pytest.mark.parametrize(
    "placement_status, filled_qty, avg_price",
    [
        (OrderStatus.FILLED, Decimal("5"), Decimal("100.00")),
        (OrderStatus.PARTIAL, Decimal("2"), Decimal("100.00")),
        (OrderStatus.REJECTED, Decimal("0"), None),
    ],
)
def test_approve_definitive_placement_surfaced_no_reconcile(
    client, placement_status, filled_qty, avg_price
):
    # (a) A definitive placement (filled/partial/rejected) is surfaced honestly
    # at HTTP 200, get_order_status is NOT called, and place_order is called once.
    # A non-`filled` status is truthful data, not coerced into an error/phantom.
    placement = OrderOutcome(
        status=placement_status,
        filled_qty=filled_qty,
        avg_price=avg_price,
        broker_ref="scripted-ref",
    )
    adapter = _ScriptedAdapter(placement=placement)
    resp = _approve_with_scripted(client, adapter)

    assert resp.status_code == 200, resp.text  # honest body, not an error envelope
    body = resp.json()
    assert body["status"] == placement_status.value
    assert Decimal(body["filled_qty"]) == filled_qty
    if avg_price is None:
        assert body["avg_price"] is None  # never coerced into a phantom fill
    else:
        assert Decimal(body["avg_price"]) == avg_price
    assert len(adapter.calls) == 1  # placed exactly once
    assert adapter.status_calls == []  # definitive → no reconciliation read


def test_approve_timeout_reconciles_to_filled(client):
    # (b) A `timeout` placement reconciles once via get_order_status → filled;
    # place_order is called EXACTLY once (no double-place), reconciled state wins.
    placement = OrderOutcome(
        status=OrderStatus.TIMEOUT, filled_qty=Decimal("0"), avg_price=None
    )
    reconciled = OrderOutcome(
        status=OrderStatus.FILLED,
        filled_qty=Decimal("5"),
        avg_price=Decimal("100.00"),
        broker_ref="scripted-ref",
    )
    adapter = _ScriptedAdapter(placement=placement, reconciled=reconciled)
    resp = _approve_with_scripted(client, adapter)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "filled"  # the user sees the reconciled true state
    assert Decimal(body["filled_qty"]) == Decimal("5")
    assert len(adapter.calls) == 1  # placed at most once (no double-place)
    assert len(adapter.status_calls) == 1  # reconciled exactly once


def test_approve_pending_stays_pending_honestly(client):
    # (c) A `pending` placement reconciles once; the broker still reports
    # `pending`, and that honest state is surfaced (no phantom success, no
    # re-place, no wait-loop).
    placement = OrderOutcome(
        status=OrderStatus.PENDING, filled_qty=Decimal("0"), avg_price=None
    )
    reconciled = OrderOutcome(
        status=OrderStatus.PENDING, filled_qty=Decimal("0"), avg_price=None
    )
    adapter = _ScriptedAdapter(placement=placement, reconciled=reconciled)
    resp = _approve_with_scripted(client, adapter)

    assert resp.status_code == 200, resp.text  # honest body, not an error
    body = resp.json()
    assert body["status"] == "pending"
    assert Decimal(body["filled_qty"]) == Decimal("0")
    assert body["avg_price"] is None
    assert len(adapter.calls) == 1
    assert len(adapter.status_calls) == 1  # reconciled once, no loop


@pytest.mark.asyncio
async def test_reconciliation_reuses_same_idempotency_key():
    # (d) Reuse-key reconciliation canary: on an indeterminate placement the
    # engine calls place_order once and get_order_status once, and the key passed
    # to BOTH is identical (so a timeout never double-places).
    placement = OrderOutcome(
        status=OrderStatus.TIMEOUT, filled_qty=Decimal("0"), avg_price=None
    )
    reconciled = OrderOutcome(
        status=OrderStatus.FILLED,
        filled_qty=Decimal("5"),
        avg_price=Decimal("100.00"),
    )
    adapter = _ScriptedAdapter(placement=placement, reconciled=reconciled)
    intent = OrderIntent(symbol="VTI", side=OrderSide.BUY, amount=Decimal("500"))

    outcome = await execute_approved_order(
        intent, broker=adapter, broker_session=_live_session()
    )

    assert outcome.status is OrderStatus.FILLED
    assert len(adapter.calls) == 1
    assert len(adapter.status_calls) == 1
    _, placed_key = adapter.calls[0]
    reconciled_key = adapter.status_calls[0]
    assert placed_key == reconciled_key  # SAME idempotency key reused


@pytest.mark.asyncio
async def test_definitive_placement_never_reconciles_unit():
    # (a, unit level) a definitive placement returns unchanged and the engine
    # never calls get_order_status.
    placement = OrderOutcome(
        status=OrderStatus.FILLED,
        filled_qty=Decimal("5"),
        avg_price=Decimal("100.00"),
    )
    adapter = _ScriptedAdapter(placement=placement)  # no reconciled outcome
    intent = OrderIntent(symbol="VTI", side=OrderSide.BUY, amount=Decimal("500"))

    outcome = await execute_approved_order(
        intent, broker=adapter, broker_session=_live_session()
    )

    assert outcome == placement
    assert len(adapter.calls) == 1
    assert adapter.status_calls == []


@pytest.mark.asyncio
async def test_fake_place_order_idempotent_by_key():
    # (e) The fake is idempotency-keyed: re-placing with the SAME key returns the
    # identical recorded outcome and records the order only once (no duplicate).
    adapter = FakeBrokerAdapter()
    intent = OrderIntent(symbol="VTI", side=OrderSide.BUY, amount=Decimal("500"))

    first = await adapter.place_order(intent, idempotency_key="dup-key")
    second = await adapter.place_order(intent, idempotency_key="dup-key")

    assert first == second  # identical recorded outcome
    assert first is second  # literally the recorded object (recorded once)
    assert len(adapter._orders) == 1  # only one order recorded


@pytest.mark.asyncio
async def test_fake_get_order_status_placed_vs_unknown_key():
    # (f) get_order_status returns the recorded outcome for a placed key; an
    # UNKNOWN key gets an honest `pending` outcome — the fake never invents a fill.
    adapter = FakeBrokerAdapter()
    intent = OrderIntent(symbol="VTI", side=OrderSide.BUY, amount=Decimal("500"))

    placed = await adapter.place_order(intent, idempotency_key="known-key")
    status = await adapter.get_order_status("known-key")
    assert status == placed  # recorded outcome round-trips

    unknown = await adapter.get_order_status("never-placed")
    assert unknown.status is OrderStatus.PENDING
    assert unknown.filled_qty == Decimal("0")
    assert unknown.avg_price is None
    assert unknown.broker_ref is None  # never a phantom fill


@pytest.mark.asyncio
async def test_schwab_get_order_status_unknown_key_is_pending_no_phantom_fill(
    monkeypatch,
):
    # (g) The Schwab get_order_status (now wired, Story 6.3) NEVER invents a fill.
    # An UNKNOWN key (nothing placed on this instance) short-circuits to an honest
    # PENDING — without touching the network or needing a token — and NEVER
    # searches the account (no get_orders_for_account, no attribute-matching).
    from brokers.port import OrderStatus
    from brokers.schwab_adapter import SchwabAdapter

    monkeypatch.setenv("SCHWAB_CLIENT_ID", "id")
    monkeypatch.setenv("SCHWAB_CLIENT_SECRET", "secret")
    monkeypatch.setenv("SCHWAB_CALLBACK_URL", "https://example.com/cb")
    adapter = SchwabAdapter()  # no token bound; unknown-key path needs none

    outcome = await adapter.get_order_status("never-placed")
    assert outcome.status is OrderStatus.PENDING
    assert outcome.filled_qty == Decimal("0")
    assert outcome.broker_ref is None


# =============================================================================
# STORY 4.9 — CO-SIGNED IMMUTABLE DECISION RECORD
# =============================================================================


def test_recommend_persists_one_proposed_record_scoped(client):
    # (a) A /recommend writes EXACTLY ONE proposed DecisionRecord — owner-scoped,
    # carrying the immutable snapshot (action_label/reasoning/evidence/
    # uncertainties/order_intent) + schema_version — and returns decision_id.
    email = _unique_email()
    _clean_market([SYM])
    _insert_series(SYM, [Decimal("100"), Decimal("92")])
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        uid = _user_id_for(email)

        resp = client.post(
            "/api/coach/recommend",
            json={"symbol": SYM, "question": "Should I invest now?"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        decision_id = body["decision_id"]
        assert decision_id

        rows = _decision_rows(uid)
        assert len(rows) == 1  # EXACTLY one proposed record
        row = rows[0]
        assert str(row["id"]) == decision_id
        assert row["status"] == "proposed"
        assert row["schema_version"] == 1
        assert row["co_signed_at"] is None
        # Story 6.1: the STABLE per-decision idempotency key is minted+persisted
        # at propose time (no longer NULL until co-sign).
        assert row["idempotency_key"]
        assert row["cosign_snapshot"] is None
        snap = row["recommendation_snapshot"]
        # Snapshot mirrors what the user saw (backend-blessed, not client-authored).
        assert snap["action_label"] == body["action_label"]
        assert snap["reasoning"] == body["reasoning"]
        assert "order_intent" in snap
        assert len(snap["evidence"]) == len(body["evidence"]) >= 1
        assert snap["uncertainties"] == body["uncertainties"]
    finally:
        _clean_market([SYM])
        _delete_user(email)


def test_approve_cosigns_referenced_record_immutably(client):
    # (b) Happy-path approve co-signs the referenced record: it becomes cosigned
    # with co_signed_at, executed order_intent, idempotency_key, and the reconciled
    # outcome — while recommendation_snapshot/schema_version are BYTE-IDENTICAL to
    # the proposed snapshot (immutability).
    email = _unique_email()
    spy = _SpyAdapter()
    client.app.dependency_overrides[get_broker] = lambda: spy
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        uid = _user_id_for(email)
        _insert_token_sync(uid, _live())
        decision_id = _recommend_decision_id(client, headers)

        proposed = _decision_rows(uid)[0]
        proposed_snapshot = proposed["recommendation_snapshot"]
        proposed_version = proposed["schema_version"]
        proposed_created = proposed["created_at"]
        proposed_key = proposed["idempotency_key"]
        assert proposed_key  # stable key set at propose (Story 6.1)

        resp = client.post(
            "/api/coach/approve",
            json={
                "decision_id": decision_id,
                "order_intent": {"symbol": "VTI", "side": "buy", "amount": "500"},
            },
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "filled"
        assert len(spy.calls) == 1

        cosigned = _decision_rows(uid)[0]
        assert cosigned["status"] == "cosigned"
        assert cosigned["co_signed_at"] is not None
        assert cosigned["idempotency_key"]
        # Story 6.1: the cosigned key EQUALS the proposed key (stable per decision).
        assert cosigned["idempotency_key"] == proposed_key
        cosign_snap = cosigned["cosign_snapshot"]
        # Executed order_intent captured (money fixed-point string).
        assert cosign_snap["order_intent"] == {
            "symbol": "VTI",
            "side": "buy",
            "amount": "500",
        }
        # Reconciled outcome captured honestly.
        assert cosign_snap["outcome"]["status"] == "filled"
        assert cosign_snap["outcome"]["broker_ref"]
        # IMMUTABILITY: the proposed snapshot/version/created_at are untouched.
        assert cosigned["recommendation_snapshot"] == proposed_snapshot
        assert cosigned["schema_version"] == proposed_version
        assert cosigned["created_at"] == proposed_created
    finally:
        client.app.dependency_overrides.pop(get_broker, None)
        _delete_user(email)


def test_cosigned_record_is_replay_ready(client):
    # (c) Replay-readiness: the cosigned record's immutable snapshot carries
    # reasoning, the evidence list, uncertainties, and schema_version.
    email = _unique_email()
    spy = _SpyAdapter()
    client.app.dependency_overrides[get_broker] = lambda: spy
    _clean_market([SYM])
    _insert_series(SYM, [Decimal("100"), Decimal("92")])
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        uid = _user_id_for(email)
        _insert_token_sync(uid, _live())

        # A seeded recommend so the evidence list is non-trivial.
        rec = client.post(
            "/api/coach/recommend",
            json={"symbol": SYM, "question": "Should I invest now?"},
            headers=headers,
        )
        decision_id = rec.json()["decision_id"]

        resp = client.post(
            "/api/coach/approve",
            json={
                "decision_id": decision_id,
                "order_intent": {"symbol": "VTI", "side": "buy", "amount": "500"},
            },
            headers=headers,
        )
        assert resp.status_code == 200, resp.text

        row = _decision_rows(uid)[0]
        assert row["status"] == "cosigned"
        assert row["schema_version"] == 1
        snap = row["recommendation_snapshot"]
        assert snap["reasoning"].strip()
        assert isinstance(snap["evidence"], list) and len(snap["evidence"]) >= 1
        # Each evidence record keeps the AD-12 six-field shape.
        for record in snap["evidence"]:
            assert set(record) == {
                "id",
                "kind",
                "statement",
                "stats",
                "source",
                "as_of",
            }
        assert any(u.strip() for u in snap["uncertainties"])
    finally:
        client.app.dependency_overrides.pop(get_broker, None)
        _clean_market([SYM])
        _delete_user(email)


def test_double_approve_is_idempotent_no_double_place(client):
    # (d) A second /approve with the SAME decision_id returns the RECORDED outcome
    # and the broker spy shows place_order called EXACTLY once total (no double
    # placement across requests).
    email = _unique_email()
    spy = _SpyAdapter()
    client.app.dependency_overrides[get_broker] = lambda: spy
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        uid = _user_id_for(email)
        _insert_token_sync(uid, _live())
        decision_id = _recommend_decision_id(client, headers)
        body = {
            "decision_id": decision_id,
            "order_intent": {"symbol": "VTI", "side": "buy", "amount": "500"},
        }

        first = client.post("/api/coach/approve", json=body, headers=headers)
        assert first.status_code == 200, first.text
        second = client.post("/api/coach/approve", json=body, headers=headers)
        assert second.status_code == 200, second.text

        # Same recorded outcome on the replay; broker NEVER re-invoked.
        assert second.json() == first.json()
        assert len(spy.calls) == 1  # place_order called exactly once TOTAL
        assert spy.status_calls == []
        # Still exactly one cosigned record (co-sign is one-shot).
        rows = _decision_rows(uid)
        assert len(rows) == 1 and rows[0]["status"] == "cosigned"
    finally:
        client.app.dependency_overrides.pop(get_broker, None)
        _delete_user(email)


def test_approve_unknown_decision_id_returns_404(client):
    # (e) An unknown decision_id → 404; broker untouched, nothing persisted.
    email = _unique_email()
    spy = _SpyAdapter()
    client.app.dependency_overrides[get_broker] = lambda: spy
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        uid = _user_id_for(email)
        _insert_token_sync(uid, _live())

        resp = client.post(
            "/api/coach/approve",
            json={
                "decision_id": str(uuid.uuid4()),  # never persisted
                "order_intent": {"symbol": "VTI", "side": "buy", "amount": "500"},
            },
            headers=headers,
        )
        assert resp.status_code == 404, resp.text
        assert spy.calls == []  # broker NEVER called
        assert _decision_rows(uid) == []  # nothing persisted
    finally:
        client.app.dependency_overrides.pop(get_broker, None)
        _delete_user(email)


def test_approve_foreign_decision_id_returns_404(client):
    # (e, foreign variant) A decision_id owned by ANOTHER user is invisible under
    # the caller's scope → 404; broker untouched, the foreign record stays proposed.
    owner_email = _unique_email()
    attacker_email = _unique_email()
    spy = _SpyAdapter()
    client.app.dependency_overrides[get_broker] = lambda: spy
    try:
        # Owner recommends → a proposed record owned by them.
        _register(client, owner_email)
        owner_token = _login(client, owner_email)
        owner_headers = {"Authorization": f"Bearer {owner_token}"}
        owner_uid = _user_id_for(owner_email)
        foreign_decision_id = _recommend_decision_id(client, owner_headers)

        # Attacker (different user) tries to approve the owner's decision_id.
        _register(client, attacker_email)
        attacker_token = _login(client, attacker_email)
        attacker_headers = {"Authorization": f"Bearer {attacker_token}"}
        _insert_token_sync(_user_id_for(attacker_email), _live())

        resp = client.post(
            "/api/coach/approve",
            json={
                "decision_id": foreign_decision_id,
                "order_intent": {"symbol": "VTI", "side": "buy", "amount": "500"},
            },
            headers=attacker_headers,
        )
        assert resp.status_code == 404, resp.text  # foreign row invisible
        assert spy.calls == []  # broker NEVER called
        # The owner's record is untouched (still proposed).
        owner_rows = _decision_rows(owner_uid)
        assert len(owner_rows) == 1 and owner_rows[0]["status"] == "proposed"
    finally:
        client.app.dependency_overrides.pop(get_broker, None)
        _delete_user(owner_email)
        _delete_user(attacker_email)


def test_decision_record_sole_writer_canary():
    # (g) AD-6: DecisionRecord(...) CONSTRUCTION appears ONLY in
    # coach/decision_record.py (its model is DEFINED in db/models.py). No other
    # module — API handlers included — may construct or persist the model.
    import pathlib

    backend = pathlib.Path(__file__).resolve().parent.parent
    offenders: list[str] = []
    for path in backend.rglob("*.py"):
        rel = path.relative_to(backend)
        parts = rel.parts
        if parts and parts[0] in {"tests", ".venv"}:
            continue
        if rel.as_posix() in {
            "db/models.py",  # the model DEFINITION (class DecisionRecord)
            "coach/decision_record.py",  # the SOLE writer
        }:
            continue
        text = path.read_text(encoding="utf-8")
        if "DecisionRecord(" in text:
            offenders.append(rel.as_posix())
    assert offenders == [], (
        "DecisionRecord(...) must be constructed ONLY by coach.decision_record "
        f"(AD-6). Unexpected constructors: {offenders}"
    )


# =============================================================================
# STORY 6.1 — ATOMIC DECISION CLAIM & IDEMPOTENCY HARDENING
# =============================================================================


class _BlockingSpyAdapter(BrokerPort):
    """A broker whose ``place_order`` BLOCKS until the test releases it.

    Lets a test hold one approve's placement in-flight while a SECOND approve of
    the SAME decision runs on another thread — proving the atomic claim admits
    exactly one placer. Coordination uses ``threading.Event`` (cross-thread /
    cross-event-loop safe, unlike ``asyncio.Event``); the async ``place_order``
    polls it with ``asyncio.sleep`` so it yields its own loop while waiting.
    Records call args so ``place_order``-called-exactly-once is asserted.
    """

    provider = "fake"

    def __init__(self) -> None:
        import threading

        self.calls: list[tuple[OrderIntent, str]] = []
        self.status_calls: list[str] = []
        self.entered = threading.Event()  # set when place_order is first entered
        self.release = threading.Event()  # test sets this to let place_order finish
        self._delegate = FakeBrokerAdapter()

    def authorization_url(self, state: str) -> str:
        return self._delegate.authorization_url(state)

    def exchange_code(self, code: str, state: str) -> BrokerTokens:
        return self._delegate.exchange_code(code, state)

    def fetch_portfolio(self) -> PortfolioSnapshot:
        return self._delegate.fetch_portfolio()

    async def place_order(
        self, order_intent: OrderIntent, *, idempotency_key: str
    ) -> OrderOutcome:
        self.calls.append((order_intent, idempotency_key))
        self.entered.set()
        while not self.release.is_set():
            await asyncio.sleep(0.01)
        return await self._delegate.place_order(
            order_intent, idempotency_key=idempotency_key
        )

    async def get_order_status(self, idempotency_key: str) -> OrderOutcome:
        self.status_calls.append(idempotency_key)
        return await self._delegate.get_order_status(idempotency_key)


def _proposed_decision_row(owner: uuid.UUID) -> dict:
    return _decision_rows(owner)[0]


@pytest.mark.asyncio
async def test_two_session_claim_one_wins_one_loses(client):
    # (b) Two genuine AsyncSession claim_for_cosign calls on the SAME proposed id
    # return True then False — only one wins the atomic proposed→cosigning claim.
    email = _unique_email()
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        uid = _user_id_for(email)
        decision_id = uuid.UUID(_recommend_decision_id(client, headers))
        scope = Scope.for_user(uid)

        async with async_session_maker() as s1, async_session_maker() as s2:
            won_first = await claim_for_cosign(decision_id, scope=scope, session=s1)
            won_second = await claim_for_cosign(decision_id, scope=scope, session=s2)

        assert won_first is True
        assert won_second is False
        # The record is now cosigning (claimed, not yet cosigned).
        assert _proposed_decision_row(uid)["status"] == "cosigning"

        # release_claim reverses it back to proposed (retryable).
        async with async_session_maker() as s3:
            await release_claim(decision_id, scope=scope, session=s3)
        assert _proposed_decision_row(uid)["status"] == "proposed"
    finally:
        _delete_user(email)


def test_two_in_flight_approves_place_order_exactly_once(client):
    # (a) Two overlapping /approve on the same proposed decision with placement
    # PAUSED (blocking broker): place_order is called EXACTLY once, one caller
    # 200s and the other gets 409-in-progress, and the record ends cosigned.
    email = _unique_email()
    broker = _BlockingSpyAdapter()
    client.app.dependency_overrides[get_broker] = lambda: broker
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        uid = _user_id_for(email)
        _insert_token_sync(uid, _live())
        decision_id = _recommend_decision_id(client, headers)
        body = {
            "decision_id": decision_id,
            "order_intent": {"symbol": "VTI", "side": "buy", "amount": "500"},
        }

        import threading

        results: dict[str, int] = {}

        def _approve(tag: str) -> None:
            resp = client.post("/api/coach/approve", json=body, headers=headers)
            results[tag] = resp.status_code

        # Winner starts and blocks inside place_order (claim already committed to
        # cosigning). The second approve then runs and must see cosigning → 409.
        t1 = threading.Thread(target=_approve, args=("first",))
        t1.start()
        # Wait until the winner is inside place_order (claim committed).
        assert broker.entered.wait(timeout=10), "place_order was never entered"

        # Second approve of the SAME decision while the first is in-flight.
        resp2 = client.post("/api/coach/approve", json=body, headers=headers)

        # Let the winner finish and join.
        broker.release.set()
        t1.join(timeout=10)
        assert not t1.is_alive()

        # EXACTLY one placement, ever.
        assert len(broker.calls) == 1
        # One 200 (winner), one 409-in-progress (loser saw cosigning).
        assert results["first"] == 200
        assert resp2.status_code == 409
        assert resp2.json()["error"]["message"] == IN_PROGRESS_MESSAGE
        # The record ends cosigned.
        rows = _decision_rows(uid)
        assert len(rows) == 1 and rows[0]["status"] == "cosigned"
    finally:
        broker.release.set()  # ensure no thread is left blocked on teardown
        client.app.dependency_overrides.pop(get_broker, None)
        _delete_user(email)


def test_refusal_releases_claim_record_back_to_proposed(client):
    # (c) A won claim that then hits an integrity 409 (provider mismatch) or a
    # scope 422 (out-of-scope symbol) RELEASES the claim: the record is back at
    # proposed (not stuck cosigning) and the broker was never touched.
    email = _unique_email()
    spy = _SpyAdapter()  # provider "fake"
    client.app.dependency_overrides[get_broker] = lambda: spy
    # A live session whose provider disagrees → SessionIntegrityError after claim.
    client.app.dependency_overrides[require_live_broker_session] = (
        lambda: _live_session(provider="schwab")
    )
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        uid = _user_id_for(email)
        _insert_token_sync(uid, _live())
        decision_id = _recommend_decision_id(client, headers)

        resp = client.post(
            "/api/coach/approve",
            json={
                "decision_id": decision_id,
                "order_intent": {"symbol": "VTI", "side": "buy", "amount": "500"},
            },
            headers=headers,
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["message"] == RECONNECT_MESSAGE
        assert spy.calls == []  # broker NEVER touched
        # Claim released → record back to proposed (retryable, not stuck).
        rows = _decision_rows(uid)
        assert len(rows) == 1 and rows[0]["status"] == "proposed"
    finally:
        client.app.dependency_overrides.pop(get_broker, None)
        client.app.dependency_overrides.pop(require_live_broker_session, None)
        _delete_user(email)

    # Scope-422 variant: an out-of-scope symbol releases the claim too.
    email2 = _unique_email()
    spy2 = _SpyAdapter()
    client.app.dependency_overrides[get_broker] = lambda: spy2
    try:
        _register(client, email2)
        token2 = _login(client, email2)
        headers2 = {"Authorization": f"Bearer {token2}"}
        uid2 = _user_id_for(email2)
        _insert_token_sync(uid2, _live())
        decision_id2 = _recommend_decision_id(client, headers2)

        resp2 = client.post(
            "/api/coach/approve",
            json={
                "decision_id": decision_id2,
                "order_intent": {"symbol": "AAPL", "side": "buy", "amount": "500"},
            },
            headers=headers2,
        )
        assert resp2.status_code == 422, resp2.text
        assert spy2.calls == []
        rows2 = _decision_rows(uid2)
        assert len(rows2) == 1 and rows2[0]["status"] == "proposed"
    finally:
        client.app.dependency_overrides.pop(get_broker, None)
        _delete_user(email2)


def test_generic_placement_error_releases_claim(client):
    # (c2) A won claim whose placement raises an UNEXPECTED error (not one of the
    # two typed refusals) STILL releases the claim: the record returns to
    # proposed (retryable), never stranded in cosigning. This is the spec's
    # "any error before a successful placement, the claim is RELEASED"
    # guarantee — without it a real broker timeout/bug would leave the decision
    # stuck cosigning forever (every retry a calm 409, invisible to history).
    class _BoomAdapter(_SpyAdapter):
        async def place_order(self, order_intent, *, idempotency_key):
            self.calls.append((order_intent, idempotency_key))
            raise RuntimeError("broker exploded mid-placement")

    email = _unique_email()
    boom = _BoomAdapter()  # provider "fake" → passes the integrity gate
    client.app.dependency_overrides[get_broker] = lambda: boom
    client.app.dependency_overrides[require_live_broker_session] = (
        lambda: _live_session(provider="fake")
    )
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        uid = _user_id_for(email)
        _insert_token_sync(uid, _live())
        decision_id = _recommend_decision_id(client, headers)

        with pytest.raises(RuntimeError, match="broker exploded"):
            client.post(
                "/api/coach/approve",
                json={
                    "decision_id": decision_id,
                    "order_intent": {
                        "symbol": "VTI",
                        "side": "buy",
                        "amount": "500",
                    },
                },
                headers=headers,
            )
        # Placement was ATTEMPTED once, then the claim was released: the record
        # is back at proposed (retryable), NOT stuck cosigning.
        assert len(boom.calls) == 1
        rows = _decision_rows(uid)
        assert len(rows) == 1 and rows[0]["status"] == "proposed"
    finally:
        client.app.dependency_overrides.pop(get_broker, None)
        client.app.dependency_overrides.pop(require_live_broker_session, None)
        _delete_user(email)


def test_duplicate_idempotency_key_is_uninsertable(client):
    # (d) The DB unique index on decision_record.idempotency_key rejects a
    # duplicate key (the structural backstop; unreachable via the normal flow).
    email = _unique_email()
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        uid = _user_id_for(email)
        decision_id = _recommend_decision_id(client, headers)
        existing_key = _proposed_decision_row(uid)["idempotency_key"]
        assert existing_key

        # Attempt to insert a second decision_record reusing the SAME key.
        import psycopg

        with pytest.raises(psycopg.errors.UniqueViolation):
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO decision_record "
                        "(id, owner_id, schema_version, recommendation_snapshot, "
                        " status, idempotency_key, created_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (
                            str(uuid.uuid4()),
                            str(uid),
                            1,
                            "{}",
                            "proposed",
                            existing_key,  # DUPLICATE → UniqueViolation
                            datetime.now(timezone.utc),
                        ),
                    )
                conn.commit()
    finally:
        _delete_user(email)


# =============================================================================
# STORY 4.10 — DECISIONS HISTORY & REPLAY (read-only)
# =============================================================================


def _cosign_one(client: TestClient, headers: dict) -> str:
    """Drive the happy-path recommend→approve so ONE decision is cosigned.

    Returns the cosigned ``decision_id``. The caller must have a live session
    linked (``_insert_token_sync(..., _live())``) and a broker override in place.
    """
    decision_id = _recommend_decision_id(client, headers)
    resp = client.post(
        "/api/coach/approve",
        json={
            "decision_id": decision_id,
            "order_intent": {"symbol": "VTI", "side": "buy", "amount": "500"},
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return decision_id


def test_list_decisions_cosigned_only_newest_first_owner_scoped(client):
    # (a) GET /decisions returns ONLY this user's cosigned decisions, newest-first
    # by co_signed_at; an un-cosigned proposed record is excluded.
    email = _unique_email()
    spy = _SpyAdapter()
    client.app.dependency_overrides[get_broker] = lambda: spy
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        _insert_token_sync(_user_id_for(email), _live())

        first_id = _cosign_one(client, headers)
        second_id = _cosign_one(client, headers)
        # A proposed-but-never-cosigned record must NOT appear in the list.
        proposed_id = _recommend_decision_id(client, headers)

        resp = client.get("/api/coach/decisions", headers=headers)
        assert resp.status_code == 200, resp.text
        decisions = resp.json()["decisions"]

        ids = [d["decision_id"] for d in decisions]
        assert set(ids) == {first_id, second_id}  # cosigned only
        assert proposed_id not in ids  # proposed excluded
        # Newest co-sign first: the second cosign precedes the first.
        assert ids == [second_id, first_id]
        # Summary shape: executed symbol + reconciled outcome status.
        for d in decisions:
            assert d["action_label"]
            assert d["symbol"] == "VTI"
            assert d["co_signed_at"]
            assert d["outcome_status"] == "filled"
    finally:
        client.app.dependency_overrides.pop(get_broker, None)
        _delete_user(email)


def test_list_decisions_empty_history(client):
    # (a, empty) A user with no cosigned records gets an empty list, not an error.
    email = _unique_email()
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}

        resp = client.get("/api/coach/decisions", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["decisions"] == []
    finally:
        _delete_user(email)


def test_detail_returns_verbatim_snapshots_and_metadata(client):
    # (b) GET /decisions/{id} reproduces the persisted snapshots + metadata
    # VERBATIM — the recommendation_snapshot equals what was persisted at
    # /recommend time, and the cosign_snapshot carries the executed intent +
    # reconciled outcome. Nothing recomputed.
    email = _unique_email()
    spy = _SpyAdapter()
    client.app.dependency_overrides[get_broker] = lambda: spy
    _clean_market([SYM])
    _insert_series(SYM, [Decimal("100"), Decimal("92")])
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}
        uid = _user_id_for(email)
        _insert_token_sync(uid, _live())

        rec = client.post(
            "/api/coach/recommend",
            json={"symbol": SYM, "question": "Should I invest now?"},
            headers=headers,
        )
        decision_id = rec.json()["decision_id"]
        resp = client.post(
            "/api/coach/approve",
            json={
                "decision_id": decision_id,
                "order_intent": {"symbol": "VTI", "side": "buy", "amount": "500"},
            },
            headers=headers,
        )
        assert resp.status_code == 200, resp.text

        # The persisted row is the source of truth for "verbatim".
        row = _decision_rows(uid)[0]

        detail = client.get(f"/api/coach/decisions/{decision_id}", headers=headers)
        assert detail.status_code == 200, detail.text
        body = detail.json()

        assert body["decision_id"] == decision_id
        assert body["schema_version"] == row["schema_version"] == 1
        assert body["status"] == "cosigned"
        assert body["created_at"]
        assert body["co_signed_at"]
        # Snapshots returned EXACTLY as persisted (dict passthrough).
        assert body["recommendation_snapshot"] == row["recommendation_snapshot"]
        assert body["cosign_snapshot"] == row["cosign_snapshot"]
        # The replay carries reasoning, evidence records, uncertainties.
        snap = body["recommendation_snapshot"]
        assert snap["reasoning"].strip()
        assert len(snap["evidence"]) >= 1
        for evidence in snap["evidence"]:
            assert set(evidence) == {
                "id",
                "kind",
                "statement",
                "stats",
                "source",
                "as_of",
            }
        assert any(u.strip() for u in snap["uncertainties"])
        # The cosign snapshot carries the executed intent + reconciled outcome.
        assert body["cosign_snapshot"]["order_intent"] == {
            "symbol": "VTI",
            "side": "buy",
            "amount": "500",
        }
        assert body["cosign_snapshot"]["outcome"]["status"] == "filled"
    finally:
        client.app.dependency_overrides.pop(get_broker, None)
        _clean_market([SYM])
        _delete_user(email)


def test_detail_unknown_decision_id_returns_404(client):
    # (c) An unknown decision_id → 404; nothing leaked.
    email = _unique_email()
    try:
        _register(client, email)
        token = _login(client, email)
        headers = {"Authorization": f"Bearer {token}"}

        resp = client.get(
            f"/api/coach/decisions/{uuid.uuid4()}", headers=headers
        )
        assert resp.status_code == 404, resp.text
    finally:
        _delete_user(email)


def test_detail_foreign_decision_id_returns_404(client):
    # (c, foreign) A decision_id owned by ANOTHER user is invisible → 404.
    owner_email = _unique_email()
    attacker_email = _unique_email()
    spy = _SpyAdapter()
    client.app.dependency_overrides[get_broker] = lambda: spy
    try:
        _register(client, owner_email)
        owner_token = _login(client, owner_email)
        owner_headers = {"Authorization": f"Bearer {owner_token}"}
        _insert_token_sync(_user_id_for(owner_email), _live())
        foreign_id = _cosign_one(client, owner_headers)

        _register(client, attacker_email)
        attacker_token = _login(client, attacker_email)
        attacker_headers = {"Authorization": f"Bearer {attacker_token}"}

        resp = client.get(
            f"/api/coach/decisions/{foreign_id}", headers=attacker_headers
        )
        assert resp.status_code == 404, resp.text  # foreign row invisible
    finally:
        client.app.dependency_overrides.pop(get_broker, None)
        _delete_user(owner_email)
        _delete_user(attacker_email)


def test_cross_user_isolation_list_and_detail(client):
    # (d) User B's cosigned decision is ABSENT from A's list and 404 on A's detail.
    a_email = _unique_email()
    b_email = _unique_email()
    spy = _SpyAdapter()
    client.app.dependency_overrides[get_broker] = lambda: spy
    try:
        # B cosigns one decision.
        _register(client, b_email)
        b_token = _login(client, b_email)
        b_headers = {"Authorization": f"Bearer {b_token}"}
        _insert_token_sync(_user_id_for(b_email), _live())
        b_id = _cosign_one(client, b_headers)

        # A cosigns their own.
        _register(client, a_email)
        a_token = _login(client, a_email)
        a_headers = {"Authorization": f"Bearer {a_token}"}
        _insert_token_sync(_user_id_for(a_email), _live())
        a_id = _cosign_one(client, a_headers)

        # A's list contains only A's decision — never B's.
        resp = client.get("/api/coach/decisions", headers=a_headers)
        assert resp.status_code == 200, resp.text
        ids = [d["decision_id"] for d in resp.json()["decisions"]]
        assert ids == [a_id]
        assert b_id not in ids

        # A cannot read B's decision by id.
        detail = client.get(f"/api/coach/decisions/{b_id}", headers=a_headers)
        assert detail.status_code == 404, detail.text
    finally:
        client.app.dependency_overrides.pop(get_broker, None)
        _delete_user(a_email)
        _delete_user(b_email)
