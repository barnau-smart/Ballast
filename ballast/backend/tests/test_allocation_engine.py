"""Story 10.2 — the gap-to-target deploy-my-cash engine + endpoint.

The pure engine (``classify_holdings`` / ``plan_deployment``) is tested with no DB
— same-input-equal-output determinism, buy-only water-fill, never-past-target,
quantization residual, and the unclassified sleeve. The orchestrator
(``build_plan``) + ``GET /api/allocation/plan`` are tested against the real DB via
the TestClient (register + JWT login + per-test cleanup), seeding a user's cached
portfolio / balance / configs directly, covering every I/O-Matrix row and per-user
isolation (AD-10). Requires the docker Postgres.

⚠️ Run against the disposable ``ballast_test`` DB (never ``ballast`` — the live
Schwab link). See the story's live-link guard.
"""

from __future__ import annotations

import datetime
import json
import re
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest

from allocation.engine import (
    MIN_DEPLOY,
    classify_holdings,
    plan_deployment,
)
from api.app import create_app
from db.connection import get_connection
from fastapi.testclient import TestClient
from strategy.target_allocation import ASSET_CLASSES, resolve_target

PASSWORD = "supersecret123"


def _holding(symbol: str, market_value: str):
    """A minimal stand-in carrying just what the engine reads (symbol, market_value)."""
    return SimpleNamespace(symbol=symbol, market_value=Decimal(market_value))


# --- classify_holdings (pure) ------------------------------------------------


def test_classify_groups_by_asset_class():
    holdings = [
        _holding("VTI", "6000"),
        _holding("VOO", "1000"),  # also us_equity
        _holding("VXUS", "2000"),
        _holding("BND", "500"),
    ]
    c = classify_holdings(holdings)
    assert c.by_class["us_equity"] == Decimal("7000")
    assert c.by_class["intl_equity"] == Decimal("2000")
    assert c.by_class["bonds"] == Decimal("500")
    assert c.unclassified_value == Decimal("0")
    assert c.unclassified_symbols == []


def test_classify_is_case_insensitive_and_covers_every_class():
    c = classify_holdings([_holding("vti", "100"), _holding(" bnd ", "50")])
    assert set(c.by_class) == set(ASSET_CLASSES)
    assert c.by_class["us_equity"] == Decimal("100")
    assert c.by_class["bonds"] == Decimal("50")
    assert c.by_class["intl_equity"] == Decimal("0")


def test_classify_surfaces_unclassified_excluded_from_math():
    # VT (whole-world) + a single stock are unclassified — surfaced, not classified.
    holdings = [
        _holding("VTI", "1000"),
        _holding("VT", "500"),
        _holding("TSLA", "300"),
        _holding("vt", "200"),  # dup symbol, different case
    ]
    c = classify_holdings(holdings)
    assert c.by_class["us_equity"] == Decimal("1000")
    assert c.unclassified_value == Decimal("1000")  # 500 + 300 + 200
    # De-duplicated, upper-cased, order-stable.
    assert c.unclassified_symbols == ["VT", "TSLA"]


def test_classify_empty():
    c = classify_holdings([])
    assert all(c.by_class[cls] == Decimal("0") for cls in ASSET_CLASSES)
    assert c.unclassified_value == Decimal("0")
    assert c.unclassified_symbols == []


# --- plan_deployment (pure, deterministic) -----------------------------------

# The growth target from the spec's worked example (US .60 / Intl .30 / Bonds .10).
_GROWTH = resolve_target("growth")
_WEIGHTS = _GROWTH["weights"]
_FUNDS = _GROWTH["funds"]


def test_deploy_worked_example_split_and_primary():
    # VTI $6,000 only; $4,000 cash. base=10,000. Gaps: Intl +3,000, Bonds +1,000.
    current = {"us_equity": Decimal("6000"), "intl_equity": Decimal("0"), "bonds": Decimal("0")}
    d = plan_deployment(current, _WEIGHTS, _FUNDS, Decimal("4000"))
    amounts = {it.asset_class: it.amount for it in d.action_items}
    assert amounts["intl_equity"] == Decimal("3000.00")
    assert amounts["bonds"] == Decimal("1000.00")
    # Sorted by amount desc → the primary/first is the largest gap (VXUS $3,000).
    assert d.action_items[0].asset_class == "intl_equity"
    assert d.action_items[0].symbol == "VXUS"
    assert d.deployed == Decimal("4000.00")
    assert d.undeployed_cash == Decimal("0.00")


