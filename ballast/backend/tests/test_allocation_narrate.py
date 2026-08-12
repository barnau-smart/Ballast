"""Story 10.3 — fiduciary-advisor narration + never-invent-a-fact safeguard.

The pure narration layer (:mod:`allocation.narrate`) is tested with no DB —
evidence-record construction, the numeric + no-forecast gates, the deterministic
templated fallback (asserted against the 5 good-lesson tests as 5 explicitly-named
functions), fake-mode → fallback routing, determinism, and the calm-copy bar. The
``GET /api/allocation/narration`` endpoint is tested against the real DB via the
TestClient, asserting the ``{plan, narration}`` shape, that NO ``decision_record``
is written, and per-user isolation (AD-10).

⚠️ Run against the disposable ``ballast_test`` DB (never ``ballast`` — the live
Schwab link). See the story's live-link guard.
"""

from __future__ import annotations

import datetime
import re
import uuid
from decimal import Decimal

import pytest

from allocation.engine import (
    ActionItem,
    Plan,
    STATUS_AT_TARGET,
    STATUS_DECIDE_RESERVE,
    STATUS_DEPLOY,
    STATUS_NO_CASH,
    STATUS_NO_TARGET,
)
from allocation.narrate import (
    AllocationNarration,
    FORECAST_TERMS,
    NarrationValidationError,
    _fallback_narration,
    allowed_facts,
    build_narration_facts,
    check_no_forecast,
    check_no_invented_numbers,
    narrate_plan,
)
from api.app import create_app
from db.connection import get_connection
from fastapi.testclient import TestClient
from llm.fake_adapter import FakeLLMGateway
from precedent.evidence import EvidenceKind

PASSWORD = "supersecret123"


# --- A representative growth deploy Plan (the spec's worked example) ----------


def _growth_deploy_plan() -> Plan:
    """VTI $6,000 only; $4,000 cash; growth target → VXUS $3,000 + BND $1,000."""
    return Plan(
        status=STATUS_DEPLOY,
        action_items=[
            ActionItem("intl_equity", "VXUS", Decimal("3000.00")),
            ActionItem("bonds", "BND", Decimal("1000.00")),
        ],
        primary_order=ActionItem("intl_equity", "VXUS", Decimal("3000.00")),
        current={
            "us_equity": {"market_value": Decimal("6000.00"), "weight": Decimal("1.0000")},
            "intl_equity": {"market_value": Decimal("0"), "weight": Decimal("0")},
            "bonds": {"market_value": Decimal("0"), "weight": Decimal("0")},
        },
        target_weights={
            "us_equity": Decimal("0.60"),
            "intl_equity": Decimal("0.30"),
            "bonds": Decimal("0.10"),
        },
        investable_cash=Decimal("4000.00"),
        undeployed_cash=Decimal("0.00"),
        reason="",
        as_of=datetime.datetime(2026, 8, 12, tzinfo=datetime.timezone.utc),
    )


# --- build_narration_facts ---------------------------------------------------


def test_facts_one_per_item_plus_portfolio_summary():
    plan = _growth_deploy_plan()
    ev = build_narration_facts(plan)
    # One per action item + one PORTFOLIO summary.
    assert len(ev) == len(plan.action_items) + 1
    assert all(r.kind is EvidenceKind.STRATEGY for r in ev)
    # Every id is unique + content-addressed (strat- prefix).
    assert all(r.id.startswith("strat-") for r in ev)
    assert len({r.id for r in ev}) == len(ev)
    # The per-item statements name the asset-class label + amount.
    joined = " ".join(r.statement for r in ev)
    assert "International stocks" in joined
    assert "Bonds" in joined
    assert "3000.00" in joined and "1000.00" in joined
    # The summary record is symbol "PORTFOLIO" and carries the cash facts.
    summary = ev[-1]
    assert summary.stats["investable_cash"] == Decimal("4000.00")
    assert summary.stats["undeployed_cash"] == Decimal("0.00")


def test_facts_empty_for_no_action_statuses():
    for status in (
        STATUS_AT_TARGET,
        STATUS_NO_CASH,
        STATUS_NO_TARGET,
        STATUS_DECIDE_RESERVE,
    ):
        plan = Plan(status=status, reason="nothing to do")
        assert build_narration_facts(plan) == ()


def test_facts_deterministic_ids_same_plan():
    a = build_narration_facts(_growth_deploy_plan())
    b = build_narration_facts(_growth_deploy_plan())
    assert [r.id for r in a] == [r.id for r in b]


