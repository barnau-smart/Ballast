"""Story 10.4 — SELL-side analysis buckets: concentration + cost/fees.

The pure detectors + narration layer (:mod:`allocation.review`) are tested with no
DB — concentration/trim (incl. dust-drop, index-core/parked exclusion, sub-threshold
no-op), cost/switch (incl. unknown-fee no-op, sub-delta no-op, ``switch_to`` =
canonical), ranking, the reused 10-3 honesty gates (invented number + forecast, incl.
a violation hidden in an uncertainty → fallback), fake-mode → fallback, determinism,
the calm-copy bar, and the 5 good-lesson tests as 5 named assertions per kind. The
``GET /api/allocation/review`` endpoint is tested against the real DB via the
TestClient, asserting the ``{findings}`` shape/ranking, that NO ``decision_record``
is written and no order placed, the empty-findings no-LLM path, requires-auth, and
per-user isolation (AD-10).

⚠️ Run against the disposable ``ballast_test`` DB (never ``ballast`` — the live
Schwab link). See the story's live-link guard.
"""

from __future__ import annotations

import datetime
import re
import uuid
from dataclasses import dataclass
from decimal import Decimal

import pytest

from allocation.narrate import NarrationValidationError
from allocation.narrate import UNIT_BARE, UNIT_MONEY, UNIT_PERCENT
from allocation.review import (
    CONCENTRATION_CEILING,
    KIND_CONCENTRATION,
    KIND_COST,
    ReviewFinding,
    _fallback_review_narration,
    allowed_review_facts,
    build_review_facts,
    check_no_forecast,
    check_no_invented_numbers,
    find_concentration_findings,
    find_cost_findings,
    find_review,
    narrate_finding,
)
from api.app import create_app
from brokers.portfolio import PortfolioView
from coach.recommendation import OrderSide, OrderType
from db.connection import get_connection
from fastapi.testclient import TestClient
from llm.fake_adapter import FakeLLMGateway
from precedent.evidence import EvidenceKind

PASSWORD = "supersecret123"


# --- Lightweight holding stand-in (mirrors PortfolioCache's public shape) -----


@dataclass
class _Holding:
    symbol: str
    market_value: Decimal
    quantity: Decimal


def _view(holdings, cash="0") -> PortfolioView:
    return PortfolioView(
        holdings=list(holdings),
        cash=Decimal(cash),
        as_of=datetime.datetime(2026, 8, 12, tzinfo=datetime.timezone.utc),
    )


# --- Concentration detector --------------------------------------------------


def test_concentration_over_ceiling_produces_trim():
    """TSLA at 55% of a $10,000 portfolio → a concentration trim back to 40%
    ($1,500), whole-share sized, SELL MARKET."""
    view = _view(
        [
            _Holding("TSLA", Decimal("5500"), Decimal("20")),
            _Holding("VTI", Decimal("4500"), Decimal("10")),
        ]
    )
    findings = find_concentration_findings(view, None)
    assert len(findings) == 1
    f = findings[0]
    assert f.kind == KIND_CONCENTRATION
    assert f.symbol == "TSLA"
    assert f.switch_to is None
    # 5500 - 0.40*10000 = 1500.
    assert f.amount == Decimal("1500.00")
    assert f.weight == Decimal("5500") / Decimal("10000")
    assert f.order_intent.side is OrderSide.SELL
    assert f.order_intent.order_type is OrderType.MARKET
    assert f.order_intent.amount == Decimal("1500.00")


