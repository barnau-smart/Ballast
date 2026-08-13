"""Story 10.5 tests — the linked cost-switch SELL + deferred BUY pair.

REAL DB (docker Postgres), TestClient + register + JWT login, per-test cleanup —
the endpoint-test convention, entirely offline (fake LLM/broker). Verifies:

- A SERVER-VERIFIED cost-switch SELL (co-signed) creates ONE linked
  ``awaiting_funds`` ``PendingBuy`` for the cheaper canonical fund, linked by
  ``sell_decision_id``; the ``/approve`` response carries ``linked_buy_queued=True``.
- The SECURITY gate: a client-supplied ``switch_to`` on an arbitrary/unheld symbol,
  a non-canonical target, or a self-switch is REJECTED — no snapshot key, no scope
  widening, no linked buy, and the out-of-scope SELL is refused (422). This closes
  the iteration-1 client-trusted scope-gate bypass.
- Dedupe on the originating ``sell_decision_id`` (replay is a no-op; two distinct
  switches each queue; a pre-seeded 9.3 deploy buy sharing (symbol, amount) is
  never conflated).
- ``_is_placed`` gating across every OrderStatus × broker_ref (a
  ``pending``/``timeout``-without-``broker_ref`` seeds nothing).
- Per-user scoping; a non-switch SELL creates none; read endpoints write nothing;
  sub-share proceeds degrade calmly at resume; a linkage failure never fails the
  placed SELL.

The FakeBrokerAdapter fills a MARKET order (with a ``broker_ref``), so a genuine
cost-switch SELL is ``filled`` end to end. ``AGTHX`` (0.61% ER, US_EQUITY,
NON-index-core) → ``VTI`` (0.03%) is a genuine cost switch (gap 0.58 ≥ 0.20).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from brokers.port import OrderOutcome, OrderStatus
from coach.execution import _is_placed
from db.connection import get_connection

PASSWORD = "supersecret123"
AS_OF = datetime(2026, 7, 27, tzinfo=timezone.utc)


@pytest.fixture
def client() -> TestClient:
    with TestClient(create_app()) as c:
        yield c


def _unique_email() -> str:
    return f"cost-switch-{uuid.uuid4().hex}@example.com"


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


def _seed_balance(uid: uuid.UUID, cash: str, as_of: datetime = AS_OF) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO portfolio_balance (id, owner_id, cash, as_of) "
                "VALUES (%s, %s, %s, %s)",
                (str(uuid.uuid4()), str(uid), cash, as_of),
            )
        conn.commit()


def _bump_balance(uid: uuid.UUID, cash: str) -> None:
    newer = datetime(2026, 7, 28, tzinfo=timezone.utc)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE portfolio_balance SET cash = %s, as_of = %s "
                "WHERE owner_id = %s",
                (cash, newer, str(uid)),
            )
        conn.commit()


def _seed_holding(
    uid: uuid.UUID, symbol: str, quantity: str, market_value: str
) -> None:
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


def _seed_config(uid: uuid.UUID, *, parked_symbols: list[str]) -> None:
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
                    "0",
                    True,
                    json.dumps(parked_symbols),
                    now,
                    now,
                ),
            )
        conn.commit()


def _insert_token(uid: uuid.UUID) -> None:
    from brokers.crypto import encrypt_token

    live = datetime.now(timezone.utc) + timedelta(days=3)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO brokerage_token "
                "(id, owner_id, provider, access_token, refresh_token, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    str(uuid.uuid4()),
                    str(uid),
                    "fake",
                    encrypt_token("access"),
                    encrypt_token("refresh"),
                    live,
                ),
            )
        conn.commit()


def _pending_buys(uid: uuid.UUID) -> list[dict]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, buy_intent, amount, status, sell_decision_id "
                "FROM pending_buy WHERE owner_id = %s",
                (str(uid),),
            )
            cols = [c.name for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def _decision_status(decision_id: str) -> str | None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM decision_record WHERE id = %s", (decision_id,)
            )
            row = cur.fetchone()
    return None if row is None else row[0]


def _setup_switch_user(client: TestClient, email: str) -> tuple[uuid.UUID, dict]:
    """Register + seed a user holding a high-fee AGTHX position (a genuine switch)."""
    _register(client, email)
    uid = _user_id_for(email)
    _seed_balance(uid, "50.00")
    # 20 shares @ $100 = $2000 held AGTHX (non-index-core, 0.61% ER). Its canonical
    # cheaper same-class fund is VTI (0.03%). gap 0.58 >= 0.20 → a genuine switch.
    _seed_holding(uid, "AGTHX", "20", "2000.00")
    _seed_config(uid, parked_symbols=[])
    _insert_token(uid)
    headers = {"Authorization": f"Bearer {_login(client, email)}"}
    return uid, headers


# --- _is_placed unit coverage (every OrderStatus × broker_ref) ---------------


def test_is_placed_across_status_and_broker_ref():
    def outcome(status, ref):
        return OrderOutcome(
            status=status, filled_qty=Decimal("0"), avg_price=None, broker_ref=ref
        )

    # filled/partial are placed regardless of broker_ref.
    assert _is_placed(outcome(OrderStatus.FILLED, "r")) is True
    assert _is_placed(outcome(OrderStatus.FILLED, None)) is True
    assert _is_placed(outcome(OrderStatus.PARTIAL, "r")) is True
    assert _is_placed(outcome(OrderStatus.PARTIAL, None)) is True
    # pending/timeout are placed ONLY with a broker_ref.
    assert _is_placed(outcome(OrderStatus.PENDING, "r")) is True
    assert _is_placed(outcome(OrderStatus.PENDING, None)) is False
    assert _is_placed(outcome(OrderStatus.TIMEOUT, "r")) is True
    assert _is_placed(outcome(OrderStatus.TIMEOUT, None)) is False
    # rejected is never placed.
    assert _is_placed(outcome(OrderStatus.REJECTED, "r")) is False
    assert _is_placed(outcome(OrderStatus.REJECTED, None)) is False


# --- happy path: verified switch co-sign queues ONE linked buy ---------------


def test_verified_cost_switch_queues_one_linked_pending_buy(client):
    email = _unique_email()
    try:
        uid, headers = _setup_switch_user(client, email)

        # /recommend with the genuine (AGTHX -> VTI) switch: the server verifies it.
        rec = client.post(
            "/api/coach/recommend",
            headers=headers,
            json={
                "symbol": "AGTHX",
                "side": "sell",
                "amount": "2000.00",
                "switch_to": "VTI",
            },
        )
        assert rec.status_code == 200, rec.text
        decision_id = rec.json()["decision_id"]

        # Approve the SELL — the fake broker fills it; the linked buy is queued.
        ap = client.post(
            "/api/coach/approve",
            headers=headers,
            json={
                "decision_id": decision_id,
                "order_intent": {
                    "symbol": "AGTHX",
                    "side": "sell",
                    "amount": "2000.00",
                },
            },
        )
        assert ap.status_code == 200, ap.text
        body = ap.json()
        assert body["status"] == "filled"
        assert body["linked_buy_queued"] is True

        # Exactly ONE linked awaiting_funds pending buy for VTI exists.
        rows = _pending_buys(uid)
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "awaiting_funds"
        assert str(row["buy_intent"]["symbol"]).upper() == "VTI"
        assert row["buy_intent"]["side"] == "buy"
        assert Decimal(str(row["amount"])) == Decimal("2000.00")
        assert str(row["sell_decision_id"]) == decision_id

        # It surfaces on the existing pending-buy list.
        lst = client.get("/api/cash/pending-buys", headers=headers)
        assert lst.status_code == 200
        pend = lst.json()["pending_buys"]
        assert len(pend) == 1
        assert pend[0]["buy_intent"]["symbol"] == "VTI"
        assert pend[0]["sell_decision_id"] == decision_id
    finally:
        _delete_user(email)


# --- SECURITY: arbitrary / non-canonical / self switch_to is rejected --------


@pytest.mark.parametrize(
    "sell_symbol,switch_to",
    [
        ("GME", "VTI"),        # arbitrary/unheld symbol
        ("AGTHX", "BND"),      # non-canonical target for AGTHX's class
        ("AGTHX", "AGTHX"),    # self-switch
    ],
)
def test_untrusted_switch_to_is_rejected_no_widening_no_buy(
    client, sell_symbol, switch_to
):
    email = _unique_email()
    try:
        uid, headers = _setup_switch_user(client, email)
        # Also hold GME so "unheld" isn't the only reason GME fails (it is still
        # not a cost finding — no ER — so no switch is derivable).
        _seed_holding(uid, "GME", "10", "5000.00")

        rec = client.post(
            "/api/coach/recommend",
            headers=headers,
            json={
                "symbol": sell_symbol,
                "side": "sell",
                "amount": "2000.00",
                "switch_to": switch_to,
            },
        )
        assert rec.status_code == 200, rec.text
        decision_id = rec.json()["decision_id"]

        # No switch_to was threaded onto the snapshot (server dropped it to None).
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT recommendation_snapshot FROM decision_record "
                    "WHERE id = %s",
                    (decision_id,),
                )
                (snap,) = cur.fetchone()
        assert "switch_to" not in (snap or {}), snap

        # Approving the out-of-scope SELL is REFUSED (422) — the beginner-guardrail
        # SELL scope gate is NOT widened by the untrusted switch_to. GME/AGTHX are
        # non-index-core, and the snapshot carried no verified switch.
        ap = client.post(
            "/api/coach/approve",
            headers=headers,
            json={
                "decision_id": decision_id,
                "order_intent": {
                    "symbol": sell_symbol,
                    "side": "sell",
                    "amount": "2000.00",
                },
            },
        )
        assert ap.status_code == 422, ap.text

        # No pending buy was created — the dead-buy footgun is closed.
        assert _pending_buys(uid) == []
        # The decision returns to 'proposed' (retryable), never cosigned.
        assert _decision_status(decision_id) == "proposed"
    finally:
        _delete_user(email)


# --- dedupe on sell_decision_id (replay no-op; two distinct switches queue) ---


def test_reapprove_is_noop_but_two_distinct_switches_each_queue(client):
    email = _unique_email()
    try:
        uid, headers = _setup_switch_user(client, email)

        def recommend_and_approve() -> str:
            rec = client.post(
                "/api/coach/recommend",
                headers=headers,
                json={
                    "symbol": "AGTHX",
                    "side": "sell",
                    "amount": "2000.00",
                    "switch_to": "VTI",
                },
            )
            assert rec.status_code == 200, rec.text
            did = rec.json()["decision_id"]
            ap = client.post(
                "/api/coach/approve",
                headers=headers,
                json={
                    "decision_id": did,
                    "order_intent": {
                        "symbol": "AGTHX",
                        "side": "sell",
                        "amount": "2000.00",
                    },
                },
            )
            assert ap.status_code == 200, ap.text
            return did

        did1 = recommend_and_approve()
        # Re-approve the SAME decision → idempotent recorded outcome, NO new buy.
        again = client.post(
            "/api/coach/approve",
            headers=headers,
            json={
                "decision_id": did1,
                "order_intent": {
                    "symbol": "AGTHX",
                    "side": "sell",
                    "amount": "2000.00",
                },
            },
        )
        assert again.status_code == 200, again.text
        assert len(_pending_buys(uid)) == 1

        # A DISTINCT switch SELL (its own decision) into the SAME (VTI, 2000) queues
        # its OWN buy — dedupe is on sell_decision_id, not (symbol, amount).
        did2 = recommend_and_approve()
        assert did2 != did1
        rows = _pending_buys(uid)
        assert len(rows) == 2
        sell_ids = {str(r["sell_decision_id"]) for r in rows}
        assert sell_ids == {did1, did2}
    finally:
        _delete_user(email)


def test_no_conflation_with_preexisting_93_deploy_buy(client):
    # A pre-existing Story 9.3 deploy/liquidation PendingBuy sharing (VTI, 2000) —
    # with a NULL sell_decision_id — must NOT be swallowed by the cost-switch
    # producer, and vice-versa.
    email = _unique_email()
    try:
        uid, headers = _setup_switch_user(client, email)
        # Pre-seed a 9.3-style deploy buy for the same (symbol, amount), null link.
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO pending_buy "
                    "(id, owner_id, buy_intent, amount, status, sell_decision_id, "
                    " created_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        str(uuid.uuid4()),
                        str(uid),
                        '{"symbol": "VTI", "side": "buy", "amount": "2000.00"}',
                        "2000.00",
                        "awaiting_funds",
                        None,
                        datetime.now(timezone.utc),
                    ),
                )
            conn.commit()

        rec = client.post(
            "/api/coach/recommend",
            headers=headers,
            json={
                "symbol": "AGTHX",
                "side": "sell",
                "amount": "2000.00",
                "switch_to": "VTI",
            },
        )
        decision_id = rec.json()["decision_id"]
        ap = client.post(
            "/api/coach/approve",
            headers=headers,
            json={
                "decision_id": decision_id,
                "order_intent": {
                    "symbol": "AGTHX",
                    "side": "sell",
                    "amount": "2000.00",
                },
            },
        )
        assert ap.status_code == 200, ap.text
        assert ap.json()["linked_buy_queued"] is True

        # TWO distinct awaiting buys now: the 9.3 deploy (null link) + the switch.
        rows = _pending_buys(uid)
        assert len(rows) == 2
        linked = [r for r in rows if r["sell_decision_id"] is not None]
        unlinked = [r for r in rows if r["sell_decision_id"] is None]
        assert len(linked) == 1 and len(unlinked) == 1
        assert str(linked[0]["sell_decision_id"]) == decision_id
    finally:
        _delete_user(email)


# --- non-switch SELL creates none; per-user scoping --------------------------


def test_non_switch_sell_creates_no_pending_buy(client):
    # A plain in-scope index-core SELL (no switch_to) never queues a linked buy.
    email = _unique_email()
    try:
        _register(client, email)
        uid = _user_id_for(email)
        _seed_balance(uid, "50.00")
        _seed_holding(uid, "VTI", "10", "2000.00")
        _seed_config(uid, parked_symbols=[])
        _insert_token(uid)
        headers = {"Authorization": f"Bearer {_login(client, email)}"}

        rec = client.post(
            "/api/coach/recommend",
            headers=headers,
            json={"symbol": "VTI", "side": "sell", "amount": "500.00"},
        )
        decision_id = rec.json()["decision_id"]
        ap = client.post(
            "/api/coach/approve",
            headers=headers,
            json={
                "decision_id": decision_id,
                "order_intent": {"symbol": "VTI", "side": "sell", "amount": "500.00"},
            },
        )
        assert ap.status_code == 200, ap.text
        assert ap.json()["linked_buy_queued"] is False
        assert _pending_buys(uid) == []
    finally:
        _delete_user(email)


def test_linked_buy_is_per_user_scoped(client):
    # The linked buy is reachable only through the owner's scope — a second user
    # never sees it.
    email_a = _unique_email()
    email_b = _unique_email()
    try:
        uid_a, headers_a = _setup_switch_user(client, email_a)
        _register(client, email_b)
        headers_b = {"Authorization": f"Bearer {_login(client, email_b)}"}

        rec = client.post(
            "/api/coach/recommend",
            headers=headers_a,
            json={
                "symbol": "AGTHX",
                "side": "sell",
                "amount": "2000.00",
                "switch_to": "VTI",
            },
        )
        did = rec.json()["decision_id"]
        client.post(
            "/api/coach/approve",
            headers=headers_a,
            json={
                "decision_id": did,
                "order_intent": {
                    "symbol": "AGTHX",
                    "side": "sell",
                    "amount": "2000.00",
                },
            },
        )
        # A owns one; B sees none.
        assert len(client.get("/api/cash/pending-buys", headers=headers_a).json()["pending_buys"]) == 1
        assert client.get("/api/cash/pending-buys", headers=headers_b).json()["pending_buys"] == []
    finally:
        _delete_user(email_a)
        _delete_user(email_b)


# --- calm sub-share degrade at resume (shared with the 9.3 path) -------------


def test_sub_share_switch_buy_degrades_calmly_at_resume(client):
    # Once the SELL settles, the linked BUY resumes — but if the settled proceeds
    # fall short of one whole share of VTI, the resume mints a proposed BUY (a
    # calm, human-co-signable step), and whole-share flooring at /approve refuses a
    # 0-share order calmly (422), never a phantom order. The shared resume path
    # (api/cash.resume_pending_buy + whole_share_quantity) handles this.
    email = _unique_email()
    try:
        uid, headers = _setup_switch_user(client, email)
        rec = client.post(
            "/api/coach/recommend",
            headers=headers,
            json={
                "symbol": "AGTHX",
                "side": "sell",
                "amount": "2000.00",
                "switch_to": "VTI",
            },
        )
        did = rec.json()["decision_id"]
        client.post(
            "/api/coach/approve",
            headers=headers,
            json={
                "decision_id": did,
                "order_intent": {
                    "symbol": "AGTHX",
                    "side": "sell",
                    "amount": "2000.00",
                },
            },
        )
        pend = client.get("/api/cash/pending-buys", headers=headers).json()["pending_buys"]
        pending_buy_id = pend[0]["pending_buy_id"]

        # Not enough cash to resume yet (50 < 2000) — calm 409, nothing minted.
        early = client.post(
            f"/api/cash/pending-buys/{pending_buy_id}/resume", headers=headers
        )
        assert early.status_code == 409, early.text

        # Settle enough to cover the buy amount (funds_ready), then resume.
        _bump_balance(uid, "2000.00")
        res = client.post(
            f"/api/cash/pending-buys/{pending_buy_id}/resume", headers=headers
        )
        assert res.status_code == 200, res.text
        resume_decision = res.json()["decision_id"]
        buy_intent = res.json()["order_intent"]
        assert buy_intent["symbol"] == "VTI"
        assert buy_intent["side"] == "buy"

        # The proposed BUY exists and is human-co-signable (never auto-placed).
        assert _decision_status(resume_decision) == "proposed"
    finally:
        _delete_user(email)


# --- linkage failure never fails a placed SELL -------------------------------


def test_linkage_failure_never_fails_placed_sell(client, monkeypatch):
    # If the linked-buy creation raises AFTER the SELL is placed + co-signed, the
    # SELL still succeeds (200) and linked_buy_queued=False — the co-sign state is
    # NOT rolled back. We force the failure by monkeypatching the creation helper.
    email = _unique_email()
    try:
        uid, headers = _setup_switch_user(client, email)

        async def _boom(*args, **kwargs):
            raise RuntimeError("simulated linkage failure")

        # Patch the lazily-imported creator in api.cash (imported inside the helper).
        import api.cash as cash_mod

        monkeypatch.setattr(cash_mod, "create_switch_pending_buy", _boom)

        rec = client.post(
            "/api/coach/recommend",
            headers=headers,
            json={
                "symbol": "AGTHX",
                "side": "sell",
                "amount": "2000.00",
                "switch_to": "VTI",
            },
        )
        did = rec.json()["decision_id"]
        ap = client.post(
            "/api/coach/approve",
            headers=headers,
            json={
                "decision_id": did,
                "order_intent": {
                    "symbol": "AGTHX",
                    "side": "sell",
                    "amount": "2000.00",
                },
            },
        )
        assert ap.status_code == 200, ap.text
        assert ap.json()["status"] == "filled"
        assert ap.json()["linked_buy_queued"] is False
        # The SELL co-sign committed durably (not rolled back) — no linked buy.
        assert _decision_status(did) == "cosigned"
        assert _pending_buys(uid) == []
    finally:
        _delete_user(email)


# --- PATCH 1: deploy dedupe must SKIP a cost-switch buy -----------------------


def test_deploy_dedupe_skips_cost_switch_buy(client):
    # The Story 9.3 deploy/liquidation dedupe (_find_awaiting_pending_buy, keyed on
    # (symbol, amount)) must NOT return a Story 10.5 cost-switch buy sharing the same
    # (symbol, amount) — else the deploy path would conflate two economically-distinct
    # buys and re-surface the cost-switch's sell_decision_id as the deploy's sell. The
    # 10.5 dedupe (by sell_decision_id) must still find it.
    import asyncio

    from api.cash import (
        _find_awaiting_pending_buy,
        _find_awaiting_pending_buy_by_sell_decision,
        create_switch_pending_buy,
    )
    from db.scope import Scope
    from db.session import async_session_maker

    email = _unique_email()
    try:
        uid, _ = _setup_switch_user(client, email)
        sell_decision_id = uuid.uuid4()

        async def _run():
            async with async_session_maker() as session:
                scope = Scope.for_user(uid)
                # Seed a cost-switch buy of (VTI, 2000.00) tagged source=cost_switch.
                created = await create_switch_pending_buy(
                    scope,
                    session,
                    switch_to="VTI",
                    amount=Decimal("2000.00"),
                    sell_decision_id=sell_decision_id,
                )
                await session.commit()
                assert created is not None
                assert (created.buy_intent or {}).get("source") == "cost_switch"

                # The deploy dedupe for the SAME (symbol, amount) must NOT return it.
                deploy_hit = await _find_awaiting_pending_buy(
                    scope, session, symbol="VTI", amount=Decimal("2000.00")
                )
                assert deploy_hit is None

                # The 10.5 dedupe (by sell_decision_id) DOES still return it.
                switch_hit = await _find_awaiting_pending_buy_by_sell_decision(
                    scope, session, sell_decision_id=sell_decision_id
                )
                assert switch_hit is not None
                assert str(switch_hit.id) == str(created.id)

        asyncio.run(_run())
    finally:
        _delete_user(email)


def test_deploy_dedupe_still_matches_a_genuine_deploy_row(client):
    # 9.3 idempotency preserved: a legacy/deploy awaiting_funds row (no `source` key)
    # sharing (symbol, amount) IS still matched by the deploy dedupe.
    import asyncio

    from api.cash import _find_awaiting_pending_buy
    from db.scope import Scope
    from db.session import async_session_maker

    email = _unique_email()
    try:
        _register(client, email)
        uid = _user_id_for(email)
        # Pre-seed a 9.3-style deploy buy (no source tag) for (VTI, 2000.00).
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO pending_buy "
                    "(id, owner_id, buy_intent, amount, status, sell_decision_id, "
                    " created_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        str(uuid.uuid4()),
                        str(uid),
                        '{"symbol": "VTI", "side": "buy", "amount": "2000.00"}',
                        "2000.00",
                        "awaiting_funds",
                        None,
                        datetime.now(timezone.utc),
                    ),
                )
            conn.commit()

        async def _run():
            async with async_session_maker() as session:
                scope = Scope.for_user(uid)
                hit = await _find_awaiting_pending_buy(
                    scope, session, symbol="VTI", amount=Decimal("2000.00")
                )
                assert hit is not None
                assert (hit.buy_intent or {}).get("source") is None

        asyncio.run(_run())
    finally:
        _delete_user(email)


# --- PATCH 3: linked_buy_queued is server-truth on idempotent re-approve ------


def test_reapprove_returns_linked_buy_queued_true(client):
    # PATCH 3: a first cost-switch approve queues the linked buy (flag true, 1 buy); a
    # SECOND idempotent approve of the SAME decision must ALSO return
    # linked_buy_queued=True (server-truth existence check) and still exactly 1 buy —
    # the reassurance must not disappear on refresh/re-submit.
    email = _unique_email()
    try:
        uid, headers = _setup_switch_user(client, email)
        rec = client.post(
            "/api/coach/recommend",
            headers=headers,
            json={
                "symbol": "AGTHX",
                "side": "sell",
                "amount": "2000.00",
                "switch_to": "VTI",
            },
        )
        did = rec.json()["decision_id"]
        order = {
            "decision_id": did,
            "order_intent": {"symbol": "AGTHX", "side": "sell", "amount": "2000.00"},
        }
        first = client.post("/api/coach/approve", headers=headers, json=order)
        assert first.status_code == 200, first.text
        assert first.json()["linked_buy_queued"] is True
        assert len(_pending_buys(uid)) == 1

        second = client.post("/api/coach/approve", headers=headers, json=order)
        assert second.status_code == 200, second.text
        assert second.json()["linked_buy_queued"] is True
        # Still exactly one linked pending buy — no duplicate.
        assert len(_pending_buys(uid)) == 1
    finally:
        _delete_user(email)


# --- read endpoints place/write nothing --------------------------------------


def test_review_endpoint_writes_nothing(client):
    email = _unique_email()
    try:
        uid, headers = _setup_switch_user(client, email)
        r = client.get("/api/allocation/review", headers=headers)
        assert r.status_code == 200, r.text
        # A cost finding surfaces (AGTHX -> VTI) but NOTHING is written/placed.
        findings = r.json()["findings"]
        assert any(
            f["kind"] == "cost" and f["switch_to"] == "VTI" for f in findings
        )
        assert _pending_buys(uid) == []
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM decision_record WHERE owner_id = %s",
                    (str(uid),),
                )
                (n,) = cur.fetchone()
        assert n == 0
    finally:
        _delete_user(email)