# --- allowed_facts -----------------------------------------------------------


def test_allowed_facts_contains_amounts_cash_and_weights_both_forms():
    plan = _growth_deploy_plan()
    allowed = allowed_facts(plan)
    # Amounts + cash.
    assert Decimal("3000.00") in allowed
    assert Decimal("1000.00") in allowed
    assert Decimal("4000.00") in allowed  # investable
    assert Decimal("0.00") in allowed  # undeployed
    # Current sleeve market value + weight (fraction AND 0–100 percent).
    assert Decimal("6000.00") in allowed
    assert Decimal("1.0000") in allowed
    assert Decimal("100") in allowed  # 1.0000 * 100
    # Target weights as BOTH fraction and percent (growth 0.60/0.30/0.10 among them).
    assert Decimal("0.60") in allowed and Decimal("60") in allowed
    assert Decimal("0.30") in allowed and Decimal("30") in allowed
    assert Decimal("0.10") in allowed and Decimal("10") in allowed
    # The recognized stock/bond split (equity sum 0.90 vs bond 0.10) is citable.
    assert Decimal("90") in allowed and Decimal("0.90") in allowed
    # Comparison is by Decimal value (scale-insensitive).
    assert any(v == Decimal("3000") for v in allowed)


def test_allowed_facts_uses_users_model_not_cross_model_union():
    """The allow-set admits ONLY the user's own target weights — a wrong-model
    target share (e.g. balanced's 65/35, or a bare 20%) is NOT admitted, so the
    never-invent gate rejects it as a fabricated fact rather than accepting it."""
    plan = _growth_deploy_plan()  # growth = 60/30/10
    allowed = allowed_facts(plan)
    # 20% and 65% are legitimate for OTHER models but not growth → not in the set.
    assert Decimal("20") not in allowed and Decimal("0.20") not in allowed
    assert Decimal("65") not in allowed and Decimal("0.35") not in allowed
    # And the gate rejects a wrong-but-plausible target percentage.
    with pytest.raises(NarrationValidationError):
        check_no_invented_numbers("Your international target is 20%.", allowed)


# --- check_no_invented_numbers -----------------------------------------------


def test_check_no_invented_numbers_accepts_engine_numbers():
    allowed = allowed_facts(_growth_deploy_plan())
    # $-prefixed, comma-grouped, percent, and scale variants all normalize into set.
    check_no_invented_numbers(
        "Buy $3,000.00 of VXUS (30%) and $1,000 of BND; leftover $0.00.", allowed
    )


def test_check_no_invented_numbers_rejects_injected_fabrication():
    allowed = allowed_facts(_growth_deploy_plan())
    with pytest.raises(NarrationValidationError):
        check_no_invented_numbers("This will return $9,999 next quarter.", allowed)


def test_check_no_invented_numbers_ignores_pure_punctuation():
    allowed = allowed_facts(_growth_deploy_plan())
    # No numeric tokens → never raises.
    check_no_invented_numbers("Buy the broad index funds — no chase.", allowed)


# --- check_no_forecast -------------------------------------------------------


def test_check_no_forecast_rejects_prediction_language():
    for phrase in ("the market will rise", "this stock will outperform", "next year"):
        with pytest.raises(NarrationValidationError):
            check_no_forecast(phrase)


def test_check_no_forecast_allows_situational_opinion():
    # Plain principle/opinion (no forecast word) passes.
    check_no_forecast(
        "Diversifying across broad index funds moves you toward your chosen mix."
    )


def test_forecast_terms_is_named_nonempty_tuple():
    assert isinstance(FORECAST_TERMS, tuple) and len(FORECAST_TERMS) > 0


# --- The 5 good-lesson tests (against _fallback_narration) --------------------


def _fallback() -> AllocationNarration:
    plan = _growth_deploy_plan()
    return _fallback_narration(plan, build_narration_facts(plan))


def test_lesson_principle_not_pick():
    """Frames the move as rebalancing toward the CHOSEN target by asset CLASS —
    names the asset-class labels, never a stock-pick / hot / winner word."""
    n = _fallback()
    blob = (n.action_label + " " + n.reasoning).lower()
    assert "international stocks" in blob and "bonds" in blob
    assert "target mix" in blob
    for pick_word in ("hot", "winner", "hot pick", "hot stock", "best stock"):
        assert pick_word not in blob or "not a bet on any hot pick" in blob


def test_lesson_why_generalizes():
    """States a settled, generalizable principle (broad, low-cost index funds),
    not a one-off tip."""
    blob = _fallback().reasoning.lower()
    assert "broad" in blob and "index funds" in blob


