"""Story 7.6 — the read-only live pre-flight payload-shape harness.

Every I/O-matrix row is covered OFFLINE: zero network, zero credentials, zero
paid calls, zero orders. The schwab-py client is MOCKED exactly as
``tests/test_schwab_adapter.py`` mocks it (the SDK is obtained via importlib and
``client_from_access_functions`` is patched to return a fake client), and the
Anthropic ``Message`` is a crafted double exactly as ``tests/test_llm_gateway.py``
uses. No test here makes a real Schwab or Anthropic call.

Rows:
  - Capture OFF (unset dir): tapped seams exercised -> NO files written; parsed
    result identical to non-tapped behavior.
  - Capture ON, on-shape (all 5 seams) -> verdict PASS; skeleton files written.
  - Renamed field -> MISSING + RENAMED-CANDIDATE hint, verdict DRIFT.
  - Missing field (no sibling) -> MISSING, verdict DRIFT.
  - Type drift (askPrice as string) -> TYPE-MISMATCH, verdict DRIFT.
  - Token one-of (expires_in only / expires_at only) -> expiry OK, not MISSING.
  - Redaction: token + accountNumber + hashValue never appear in the file.
  - Zero-order: full harness run against a broker spy records zero place_order,
    and the package source contains no ``place_order`` reference.
  - Order-status out-of-scope line present in the report.
"""

from __future__ import annotations

import importlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

from brokers.port import OrderStatus
from brokers.schwab_adapter import SchwabAdapter
from coach.recommendation import OrderIntent, OrderSide
from preflight import drift, run
from preflight.capture import PayloadCapture, capture, capture_enabled, to_shape

_auth = importlib.import_module("schwab.auth")


# --- Crafted SDK doubles (mirrors test_schwab_adapter.py) ---------------------


class _FakeResponse:
    def __init__(self, *, json_data=None, is_error: bool = False, headers=None):
        self._json = json_data if json_data is not None else {}
        self.is_error = is_error
        self.status_code = 400 if is_error else 200
        self.headers = headers or {}

    def json(self):
        return self._json


class _FakeAccountFields:
    POSITIONS = "positions"


class _FakeAccount:
    Fields = _FakeAccountFields


class _FakeClient:
    """A stand-in for a schwab-py trading ``Client`` — a READ-only spy.

    Records every ``place_order`` call (there must be zero) so the zero-order
    guarantee is asserted structurally.
    """

    Account = _FakeAccount

    def __init__(self, *, account_numbers=None, quote=None, account=None):
        self._account_numbers = account_numbers or [
            {"accountNumber": "123", "hashValue": "HASH123"}
        ]
        self._quote = quote or {"VTI": {"quote": {"askPrice": 250.5}}}
        self._account = (
            account
            if account is not None
            else {
                "securitiesAccount": {
                    "currentBalances": {"cashBalance": 1000.0},
                    "positions": [
                        {
                            "instrument": {"symbol": "VOO"},
                            "longQuantity": 3.0,
                            "marketValue": 1200.0,
                        }
                    ],
                }
            }
        )
        self.placed: list[tuple] = []
        self.get_account_calls: list[tuple] = []

    def get_account_numbers(self):
        return _FakeResponse(json_data=self._account_numbers)

    def get_quote(self, symbol):
        return _FakeResponse(json_data=self._quote)

    def get_account(self, account_hash, *, fields=None):
        self.get_account_calls.append((account_hash, fields))
        return _FakeResponse(json_data=self._account)

    def place_order(self, account_hash, order_spec):  # pragma: no cover
        # Should NEVER be called by the read-only harness — recorded so the spy
        # test fails loudly if it ever is.
        self.placed.append((account_hash, order_spec))
        return _FakeResponse(headers={"Location": "loc"})


class _Block:
    def __init__(self, type: str, text: str | None = None):
        self.type = type
        if text is not None:
            self.text = text


class _Message:
    def __init__(self, stop_reason: str, content: list[_Block]):
        self.stop_reason = stop_reason
        self.content = content


def _on_shape_output_json() -> str:
    return json.dumps(
        {
            "action_label": "hold",
            "reasoning": "steady",
            "evidence": ["E1", "E2"],
            "uncertainties": ["u1"],
        }
    )


def _on_shape_message() -> _Message:
    return _Message("end_turn", [_Block("text", _on_shape_output_json())])


