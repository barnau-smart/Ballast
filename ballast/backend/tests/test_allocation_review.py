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
    BOND_SHORTFALL,
    CONCENTRATION_CEILING,
    COVERAGE_MIN,
    KIND_BOND_FLOOR,
    KIND_CONCENTRATION,
    KIND_COST,
    Coverage,
    ReviewFinding,
    _fallback_review_narration,
    allowed_review_facts,
    build_review_facts,
    check_no_forecast,
    check_no_invented_numbers,
    BLENDED_ER_INFO,
    SINGLE_NAME_AGG_MAX,
    Fees,
    SingleStock,
    compute_coverage,
    compute_fees,
    coverage_message,
    fees_message,
    single_stock_from_coverage,
    single_stock_message,
    find_bond_floor_finding,
    find_concentration_findings,
    find_cost_findings,
    find_review,
    narrate_finding,
)
from api.allocation import _coverage_out, _fees_out, _single_stock_out
from strategy.target_allocation import BONDS, INTL_EQUITY, US_EQUITY
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
        assert set(body.keys()) == {"findings", "coverage", "single_stock", "fees"}
        # Blended ER = (2000*0.61 + 2500*0.03)/4500 = 0.2878% < 0.30 band → no fees note.
        assert body["fees"] is None
        # TSLA 5500 + AGTHX 2000 unclassified of 10000 total → 25% coverage → inadequate.
        assert body["coverage"]["adequate"] is False
        assert body["coverage"]["coverage"] == "25.00"
        assert body["coverage"]["message"]  # calm informational line present when low
        # 75% single-stock sleeve → over the 25% band → the 11.3 note is present + distinct.
        assert body["single_stock"] is not None
        assert body["single_stock"]["pct"] == "75.00"
        assert body["single_stock"]["message"] != body["coverage"]["message"]
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
        body = r.json()
        assert body["findings"] == []
        # All index-core → fully classified → adequate, no informational message.
        assert body["coverage"]["adequate"] is True
        assert body["coverage"]["coverage"] == "100.00"
        assert body["coverage"]["unclassified_value"] == "0.00"
        assert body["coverage"]["message"] is None
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

        # B sees ONLY its own (empty) state — never A's holdings. Empty portfolio →
        # coverage is null (nothing to measure), not a fabricated 0%/100%.
        b_body = client.get("/api/allocation/review", headers=headers_b).json()
        assert b_body == {"findings": [], "coverage": None, "single_stock": None, "fees": None}
    finally:
        _delete_user(email_a)
        _delete_user(email_b)


# --- Coverage meta-check (Story 11.1) ----------------------------------------


@dataclass
class _Cfg:
    parked_symbols: list


def test_coverage_mixed_below_threshold():
    """A big unclassified single-stock sleeve drops coverage below the floor."""
    view = _view(
        [
            _Holding("TSLA", Decimal("5500"), Decimal("20")),  # unclassified
            _Holding("VTI", Decimal("4500"), Decimal("10")),   # index-core
        ]
    )
    cov = compute_coverage(view, None)
    assert cov is not None
    assert cov.coverage == Decimal("0.45")
    assert cov.adequate is False
    assert cov.unclassified_value == Decimal("5500")
    assert cov.unclassified_symbols == ["TSLA"]


def test_coverage_all_classified_is_adequate():
    """All index-core → fully classified → coverage 1.0, adequate, nothing unclassified."""
    view = _view(
        [
            _Holding("VTI", Decimal("6000"), Decimal("30")),
            _Holding("VXUS", Decimal("3000"), Decimal("50")),
            _Holding("BND", Decimal("1000"), Decimal("14")),
        ]
    )
    cov = compute_coverage(view, None)
    assert cov is not None
    assert cov.coverage == Decimal("1")
    assert cov.adequate is True
    assert cov.unclassified_value == Decimal("0")
    assert cov.unclassified_symbols == []