def test_find_review_aggregates_duplicate_symbol_rows_into_one_trim():
    """Two cache rows for the SAME ticker are ONE economic position: they must
    produce a single concentration trim at the SUMMED weight, never two overlapping
    SELLs a human could co-sign into an oversell (regression: dup-symbol double-trim)."""
    # TSLA split across two rows (9000 + 9000 = 18000 = 90% of a $20,000 total); VTI 10%.
    view = _view(
        [
            _Holding("TSLA", Decimal("9000"), Decimal("30")),
            _Holding("TSLA", Decimal("9000"), Decimal("30")),
            _Holding("VTI", Decimal("2000"), Decimal("5")),
        ]
    )
    findings = find_review(view, None)
    tsla = [f for f in findings if f.symbol == "TSLA"]
    assert len(tsla) == 1  # ONE trim, not two
    f = tsla[0]
    # Real TSLA weight is 18000/20000 = 90% (not the per-row 45%); trim back to 40%:
    # 18000 - 0.40 * 20000 = 10000.
    assert f.weight == Decimal("18000") / Decimal("20000")
    assert f.amount == Decimal("10000.00")
    assert f.order_intent.amount == Decimal("10000.00")


def test_concentration_includes_cash_in_total():
    """The weight denominator is Σ holdings + cash — cash dilutes the weight."""
    # TSLA 4000 of (4000 holdings + 6000 cash) = 40% exactly → NOT over the ceiling.
    view = _view([_Holding("TSLA", Decimal("4000"), Decimal("10"))], cash="6000")
    assert find_concentration_findings(view, None) == []


def test_concentration_sub_threshold_no_finding():
    """A non-index stock UNDER the ceiling is left alone (aware-but-don't-act
    deferred) — never a manufactured trade."""
    view = _view(
        [
            _Holding("TSLA", Decimal("3000"), Decimal("10")),
            _Holding("VTI", Decimal("7000"), Decimal("10")),
        ]
    )
    assert find_concentration_findings(view, None) == []


def test_concentration_excludes_index_core():
    """A broad index-core position over the ceiling is asset-class balance for the
    deploy path — NEVER trimmed as single-name risk."""
    view = _view(
        [
            _Holding("VTI", Decimal("8000"), Decimal("40")),
            _Holding("BND", Decimal("2000"), Decimal("10")),
        ]
    )
    assert find_concentration_findings(view, None) == []


def test_concentration_excludes_parked(_seed=None):
    """A declared parked money-market symbol over the ceiling is never flagged."""

    @dataclass
    class _Cfg:
        parked_symbols: list

    view = _view([_Holding("SWVXX", Decimal("6000"), Decimal("6000"))], cash="1000")
    cfg = _Cfg(parked_symbols=["SWVXX"])
    assert find_concentration_findings(view, cfg) == []


def test_concentration_dust_trim_dropped():
    """A trim that sizes to < 1 whole share is dust → dropped (no order)."""
    # A very high-priced single share: qty 1, value 9000 of 10000 = 90% over ceiling.
    # Trim = 9000 - 0.40*10000 = 5000, but unit price = 9000/1 = 9000, and
    # floor(5000/9000) = 0 shares → dust, dropped.
    view = _view(
        [
            _Holding("BRKA", Decimal("9000"), Decimal("1")),
            _Holding("VTI", Decimal("1000"), Decimal("5")),
        ]
    )
    assert find_concentration_findings(view, None) == []


# --- Cost detector -----------------------------------------------------------


def test_cost_high_fee_fund_switch_to_canonical():
    """AGTHX (0.61% ER, US equity) → a cost switch: SELL the whole position,
    ``switch_to`` = the canonical VTI (0.03%), both real ERs carried."""
    view = _view([_Holding("AGTHX", Decimal("3000"), Decimal("30"))])
    findings = find_cost_findings(view, None)
    assert len(findings) == 1
    f = findings[0]
    assert f.kind == KIND_COST
    assert f.symbol == "AGTHX"
    assert f.switch_to == "VTI"  # canonical US-equity fund
    assert f.amount == Decimal("3000.00")  # whole position
    assert f.expense_ratio == Decimal("0.61")
    assert f.cheaper_expense_ratio == Decimal("0.03")
    assert f.order_intent.side is OrderSide.SELL
    assert f.order_intent.order_type is OrderType.MARKET