def test_cash_exceeds_gaps_leftover_reported():
    """The 'cash exceeds deployable gaps' row: leftover cash is left UNDEPLOYED and
    reported honestly (never chased past target).

    With a buy-only water-fill (``base = classified + cash``), whenever a class is
    meaningfully underweight the total positive gap grows with the cash, so full
    cash normally deploys. The genuine leftover appears when the ONLY remaining gaps
    quantize below ``MIN_DEPLOY`` (dust): those buys are dropped and their cash is
    reported as ``undeployed_cash`` — the honest 'nothing worth doing with the last
    few dollars' outcome. This is the closest real analogue to the spec's
    cash-exceeds-gaps row (its worked figure double-counts the US gap)."""
    # Already essentially at target; only a sub-dollar sliver is underweight.
    current = {
        "us_equity": Decimal("60000"),
        "intl_equity": Decimal("30000"),
        "bonds": Decimal("10000"),
    }
    d = plan_deployment(current, _WEIGHTS, _FUNDS, Decimal("1.50"))
    # base ≈ 100,001.50. The account is at target proportions, so each class's share
    # of the $1.50 is ≈ its weight × 1.50 (US 0.90 / Intl 0.45 / Bonds 0.15) — every
    # allocation < MIN_DEPLOY → dropped → the whole $1.50 is left undeployed.
    assert d.action_items == []
    assert d.deployed == Decimal("0")
    assert d.undeployed_cash == Decimal("1.50")

    # And the always-true invariant across a spread of inputs: never past target,
    # Σ deployed ≤ investable cash, undeployed = cash − deployed (all ≥ 0).
    for cur, cash in [
        ({"us_equity": Decimal("6000"), "intl_equity": Decimal("0"), "bonds": Decimal("0")}, Decimal("4000")),
        ({"us_equity": Decimal("100000"), "intl_equity": Decimal("0"), "bonds": Decimal("0")}, Decimal("10000")),
        ({"us_equity": Decimal("100"), "intl_equity": Decimal("5000"), "bonds": Decimal("100")}, Decimal("1000")),
        ({"us_equity": Decimal("3210"), "intl_equity": Decimal("1234"), "bonds": Decimal("77")}, Decimal("2571.33")),
    ]:
        dd = plan_deployment(cur, _WEIGHTS, _FUNDS, cash)
        classified = sum(cur.values(), Decimal("0"))
        base = classified + cash
        assert dd.deployed >= Decimal("0")
        assert dd.deployed <= cash
        assert dd.undeployed_cash == cash - dd.deployed
        for it in dd.action_items:
            gap = (_WEIGHTS[it.asset_class] * base) - cur[it.asset_class]
            assert it.amount <= gap  # never past that class's target


def test_zero_cash_no_underweight_is_noop():
    # Perfectly on-target already: no positive gap the cash can add to.
    current = {"us_equity": Decimal("600"), "intl_equity": Decimal("300"), "bonds": Decimal("100")}
    # With cash, EVERY class becomes underweight vs the enlarged base, so to test a
    # true no-op we pass zero investable cash.
    d = plan_deployment(current, _WEIGHTS, _FUNDS, Decimal("0"))
    assert d.action_items == []
    assert d.deployed == Decimal("0")
    assert d.undeployed_cash == Decimal("0")


def test_dust_below_min_deploy_dropped():
    # A tiny gap that quantizes below MIN_DEPLOY is dropped.
    current = {"us_equity": Decimal("0.60"), "intl_equity": Decimal("0.30"), "bonds": Decimal("0.10")}
    d = plan_deployment(current, _WEIGHTS, _FUNDS, Decimal("0.50"))
    # to_deploy is at most $0.50 split three ways — every allocation < $1.00.
    assert all(it.amount >= MIN_DEPLOY for it in d.action_items)
    # None survive → nothing deployed, cash left.
    assert d.action_items == []
    assert d.undeployed_cash == Decimal("0.50")