def test_coverage_parked_money_market_counts_as_known():
    """A declared parked money-market holding (SWVXX) is KNOWN cash, never unclassified —
    so it must NOT drag coverage down."""
    view = _view(
        [
            _Holding("SWVXX", Decimal("6000"), Decimal("6000")),  # parked → known
            _Holding("VTI", Decimal("4000"), Decimal("20")),
        ]
    )
    cov = compute_coverage(view, _Cfg(parked_symbols=["SWVXX"]))
    assert cov is not None
    assert cov.unclassified_value == Decimal("0")
    assert cov.coverage == Decimal("1")
    assert cov.adequate is True


def test_coverage_cash_counts_toward_the_known_side():
    """Cash is part of total AND the classified/known side — it raises coverage."""
    # VTI 2000 classified + TSLA 2000 unclassified + 6000 cash = 10000 total; 20% unclassified.
    view = _view(
        [
            _Holding("VTI", Decimal("2000"), Decimal("10")),
            _Holding("TSLA", Decimal("2000"), Decimal("8")),
        ],
        cash="6000",
    )
    cov = compute_coverage(view, None)
    assert cov is not None
    assert cov.coverage == Decimal("0.80")
    assert cov.adequate is True  # exactly at the floor → adequate (>=)


def test_coverage_boundary_is_inclusive():
    """coverage == COVERAGE_MIN is adequate (the floor is inclusive)."""
    # unclassified 2000 of 10000 → coverage exactly 0.80 == COVERAGE_MIN.
    view = _view(
        [
            _Holding("TSLA", Decimal("2000"), Decimal("8")),
            _Holding("VTI", Decimal("8000"), Decimal("40")),
        ]
    )
    cov = compute_coverage(view, None)
    assert cov is not None
    assert cov.coverage == COVERAGE_MIN
    assert cov.adequate is True


def test_coverage_empty_portfolio_returns_none():
    """No holdings and no cash → nothing to measure → None (never a fabricated 0%/100%)."""
    assert compute_coverage(_view([], cash="0"), None) is None


def test_coverage_non_finite_market_value_does_not_over_report():
    """An unpriced (NaN) unclassified holding must NOT zero the whole sleeve / report 100%
    coverage. It is dropped from BOTH sides (like ``_total_portfolio_value``), so coverage
    honestly reflects the finite split — the remaining unclassified AAPL still counts."""
    view = _view(
        [
            _Holding("TSLA", Decimal("NaN"), Decimal("5")),   # unpriced → excluded both sides
            _Holding("VTI", Decimal("4000"), Decimal("20")),  # index-core
            _Holding("AAPL", Decimal("3000"), Decimal("12")),  # unclassified, finite
        ]
    )
    cov = compute_coverage(view, None)
    assert cov is not None
    # total = 7000 (NaN skipped); unclassified = 3000 (AAPL); coverage = 1 - 3000/7000.
    assert cov.unclassified_value == Decimal("3000")
    assert cov.coverage == (Decimal("1") - Decimal("3000") / Decimal("7000"))
    assert cov.adequate is False  # ~0.571 < 0.80 → the message would show, honestly
    assert "AAPL" in cov.unclassified_symbols
    assert "TSLA" not in cov.unclassified_symbols


def test_coverage_message_is_calm_and_cites_only_real_numbers():
    """The low-coverage message states the computed percent + value + symbols, stays calm,
    and never forecasts."""
    cov = compute_coverage(
        _view([_Holding("TSLA", Decimal("6000"), Decimal("20")),
               _Holding("VTI", Decimal("4000"), Decimal("20"))]),
        None,
    )
    assert cov is not None and cov.adequate is False
    msg = coverage_message(cov)
    assert "40.00%" in msg  # 1 - 6000/10000 = 40%
    assert "6,000.00" in msg or "6000.00" in msg
    assert "TSLA" in msg
    _assert_calm(msg)
    check_no_forecast(msg)  # must not raise — no prediction language