def test_cost_unknown_fund_no_finding():
    """A held fund not in the expense table has no known ER → no cost finding
    (never invent a ratio)."""
    view = _view([_Holding("ZZZZ", Decimal("3000"), Decimal("30"))])
    assert find_cost_findings(view, None) == []


def test_cost_index_core_no_finding():
    """An index-core / canonical fund is the cheap target — the delta is ≤ 0 (or it
    IS its own canonical), so it never fires a cost switch."""
    view = _view(
        [
            _Holding("VTI", Decimal("3000"), Decimal("15")),  # its own canonical
            _Holding("SWPPX", Decimal("2000"), Decimal("40")),  # 0.02 vs VTI 0.03
        ]
    )
    assert find_cost_findings(view, None) == []


def test_cost_sub_delta_no_finding():
    """A fund whose ER exceeds the canonical by LESS than the material delta does
    not fire. VFINX (0.14%) vs VTI (0.03%) is a 0.11pp gap < 0.20pp."""
    view = _view([_Holding("VFINX", Decimal("3000"), Decimal("30"))])
    assert find_cost_findings(view, None) == []


def test_cost_excludes_parked():
    """A parked symbol is excluded from the cost bucket even if it were in the
    table."""

    @dataclass
    class _Cfg:
        parked_symbols: list

    view = _view([_Holding("AGTHX", Decimal("3000"), Decimal("30"))])
    cfg = _Cfg(parked_symbols=["AGTHX"])
    assert find_cost_findings(view, cfg) == []


def test_cost_dust_switch_dropped():
    """A switch that sizes to < 1 whole share is dust → dropped."""
    # AGTHX qty 1, value 3000 → unit 3000; floor(3000/3000) = 1 share is NOT dust,
    # so make it size to 0: value 2999 with unit 3000 → floor(2999/3000)=0.
    view = _view([_Holding("AGTHX", Decimal("2999.00"), Decimal("0.9997"))])
    # unit = 2999/0.9997 ≈ 3000.9; floor(2999/3000.9) = 0 → dust.
    assert find_cost_findings(view, None) == []


# --- find_review ranking -----------------------------------------------------


def test_find_review_ranks_by_amount_desc_ties_symbol_asc():
    """Both buckets fire → findings ranked by SELL amount desc, ties by symbol asc."""
    view = _view(
        [
            _Holding("TSLA", Decimal("5500"), Decimal("20")),  # concentration $1500
            _Holding("AGTHX", Decimal("2000"), Decimal("30")),  # cost $2000
            _Holding("VTI", Decimal("2500"), Decimal("12")),
        ],
        cash="0",
    )
    findings = find_review(view, None)
    assert [f.symbol for f in findings] == ["AGTHX", "TSLA"]  # 2000 before 1500
    assert findings[0].kind == KIND_COST
    assert findings[1].kind == KIND_CONCENTRATION


def test_find_review_nothing_to_fix_empty():
    """A diversified, low-cost, all-index-core portfolio → no findings (honest)."""
    view = _view(
        [
            _Holding("VTI", Decimal("6000"), Decimal("30")),
            _Holding("VXUS", Decimal("3000"), Decimal("50")),
            _Holding("BND", Decimal("1000"), Decimal("14")),
        ]
    )
    assert find_review(view, None) == []


def test_find_review_same_symbol_prefers_cost_switch_over_trim():
    """A non-index HIGH-FEE fund held OVER the ceiling qualifies for BOTH buckets;
    find_review must surface ONLY the cost switch (whole-position sell subsumes the
    partial trim) — never two overlapping SELLs for one holding."""
    view = _view(
        [
            _Holding("AGTHX", Decimal("5500"), Decimal("20")),  # 55%, non-index, high-fee
            _Holding("VTI", Decimal("4500"), Decimal("10")),
        ],
        cash="0",
    )
    # Both detectors fire for AGTHX in isolation.
    assert len(find_concentration_findings(view, None)) == 1
    assert len(find_cost_findings(view, None)) == 1
    # But find_review dedupes by symbol, preferring the cost switch.
    findings = find_review(view, None)
    assert len(findings) == 1
    assert findings[0].symbol == "AGTHX"
    assert findings[0].kind == KIND_COST
    assert findings[0].amount == Decimal("5500.00")  # the whole position, not the $1500 trim