def test_mixed_surviving_buy_and_dropped_dust_accounts_honestly():
    # One real buy survives while another positive-gap class is dropped as dust:
    # base=999.90. Intl gap +299.97 (survives), Bonds gap +0.99 (a genuine positive
    # gap whose proportional split quantizes < MIN_DEPLOY → dropped), US overweight
    # (no gap). Proves the dropped dust cash is returned to undeployed, never lost.
    current = {"us_equity": Decimal("600"), "intl_equity": Decimal("0"), "bonds": Decimal("99")}
    cash = Decimal("300.90")
    d = plan_deployment(current, _WEIGHTS, _FUNDS, cash)
    classes = {it.asset_class for it in d.action_items}
    # (a) The dust item (bonds) is absent from action_items; only Intl survives.
    assert classes == {"intl_equity"}
    assert all(it.amount >= MIN_DEPLOY for it in d.action_items)
    # (b) deployed == Σ surviving item amounts.
    assert d.deployed == sum((it.amount for it in d.action_items), Decimal("0"))
    # (c) The dropped dust cash is returned to undeployed (not lost).
    assert d.undeployed_cash == cash - d.deployed
    # (d) Every surviving buy stays at/under its class's true gap (never past target).
    base = sum(current.values(), Decimal("0")) + cash
    for it in d.action_items:
        gap = (_WEIGHTS[it.asset_class] * base) - current[it.asset_class]
        assert it.amount <= gap


def test_residual_cent_assigned_to_largest_gap_sums_exactly():
    # When the gaps are EXACT cents, the residual reconciliation makes Σ allocations
    # equal to_deploy exactly (no phantom, no shortfall). The spec's worked example
    # (US 0 / Intl 0 / Bonds 0, cash makes clean gaps) is the canonical case: hold
    # $6,000 VTI, deploy $4,000 → Intl $3,000.00 + Bonds $1,000.00 == $4,000.00.
    current = {"us_equity": Decimal("6000"), "intl_equity": Decimal("0"), "bonds": Decimal("0")}
    d = plan_deployment(current, _WEIGHTS, _FUNDS, Decimal("4000"))
    total = sum((it.amount for it in d.action_items), Decimal("0"))
    assert total == Decimal("4000.00")
    assert d.deployed == Decimal("4000.00")

    # A residual-cent case that DOES land cleanly: an even split of $9.00 across all
    # three classes (base makes each gap an exact-cent multiple isn't required —
    # here every class is underweight from zero so gaps are .60/.30/.10 of base).
    # $30.00 cash from empty → US 18.00 / Intl 9.00 / Bonds 3.00 == 30.00 exactly.
    empty = {"us_equity": Decimal("0"), "intl_equity": Decimal("0"), "bonds": Decimal("0")}
    d2 = plan_deployment(empty, _WEIGHTS, _FUNDS, Decimal("30.00"))
    by = {it.asset_class: it.amount for it in d2.action_items}
    assert by["us_equity"] == Decimal("18.00")
    assert by["intl_equity"] == Decimal("9.00")
    assert by["bonds"] == Decimal("3.00")
    assert sum(by.values(), Decimal("0")) == Decimal("30.00")

    # HONESTY over exactness: when the last cent can't be deployed WITHOUT
    # overshooting a class's sub-cent gap, it stays UNDEPLOYED rather than push a
    # class past target. $100.01 from empty → gaps 60.006/30.003/10.001, whose
    # cent-caps sum to 100.00, so 1¢ is honestly left undeployed (never chased).
    d3 = plan_deployment(empty, _WEIGHTS, _FUNDS, Decimal("100.01"))
    assert d3.deployed == Decimal("100.00")
    assert d3.undeployed_cash == Decimal("0.01")
    base3 = Decimal("100.01")
    for it in d3.action_items:
        gap = _WEIGHTS[it.asset_class] * base3
        assert it.amount <= gap  # never past target, even by a sub-cent


def test_never_deploys_past_any_class_target():
    current = {"us_equity": Decimal("0"), "intl_equity": Decimal("0"), "bonds": Decimal("5000")}
    d = plan_deployment(current, _WEIGHTS, _FUNDS, Decimal("2000"))
    base = Decimal("5000") + Decimal("2000")
    for it in d.action_items:
        gap = (_WEIGHTS[it.asset_class] * base) - current[it.asset_class]
        assert it.amount <= gap
    # Σ deployed ≤ investable and ≤ Σ positive gaps.
    assert d.deployed <= Decimal("2000")