def test_coverage_out_serializes_and_gates_message():
    """_coverage_out: percent+money as fixed-point strings; message ONLY when inadequate."""
    low = _coverage_out(
        Coverage(
            coverage=Decimal("0.25"),
            adequate=False,
            unclassified_value=Decimal("7500"),
            unclassified_symbols=["TSLA", "AGTHX"],
            total=Decimal("10000"),
        )
    )
    assert low is not None
    assert low.coverage == "25.00"
    assert low.unclassified_value == "7500.00"
    assert low.unclassified_symbols == ["TSLA", "AGTHX"]
    assert low.message  # present when inadequate

    high = _coverage_out(
        Coverage(
            coverage=Decimal("1"),
            adequate=True,
            unclassified_value=Decimal("0"),
            unclassified_symbols=[],
            total=Decimal("10000"),
        )
    )
    assert high is not None
    assert high.coverage == "100.00"
    assert high.message is None  # adequate → no informational line

    assert _coverage_out(None) is None


# --- Blended expense summary (Story 11.4) ------------------------------------


def test_fees_blended_math_and_coverage():
    """Dollar-weighted blended ER + annual $ + honest coverage over priced funds only."""
    view = _view([
        _Holding("AGTHX", Decimal("3000"), Decimal("30")),  # 0.61% (priced)
        _Holding("VTI", Decimal("1000"), Decimal("5")),      # 0.03% (priced)
        _Holding("TSLA", Decimal("1000"), Decimal("4")),     # no ER (excluded)
    ])
    fees = compute_fees(view)
    assert fees is not None
    # weighted = 3000*0.61 + 1000*0.03 = 1860; priced = 4000; total = 5000.
    assert fees.blended_er == Decimal("1860") / Decimal("4000")   # 0.465%
    assert fees.annual_cost == Decimal("1860") / Decimal("100")   # $18.60
    assert fees.coverage == Decimal("4000") / Decimal("5000")     # 0.80 (TSLA excluded)
    assert fees.over is True                                       # 0.465 > 0.30


def test_fees_below_band_not_over():
    """All-cheap-index → blended under the band → not over."""
    view = _view([_Holding("VTI", Decimal("6000"), Decimal("30")), _Holding("BND", Decimal("4000"), Decimal("28"))])
    fees = compute_fees(view)
    assert fees is not None and fees.over is False                # 0.03% << 0.30


def test_fees_no_priced_funds_returns_none():
    """Only individual stocks (no known ER) → None (never invent a fee)."""
    assert compute_fees(_view([_Holding("TSLA", Decimal("5000"), Decimal("20"))])) is None


def test_fees_empty_returns_none():
    assert compute_fees(_view([], cash="0")) is None


def test_fees_non_finite_skipped():
    """A NaN-priced fund is skipped, never poisoning the blend."""
    view = _view([
        _Holding("AGTHX", Decimal("NaN"), Decimal("30")),   # unpriced → skipped
        _Holding("VTI", Decimal("1000"), Decimal("5")),
    ])
    fees = compute_fees(view)
    assert fees is not None
    assert fees.blended_er == Decimal("0.03")               # only VTI counted
    assert fees.over is False


def test_fees_message_is_calm_and_states_coverage():
    view = _view([_Holding("AGTHX", Decimal("3000"), Decimal("30")), _Holding("TSLA", Decimal("7000"), Decimal("20"))])
    fees = compute_fees(view)                                # coverage 30%
    msg = fees_message(fees)
    assert "30.00%" in msg          # honest fee coverage (stocks not implied free)
    assert "0.61%" in msg           # blended (only AGTHX priced)
    _assert_calm(msg)
    check_no_forecast(msg)


def test_fees_out_surfaces_only_when_over():
    # AGTHX 3000 (0.61%) + VTI 2000 (0.03%): weighted 1890, blended 0.378%, annual $18.90.
    over = _fees_out(compute_fees(_view([_Holding("AGTHX", Decimal("3000"), Decimal("30")), _Holding("VTI", Decimal("2000"), Decimal("10"))])))
    assert over is not None
    assert over.blended_er == "0.38"          # 0.378 → 0.38 cent-rounded
    assert over.annual_cost == "18.90"
    assert over.coverage == "100.00"          # both holdings are priced funds
    assert over.message
    assert _fees_out(compute_fees(_view([_Holding("VTI", Decimal("9000"), Decimal("45"))]))) is None  # under band
    assert _fees_out(None) is None