def test_concentration_ceiling_matches_coach_pipeline():
    """The review ceiling is a deliberate mirror of the coach's single-position
    ceiling — guard against silent drift without coupling the runtime import graph."""
    from coach.pipeline import CONCENTRATION_SHARE_CEILING

    assert CONCENTRATION_CEILING == CONCENTRATION_SHARE_CEILING


# --- build_review_facts + allowed_review_facts --------------------------------


def _conc_finding() -> ReviewFinding:
    view = _view(
        [
            _Holding("TSLA", Decimal("5500"), Decimal("20")),
            _Holding("VTI", Decimal("4500"), Decimal("10")),
        ]
    )
    return find_concentration_findings(view, None)[0]


def _cost_finding() -> ReviewFinding:
    view = _view([_Holding("AGTHX", Decimal("3000"), Decimal("30"))])
    return find_cost_findings(view, None)[0]


def test_facts_one_strategy_record_content_addressed():
    for f in (_conc_finding(), _cost_finding()):
        ev = build_review_facts(f)
        assert len(ev) == 1
        assert ev[0].kind is EvidenceKind.STRATEGY
        assert ev[0].id.startswith("strat-")
        # Deterministic id for the same finding.
        assert build_review_facts(f)[0].id == ev[0].id


def test_allowed_facts_admits_amount_value_weights_ceiling_and_ers():
    conc = _conc_finding()
    allowed = allowed_review_facts(conc)
    # Story 10.6 — tagged (value, unit) pairs.
    assert (Decimal("1500.00"), UNIT_MONEY) in allowed  # sell amount
    assert (Decimal("5500"), UNIT_MONEY) in allowed  # holding value
    assert (Decimal("0.55"), UNIT_BARE) in allowed and (Decimal("55"), UNIT_PERCENT) in allowed
    assert (Decimal("0.40"), UNIT_BARE) in allowed and (Decimal("40"), UNIT_PERCENT) in allowed

    cost = _cost_finding()
    callowed = allowed_review_facts(cost)
    # Expense ratios cited "0.61%"/"0.03%" → PERCENT.
    assert (Decimal("0.61"), UNIT_PERCENT) in callowed and (Decimal("0.03"), UNIT_PERCENT) in callowed
    assert (Decimal("3000.00"), UNIT_MONEY) in callowed


def test_review_gate_rejects_bare_count_matching_the_ceiling_percent():
    """Story 10.6 unit-aware gate on the review side: the concentration ceiling is
    40%, so a fabricated bare "40 stocks" (40 is a PERCENT, not a bare fact) must be
    rejected, while "40%" cited with its unit still passes."""
    allowed = allowed_review_facts(_conc_finding())
    check_no_invented_numbers("Back toward the 40% single-position ceiling.", allowed)
    with pytest.raises(NarrationValidationError):
        check_no_invented_numbers("Spread across 40 stocks instead.", allowed)


def test_allowed_facts_rejects_wrong_er():
    """A wrong-but-plausible ER the LLM might invent is not admitted → the gate
    rejects it as a fabricated fact."""
    allowed = allowed_review_facts(_cost_finding())
    with pytest.raises(NarrationValidationError):
        check_no_invented_numbers("This fund charges 0.75% a year.", allowed)


# --- The 5 good-lesson tests (per kind, as 5 named assertions) -----------------


def _conc_fallback():
    f = _conc_finding()
    return _fallback_review_narration(f, build_review_facts(f)), f


def _cost_fallback():
    f = _cost_finding()
    return _fallback_review_narration(f, build_review_facts(f)), f