def test_lesson_recognized_best_practice():
    """Names a recognized best practice: diversification / rebalance / broad index."""
    blob = _fallback().reasoning.lower()
    assert "diversification" in blob and "rebalancing" in blob


def test_lesson_teaches_the_tradeoff():
    """States the tradeoff (doesn't time the market / leftover cash stays put) AND
    carries ≥1 real uncertainty."""
    n = _fallback()
    blob = n.reasoning.lower()
    assert "time the market" in blob
    assert "undeployed" in blob or "left undeployed" in blob
    assert any(u and u.strip() for u in n.uncertainties)


def test_lesson_facts_not_forecast():
    """Every stated number ∈ the engine allow-set AND no forecast language."""
    plan = _growth_deploy_plan()
    n = _fallback_narration(plan, build_narration_facts(plan))
    allowed = allowed_facts(plan)
    combined = n.reasoning + " " + n.action_label
    check_no_invented_numbers(combined, allowed)  # raises on a fabricated number
    check_no_forecast(combined)  # raises on prediction language


def test_fallback_cites_every_evidence_id():
    plan = _growth_deploy_plan()
    ev = build_narration_facts(plan)
    n = _fallback_narration(plan, ev)
    assert n.evidence == ev  # cites every real record
    assert n.status == "deploy"


# --- narrate_plan (fake mode → fallback; determinism; no-action passthrough) --


class _ScriptedGateway(FakeLLMGateway):
    """A gateway that returns a fixed narration output citing the plan's real
    evidence ids — so the structural gate passes and we can probe the honesty
    gates in isolation."""

    def __init__(self, plan: Plan, *, uncertainty: str):
        self._ids = [r.id for r in build_narration_facts(plan)]
        self._uncertainty = uncertainty

    def complete(self, request):  # noqa: D401
        from llm.port import LLMResponse

        return LLMResponse(
            output={
                "action_label": "Put your idle cash to work toward your target mix",
                "reasoning": (
                    "You're light on International stocks and Bonds versus your "
                    "target, so this buys the broad index funds for those classes."
                ),
                "evidence": self._ids,
                "uncertainties": [self._uncertainty],
            },
            model="scripted",
            provider="fake",
        )


def test_narrate_plan_accepts_clean_scripted_narration():
    """A well-formed narration citing real ids with a clean uncertainty is blessed
    (proves the scripted path can actually reach the accept branch)."""
    plan = _growth_deploy_plan()
    n = narrate_plan(
        _ScriptedGateway(plan, uncertainty="A fill isn't guaranteed; markets move."),
        plan,
    )
    assert n.action_label == "Put your idle cash to work toward your target mix"
    assert "broad index funds" in n.reasoning
    assert n.status == "deploy"


def test_narrate_plan_accepts_narration_restating_engine_numbers():
    """The ACCEPT branch must let a narration that RESTATES real engine figures
    ($3,000.00, 30%, 10%) through ``check_no_invented_numbers`` — proving the numeric
    gate clears a realistic multi-number narration verbatim, not only number-free
    prose. Guards against a silent over-strict regression where the whole feature
    would always degrade to the template."""
    plan = _growth_deploy_plan()
    ids = [r.id for r in build_narration_facts(plan)]

    class _NumbersGateway(FakeLLMGateway):
        def complete(self, request):  # noqa: D401
            from llm.port import LLMResponse

            return LLMResponse(
                output={
                    "action_label": "Put your idle cash to work toward your target mix",
                    "reasoning": (
                        "You're light on International stocks (target 30%) and Bonds "
                        "(target 10%), so this buys $3,000.00 of VXUS and $1,000.00 of "
                        "BND toward your chosen mix."
                    ),
                    "evidence": ids,
                    "uncertainties": ["A fill isn't guaranteed; markets move."],
                },
                model="scripted",
                provider="fake",
            )

    n = narrate_plan(_NumbersGateway(), plan)
    # Blessed (NOT degraded) — the numbered reasoning survived the numeric gate.
    assert n.status == "deploy"
    assert "$3,000.00" in n.reasoning and "30%" in n.reasoning
    assert (
        n.reasoning
        != _fallback_narration(plan, build_narration_facts(plan)).reasoning
    )