def test_determinism_same_input_equal_output():
    current = {"us_equity": Decimal("3210"), "intl_equity": Decimal("1234"), "bonds": Decimal("77")}
    cash = Decimal("2571.33")
    a = plan_deployment(current, _WEIGHTS, _FUNDS, cash)
    b = plan_deployment(dict(current), _WEIGHTS, _FUNDS, cash)
    assert [(it.asset_class, it.symbol, it.amount) for it in a.action_items] == [
        (it.asset_class, it.symbol, it.amount) for it in b.action_items
    ]
    assert a.deployed == b.deployed
    assert a.undeployed_cash == b.undeployed_cash


def test_zero_or_negative_cash_is_noop():
    current = {"us_equity": Decimal("100"), "intl_equity": Decimal("0"), "bonds": Decimal("0")}
    for cash in (Decimal("0"), Decimal("-5")):
        d = plan_deployment(current, _WEIGHTS, _FUNDS, cash)
        assert d.action_items == []
        assert d.deployed == Decimal("0")


# --- Endpoint / orchestrator tests (REAL DB) ---------------------------------


def _unique_email() -> str:
    return f"alloc-plan-{uuid.uuid4().hex}@example.com"


def _delete_user(email: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM "user" WHERE email = %s', (email,))
        conn.commit()


def _user_id_for(email: str) -> uuid.UUID:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT id FROM "user" WHERE email = %s', (email,))
            (uid,) = cur.fetchone()
    return uuid.UUID(str(uid))


def _register(client: TestClient, email: str) -> None:
    resp = client.post("/api/auth/register", json={"email": email, "password": PASSWORD})
    assert resp.status_code == 201, resp.text


def _login(client: TestClient, email: str) -> str:
    resp = client.post(
        "/api/auth/jwt/login",
        data={"username": email, "password": PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _seed_balance(uid: uuid.UUID, cash: str) -> None:
    now = datetime.datetime.now(datetime.timezone.utc)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO portfolio_balance (id, owner_id, cash, as_of) "
                "VALUES (%s, %s, %s, %s)",
                (str(uuid.uuid4()), str(uid), cash, now),
            )
        conn.commit()


def _seed_holding(uid: uuid.UUID, symbol: str, market_value: str) -> None:
    now = datetime.datetime.now(datetime.timezone.utc)
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
                    "1",
                    market_value,
                    None,
                    "0",
                    now,
                ),
            )
        conn.commit()


def _seed_cash_config(
    uid: uuid.UUID,
    *,
    reserve_amount: str | None,
    reserve_decided: bool,
    parked_symbols: list[str] | None = None,
) -> None:
    now = datetime.datetime.now(datetime.timezone.utc)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO cash_config "
                "(id, owner_id, reserve_amount, reserve_decided, parked_symbols, "
                "created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    str(uuid.uuid4()),
                    str(uid),
                    reserve_amount,
                    reserve_decided,
                    json.dumps(parked_symbols or []),
                    now,
                    now,
                ),
            )
        conn.commit()


@pytest.fixture
def client() -> TestClient:
    with TestClient(create_app()) as c:
        yield c


def test_requires_authentication(client):
    assert client.get("/api/allocation/plan").status_code == 401