class _FakeGateway:
    """A gateway double whose ``complete`` runs the REAL AnthropicGateway parse tap.

    It drives ``AnthropicGateway._parse_message`` against a crafted Message so the
    llm_message + llm_output taps fire (offline, no SDK, no key).
    """

    def __init__(self, message: _Message):
        self._message = message

    def complete(self, request):
        from llm.anthropic_adapter import AnthropicGateway

        # Bypass __init__ (which requires a key) — we only exercise the parse tap.
        gw = AnthropicGateway.__new__(AnthropicGateway)
        gw.provider = "anthropic"
        return gw._parse_message(self._message, "claude-sonnet-4-6")


@pytest.fixture(autouse=True)
def _configured_schwab_env(monkeypatch):
    monkeypatch.setenv("SCHWAB_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("SCHWAB_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("SCHWAB_CALLBACK_URL", "https://127.0.0.1/callback")


def _install_client(monkeypatch, client: _FakeClient) -> None:
    monkeypatch.setattr(
        _auth, "client_from_access_functions", lambda **kwargs: client
    )


def _on_shape_token() -> dict:
    return {
        "access_token": "SECRET-ACCESS",
        "refresh_token": "SECRET-REFRESH",
        "expires_at": 1700000000,
    }


def _adapter() -> SchwabAdapter:
    return SchwabAdapter(token_read_func=_on_shape_token)


# --- to_shape() reduction / redaction ----------------------------------------


def test_to_shape_dict_scalars_and_arrays():
    shape = to_shape(
        {
            "s": "x",
            "i": 5,
            "f": 1.5,
            "b": True,
            "n": None,
            "arr": [{"k": "v"}, {"k": "v2"}],
            "empty": [],
        }
    )
    assert shape["s"] == "str"
    assert shape["i"] == "int"
    assert shape["f"] == "float"
    assert shape["b"] == "bool"
    assert shape["n"] == "NoneType"
    assert shape["arr"] == {"type": "array", "len": 2, "item": {"k": "str"}}
    assert shape["empty"] == {"type": "array", "len": 0, "item": None}


def test_to_shape_sdk_object_via_attributes():
    msg = _on_shape_message()
    shape = to_shape(msg)
    assert shape["stop_reason"] == "str"
    assert shape["content"]["type"] == "array"
    assert shape["content"]["item"]["type"] == "str"
    assert shape["content"]["item"]["text"] == "str"


def test_redaction_no_secret_value_survives(tmp_path):
    secret_token = "SECRET-ACCESS-TOKEN-abc123"
    secret_refresh = "SECRET-REFRESH-xyz789"
    secret_acct = "987654321"
    secret_hash = "OPAQUEHASHVALUE-deadbeef"
    payload = {
        "access_token": secret_token,
        "refresh_token": secret_refresh,
        "accounts": [{"accountNumber": secret_acct, "hashValue": secret_hash}],
    }
    sink = PayloadCapture(str(tmp_path))
    path = sink.capture("token", payload)
    written = path.read_text()
    for secret in (secret_token, secret_refresh, secret_acct, secret_hash):
        assert secret not in written
    # Only type names / array structure survive.
    assert '"str"' in written


# --- Capture sink OFF/ON ------------------------------------------------------


def test_capture_off_is_noop(monkeypatch, tmp_path):
    monkeypatch.setenv("PREFLIGHT_CAPTURE_DIR", "")
    from api.config import get_settings

    settings = get_settings()
    assert capture_enabled(settings) is False
    assert capture(settings, "token", {"access_token": "x"}) is None
    assert list(tmp_path.iterdir()) == []


def test_capture_on_writes_skeleton(monkeypatch, tmp_path):
    monkeypatch.setenv("PREFLIGHT_CAPTURE_DIR", str(tmp_path))
    from api.config import get_settings

    settings = get_settings()
    assert capture_enabled(settings) is True
    path = capture(settings, "token", {"access_token": "x", "expires_in": 3600})
    assert path.exists()
    skeleton = json.loads(path.read_text())
    assert skeleton == {"access_token": "str", "expires_in": "int"}


# --- Tap capture-OFF: no files, behavior unchanged ---------------------------


def test_taps_off_no_files_and_behavior_unchanged(monkeypatch, tmp_path):
    monkeypatch.setenv("PREFLIGHT_CAPTURE_DIR", "")
    client = _FakeClient()
    _install_client(monkeypatch, client)
    adapter = _adapter()

    # Exercise the 4 broker seams.
    tokens = SchwabAdapter._to_broker_tokens(
        {"access_token": "a", "refresh_token": "r", "expires_in": 3600}
    )
    assert tokens.access_token == "a"
    assert adapter._account_hash(client) == "HASH123"
    snapshot = adapter.fetch_portfolio()
    assert snapshot.cash == Decimal("1000.0")
    assert snapshot.holdings[0].symbol == "VOO"
    assert adapter._quote_ask(client, "VTI") == Decimal("250.5")

    # No capture files written anywhere (dir is unset).
    assert list(tmp_path.iterdir()) == []


def test_llm_tap_off_no_files_and_behavior_unchanged(monkeypatch, tmp_path):
    monkeypatch.setenv("PREFLIGHT_CAPTURE_DIR", "")
    gateway = _FakeGateway(_on_shape_message())
    resp = gateway.complete(None)
    assert resp.output["action_label"] == "hold"
    assert list(tmp_path.iterdir()) == []


# --- Full harness ON, on-shape -> PASS ---------------------------------------


def _run_full_harness(monkeypatch, tmp_path, *, client=None, message=None):
    monkeypatch.setenv("PREFLIGHT_CAPTURE_DIR", str(tmp_path))
    client = client or _FakeClient()
    _install_client(monkeypatch, client)
    adapter = _adapter()
    gateway = _FakeGateway(message or _on_shape_message())
    from api.config import get_settings

    report = run.run(broker=adapter, gateway=gateway, settings=get_settings())
    return report, client


def test_full_harness_on_shape_pass(monkeypatch, tmp_path):
    report, client = _run_full_harness(monkeypatch, tmp_path)
    assert report.overall == drift.PASS
    # Every declared seam wrote a skeleton file.
    for seam in drift.declared_seams():
        assert (tmp_path / f"{seam}.json").exists()
    # The report file was written and every field is OK.
    for sr in report.seams:
        for r in sr.results:
            assert r.verdict == drift.OK
    # Zero orders placed.
    assert client.placed == []


# --- Renamed / missing / type-drift ------------------------------------------


def test_renamed_field_missing_with_candidate_hint(monkeypatch, tmp_path):
    # cashBalance -> cashBalanceValue (a sibling of the right numeric type).
    account = {
        "securitiesAccount": {
            "currentBalances": {"cashBalanceValue": 1000.0},
            "positions": [
                {
                    "instrument": {"symbol": "VOO"},
                    "longQuantity": 3.0,
                    "marketValue": 1200.0,
                }
            ],
        }
    }
    client = _FakeClient(account=account)
    report, _ = _run_full_harness(monkeypatch, tmp_path, client=client)
    assert report.overall == drift.DRIFT
    portfolio = next(s for s in report.seams if s.seam == drift.SEAM_PORTFOLIO)
    cash = next(r for r in portfolio.results if r.field_label.endswith("cashBalance"))
    assert cash.verdict == drift.RENAMED_CANDIDATE
    assert cash.candidate == "cashBalanceValue"


def test_missing_field_no_sibling(monkeypatch, tmp_path):
    # cashBalance absent, no numeric sibling in currentBalances.
    account = {
        "securitiesAccount": {
            "currentBalances": {"accountType": "CASH"},
            "positions": [
                {
                    "instrument": {"symbol": "VOO"},
                    "longQuantity": 3.0,
                    "marketValue": 1200.0,
                }
            ],
        }
    }
    client = _FakeClient(account=account)
    report, _ = _run_full_harness(monkeypatch, tmp_path, client=client)
    assert report.overall == drift.DRIFT
    portfolio = next(s for s in report.seams if s.seam == drift.SEAM_PORTFOLIO)
    cash = next(r for r in portfolio.results if r.field_label.endswith("cashBalance"))
    assert cash.verdict == drift.MISSING
    assert cash.candidate is None


def test_type_drift_askprice_string(monkeypatch, tmp_path):
    client = _FakeClient(quote={"VTI": {"quote": {"askPrice": "250.5"}}})
    report, _ = _run_full_harness(monkeypatch, tmp_path, client=client)
    assert report.overall == drift.DRIFT
    quote = next(s for s in report.seams if s.seam == drift.SEAM_QUOTE)
    ask = next(r for r in quote.results if r.field_label.endswith("askPrice"))
    assert ask.verdict == drift.TYPE_MISMATCH


# --- Token one-of -------------------------------------------------------------


def test_token_one_of_expires_in_only():
    shape = to_shape(
        {"access_token": "a", "refresh_token": "r", "expires_in": 3600}
    )
    results = drift.compare(drift.SEAM_TOKEN, shape)
    expiry = next(r for r in results if "expires" in r.field_label)
    assert expiry.verdict == drift.OK
    assert drift.overall_verdict(results) == drift.PASS


def test_token_one_of_expires_at_only():
    shape = to_shape(
        {"access_token": "a", "refresh_token": "r", "expires_at": 1700000000}
    )
    results = drift.compare(drift.SEAM_TOKEN, shape)
    expiry = next(r for r in results if "expires" in r.field_label)
    assert expiry.verdict == drift.OK
    assert drift.overall_verdict(results) == drift.PASS


def test_token_one_of_both_absent_is_missing():
    shape = to_shape({"access_token": "a", "refresh_token": "r"})
    results = drift.compare(drift.SEAM_TOKEN, shape)
    expiry = next(r for r in results if "expires" in r.field_label)
    assert expiry.verdict == drift.MISSING
    assert drift.overall_verdict(results) == drift.DRIFT


# --- Zero-order guarantee -----------------------------------------------------


def test_full_harness_places_zero_orders(monkeypatch, tmp_path):
    report, client = _run_full_harness(monkeypatch, tmp_path)
    assert client.placed == []  # spy: zero place_order calls


def test_preflight_package_calls_no_order_mutating_method():
    """The structural zero-order guarantee: the package makes NO call to any
    order-mutating client method nor the approve path.

    Scans for CALL patterns (``.<method>(``) rather than bare substrings, so a
    docstring may name the methods (the guarantee prose does) without tripping the
    check, while a real ``client.place_order(...)`` / ``.cancel_order(`` /
    ``.replace_order(`` / ``.approve(`` call would fail it.
    """
    forbidden_calls = (
        ".place_order(",
        ".cancel_order(",
        ".replace_order(",
        ".approve(",
    )
    pkg_dir = Path(run.__file__).parent
    for path in pkg_dir.glob("*.py"):
        source = path.read_text()
        for call in forbidden_calls:
            assert call not in source, f"{path} calls {call}"


# --- Order-status out-of-scope line ------------------------------------------


def test_order_status_out_of_scope_line_in_report(monkeypatch, tmp_path):
    report, _ = _run_full_harness(monkeypatch, tmp_path)
    assert "_map_order" in report.text
    assert "Story 7.7" in report.text
    assert "NOT confirmed" in report.text


def test_order_status_out_of_scope_line_helper():
    line = drift.order_status_out_of_scope_line()
    assert "_map_order" in line
    assert "Story 7.7" in line


# --- Token-reconstructed caveat ----------------------------------------------


def test_token_reconstructed_caveat_in_report(monkeypatch, tmp_path):
    report, _ = _run_full_harness(monkeypatch, tmp_path)
    assert "reconstructed" in report.text.lower()
    assert "OAuth" in report.text


def test_token_reconstructed_caveat_helper():
    line = drift.token_reconstructed_caveat_line()
    assert "reconstructed" in line.lower()


# --- Missing seam forces non-PASS (INCOMPLETE) -------------------------------


def test_missing_seam_forces_incomplete(monkeypatch, tmp_path):
    # gateway=None -> the two LLM seams are never driven; a declared seam with no
    # capture must be INCOMPLETE and drag the overall off PASS.
    monkeypatch.setenv("PREFLIGHT_CAPTURE_DIR", str(tmp_path))
    client = _FakeClient()
    _install_client(monkeypatch, client)
    adapter = _adapter()
    from api.config import get_settings

    report = run.run(broker=adapter, gateway=None, settings=get_settings())
    assert report.overall == drift.INCOMPLETE
    llm_seams = [
        s
        for s in report.seams
        if s.seam in (drift.SEAM_LLM_MESSAGE, drift.SEAM_LLM_OUTPUT)
    ]
    assert llm_seams and all(s.verdict == drift.INCOMPLETE for s in llm_seams)
    assert "no gateway provided" in report.text
    assert client.placed == []


# --- Per-seam error isolation: one seam raising still yields a report ---------


class _QuoteRaises(_FakeClient):
    def get_quote(self, symbol):
        raise RuntimeError("boom quote")


def test_drive_error_isolation_still_reports(monkeypatch, tmp_path):
    monkeypatch.setenv("PREFLIGHT_CAPTURE_DIR", str(tmp_path))
    client = _QuoteRaises()
    _install_client(monkeypatch, client)
    adapter = _adapter()
    gateway = _FakeGateway(_on_shape_message())
    from api.config import get_settings

    report = run.run(broker=adapter, gateway=gateway, settings=get_settings())
    # The quote seam raised, but the run still captured the other seams and built
    # a report — seams 1-3/5 are not lost because seam 4 failed.
    assert (tmp_path / "account_numbers.json").exists()
    assert (tmp_path / "portfolio.json").exists()
    assert not (tmp_path / "quote.json").exists()
    quote = next(s for s in report.seams if s.seam == drift.SEAM_QUOTE)
    assert quote.verdict == drift.INCOMPLETE
    assert report.overall == drift.INCOMPLETE
    assert "boom quote" in report.text
    assert client.placed == []


# --- Stale captures cleared before a run -------------------------------------


def test_stale_capture_is_cleared(monkeypatch, tmp_path):
    monkeypatch.setenv("PREFLIGHT_CAPTURE_DIR", str(tmp_path))
    # A stale on-shape capture from a "previous run" for a seam we will NOT drive
    # this run (gateway=None). It must be cleared, never folded into the verdict.
    stale = {
        "action_label": "str",
        "reasoning": "str",
        "evidence": {"type": "array", "len": 1, "item": "str"},
        "uncertainties": {"type": "array", "len": 1, "item": "str"},
    }
    (tmp_path / "llm_output.json").write_text(json.dumps(stale))
    client = _FakeClient()
    _install_client(monkeypatch, client)
    adapter = _adapter()
    from api.config import get_settings

    report = run.run(broker=adapter, gateway=None, settings=get_settings())
    assert not (tmp_path / "llm_output.json").exists()
    llm_output = next(s for s in report.seams if s.seam == drift.SEAM_LLM_OUTPUT)
    assert llm_output.verdict == drift.INCOMPLETE
    assert report.overall == drift.INCOMPLETE


# --- Account-numbers + LLM seam drift compare direct -------------------------


def test_account_numbers_seam_on_shape():
    shape = to_shape([{"accountNumber": "123", "hashValue": "HASH"}])
    results = drift.compare(drift.SEAM_ACCOUNT, shape)
    assert drift.overall_verdict(results) == drift.PASS


def test_llm_output_seam_missing_evidence_is_drift():
    # No array-typed sibling for evidence to be confused with -> a true MISSING.
    shape = to_shape(
        {"action_label": "hold", "reasoning": "x", "uncertainties": "oops"}
    )
    results = drift.compare(drift.SEAM_LLM_OUTPUT, shape)
    evidence = next(r for r in results if r.field_label == "evidence")
    assert evidence.verdict == drift.MISSING
    assert drift.overall_verdict(results) == drift.DRIFT


def test_llm_output_seam_renamed_array_field_is_candidate():
    # evidence absent but an array sibling ("cited") of the right type present.
    shape = to_shape(
        {
            "action_label": "hold",
            "reasoning": "x",
            "cited": ["E1"],
            "uncertainties": ["u1"],
        }
    )
    results = drift.compare(drift.SEAM_LLM_OUTPUT, shape)
    evidence = next(r for r in results if r.field_label == "evidence")
    assert evidence.verdict == drift.RENAMED_CANDIDATE
    assert evidence.candidate == "cited"


def test_run_requires_capture_dir(monkeypatch):
    monkeypatch.setenv("PREFLIGHT_CAPTURE_DIR", "")
    from api.config import get_settings

    with pytest.raises(RuntimeError):
        run.run(broker=None, gateway=None, settings=get_settings())


def test_env_only_capture_dir_still_fires_taps(monkeypatch, tmp_path):
    """Config via .env (settings) but NOT in the process env must still capture.

    ``Settings`` can source ``PREFLIGHT_CAPTURE_DIR`` from a ``.env`` file that
    never reaches ``os.environ``, while the adapter taps gate on a cheap
    ``os.environ`` read. ``run()`` must mirror the settings-resolved dir into the
    process env so the taps fire; otherwise the orchestrator drives every seam
    while each tap no-ops → a silent all-INCOMPLETE run with no captures.
    """
    # Simulate ".env only": the field is set on the Settings object, but the
    # process env carries no PREFLIGHT_CAPTURE_DIR.
    monkeypatch.delenv("PREFLIGHT_CAPTURE_DIR", raising=False)
    from api.config import Settings

    settings = Settings(PREFLIGHT_CAPTURE_DIR=str(tmp_path))
    client = _FakeClient()
    _install_client(monkeypatch, client)
    adapter = _adapter()
    gateway = _FakeGateway(_on_shape_message())

    report = run.run(broker=adapter, gateway=gateway, settings=settings)

    # Taps fired: every declared seam captured and the verdict is a real PASS
    # (not the silent all-INCOMPLETE that a no-op tap would produce).
    assert report.overall == drift.PASS
    for seam in drift.declared_seams():
        assert (tmp_path / f"{seam}.json").exists()
    assert client.placed == []