def test_check_no_invented_numbers_rejects_sign_flipped_value():
    """A leading ``-`` is captured and compared SIGNED: ``-30%`` (when ``30`` is a
    legit engine weight) is a sign-flipped fabrication and must be rejected, not
    laundered into the unsigned ``30`` and falsely accepted."""
    allowed = allowed_facts(_growth_deploy_plan())  # 30 is a legit target percent
    check_no_invented_numbers("Your international target is 30%.", allowed)  # ok
    with pytest.raises(NarrationValidationError):
        check_no_invented_numbers("You could be down -30% here.", allowed)


def test_narrate_plan_gates_a_forecast_hidden_in_an_uncertainty():
    """An LLM-authored uncertainty is surfaced to the user, so a FORECAST hiding in
    it must still degrade to the deterministic template (the uncertainties field is
    not a gate blind spot)."""
    plan = _growth_deploy_plan()
    n = narrate_plan(
        _ScriptedGateway(plan, uncertainty="Bonds will outperform stocks next year."),
        plan,
    )
    # Degraded to the fallback: the forecast never reaches the user.
    assert n.uncertainties == _fallback_narration(
        plan, build_narration_facts(plan)
    ).uncertainties
    for u in n.uncertainties:
        check_no_forecast(u)


def test_narrate_plan_gates_an_invented_number_hidden_in_an_uncertainty():
    """A fabricated number hiding in an uncertainty line must also degrade to the
    template rather than surface an unvalidated figure."""
    plan = _growth_deploy_plan()
    n = narrate_plan(
        _ScriptedGateway(plan, uncertainty="You might lose $8,675 in a downturn."),
        plan,
    )
    allowed = allowed_facts(plan)
    # Degraded to the fallback — its uncertainty carries no non-engine number.
    for u in n.uncertainties:
        check_no_invented_numbers(u, allowed)
    assert n.reasoning == _fallback_narration(
        plan, build_narration_facts(plan)
    ).reasoning


def test_narrate_plan_fake_mode_returns_fallback_template():
    """Fake gateway fills evidence with an unbacked id → UnbackedEvidenceError →
    the deterministic template. The result must NOT contain a "fake-" placeholder
    and must pass BOTH validators."""
    plan = _growth_deploy_plan()
    n = narrate_plan(FakeLLMGateway(), plan)
    assert n.status == "deploy"
    combined = n.reasoning + " " + n.action_label
    assert "fake-" not in combined
    allowed = allowed_facts(plan)
    check_no_invented_numbers(combined, allowed)
    check_no_forecast(combined)
    # It cites the real engine evidence records.
    assert len(n.evidence) == len(build_narration_facts(plan))
    assert all(r.kind is EvidenceKind.STRATEGY for r in n.evidence)


def test_narrate_plan_deterministic_same_plan_equal_narration():
    a = narrate_plan(FakeLLMGateway(), _growth_deploy_plan())
    b = narrate_plan(FakeLLMGateway(), _growth_deploy_plan())
    assert a == b


def test_narrate_plan_no_action_passthrough_no_gateway_call():
    """Every no-action status → deterministic calm plan.reason passthrough with NO
    gateway call (a gateway that raises if touched proves it)."""

    class _ExplodingGateway(FakeLLMGateway):
        def complete(self, request):  # noqa: D401
            raise AssertionError("gateway must not be called for a no-action status")

    for status in (
        STATUS_AT_TARGET,
        STATUS_NO_CASH,
        STATUS_NO_TARGET,
        STATUS_DECIDE_RESERVE,
    ):
        plan = Plan(status=status, reason="There's nothing to do here.")
        n = narrate_plan(_ExplodingGateway(), plan)
        assert n.status == status
        assert n.reasoning == "There's nothing to do here."
        assert n.evidence == ()
        assert any(u and u.strip() for u in n.uncertainties)


# --- Calm-copy tone bar (mirrors test_allocation_engine.FORBIDDEN) ------------

_FORBIDDEN = [
    "urgent", "hurry", "act now", "act fast", "don't miss", "dont miss",
    "missing out", "miss out", "last chance", "limited time", "warning",
    "alarm", "panic", "crash", "plunge", "fear", "red", "alert", "immediately",
]


def _assert_calm(text: str) -> None:
    blob = str(text).lower()
    for word in _FORBIDDEN:
        pattern = r"\b" + re.escape(word) + r"\b"
        assert not re.search(pattern, blob), f"narration copy should never say {word!r}"


def test_fallback_copy_is_calm():
    n = _fallback()
    _assert_calm(n.action_label)
    _assert_calm(n.reasoning)
    for u in n.uncertainties:
        _assert_calm(u)


def test_narrate_plan_deploy_copy_is_calm():
    n = narrate_plan(FakeLLMGateway(), _growth_deploy_plan())
    _assert_calm(n.reasoning + " " + n.action_label)