def test_lesson_concentration_principle_not_pick():
    """Frames the move as diversifying out of single-name risk into the CORE — never
    a stock-pick / hot / winner buy."""
    n, _ = _conc_fallback()
    blob = (n.action_label + " " + n.reasoning).lower()
    assert "principle" in blob and "diversification" in blob
    assert "not chasing a winner" in blob or "de-speculating" in blob


def test_lesson_concentration_why_generalizes():
    n, _ = _conc_fallback()
    blob = n.reasoning.lower()
    assert "broad index core" in blob and "whole market" in blob


def test_lesson_concentration_recognized_best_practice():
    n, _ = _conc_fallback()
    assert "diversification" in n.reasoning.lower()


def test_lesson_concentration_teaches_the_tradeoff():
    n, _ = _conc_fallback()
    blob = n.reasoning.lower()
    assert "tradeoff" in blob and "cap the upside" in blob
    assert any(u and u.strip() for u in n.uncertainties)


def test_lesson_concentration_facts_not_forecast():
    n, f = _conc_fallback()
    allowed = allowed_review_facts(f)
    combined = n.reasoning + " " + n.action_label
    check_no_invented_numbers(combined, allowed)
    check_no_forecast(combined)


def test_lesson_cost_principle_not_pick():
    n, _ = _cost_fallback()
    blob = (n.action_label + " " + n.reasoning).lower()
    assert "principle" in blob and "minimize fund fees" in blob
    assert "not a bet on a hot pick" in blob or "like-for-like" in blob


def test_lesson_cost_why_generalizes():
    n, _ = _cost_fallback()
    blob = n.reasoning.lower()
    assert "compound" in blob and "same broad exposure" in blob


def test_lesson_cost_recognized_best_practice():
    n, _ = _cost_fallback()
    assert "minimize fund fees" in n.reasoning.lower()


def test_lesson_cost_teaches_the_tradeoff():
    n, _ = _cost_fallback()
    blob = n.reasoning.lower()
    assert "tradeoff" in blob
    # Tax noted honestly but NOT computed.
    assert "taxable gain" in blob and "don't calculate tax" in blob
    assert any(u and u.strip() for u in n.uncertainties)


def test_lesson_cost_facts_not_forecast():
    n, f = _cost_fallback()
    allowed = allowed_review_facts(f)
    combined = n.reasoning + " " + n.action_label
    check_no_invented_numbers(combined, allowed)
    check_no_forecast(combined)


def test_fallback_cites_evidence_and_kind_status():
    for fb in (_conc_fallback, _cost_fallback):
        n, f = fb()
        ev = build_review_facts(f)
        assert n.evidence == ev
        assert n.status == f.kind


# --- narrate_finding: gates, fake mode, determinism ---------------------------


class _ScriptedGateway(FakeLLMGateway):
    """A gateway returning a fixed narration citing the finding's real evidence id
    so the structural gate passes and we can probe the honesty gates in isolation."""

    def __init__(self, finding: ReviewFinding, *, uncertainty: str):
        self._ids = [r.id for r in build_review_facts(finding)]
        self._uncertainty = uncertainty

    def complete(self, request):  # noqa: D401
        from llm.port import LLMResponse

        return LLMResponse(
            output={
                "action_label": "Consider this de-speculating move",
                "reasoning": (
                    "This trims a large single position back toward your broad, "
                    "diversified index core — plain diversification, not a hot pick."
                ),
                "evidence": self._ids,
                "uncertainties": [self._uncertainty],
            },
            model="scripted",
            provider="fake",
        )


def test_narrate_finding_accepts_clean_scripted_narration():
    f = _conc_finding()
    n = narrate_finding(
        _ScriptedGateway(f, uncertainty="A fill isn't guaranteed; markets move."),
        f,
    )
    assert n.status == KIND_CONCENTRATION
    assert "diversified index core" in n.reasoning