def test_no_target_when_undecided(client):
    email = _unique_email()
    try:
        _register(client, email)
        headers = {"Authorization": f"Bearer {_login(client, email)}"}
        uid = _user_id_for(email)
        _seed_balance(uid, "5000")
        _seed_cash_config(uid, reserve_amount="0", reserve_decided=True)

        r = client.get("/api/allocation/plan", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "no_target"
        assert body["action_items"] == []
        assert body["primary_order"] is None
        assert body["reason"]  # a calm prompt
        _assert_calm(body["reason"])
    finally:
        _delete_user(email)


def test_decide_reserve_when_reserve_never_decided(client):
    email = _unique_email()
    try:
        _register(client, email)
        headers = {"Authorization": f"Bearer {_login(client, email)}"}
        uid = _user_id_for(email)
        _seed_balance(uid, "5000")
        client.put("/api/target-allocation", headers=headers, json={"model": "growth"})
        # No cash config row at all → never-decided reserve → decide_reserve
        # (NOT silently 0).

        r = client.get("/api/allocation/plan", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "decide_reserve"
        assert body["primary_order"] is None
        _assert_calm(body["reason"])
    finally:
        _delete_user(email)


def test_decide_reserve_when_config_exists_but_never_decided(client):
    email = _unique_email()
    try:
        _register(client, email)
        headers = {"Authorization": f"Bearer {_login(client, email)}"}
        uid = _user_id_for(email)
        _seed_balance(uid, "5000")
        client.put("/api/target-allocation", headers=headers, json={"model": "growth"})
        # A config row exists but the reserve was never decided (parked-only write).
        _seed_cash_config(uid, reserve_amount=None, reserve_decided=False)

        body = client.get("/api/allocation/plan", headers=headers).json()
        assert body["status"] == "decide_reserve"
    finally:
        _delete_user(email)


def test_no_cash_when_investable_non_positive(client):
    email = _unique_email()
    try:
        _register(client, email)
        headers = {"Authorization": f"Bearer {_login(client, email)}"}
        uid = _user_id_for(email)
        _seed_balance(uid, "1000")
        client.put("/api/target-allocation", headers=headers, json={"model": "growth"})
        # Reserve ($2,000) exceeds ready-to-trade ($1,000) → no investable cash.
        _seed_cash_config(uid, reserve_amount="2000", reserve_decided=True)

        body = client.get("/api/allocation/plan", headers=headers).json()
        assert body["status"] == "no_cash"
        assert body["primary_order"] is None
        _assert_calm(body["reason"])
    finally:
        _delete_user(email)


def test_deploy_end_to_end_with_fixed_point_wire_strings(client):
    email = _unique_email()
    try:
        _register(client, email)
        headers = {"Authorization": f"Bearer {_login(client, email)}"}
        uid = _user_id_for(email)
        # VTI $6,000 only; $4,000 ready-to-trade cash, reserve declined (0).
        _seed_balance(uid, "4000")
        _seed_holding(uid, "VTI", "6000")
        client.put("/api/target-allocation", headers=headers, json={"model": "growth"})
        _seed_cash_config(uid, reserve_amount="0", reserve_decided=True)

        r = client.get("/api/allocation/plan", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "deploy"
        # action_items sorted by amount desc, canonical fund + amount as strings.
        items = body["action_items"]
        assert items[0]["symbol"] == "VXUS"
        assert items[0]["amount"] == "3000.00"
        assert items[0]["asset_class"] == "intl_equity"
        assert items[1]["symbol"] == "BND"
        assert items[1]["amount"] == "1000.00"
        # primary_order = the largest-gap MARKET BUY.
        po = body["primary_order"]
        assert po == {
            "symbol": "VXUS",
            "side": "buy",
            "amount": "3000.00",
            "order_type": "market",
        }
        # Fixed-point wire strings everywhere (no float, no E-notation).
        assert body["investable_cash"] == "4000.00"
        assert body["undeployed_cash"] == "0.00"
        assert body["current"]["us_equity"]["market_value"] == "6000.00"
        assert re.fullmatch(r"-?\d+(\.\d+)?", body["current"]["us_equity"]["weight"])
        # Σ deployed ≤ investable.
        total = sum(Decimal(it["amount"]) for it in items)
        assert total <= Decimal(body["investable_cash"])
    finally:
        _delete_user(email)


def test_plan_endpoint_writes_nothing_places_no_order(client):
    # The load-bearing "populate, don't submit" safety guardrail: the read-only
    # plan GET must NEVER write a decision_record (no proposal/decision) and place
    # no order — count decision_record rows immediately before and after.
    email = _unique_email()
    try:
        _register(client, email)
        headers = {"Authorization": f"Bearer {_login(client, email)}"}
        uid = _user_id_for(email)
        # A full deploy setup (mirrors the end-to-end test) so the plan is a real
        # `deploy` — the case most likely to (wrongly) write something.
        _seed_balance(uid, "4000")
        _seed_holding(uid, "VTI", "6000")
        client.put("/api/target-allocation", headers=headers, json={"model": "growth"})
        _seed_cash_config(uid, reserve_amount="0", reserve_decided=True)

        def _decision_count() -> int:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT count(*) FROM decision_record WHERE owner_id = %s",
                        (str(uid),),
                    )
                    (n,) = cur.fetchone()
            return int(n)

        before = _decision_count()
        r = client.get("/api/allocation/plan", headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "deploy"  # the write-prone path
        after = _decision_count()
        assert before == after == 0  # nothing written, no order placed
    finally:
        _delete_user(email)


def test_unclassified_surfaced_but_excluded_from_math(client):
    email = _unique_email()
    try:
        _register(client, email)
        headers = {"Authorization": f"Bearer {_login(client, email)}"}
        uid = _user_id_for(email)
        _seed_balance(uid, "4000")
        _seed_holding(uid, "VTI", "6000")
        _seed_holding(uid, "VT", "2000")  # whole-world → unclassified
        _seed_holding(uid, "TSLA", "1000")  # single stock → unclassified
        client.put("/api/target-allocation", headers=headers, json={"model": "growth"})
        _seed_cash_config(uid, reserve_amount="0", reserve_decided=True)

        body = client.get("/api/allocation/plan", headers=headers).json()
        # A valid deploy plan is still produced on the classified sleeve + cash,
        # UNAFFECTED by VT/TSLA (same split as without them).
        assert body["status"] == "deploy"
        items = {it["asset_class"]: it["amount"] for it in body["action_items"]}
        assert items["intl_equity"] == "3000.00"
        assert items["bonds"] == "1000.00"
        # The unclassified sleeve is surfaced honestly.
        assert body["unclassified"]["market_value"] == "3000.00"  # 2000 + 1000
        assert set(body["unclassified"]["symbols"]) == {"VT", "TSLA"}
    finally:
        _delete_user(email)


def test_no_cash_when_on_target_and_zero_investable(client):
    email = _unique_email()
    try:
        _register(client, email)
        headers = {"Authorization": f"Bearer {_login(client, email)}"}
        uid = _user_id_for(email)
        # Already exactly on-target and NO investable cash → nothing to do.
        _seed_balance(uid, "0")
        _seed_holding(uid, "VTI", "600")
        _seed_holding(uid, "VXUS", "300")
        _seed_holding(uid, "BND", "100")
        client.put("/api/target-allocation", headers=headers, json={"model": "growth"})
        _seed_cash_config(uid, reserve_amount="0", reserve_decided=True)

        body = client.get("/api/allocation/plan", headers=headers).json()
        # Zero investable cash → no_cash is the honest status here.
        assert body["status"] == "no_cash"
        assert body["action_items"] == []
    finally:
        _delete_user(email)


def test_at_target_with_cash_but_no_underweight(client):
    email = _unique_email()
    try:
        _register(client, email)
        headers = {"Authorization": f"Bearer {_login(client, email)}"}
        uid = _user_id_for(email)
        # Heavily overweight in stocks; small cash. base makes only tiny/zero gaps,
        # but to force at_target we make the account already past target in every
        # class the cash could reach: hold ONLY the canonical funds far above target
        # so adding the small cash can't create a positive gap big enough to survive.
        # Simplest deterministic at_target: hold enough that with the cash added,
        # every class is already at/above its target share.
        _seed_balance(uid, "1")  # $1 investable
        _seed_holding(uid, "VTI", "60000")
        _seed_holding(uid, "VXUS", "30000")
        _seed_holding(uid, "BND", "10000")
        client.put("/api/target-allocation", headers=headers, json={"model": "growth"})
        _seed_cash_config(uid, reserve_amount="0", reserve_decided=True)

        body = client.get("/api/allocation/plan", headers=headers).json()
        # $1 across three tiny gaps → all dust → at_target (nothing to buy).
        assert body["status"] == "at_target"
        assert body["action_items"] == []
        _assert_calm(body["reason"])
    finally:
        _delete_user(email)


def test_parked_money_market_adds_to_deployable(client):
    """Story 10.8 supersedes the Group-B behavior: parked money-market is a
    cash-equivalent the user holds instead of idle cash, so it is DEPLOYABLE.
    $4,000 settlement cash + a $4,000 SWVXX parked holding, reserve $0 →
    investable = 4,000 + 4,000 = $8,000 (never `view.cash − parked`, never just
    settlement). Execution funds any buy beyond settlement by liquidating SWVXX."""
    email = _unique_email()
    try:
        _register(client, email)
        headers = {"Authorization": f"Bearer {_login(client, email)}"}
        uid = _user_id_for(email)
        _seed_balance(uid, "4000")
        _seed_holding(uid, "SWVXX", "4000")  # parked money-market
        client.put("/api/target-allocation", headers=headers, json={"model": "growth"})
        _seed_cash_config(
            uid, reserve_amount="0", reserve_decided=True, parked_symbols=["SWVXX"]
        )

        body = client.get("/api/allocation/plan", headers=headers).json()
        assert body["status"] == "deploy"
        assert body["investable_cash"] == "8000.00"
    finally:
        _delete_user(email)


def test_per_user_isolation(client):
    email_a = _unique_email()
    email_b = _unique_email()
    try:
        _register(client, email_a)
        _register(client, email_b)
        headers_a = {"Authorization": f"Bearer {_login(client, email_a)}"}
        headers_b = {"Authorization": f"Bearer {_login(client, email_b)}"}
        uid_a = _user_id_for(email_a)

        # A: a full deploy setup. B: nothing at all.
        _seed_balance(uid_a, "4000")
        _seed_holding(uid_a, "VTI", "6000")
        client.put("/api/target-allocation", headers=headers_a, json={"model": "growth"})
        _seed_cash_config(uid_a, reserve_amount="0", reserve_decided=True)

        a_body = client.get("/api/allocation/plan", headers=headers_a).json()
        assert a_body["status"] == "deploy"

        # B sees ONLY its own (empty) state — never A's holdings/cash/target.
        b_body = client.get("/api/allocation/plan", headers=headers_b).json()
        assert b_body["status"] == "no_target"
        assert b_body["action_items"] == []
        assert b_body["unclassified"]["symbols"] == []
    finally:
        _delete_user(email_a)
        _delete_user(email_b)


def test_plan_serializes_target_weights(client):
    """Story 10.7 — `target_weights` is on the wire (fixed-point fraction strings) so
    the UI can show current-vs-target, matching the user's chosen model (growth = 60/30/10)."""
    email = _unique_email()
    try:
        _register(client, email)
        headers = {"Authorization": f"Bearer {_login(client, email)}"}
        uid = _user_id_for(email)
        _seed_balance(uid, "4000")
        _seed_holding(uid, "VTI", "6000")
        client.put("/api/target-allocation", headers=headers, json={"model": "growth"})
        _seed_cash_config(uid, reserve_amount="0", reserve_decided=True)

        body = client.get("/api/allocation/plan", headers=headers).json()
        assert body["status"] == "deploy"
        tw = body["target_weights"]
        assert tw and sum(Decimal(v) for v in tw.values()) == Decimal("1.00")
        assert sorted(Decimal(v) for v in tw.values()) == [
            Decimal("0.10"),
            Decimal("0.30"),
            Decimal("0.60"),
        ]
    finally:
        _delete_user(email)


def test_plan_target_weights_empty_when_no_target(client):
    """A plan with no chosen model (no_target) carries an empty target_weights map —
    the UI degrades calmly rather than rendering a phantom target."""
    email = _unique_email()
    try:
        _register(client, email)
        headers = {"Authorization": f"Bearer {_login(client, email)}"}
        uid = _user_id_for(email)
        _seed_balance(uid, "4000")

        body = client.get("/api/allocation/plan", headers=headers).json()
        assert body["status"] == "no_target"
        assert body["target_weights"] == {}
    finally:
        _delete_user(email)


def test_money_market_counted_as_deployable(client):
    """Story 10.8 — parked money-market is DEPLOYABLE. MasterB's real setup:
    settlement $12,182.82 + SWVXX $93,766.26 (parked) + reserve $40,000 →
    investable = 12,182.82 + 93,766.26 − 40,000 = $65,949.08 → a deploy plan
    (was 'no_cash' before this story)."""
    email = _unique_email()
    try:
        _register(client, email)
        headers = {"Authorization": f"Bearer {_login(client, email)}"}
        uid = _user_id_for(email)
        _seed_balance(uid, "12182.82")
        _seed_holding(uid, "SWVXX", "93766.26")
        client.put("/api/target-allocation", headers=headers, json={"model": "growth"})
        _seed_cash_config(
            uid, reserve_amount="40000", reserve_decided=True, parked_symbols=["SWVXX"]
        )

        body = client.get("/api/allocation/plan", headers=headers).json()
        assert body["status"] == "deploy"
        # Reserve comes out of TOTAL (settlement + parked MM), not settlement alone.
        assert body["investable_cash"] == "65949.08"
    finally:
        _delete_user(email)


def test_untagged_money_market_is_not_deployable(client):
    """Only the user's DECLARED parked_symbols count as deployable cash. The same
    SWVXX holding, NOT tagged parked, is a plain (unclassified) holding — investable
    stays settlement − reserve = negative → 'no_cash'."""
    email = _unique_email()
    try:
        _register(client, email)
        headers = {"Authorization": f"Bearer {_login(client, email)}"}
        uid = _user_id_for(email)
        _seed_balance(uid, "12182.82")
        _seed_holding(uid, "SWVXX", "93766.26")
        client.put("/api/target-allocation", headers=headers, json={"model": "growth"})
        _seed_cash_config(
            uid, reserve_amount="40000", reserve_decided=True, parked_symbols=[]
        )

        body = client.get("/api/allocation/plan", headers=headers).json()
        assert body["status"] == "no_cash"  # SWVXX untagged → not counted
    finally:
        _delete_user(email)


def test_deploy_buy_funds_via_liquidation_reserve_protected(client):
    """Story 10.8 Phase 2 — a deploy buy beyond settlement cash COMPOSES with the
    Epic 9 (9-3) just-in-time liquidation: the primary buy from MasterB's deploy plan
    (> $12,182.82 settlement) triggers a coverable liquidation that SELLS SWVXX for
    the shortfall, drawing only from parked ABOVE the $40k reserve — never touching
    the reserve, never margin. Proves the analysis figure ($65,949 investable) is
    backed by real settled cash at execution."""
    email = _unique_email()
    try:
        _register(client, email)
        headers = {"Authorization": f"Bearer {_login(client, email)}"}
        uid = _user_id_for(email)
        _seed_balance(uid, "12182.82")
        _seed_holding(uid, "SWVXX", "93766.26")
        client.put("/api/target-allocation", headers=headers, json={"model": "growth"})
        _seed_cash_config(
            uid, reserve_amount="40000", reserve_decided=True, parked_symbols=["SWVXX"]
        )

        plan = client.get("/api/allocation/plan", headers=headers).json()
        assert plan["status"] == "deploy"
        primary = plan["primary_order"]
        assert primary is not None
        # The primary buy exceeds settlement cash → it must be funded by liquidation.
        assert Decimal(primary["amount"]) > Decimal("12182.82")

        liq = client.post(
            "/api/cash/liquidation-plan",
            headers=headers,
            json={"symbol": primary["symbol"], "amount": primary["amount"]},
        ).json()
        assert liq["needs_liquidation"] is True
        assert liq["coverable"] is True  # available parked $53,766.26 covers the shortfall
        assert liq["sell_symbol"] == "SWVXX"
        # Never sells into the reserve: the sell never exceeds parked − reserve.
        assert Decimal(liq["sell_amount"]) <= Decimal("53766.26")
    finally:
        _delete_user(email)


# --- Calm-copy tone bar (mirrors test_digest_compose.FORBIDDEN) ---------------

_FORBIDDEN = [
    "urgent", "hurry", "act now", "act fast", "don't miss", "dont miss",
    "missing out", "miss out", "last chance", "limited time", "warning",
    "alarm", "panic", "crash", "plunge", "fear", "red", "alert", "immediately",
]


def _assert_calm(text: str) -> None:
    blob = str(text).lower()
    for word in _FORBIDDEN:
        pattern = r"\b" + re.escape(word) + r"\b"
        assert not re.search(pattern, blob), f"plan copy should never say {word!r}"