# --- Single-stock aggregate concentration (Story 11.3) -----------------------


def _cov(unclassified, total, symbols=("TSLA",)):
    frac = Decimal(unclassified) / Decimal(total)
    return Coverage(
        coverage=Decimal("1") - frac, adequate=(Decimal("1") - frac) >= COVERAGE_MIN,
        unclassified_value=Decimal(unclassified), unclassified_symbols=list(symbols),
        total=Decimal(total),
    )


def test_single_stock_over_band_flagged():
    """Individual-stock sleeve above 25% → flagged (over=True), with value + symbols."""
    ss = single_stock_from_coverage(_cov("6000", "10000", ("TSLA", "NVDA")))
    assert ss is not None
    assert ss.fraction == Decimal("0.6")
    assert ss.over is True
    assert ss.value == Decimal("6000")
    assert ss.symbols == ["TSLA", "NVDA"]


def test_single_stock_at_or_below_band_not_over():
    """At/below 25% → over=False (nothing to flag)."""
    at = single_stock_from_coverage(_cov("2500", "10000"))   # exactly 25%
    assert at is not None and at.over is False               # strictly-greater band
    below = single_stock_from_coverage(_cov("1000", "10000"))
    assert below is not None and below.over is False


def test_single_stock_none_when_nothing_to_measure():
    """No coverage (empty portfolio) → None."""
    assert single_stock_from_coverage(None) is None


def test_single_stock_message_is_calm_distinct_and_cites_numbers():
    """The risk-framed message is calm, cites the computed %/symbols, and differs from the
    coverage-honesty line for the SAME sleeve."""
    cov = _cov("6000", "10000", ("TSLA", "NVDA"))
    ss = single_stock_from_coverage(cov)
    msg = single_stock_message(ss)
    assert "60.00%" in msg and "TSLA" in msg
    _assert_calm(msg)
    check_no_forecast(msg)
    assert msg != coverage_message(cov)   # complementary, not duplicate


def test_single_stock_out_surfaces_only_when_over():
    """_single_stock_out serializes only when over the band; fixed-point strings."""
    over = _single_stock_out(single_stock_from_coverage(_cov("7500", "10000", ("TSLA",))))
    assert over is not None
    assert over.pct == "75.00"
    assert over.value == "7500.00"
    assert over.message
    # under the band → None (informational only when it matters)
    assert _single_stock_out(single_stock_from_coverage(_cov("1000", "10000"))) is None
    assert _single_stock_out(None) is None


# --- Bond-floor / risk-capacity (Story 11.2) ---------------------------------

# Conservative-shaped target (60% bonds) for the bond-floor tests.
_CONSERVATIVE = {US_EQUITY: Decimal("0.30"), INTL_EQUITY: Decimal("0.10"), BONDS: Decimal("0.60")}


def _adequate(total="10000"):
    return Coverage(
        coverage=Decimal("1"), adequate=True,
        unclassified_value=Decimal("0"), unclassified_symbols=[], total=Decimal(total),
    )


def test_bond_floor_fires_when_underbonded_no_cash():
    """Under-bonded vs a Conservative target, no cash → SELL overweight equity into bonds."""
    view = _view([
        _Holding("VTI", Decimal("8000"), Decimal("40")),  # us_equity, overweight
        _Holding("BND", Decimal("2000"), Decimal("14")),   # bonds 20% of a 60% target
    ])
    f = find_bond_floor_finding(view, _CONSERVATIVE, _adequate(), None, Decimal("0"))
    assert f is not None
    assert f.kind == KIND_BOND_FLOOR
    assert f.symbol == "VTI"                 # the overweight equity holding
    assert f.switch_to == "BND"              # buy bonds next
    assert f.order_intent.side == OrderSide.SELL
    assert f.amount == Decimal("4000.00")    # 0.60*10000 - 2000
    assert f.weight == Decimal("2000") / Decimal("10000")   # current bond fraction 0.20
    assert f.target_weight == Decimal("0.60")