# --- Endpoint tests (REAL DB) ------------------------------------------------


def _unique_email() -> str:
    return f"alloc-narrate-{uuid.uuid4().hex}@example.com"


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
    uid: uuid.UUID, *, reserve_amount: str | None, reserve_decided: bool
) -> None:
    import json

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
                    json.dumps([]),
                    now,
                    now,
                ),
            )
        conn.commit()


@pytest.fixture
def client() -> TestClient:
    with TestClient(create_app()) as c:
        yield c


def _seed_full_deploy(client: TestClient, uid: uuid.UUID, headers: dict) -> None:
    # VTI $6,000; $4,000 ready-to-trade; reserve declined (0); growth target.
    _seed_balance(uid, "4000")
    _seed_holding(uid, "VTI", "6000")
    client.put("/api/target-allocation", headers=headers, json={"model": "growth"})
    _seed_cash_config(uid, reserve_amount="0", reserve_decided=True)


def test_narration_requires_authentication(client):
    assert client.get("/api/allocation/narration").status_code == 401


def test_narration_deploy_shape(client):
    email = _unique_email()
    try:
        _register(client, email)
        headers = {"Authorization": f"Bearer {_login(client, email)}"}
        uid = _user_id_for(email)
        _seed_full_deploy(client, uid, headers)

        r = client.get("/api/allocation/narration", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        # {plan, narration} shape.
        assert set(body.keys()) == {"plan", "narration"}
        plan = body["plan"]
        assert plan["status"] == "deploy"
        assert plan["primary_order"]["symbol"] == "VXUS"
        assert plan["primary_order"]["amount"] == "3000.00"
        narration = body["narration"]
        assert set(narration.keys()) == {
            "action_label",
            "reasoning",
            "uncertainties",
            "evidence",
        }
        assert narration["action_label"] and narration["reasoning"]
        assert len(narration["uncertainties"]) >= 1
        # Every cited evidence record is a STRATEGY record with the fixed shape.
        assert len(narration["evidence"]) >= 1
        for rec in narration["evidence"]:
            assert rec["kind"] == "strategy"
            assert set(rec.keys()) == {
                "id",
                "kind",
                "statement",
                "stats",
                "source",
                "as_of",
            }
        _assert_calm(narration["reasoning"])
        _assert_calm(narration["action_label"])
    finally:
        _delete_user(email)


def test_narration_no_action_reason_passthrough(client):
    email = _unique_email()
    try:
        _register(client, email)
        headers = {"Authorization": f"Bearer {_login(client, email)}"}
        uid = _user_id_for(email)
        _seed_balance(uid, "5000")
        # Undecided target → no_target no-action status.

        body = client.get("/api/allocation/narration", headers=headers).json()
        assert body["plan"]["status"] == "no_target"
        # The narration reasoning is the calm plan reason passthrough; empty evidence.
        assert body["narration"]["reasoning"] == body["plan"]["reason"]
        assert body["narration"]["evidence"] == []
        _assert_calm(body["narration"]["reasoning"])
    finally:
        _delete_user(email)


def test_narration_writes_no_decision_record(client):
    email = _unique_email()
    try:
        _register(client, email)
        headers = {"Authorization": f"Bearer {_login(client, email)}"}
        uid = _user_id_for(email)
        _seed_full_deploy(client, uid, headers)

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
        r = client.get("/api/allocation/narration", headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["plan"]["status"] == "deploy"  # the write-prone path
        after = _decision_count()
        assert before == after == 0  # nothing written, no order placed
    finally:
        _delete_user(email)


def test_narration_per_user_isolation(client):
    email_a = _unique_email()
    email_b = _unique_email()
    try:
        _register(client, email_a)
        _register(client, email_b)
        headers_a = {"Authorization": f"Bearer {_login(client, email_a)}"}
        headers_b = {"Authorization": f"Bearer {_login(client, email_b)}"}
        uid_a = _user_id_for(email_a)

        # A: a full deploy setup. B: nothing at all.
        _seed_full_deploy(client, uid_a, headers_a)

        a_body = client.get("/api/allocation/narration", headers=headers_a).json()
        assert a_body["plan"]["status"] == "deploy"

        # B sees ONLY its own (empty) state — never A's holdings/cash/target.
        b_body = client.get("/api/allocation/narration", headers=headers_b).json()
        assert b_body["plan"]["status"] == "no_target"
        assert b_body["narration"]["evidence"] == []
    finally:
        _delete_user(email_a)
        _delete_user(email_b)