def test_narrate_finding_gates_invented_number():
    """An LLM number not in the finding's allow-set degrades to the template."""
    f = _cost_finding()

    class _BadNumberGateway(FakeLLMGateway):
        def complete(self, request):  # noqa: D401
            from llm.port import LLMResponse

            ids = [r.id for r in build_review_facts(f)]
            return LLMResponse(
                output={
                    "action_label": "Switch funds",
                    "reasoning": "This fund secretly charges 0.99% — switch it.",
                    "evidence": ids,
                    "uncertainties": ["A fill isn't guaranteed."],
                },
                model="scripted",
                provider="fake",
            )

    n = narrate_finding(_BadNumberGateway(), f)
    # Degraded to the deterministic fallback — the fabricated 0.99% never surfaces.
    assert n.reasoning == _fallback_review_narration(f, build_review_facts(f)).reasoning


def test_narrate_finding_gates_forecast_hidden_in_uncertainty():
    """A forecast hiding in an LLM-authored uncertainty degrades to the template."""
    f = _conc_finding()
    n = narrate_finding(
        _ScriptedGateway(f, uncertainty="TSLA will outperform the market next year."),
        f,
    )
    assert n.uncertainties == _fallback_review_narration(
        f, build_review_facts(f)
    ).uncertainties
    for u in n.uncertainties:
        check_no_forecast(u)


def test_narrate_finding_gates_invented_number_in_uncertainty():
    """A fabricated number in an uncertainty degrades to the template."""
    f = _conc_finding()
    n = narrate_finding(
        _ScriptedGateway(f, uncertainty="You might lose $8,675 in a downturn."),
        f,
    )
    allowed = allowed_review_facts(f)
    for u in n.uncertainties:
        check_no_invented_numbers(u, allowed)
    assert n.reasoning == _fallback_review_narration(f, build_review_facts(f)).reasoning


def test_narrate_finding_fake_mode_returns_fallback():
    """Fake gateway fills evidence with an unbacked id → UnbackedEvidenceError →
    the deterministic template (no ``fake-`` placeholder, passes both gates)."""
    for finding in (_conc_finding(), _cost_finding()):
        n = narrate_finding(FakeLLMGateway(), finding)
        assert n.status == finding.kind
        combined = n.reasoning + " " + n.action_label
        assert "fake-" not in combined
        allowed = allowed_review_facts(finding)
        check_no_invented_numbers(combined, allowed)
        check_no_forecast(combined)
        assert len(n.evidence) == 1
        assert all(r.kind is EvidenceKind.STRATEGY for r in n.evidence)


def test_narrate_finding_deterministic():
    f = _cost_finding()
    assert narrate_finding(FakeLLMGateway(), f) == narrate_finding(FakeLLMGateway(), f)


# --- Calm-copy tone bar ------------------------------------------------------

_FORBIDDEN = [
    "urgent", "hurry", "act now", "act fast", "don't miss", "dont miss",
    "missing out", "miss out", "last chance", "limited time", "warning",
    "alarm", "panic", "crash", "plunge", "fear", "red", "alert", "immediately",
]


def _assert_calm(text: str) -> None:
    blob = str(text).lower()
    for word in _FORBIDDEN:
        pattern = r"\b" + re.escape(word) + r"\b"
        assert not re.search(pattern, blob), f"review copy should never say {word!r}"


def test_fallback_copy_is_calm():
    for fb in (_conc_fallback, _cost_fallback):
        n, _ = fb()
        _assert_calm(n.action_label)
        _assert_calm(n.reasoning)
        for u in n.uncertainties:
            _assert_calm(u)


# --- Endpoint tests (REAL DB) ------------------------------------------------


def _unique_email() -> str:
    return f"alloc-review-{uuid.uuid4().hex}@example.com"


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


def _seed_holding(
    uid: uuid.UUID, symbol: str, market_value: str, quantity: str = "1"
) -> None:
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
                    quantity,
                    market_value,
                    None,
                    "0",
                    now,
                ),
            )
        conn.commit()


@pytest.fixture
def client() -> TestClient:
    with TestClient(create_app()) as c:
        yield c