def test_bond_floor_within_band_no_finding():
    """Bonds within BOND_SHORTFALL of target → no finding (downside-only)."""
    view = _view([
        _Holding("VTI", Decimal("5000"), Decimal("25")),
        _Holding("BND", Decimal("5000"), Decimal("35")),   # 50% vs 60% target → 10pp ≤ 15pp
    ])
    assert find_bond_floor_finding(view, _CONSERVATIVE, _adequate(), None, Decimal("0")) is None


def test_bond_floor_over_bonded_no_finding():
    """More bonds than target → never fires on the upside."""
    view = _view([
        _Holding("VTI", Decimal("3000"), Decimal("15")),
        _Holding("BND", Decimal("7000"), Decimal("49")),   # 70% > 60% target
    ])
    assert find_bond_floor_finding(view, _CONSERVATIVE, _adequate(), None, Decimal("0")) is None


def test_bond_floor_no_target_none():
    """No chosen target → no finding."""
    view = _view([_Holding("VTI", Decimal("8000"), Decimal("40")), _Holding("BND", Decimal("2000"), Decimal("14"))])
    assert find_bond_floor_finding(view, None, _adequate(), None, Decimal("0")) is None


def test_bond_floor_low_coverage_gated():
    """Inadequate 11.1 coverage → no finding (class weights untrustworthy)."""
    view = _view([_Holding("VTI", Decimal("8000"), Decimal("40")), _Holding("BND", Decimal("2000"), Decimal("14"))])
    low = Coverage(coverage=Decimal("0.5"), adequate=False, unclassified_value=Decimal("5000"),
                   unclassified_symbols=["TSLA"], total=Decimal("10000"))
    assert find_bond_floor_finding(view, _CONSERVATIVE, low, None, Decimal("0")) is None
    assert find_bond_floor_finding(view, _CONSERVATIVE, None, None, Decimal("0")) is None


def test_bond_floor_defers_when_cash_covers_shortfall():
    """When the deploy plan's cash-funded bond buy covers the gap → defer (no finding)."""
    view = _view([_Holding("VTI", Decimal("8000"), Decimal("40")), _Holding("BND", Decimal("2000"), Decimal("14"))])
    # bond_gap = 4000; deploy buys 4000 of bonds with cash → residual 0 → defer.
    assert find_bond_floor_finding(view, _CONSERVATIVE, _adequate(), None, Decimal("4000")) is None


def test_bond_floor_sells_only_the_residual_on_partial_cash():
    """Partial cash → SELL sized to the residual the deploy buy can't cover."""
    view = _view([_Holding("VTI", Decimal("8000"), Decimal("40")), _Holding("BND", Decimal("2000"), Decimal("14"))])
    f = find_bond_floor_finding(view, _CONSERVATIVE, _adequate(), None, Decimal("1500"))
    assert f is not None
    assert f.amount == Decimal("2500.00")    # 4000 gap - 1500 deploy buy


def test_bond_floor_dust_dropped():
    """A residual that sizes to < 1 whole share of the equity holding → dropped."""
    # VTI qty 1 → unit 8000; residual 4000 → floor(4000/8000)=0 shares → dust.
    view = _view([_Holding("VTI", Decimal("8000"), Decimal("1")), _Holding("BND", Decimal("2000"), Decimal("14"))])
    assert find_bond_floor_finding(view, _CONSERVATIVE, _adequate(), None, Decimal("0")) is None


