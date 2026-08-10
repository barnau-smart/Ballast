"""Story 9.3 tests — the just-in-time liquidation + deferred/resumed buy endpoints.

REAL DB (docker Postgres), TestClient + register + JWT login, per-test cleanup —
the endpoint-test convention. Verifies the four ``/api/cash`` endpoints over the
wire, entirely offline (fake LLM/broker):

- ``POST /liquidation-plan`` — sufficient cash (no pending buy); a coverable
  shortfall (creates a durable PendingBuy + a proposed SELL decision, dedupes on
  a repeat); money as fixed-point strings + ``as_of`` carried.
- ``GET /pending-buys`` — lists awaiting-funds buys with a LIVE ``funds_ready``.
- ``POST /pending-buys/{id}/resume`` — guarded on ``funds_ready`` (calm 409 when
  not), transitions to ``resumed`` and mints a proposed BUY decision.
- ``POST /pending-buys/{id}/cancel`` — transitions to ``cancelled``.
- 401 on every endpoint when unauthenticated.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from db.connection import get_connection

PASSWORD = "supersecret123"
AS_OF = datetime(2026, 7, 27, tzinfo=timezone.utc)


@pytest.fixture
def client() -> TestClient:
    with TestClient(create_app()) as c:
        yield c


def _unique_email() -> str:
    return f"liq-ep-{uuid.uuid4().hex}@example.com"


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


def _seed_balance(uid: uuid.UUID, cash: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO portfolio_balance (id, owner_id, cash, as_of) "
                "VALUES (%s, %s, %s, %s)",
                (str(uuid.uuid4()), str(uid), cash, AS_OF),
            )
        conn.commit()


def _bump_balance(uid: uuid.UUID, cash: str) -> None:
    """Advance the ready-to-trade cash (simulate a settled sell) — newer as_of."""
    newer = datetime(2026, 7, 28, tzinfo=timezone.utc)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE portfolio_balance SET cash = %s, as_of = %s "
                "WHERE owner_id = %s",
                (cash, newer, str(uid)),
            )
        conn.commit()


def _seed_holding(uid: uuid.UUID, symbol: str, quantity: str, market_value: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO portfolio_cache "
                "(id, owner_id, symbol, quantity, market_value, cost_basis, cash, as_of) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    str(uuid.uuid4()),
                    str(uid),
                    symbol,
                    quantity,
                    market_value,
                    None,
                    "0",
                    AS_OF,
                ),
            )
        conn.commit()


def _seed_config(
    uid: uuid.UUID,
    *,
    reserve_amount: str | None,
    reserve_decided: bool,
    parked_symbols: list[str],
) -> None:
    import json

    now = datetime.now(timezone.utc)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO cash_config "
                "(id, owner_id, reserve_amount, reserve_decided, parked_symbols, "
                " created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    str(uuid.uuid4()),
                    str(uid),
                    reserve_amount,
                    reserve_decided,
                    json.dumps(parked_symbols),
                    now,
                    now,
                ),
            )
        conn.commit()


def _decision_status(decision_id: str) -> str | None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM decision_record WHERE id = %s", (decision_id,)
            )
            row = cur.fetchone()
    return None if row is None else row[0]


# --- tests -------------------------------------------------------------------


def test_plan_sufficient_cash_no_pending_buy(client):
    email = _unique_email()
    try:
        _register(client, email)
        uid = _user_id_for(email)
        _seed_balance(uid, "1000.00")
        _seed_config(
            uid, reserve_amount=None, reserve_decided=False, parked_symbols=["SWVXX"]
        )
        headers = {"Authorization": f"Bearer {_login(client, email)}"}

        r = client.post(
            "/api/cash/liquidation-plan",
            headers=headers,
            json={"symbol": "VTI", "amount": "500.00"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["needs_liquidation"] is False
        assert body["pending_buy_id"] is None
        assert body["sell_decision_id"] is None
        assert body["ready_to_trade"] == "1000.00"  # fixed-point string
        assert body["as_of"] is not None

        # No pending buy was created.
        r = client.get("/api/cash/pending-buys", headers=headers)
        assert r.json()["pending_buys"] == []
    finally:
        _delete_user(email)


def test_plan_rejects_invalid_buy_amount(client):
    # Fail-closed money path (mirrors the 9-1 reserve guard): a non-positive,
    # over-range, or over-precision buy amount is a calm 422, never a raw 500 or a
    # silently-rounded/corrupt figure — and mints no durable pending buy.
    email = _unique_email()
    try:
        _register(client, email)
        uid = _user_id_for(email)
        _seed_balance(uid, "100.00")
        _seed_config(
            uid, reserve_amount="0", reserve_decided=True, parked_symbols=["SWVXX"]
        )
        headers = {"Authorization": f"Bearer {_login(client, email)}"}

        for bad in ("0", "-5.00", "700.005", "NaN", "Infinity", "1e19"):
            r = client.post(
                "/api/cash/liquidation-plan",
                headers=headers,
                json={"symbol": "VTI", "amount": bad},
            )
            assert r.status_code == 422, f"{bad!r} -> {r.status_code}: {r.text}"

        # None of the rejected requests created a durable pending buy.
        r = client.get("/api/cash/pending-buys", headers=headers)
        assert r.json()["pending_buys"] == []
    finally:
        _delete_user(email)


def test_plan_coverable_creates_pending_buy_and_sell_decision_and_dedupes(client):
    email = _unique_email()
    try:
        _register(client, email)
        uid = _user_id_for(email)
        _seed_balance(uid, "200.00")
        _seed_holding(uid, "SWVXX", "5000", "5000.00")
        _seed_config(
            uid, reserve_amount="0", reserve_decided=True, parked_symbols=["SWVXX"]
        )
        headers = {"Authorization": f"Bearer {_login(client, email)}"}

        r = client.post(
            "/api/cash/liquidation-plan",
            headers=headers,
            json={"symbol": "VTI", "amount": "700.00"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["needs_liquidation"] is True
        assert body["coverable"] is True
        assert body["shortfall"] == "500.00"
        assert body["sell_symbol"] == "SWVXX"
        assert body["sell_amount"] == "500.00"
        assert body["est_shares"] == 500
        assert body["sell_order_intent"]["side"] == "sell"
        assert body["sell_order_intent"]["amount"] == "500.00"
        first_pending = body["pending_buy_id"]
        sell_decision_id = body["sell_decision_id"]
        assert first_pending is not None
        assert sell_decision_id is not None
        # The proposed SELL decision exists and is 'proposed'.
        assert _decision_status(sell_decision_id) == "proposed"

        # A repeat plan for the SAME (symbol, amount) DEDUPES the pending buy.
        r2 = client.post(
            "/api/cash/liquidation-plan",
            headers=headers,
            json={"symbol": "VTI", "amount": "700.00"},
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["pending_buy_id"] == first_pending

        # Exactly one awaiting pending buy exists.
        r = client.get("/api/cash/pending-buys", headers=headers)
        pending = r.json()["pending_buys"]
        assert len(pending) == 1
        assert pending[0]["pending_buy_id"] == first_pending
        assert pending[0]["amount"] == "700.00"
        assert pending[0]["funds_ready"] is False  # 200 < 700
    finally:
        _delete_user(email)


def test_plan_no_parked_creates_pending_buy_without_sell(client):
    email = _unique_email()
    try:
        _register(client, email)
        uid = _user_id_for(email)
        _seed_balance(uid, "100.00")
        _seed_config(
            uid, reserve_amount=None, reserve_decided=False, parked_symbols=[]
        )
        headers = {"Authorization": f"Bearer {_login(client, email)}"}

        r = client.post(
            "/api/cash/liquidation-plan",
            headers=headers,
            json={"symbol": "VTI", "amount": "500.00"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["needs_liquidation"] is True
        assert body["coverable"] is False
        assert body["sell_symbol"] is None
        assert body["sell_decision_id"] is None
        # A durable pending buy is still recorded so the intent survives.
        assert body["pending_buy_id"] is not None
    finally:
        _delete_user(email)


def test_plan_partial_coverage_mints_submittable_sell_decision(client):
    # Regression for the review finding: a PARTIAL shortfall (the single largest
    # parked fund can only cover part of it, coverable=False) must still return a
    # SUBMITTABLE sell — a proposed SELL decision with an id — so the user can free
    # up what they can now and the rest resumes. Previously the endpoint minted the
    # decision only when fully coverable, leaving the partial sell a UI dead-end.
    email = _unique_email()
    try:
        _register(client, email)
        uid = _user_id_for(email)
        _seed_balance(uid, "100.00")
        # One parked fund worth $300, no reserve → sellable $300 < $900 shortfall.
        _seed_holding(uid, "SWVXX", "300", "300.00")
        _seed_config(
            uid, reserve_amount="0", reserve_decided=True, parked_symbols=["SWVXX"]
        )
        headers = {"Authorization": f"Bearer {_login(client, email)}"}

        r = client.post(
            "/api/cash/liquidation-plan",
            headers=headers,
            json={"symbol": "VTI", "amount": "1000.00"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["needs_liquidation"] is True
        assert body["coverable"] is False  # $300 covers only part of the $900 short
        assert body["shortfall"] == "900.00"
        assert body["sell_symbol"] == "SWVXX"
        assert body["sell_amount"] == "300.00"
        assert body["sell_order_intent"]["side"] == "sell"
        assert body["sell_order_intent"]["amount"] == "300.00"
        # The partial sell IS submittable: a proposed SELL decision was minted.
        sell_decision_id = body["sell_decision_id"]
        assert sell_decision_id is not None
        assert _decision_status(sell_decision_id) == "proposed"
        assert body["pending_buy_id"] is not None
    finally:
        _delete_user(email)


def test_resume_refused_when_funds_not_ready(client):
    email = _unique_email()
    try:
        _register(client, email)
        uid = _user_id_for(email)
        _seed_balance(uid, "200.00")
        _seed_holding(uid, "SWVXX", "5000", "5000.00")
        _seed_config(
            uid, reserve_amount="0", reserve_decided=True, parked_symbols=["SWVXX"]
        )
        headers = {"Authorization": f"Bearer {_login(client, email)}"}

        plan = client.post(
            "/api/cash/liquidation-plan",
            headers=headers,
            json={"symbol": "VTI", "amount": "700.00"},
        ).json()
        pending_id = plan["pending_buy_id"]

        # Funds haven't settled (200 < 700) → calm 409, no decision minted.
        r = client.post(
            f"/api/cash/pending-buys/{pending_id}/resume", headers=headers
        )
        assert r.status_code == 409, r.text
        # The app wraps HTTPException in a calm {error:{type,message}} envelope.
        assert "settle" in r.json()["error"]["message"].lower()

        # Still awaiting.
        pending = client.get("/api/cash/pending-buys", headers=headers).json()[
            "pending_buys"
        ]
        assert pending[0]["status"] == "awaiting_funds"
    finally:
        _delete_user(email)


def test_resume_when_funds_ready_transitions_and_mints_buy_decision(client):
    email = _unique_email()
    try:
        _register(client, email)
        uid = _user_id_for(email)
        _seed_balance(uid, "200.00")
        _seed_holding(uid, "SWVXX", "5000", "5000.00")
        _seed_config(
            uid, reserve_amount="0", reserve_decided=True, parked_symbols=["SWVXX"]
        )
        headers = {"Authorization": f"Bearer {_login(client, email)}"}

        plan = client.post(
            "/api/cash/liquidation-plan",
            headers=headers,
            json={"symbol": "VTI", "amount": "700.00"},
        ).json()
        pending_id = plan["pending_buy_id"]

        # Simulate the sell settling: ready-to-trade cash rises to cover the buy.
        _bump_balance(uid, "800.00")

        # Now funds_ready is True on the list.
        pending = client.get("/api/cash/pending-buys", headers=headers).json()[
            "pending_buys"
        ]
        assert pending[0]["funds_ready"] is True

        r = client.post(
            f"/api/cash/pending-buys/{pending_id}/resume", headers=headers
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["order_intent"]["symbol"] == "VTI"
        assert body["order_intent"]["side"] == "buy"
        assert body["order_intent"]["amount"] == "700.00"
        buy_decision_id = body["decision_id"]
        assert _decision_status(buy_decision_id) == "proposed"

        # The pending buy is no longer awaiting (transitioned to resumed) → drops
        # off the awaiting list.
        pending = client.get("/api/cash/pending-buys", headers=headers).json()[
            "pending_buys"
        ]
        assert pending == []

        # A second resume is refused (no longer awaiting).
        r2 = client.post(
            f"/api/cash/pending-buys/{pending_id}/resume", headers=headers
        )
        assert r2.status_code == 409, r2.text
    finally:
        _delete_user(email)


def test_cancel_transitions_to_cancelled(client):
    email = _unique_email()
    try:
        _register(client, email)
        uid = _user_id_for(email)
        _seed_balance(uid, "100.00")
        _seed_config(
            uid, reserve_amount=None, reserve_decided=False, parked_symbols=[]
        )
        headers = {"Authorization": f"Bearer {_login(client, email)}"}

        plan = client.post(
            "/api/cash/liquidation-plan",
            headers=headers,
            json={"symbol": "VTI", "amount": "500.00"},
        ).json()
        pending_id = plan["pending_buy_id"]

        r = client.post(
            f"/api/cash/pending-buys/{pending_id}/cancel", headers=headers
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "cancelled"

        # Drops off the awaiting list; a second cancel is idempotent (200).
        pending = client.get("/api/cash/pending-buys", headers=headers).json()[
            "pending_buys"
        ]
        assert pending == []
        r2 = client.post(
            f"/api/cash/pending-buys/{pending_id}/cancel", headers=headers
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["status"] == "cancelled"
    finally:
        _delete_user(email)


def test_pending_buy_per_user_isolation(client):
    email_a = _unique_email()
    email_b = _unique_email()
    try:
        _register(client, email_a)
        _register(client, email_b)
        uid_a = _user_id_for(email_a)
        _seed_balance(uid_a, "100.00")
        _seed_config(
            uid_a, reserve_amount=None, reserve_decided=False, parked_symbols=[]
        )
        headers_a = {"Authorization": f"Bearer {_login(client, email_a)}"}
        headers_b = {"Authorization": f"Bearer {_login(client, email_b)}"}

        plan = client.post(
            "/api/cash/liquidation-plan",
            headers=headers_a,
            json={"symbol": "VTI", "amount": "500.00"},
        ).json()
        pending_id = plan["pending_buy_id"]

        # B never sees A's pending buy, and can't resume/cancel it (404).
        assert client.get("/api/cash/pending-buys", headers=headers_b).json()[
            "pending_buys"
        ] == []
        assert (
            client.post(
                f"/api/cash/pending-buys/{pending_id}/cancel", headers=headers_b
            ).status_code
            == 404
        )
        assert (
            client.post(
                f"/api/cash/pending-buys/{pending_id}/resume", headers=headers_b
            ).status_code
            == 404
        )
    finally:
        _delete_user(email_a)
        _delete_user(email_b)


def test_all_endpoints_require_auth(client):
    assert (
        client.post(
            "/api/cash/liquidation-plan", json={"symbol": "VTI", "amount": "500.00"}
        ).status_code
        == 401
    )
    assert client.get("/api/cash/pending-buys").status_code == 401
    fake_id = str(uuid.uuid4())
    assert client.post(f"/api/cash/pending-buys/{fake_id}/resume").status_code == 401
    assert client.post(f"/api/cash/pending-buys/{fake_id}/cancel").status_code == 401