def _decision_count(uid: uuid.UUID) -> int:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM decision_record WHERE owner_id = %s",
                (str(uid),),
            )
            (n,) = cur.fetchone()
    return int(n)


def test_review_requires_authentication(client):
    assert client.get("/api/allocation/review").status_code == 401


def test_review_shape_ranking_and_no_writes(client):
    email = _unique_email()
    try:
        _register(client, email)
        headers = {"Authorization": f"Bearer {_login(client, email)}"}
        uid = _user_id_for(email)
        # TSLA 55% (concentration $1500) + AGTHX high fee ($2000 switch); VTI core.
        _seed_balance(uid, "0")
        _seed_holding(uid, "TSLA", "5500", "20")
        _seed_holding(uid, "AGTHX", "2000", "30")
        _seed_holding(uid, "VTI", "2500", "12")

        before = _decision_count(uid)
        r = client.get("/api/allocation/review", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body.keys()) == {"findings"}
        findings = body["findings"]
        assert len(findings) == 2
        # Ranked by SELL amount desc: AGTHX ($2000) before TSLA ($1500).
        assert [f["symbol"] for f in findings] == ["AGTHX", "TSLA"]

        cost = findings[0]
        assert cost["kind"] == "cost"
        assert cost["switch_to"] == "VTI"
        assert cost["order"]["side"] == "sell"
        assert cost["order"]["order_type"] == "market"
        assert cost["order"]["amount"] == "2000.00"
        assert cost["order"]["symbol"] == "AGTHX"

        conc = findings[1]
        assert conc["kind"] == "concentration"
        assert conc["switch_to"] is None
        assert conc["order"]["side"] == "sell"
        assert conc["order"]["amount"] == "1500.00"

        # Every finding carries a well-formed advisor narration.
        for f in findings:
            narration = f["narration"]
            assert set(narration.keys()) == {
                "action_label",
                "reasoning",
                "uncertainties",
                "evidence",
            }
            assert narration["action_label"] and narration["reasoning"]
            assert len(narration["uncertainties"]) >= 1
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

        # Writes NOTHING, places no order.
        assert before == _decision_count(uid) == 0
    finally:
        _delete_user(email)


def test_review_empty_findings_nothing_to_fix(client):
    """A diversified, low-cost, all-index-core portfolio → {findings: []}."""
    email = _unique_email()
    try:
        _register(client, email)
        headers = {"Authorization": f"Bearer {_login(client, email)}"}
        uid = _user_id_for(email)
        _seed_balance(uid, "0")
        _seed_holding(uid, "VTI", "6000", "30")
        _seed_holding(uid, "VXUS", "3000", "50")
        _seed_holding(uid, "BND", "1000", "14")

        r = client.get("/api/allocation/review", headers=headers)
        assert r.status_code == 200, r.text
        assert r.json() == {"findings": []}
    finally:
        _delete_user(email)


def test_review_per_user_isolation(client):
    email_a = _unique_email()
    email_b = _unique_email()
    try:
        _register(client, email_a)
        _register(client, email_b)
        headers_a = {"Authorization": f"Bearer {_login(client, email_a)}"}
        headers_b = {"Authorization": f"Bearer {_login(client, email_b)}"}
        uid_a = _user_id_for(email_a)

        # A: an over-concentrated single stock. B: nothing at all.
        _seed_balance(uid_a, "0")
        _seed_holding(uid_a, "TSLA", "6000", "20")
        _seed_holding(uid_a, "VTI", "4000", "20")

        a_body = client.get("/api/allocation/review", headers=headers_a).json()
        assert any(f["symbol"] == "TSLA" for f in a_body["findings"])

        # B sees ONLY its own (empty) state — never A's holdings.
        b_body = client.get("/api/allocation/review", headers=headers_b).json()
        assert b_body == {"findings": []}
    finally:
        _delete_user(email_a)
        _delete_user(email_b)