def test_bond_floor_ignores_non_finite_market_value():
    """A NaN/unpriced classified holding must NOT poison classified_total/current_bond —
    it is dropped before classifying (inherits 11.1's finite-guard). Coverage (finite) and
    bond-floor now agree on the same base."""
    view = _view([
        _Holding("SCHB", Decimal("NaN"), Decimal("10")),  # unpriced us_equity → dropped
        _Holding("VTI", Decimal("8000"), Decimal("40")),   # us_equity, priced
        _Holding("BND", Decimal("2000"), Decimal("14")),    # bonds 20% of the finite base
    ])
    f = find_bond_floor_finding(view, _CONSERVATIVE, _adequate(), None, Decimal("0"))
    assert f is not None
    # finite classified base = 10000 (NaN SCHB dropped); bond_gap = 0.60*10000 - 2000 = 4000.
    assert f.weight == Decimal("2000") / Decimal("10000")
    assert f.amount == Decimal("4000.00")
    assert f.symbol == "VTI"


def test_bond_floor_never_sells_more_than_the_holding():
    """Explicit oversell guard: the SELL amount never exceeds the sold holding's value."""
    view = _view([
        _Holding("VTI", Decimal("3000"), Decimal("15")),   # only $3000 of equity held
        _Holding("BND", Decimal("1000"), Decimal("7")),     # bonds 25% of a 60% target
    ])
    f = find_bond_floor_finding(view, _CONSERVATIVE, _adequate(), None, Decimal("0"))
    assert f is not None
    # bond_gap = 0.60*4000 - 1000 = 1400; capped by VTI overweight (3000-0.30*4000=1800)
    # and by the holding value (3000) → 1400. Must never exceed the $3000 holding.
    assert f.amount <= Decimal("3000")
    assert f.order_intent.amount <= f.holding_value


def test_bond_floor_fallback_narration_is_calm_and_lesson_bearing():
    """The deterministic bond-floor fallback passes the honesty gates + is calm."""
    view = _view([_Holding("VTI", Decimal("8000"), Decimal("40")), _Holding("BND", Decimal("2000"), Decimal("14"))])
    f = find_bond_floor_finding(view, _CONSERVATIVE, _adequate(), None, Decimal("0"))
    ev = build_review_facts(f)
    narration = _fallback_review_narration(f, ev)
    blob = " ".join((narration.reasoning, narration.action_label, *narration.uncertainties))
    _assert_calm(blob)
    check_no_forecast(blob)                                   # must not raise
    check_no_invented_numbers(blob, allowed_review_facts(f))  # every number is engine-provided
    assert "60.00%" in narration.reasoning and "20.00%" in narration.reasoning
    assert len(narration.uncertainties) >= 1


def test_bond_floor_endpoint_flow(client):
    """End-to-end: a Conservative target + an under-bonded, fully-classified portfolio →
    a bond_floor finding with current/target bond % on the wire; nothing placed."""
    email = _unique_email()
    try:
        _register(client, email)
        headers = {"Authorization": f"Bearer {_login(client, email)}"}
        uid = _user_id_for(email)
        _seed_balance(uid, "0")
        _seed_holding(uid, "VTI", "8000", "40")   # us_equity, overweight
        _seed_holding(uid, "BND", "2000", "14")    # bonds 20% of a 60% target
        r = client.put("/api/target-allocation", json={"model": "conservative"}, headers=headers)
        assert r.status_code == 200, r.text

        before = _decision_count(uid)
        r = client.get("/api/allocation/review", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        # Fully index-core → coverage adequate → bond_floor can fire.
        assert body["coverage"]["adequate"] is True
        bond = [f for f in body["findings"] if f["kind"] == "bond_floor"]
        assert len(bond) == 1, body["findings"]
        f = bond[0]
        assert f["symbol"] == "VTI"
        assert f["switch_to"] == "BND"
        assert f["order"]["side"] == "sell"
        assert f["order"]["order_type"] == "market"
        assert f["order"]["amount"] == "4000.00"       # 0.60*10000 - 2000
        assert f["current_weight"] == "20.00"
        assert f["target_weight"] == "60.00"
        _assert_calm(f["narration"]["reasoning"])
        # Writes NOTHING, places no order.
        assert before == _decision_count(uid) == 0
    finally:
        _delete_user(email)
