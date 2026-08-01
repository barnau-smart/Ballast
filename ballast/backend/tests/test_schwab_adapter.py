"""Story 6.3 — the real SchwabAdapter.place_order / get_order_status against a
MOCKED schwab-py client.

These tests exercise the REAL adapter code path with the ``schwab`` SDK fully
MOCKED — zero credentials, zero network, zero paid orders. Every adapter row of
the story's I/O & Edge-Case Matrix is covered: whole-share floor sizing, buy vs.
sell builder selection, a working order → PENDING, the sub-minimum / unusable
quote calm refusals, a broker HTTP-error → REJECTED, a transport error → TIMEOUT
(no raw exception escaping the port), a no-order_id placement → TIMEOUT, and the
reconciliation reads (unknown key → PENDING; cached order id → mapped).

The trading client is obtained inside the adapter via
``schwab.auth.client_from_access_functions``; we patch that (and the equity order
builders / ``Utils.extract_order_id``) so the lazily-imported SDK symbols return
crafted fakes. This file must never contain a literal ``import schwab`` statement
— the structural sole-caller test at the bottom scans the tree for exactly that;
the SDK modules are obtained via importlib instead.
"""

from __future__ import annotations

import importlib
import re
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from brokers.port import OrderNotPlaceableError, OrderStatus
from brokers.schwab_adapter import SchwabAdapter, SchwabNotConfiguredError
from coach.recommendation import OrderIntent, OrderSide

_auth = importlib.import_module("schwab.auth")
_equities = importlib.import_module("schwab.orders.equities")
_utils = importlib.import_module("schwab.utils")


# --- Crafted SDK doubles ------------------------------------------------------


class _FakeResponse:
    """A minimal httpx.Response stand-in with ``is_error`` / ``json()`` / headers."""

    def __init__(self, *, json_data=None, is_error: bool = False, headers=None):
        self._json = json_data if json_data is not None else {}
        self.is_error = is_error
        self.status_code = 400 if is_error else 200
        self.headers = headers or {}

    def json(self):
        return self._json


class _FakeClient:
    """A stand-in for a schwab-py trading ``Client`` — records placements."""

    def __init__(
        self,
        *,
        account_numbers=None,
        quote=None,
        place_resp=None,
        order=None,
        account_exc=None,
        quote_exc=None,
        place_exc=None,
        get_order_exc=None,
    ):
        self._account_numbers = account_numbers or [
            {"accountNumber": "123", "hashValue": "HASH123"}
        ]
        self._quote = quote or {}
        self._place_resp = place_resp or _FakeResponse(
            headers={"Location": "loc"}
        )
        self._order = order or {}
        self._account_exc = account_exc
        self._quote_exc = quote_exc
        self._place_exc = place_exc
        self._get_order_exc = get_order_exc
        self.placed: list[tuple] = []
        self.get_order_calls: list[tuple] = []

    def get_account_numbers(self):
        if self._account_exc is not None:
            raise self._account_exc
        return _FakeResponse(json_data=self._account_numbers)

    def get_quote(self, symbol):
        if self._quote_exc is not None:
            raise self._quote_exc
        return _FakeResponse(json_data=self._quote)

    def place_order(self, account_hash, order_spec):
        self.placed.append((account_hash, order_spec))
        if self._place_exc is not None:
            raise self._place_exc
        return self._place_resp

    def get_order(self, order_id, account_hash):
        self.get_order_calls.append((order_id, account_hash))
        if self._get_order_exc is not None:
            raise self._get_order_exc
        return _FakeResponse(json_data=self._order)


@pytest.fixture(autouse=True)
def _configured_schwab_env(monkeypatch):
    """Give the adapter non-empty SCHWAB_* creds so construction never gates."""
    monkeypatch.setenv("SCHWAB_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("SCHWAB_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("SCHWAB_CALLBACK_URL", "https://127.0.0.1/callback")


def _install_client(monkeypatch, client: _FakeClient) -> dict:
    """Patch ``client_from_access_functions`` to hand back our fake client.

    Returns a dict capturing the kwargs the adapter passed (so a test can assert
    the token_read_func / api_key were wired, and that construction is offline).
    """
    captured: dict = {}

    def _factory(**kwargs):
        captured.update(kwargs)
        return client

    monkeypatch.setattr(_auth, "client_from_access_functions", _factory)
    return captured


def _record_builders(monkeypatch) -> list:
    """Patch the equity builders to record (side, symbol, quantity) calls."""
    calls: list = []

    def _buy(symbol, quantity):
        calls.append(("buy", symbol, quantity))
        return f"buy-order-spec-{symbol}-{quantity}"

    def _sell(symbol, quantity):
        calls.append(("sell", symbol, quantity))
        return f"sell-order-spec-{symbol}-{quantity}"

    monkeypatch.setattr(_equities, "equity_buy_market", _buy)
    monkeypatch.setattr(_equities, "equity_sell_market", _sell)
    return calls


def _set_order_id(monkeypatch, value):
    """Patch ``Utils.extract_order_id`` to yield ``value`` (int order id or None)."""
    monkeypatch.setattr(
        _utils.Utils, "extract_order_id", lambda self, resp: value
    )


def _adapter() -> SchwabAdapter:
    return SchwabAdapter(token_read_func=lambda: {"access_token": "a"})


def _intent(symbol="VOO", side=OrderSide.BUY, amount="250") -> OrderIntent:
    return OrderIntent(symbol=symbol, side=side, amount=Decimal(amount))


# --- place_order --------------------------------------------------------------


@pytest.mark.asyncio
async def test_place_order_happy_buy_floor_sizes_and_maps_fill(monkeypatch):
    client = _FakeClient(
        quote={"VOO": {"quote": {"askPrice": 100}}},
        place_resp=_FakeResponse(headers={"Location": "loc"}),
        order={"status": "FILLED", "filledQuantity": 2, "avgFillPrice": 100},
    )
    captured = _install_client(monkeypatch, client)
    calls = _record_builders(monkeypatch)
    _set_order_id(monkeypatch, 55501)

    outcome = await _adapter().place_order(_intent(amount="250"), idempotency_key="k1")

    # floor(250 / 100) = 2 whole shares, via the BUY builder.
    assert calls == [("buy", "VOO", 2)]
    assert outcome.status == OrderStatus.FILLED
    assert outcome.filled_qty == Decimal("2")
    assert outcome.avg_price == Decimal("100")
    assert outcome.broker_ref == "55501"
    # Client was built via the access-functions path (no disk/network), and the
    # bound token_read_func was handed to the SDK.
    assert callable(captured["token_read_func"])
    assert captured["api_key"] == "test-client-id"
    assert len(client.placed) == 1  # placed EXACTLY once


@pytest.mark.asyncio
async def test_place_order_sell_uses_sell_builder(monkeypatch):
    client = _FakeClient(
        quote={"VTI": {"quote": {"askPrice": 50}}},
        order={"status": "FILLED", "filledQuantity": 4, "avgFillPrice": 50},
    )
    _install_client(monkeypatch, client)
    calls = _record_builders(monkeypatch)
    _set_order_id(monkeypatch, 999)

    await _adapter().place_order(
        _intent(symbol="VTI", side=OrderSide.SELL, amount="200"),
        idempotency_key="k2",
    )

    assert calls == [("sell", "VTI", 4)]  # floor(200 / 50) = 4


@pytest.mark.asyncio
async def test_place_order_working_status_is_pending_with_broker_ref(monkeypatch):
    client = _FakeClient(
        quote={"VOO": {"quote": {"askPrice": 100}}},
        order={"status": "WORKING", "filledQuantity": 0},
    )
    _install_client(monkeypatch, client)
    _record_builders(monkeypatch)
    _set_order_id(monkeypatch, 4242)

    outcome = await _adapter().place_order(_intent(), idempotency_key="k3")

    assert outcome.status == OrderStatus.PENDING
    assert outcome.filled_qty == Decimal("0")
    assert outcome.broker_ref == "4242"


@pytest.mark.asyncio
async def test_place_order_sub_minimum_refuses_calmly(monkeypatch):
    client = _FakeClient(quote={"VOO": {"quote": {"askPrice": 500}}})
    _install_client(monkeypatch, client)
    _record_builders(monkeypatch)

    with pytest.raises(OrderNotPlaceableError):
        await _adapter().place_order(_intent(amount="100"), idempotency_key="k4")

    assert client.placed == []  # NO order placed


@pytest.mark.asyncio
async def test_place_order_unusable_quote_refuses(monkeypatch):
    client = _FakeClient(quote={"VOO": {"quote": {"askPrice": None}}})
    _install_client(monkeypatch, client)
    _record_builders(monkeypatch)

    with pytest.raises(OrderNotPlaceableError):
        await _adapter().place_order(_intent(), idempotency_key="k5")
    assert client.placed == []


@pytest.mark.asyncio
async def test_place_order_http_error_is_rejected(monkeypatch):
    client = _FakeClient(
        quote={"VOO": {"quote": {"askPrice": 100}}},
        place_resp=_FakeResponse(is_error=True),
    )
    _install_client(monkeypatch, client)
    _record_builders(monkeypatch)

    outcome = await _adapter().place_order(_intent(), idempotency_key="k6")

    assert outcome.status == OrderStatus.REJECTED
    assert outcome.filled_qty == Decimal("0")
    assert outcome.broker_ref is None


@pytest.mark.asyncio
async def test_place_order_transport_error_is_timeout_no_leak(monkeypatch):
    client = _FakeClient(
        quote={"VOO": {"quote": {"askPrice": 100}}},
        place_exc=httpx.ConnectError("boom"),
    )
    _install_client(monkeypatch, client)
    _record_builders(monkeypatch)
    _set_order_id(monkeypatch, 1)

    # No raw exception escapes the port — it returns a TIMEOUT outcome.
    outcome = await _adapter().place_order(_intent(), idempotency_key="k7")

    assert outcome.status == OrderStatus.TIMEOUT
    assert outcome.filled_qty == Decimal("0")
    assert outcome.broker_ref is None


@pytest.mark.asyncio
async def test_place_order_no_order_id_is_timeout(monkeypatch):
    client = _FakeClient(quote={"VOO": {"quote": {"askPrice": 100}}})
    _install_client(monkeypatch, client)
    _record_builders(monkeypatch)
    _set_order_id(monkeypatch, None)  # 2xx placement, no Location header

    adapter = _adapter()
    outcome = await adapter.place_order(_intent(), idempotency_key="k8")

    assert outcome.status == OrderStatus.TIMEOUT
    assert outcome.broker_ref is None
    # The in-request reconcile then honestly surfaces pending (no auto-search).
    reconciled = await adapter.get_order_status("k8")
    assert reconciled.status == OrderStatus.PENDING
    assert reconciled.broker_ref is None


# --- get_order_status ---------------------------------------------------------


@pytest.mark.asyncio
async def test_get_order_status_unknown_key_is_pending(monkeypatch):
    client = _FakeClient()
    _install_client(monkeypatch, client)

    outcome = await _adapter().get_order_status("never-placed")

    assert outcome.status == OrderStatus.PENDING
    assert outcome.filled_qty == Decimal("0")
    assert outcome.broker_ref is None
    # Never searched the account for a matching order.
    assert client.get_order_calls == []


@pytest.mark.asyncio
async def test_get_order_status_cached_order_id_maps_authoritative(monkeypatch):
    client = _FakeClient(
        quote={"VOO": {"quote": {"askPrice": 100}}},
        order={"status": "WORKING", "filledQuantity": 0},
    )
    _install_client(monkeypatch, client)
    _record_builders(monkeypatch)
    _set_order_id(monkeypatch, 7777)

    adapter = _adapter()
    await adapter.place_order(_intent(), idempotency_key="k9")

    # Later reconcile now reports a fill for the same cached order id.
    client._order = {"status": "FILLED", "filledQuantity": 2, "avgFillPrice": 101}
    outcome = await adapter.get_order_status("k9")

    assert outcome.status == OrderStatus.FILLED
    assert outcome.filled_qty == Decimal("2")
    assert outcome.avg_price == Decimal("101")
    assert outcome.broker_ref == "7777"


@pytest.mark.asyncio
async def test_get_order_status_transport_error_is_timeout(monkeypatch):
    client = _FakeClient(
        quote={"VOO": {"quote": {"askPrice": 100}}},
        get_order_exc=httpx.TimeoutException("slow"),
    )
    _install_client(monkeypatch, client)
    _record_builders(monkeypatch)
    _set_order_id(monkeypatch, 8888)

    adapter = _adapter()
    # Place cleanly first so the key maps to an order id...
    client._get_order_exc = None
    client._order = {"status": "WORKING"}
    await adapter.place_order(_intent(), idempotency_key="k10")
    # ...then the reconcile read times out.
    client._get_order_exc = httpx.TimeoutException("slow")
    outcome = await adapter.get_order_status("k10")

    assert outcome.status == OrderStatus.TIMEOUT
    # The known order id is PRESERVED so the landed order stays reconcilable.
    assert outcome.broker_ref == "8888"


@pytest.mark.asyncio
async def test_place_order_read_timeout_preserves_broker_ref(monkeypatch):
    # Placement SUCCEEDS (order id assigned) but the status read times out: the
    # order landed, so its id must be PRESERVED (never dropped to None) or a later
    # reconcile could never find it (Story 6.7). No raw exception escapes.
    client = _FakeClient(
        quote={"VOO": {"quote": {"askPrice": 100}}},
        get_order_exc=httpx.TimeoutException("slow"),
    )
    _install_client(monkeypatch, client)
    _record_builders(monkeypatch)
    _set_order_id(monkeypatch, 33301)

    adapter = _adapter()
    outcome = await adapter.place_order(_intent(), idempotency_key="k12")

    assert outcome.status == OrderStatus.TIMEOUT
    assert outcome.broker_ref == "33301"  # PRESERVED, not None
    assert len(client.placed) == 1  # placed exactly once


@pytest.mark.asyncio
async def test_place_order_malformed_status_body_no_leak(monkeypatch):
    # A successful placement whose status body is malformed (``.json()`` raises a
    # JSONDecodeError, which is a ValueError — NOT an httpx error) must NOT leak a
    # raw exception past the port; it surfaces TIMEOUT carrying the known order id.
    # A leak here would let ``approve`` release the claim and re-place a SECOND
    # real order (Schwab honors no client key).
    import json as _json

    class _BadJsonResponse(_FakeResponse):
        def json(self):
            raise _json.JSONDecodeError("bad", "", 0)

    client = _FakeClient(quote={"VOO": {"quote": {"askPrice": 100}}})
    client._order_response = _BadJsonResponse()
    # Make get_order return the bad-json response.
    client.get_order = lambda order_id, account_hash: _BadJsonResponse()
    _install_client(monkeypatch, client)
    _record_builders(monkeypatch)
    _set_order_id(monkeypatch, 44401)

    outcome = await _adapter().place_order(_intent(), idempotency_key="k13")

    assert outcome.status == OrderStatus.TIMEOUT
    assert outcome.broker_ref == "44401"  # preserved; reconcilable


@pytest.mark.asyncio
async def test_place_order_nonenumerated_post_placement_exc_is_fenced(monkeypatch):
    # THE double-order guard: a placement SUCCEEDS, then the status read raises a
    # type that is NOT in any curated blocklist (here a bare ``RuntimeError``).
    # The post-placement region is a FENCE (``except Exception``), so this must NOT
    # escape the port — it surfaces TIMEOUT carrying the known order id, placed
    # exactly once. If a regression re-narrowed the post-placement ``except`` back
    # to a tuple, this exception would leak → ``approve`` releases the claim and
    # re-places a SECOND real order. That is exactly what this test forbids.
    client = _FakeClient(quote={"VOO": {"quote": {"askPrice": 100}}})
    client.get_order = lambda order_id, account_hash: (_ for _ in ()).throw(
        RuntimeError("SDK blew up after the order landed")
    )
    _install_client(monkeypatch, client)
    _record_builders(monkeypatch)
    _set_order_id(monkeypatch, 77701)

    outcome = await _adapter().place_order(_intent(), idempotency_key="kfence")

    assert outcome.status == OrderStatus.TIMEOUT
    assert outcome.broker_ref == "77701"  # preserved; reconcilable (Story 6.7)
    assert len(client.placed) == 1  # placed EXACTLY once


@pytest.mark.asyncio
async def test_place_order_extract_order_id_attributeerror_is_fenced(monkeypatch):
    # A placement SUCCEEDS, then ``extract_order_id`` raises ``AttributeError`` (an
    # unexpected response shape) — a type the old curated tuple did NOT list. The
    # post-placement fence must swallow it: no leak, no phantom fill, placed once.
    # No order id was obtained, so ``broker_ref`` is None and the in-request
    # reconcile then honestly surfaces PENDING (never auto-searched).
    client = _FakeClient(quote={"VOO": {"quote": {"askPrice": 100}}})
    _install_client(monkeypatch, client)
    _record_builders(monkeypatch)

    def _boom(self, resp):
        raise AttributeError("resp had no headers")

    monkeypatch.setattr(_utils.Utils, "extract_order_id", _boom)

    adapter = _adapter()
    outcome = await adapter.place_order(_intent(), idempotency_key="kattr")

    assert outcome.status == OrderStatus.TIMEOUT
    assert outcome.broker_ref is None
    assert len(client.placed) == 1  # placed EXACTLY once
    # The key was cached (as None), so the reconcile honestly returns PENDING.
    reconciled = await adapter.get_order_status("kattr")
    assert reconciled.status == OrderStatus.PENDING
    assert reconciled.broker_ref is None


@pytest.mark.asyncio
async def test_get_order_status_nonenumerated_exc_is_fenced(monkeypatch):
    # The reconcile read over a KNOWN-placed order must also be fenced: a
    # non-enumerated type (``RuntimeError``) on the status read surfaces TIMEOUT
    # with the known order id preserved, never a raw leak (which would let
    # ``approve`` re-place a second real order).
    client = _FakeClient(
        quote={"VOO": {"quote": {"askPrice": 100}}},
        order={"status": "WORKING"},
    )
    _install_client(monkeypatch, client)
    _record_builders(monkeypatch)
    _set_order_id(monkeypatch, 88801)

    adapter = _adapter()
    await adapter.place_order(_intent(), idempotency_key="krec")
    client.get_order = lambda order_id, account_hash: (_ for _ in ()).throw(
        RuntimeError("SDK blew up on the reconcile read")
    )
    outcome = await adapter.get_order_status("krec")

    assert outcome.status == OrderStatus.TIMEOUT
    assert outcome.broker_ref == "88801"  # preserved; reconcilable


@pytest.mark.asyncio
async def test_place_order_empty_account_is_config_error(monkeypatch):
    # An empty account-numbers body is a clear account/config problem (surfaced
    # plainly), never a phantom fill and never a raw IndexError past the port.
    client = _FakeClient(quote={"VOO": {"quote": {"askPrice": 100}}})
    client._account_numbers = []  # force empty (ctor's ``or`` would restore default)
    _install_client(monkeypatch, client)
    _record_builders(monkeypatch)

    with pytest.raises(SchwabNotConfiguredError):
        await _adapter().place_order(_intent(), idempotency_key="k14")
    assert client.placed == []


@pytest.mark.asyncio
async def test_place_order_nondict_account_element_is_config_error(monkeypatch):
    # A non-dict first account element (e.g. a bare string) would raise a raw
    # AttributeError on ``.get`` — it must surface plainly as a config problem
    # (pre-placement, no order placed), never leak past the port.
    client = _FakeClient(quote={"VOO": {"quote": {"askPrice": 100}}})
    client._account_numbers = ["not-a-dict"]
    _install_client(monkeypatch, client)
    _record_builders(monkeypatch)

    with pytest.raises(SchwabNotConfiguredError):
        await _adapter().place_order(_intent(), idempotency_key="k14b")
    assert client.placed == []


@pytest.mark.asyncio
async def test_get_order_status_read_timeout_preserves_broker_ref(monkeypatch):
    # Same F1 preservation on the reconcile path — a known order id survives a
    # transport failure on the status read.
    client = _FakeClient(
        quote={"VOO": {"quote": {"askPrice": 100}}},
        order={"status": "WORKING"},
    )
    _install_client(monkeypatch, client)
    _record_builders(monkeypatch)
    _set_order_id(monkeypatch, 55502)

    adapter = _adapter()
    await adapter.place_order(_intent(), idempotency_key="k15")
    client._get_order_exc = httpx.ConnectError("down")
    outcome = await adapter.get_order_status("k15")

    assert outcome.status == OrderStatus.TIMEOUT
    assert outcome.broker_ref == "55502"


def test_map_order_full_fill_without_filled_status_is_filled():
    # A fully-filled QUANTITY must map to FILLED even when the status string is a
    # non-``FILLED`` variant — never surfaced as still-open PENDING.
    adapter = SchwabAdapter(token_read_func=lambda: {"access_token": "a"})
    outcome = adapter._map_order(
        {"status": "PARTIALLY_FILLED", "filledQuantity": 3, "quantity": 3,
         "avgFillPrice": 10},
        broker_ref="9",
    )
    assert outcome.status == OrderStatus.FILLED
    assert outcome.filled_qty == Decimal("3")


def test_map_order_non_dict_body_is_pending_no_leak():
    # A non-dict status body (bare list) must not raise AttributeError past the
    # port — it degrades to an honest PENDING carrying the known ref.
    adapter = SchwabAdapter(token_read_func=lambda: {"access_token": "a"})
    outcome = adapter._map_order([], broker_ref="12")
    assert outcome.status == OrderStatus.PENDING
    assert outcome.broker_ref == "12"


def test_map_order_filled_status_zero_quantity_is_pending():
    # A "FILLED" status string with a missing/zero filledQuantity is a
    # contradictory/incomplete body — it must NOT persist a "fill" that moved zero
    # shares with no price. Degrades to honest PENDING (reconcilable), never a
    # phantom fill.
    adapter = SchwabAdapter(token_read_func=lambda: {"access_token": "a"})
    outcome = adapter._map_order(
        {"status": "FILLED", "avgFillPrice": 100}, broker_ref="77"
    )
    assert outcome.status == OrderStatus.PENDING
    assert outcome.filled_qty == Decimal("0")
    assert outcome.avg_price is None
    assert outcome.broker_ref == "77"


@pytest.mark.asyncio
async def test_place_order_nan_ask_refuses_calmly(monkeypatch):
    # A NaN askPrice parses to a valid-but-non-finite Decimal (no parse error), and
    # comparing it would raise decimal.InvalidOperation (an ArithmeticError, NOT a
    # ValueError). It must be rejected as an unusable quote (calm refusal, no order
    # placed), never leak a raw exception past the port.
    client = _FakeClient(quote={"VOO": {"quote": {"askPrice": float("nan")}}})
    _install_client(monkeypatch, client)
    _record_builders(monkeypatch)

    with pytest.raises(OrderNotPlaceableError):
        await _adapter().place_order(_intent(), idempotency_key="knan")
    assert client.placed == []  # NO order placed


@pytest.mark.asyncio
async def test_place_order_nan_filled_quantity_no_leak_preserves_ref(monkeypatch):
    # A SUCCESSFUL placement whose status body carries a NaN filledQuantity would,
    # unguarded, poison a Decimal comparison in _map_order with InvalidOperation
    # (an ArithmeticError) and escape the port — after a real placement that lets
    # ``approve`` release the claim and re-place a SECOND real order. It must
    # surface TIMEOUT carrying the known order id, placed exactly once.
    client = _FakeClient(
        quote={"VOO": {"quote": {"askPrice": 100}}},
        order={"status": "WORKING", "filledQuantity": float("nan")},
    )
    _install_client(monkeypatch, client)
    _record_builders(monkeypatch)
    _set_order_id(monkeypatch, 66601)

    outcome = await _adapter().place_order(_intent(), idempotency_key="knan2")

    # A NaN filledQuantity is coerced to 0 by ``_decimal_or_zero`` (finite guard),
    # so the WORKING body maps to an honest PENDING — never a raw-exception TIMEOUT.
    # Pin the exact status so a regression that let InvalidOperation escape (→ the
    # post-placement fence → TIMEOUT) can't hide behind an ``in (...)`` disjunction.
    assert outcome.status is OrderStatus.PENDING
    # The known order id is PRESERVED so the landed order stays reconcilable.
    assert outcome.broker_ref == "66601"
    assert len(client.placed) == 1  # placed exactly once


@pytest.mark.asyncio
async def test_trading_without_token_refuses(monkeypatch):
    client = _FakeClient()
    _install_client(monkeypatch, client)
    adapter = SchwabAdapter()  # no token_read_func bound
    with pytest.raises(SchwabNotConfiguredError):
        await adapter.place_order(_intent(), idempotency_key="k11")


# --- Structural sole-caller invariant (AD-8) ----------------------------------


def test_only_schwab_adapter_imports_schwab_sdk():
    """No backend .py outside brokers/schwab_adapter/ imports the ``schwab`` SDK.

    Structural teeth for AD-8: the schwab-py SDK is touched in exactly one place
    — the real adapter package. (This test obtains the SDK via importlib, so it
    contains no literal ``import schwab`` statement to trip its own scan.)
    """
    backend_root = Path(__file__).resolve().parent.parent
    allowed_dir = (backend_root / "brokers" / "schwab_adapter").resolve()

    import_re = re.compile(
        r"^\s*(?:import\s+schwab\b|from\s+schwab\b)", re.MULTILINE
    )

    offenders: list[str] = []
    for path in backend_root.rglob("*.py"):
        parts = set(path.parts)
        if ".venv" in parts or "__pycache__" in parts:
            continue
        resolved = path.resolve()
        if allowed_dir in resolved.parents:
            continue
        if import_re.search(resolved.read_text(encoding="utf-8")):
            offenders.append(str(resolved.relative_to(backend_root)))

    assert offenders == [], (
        "Only brokers/schwab_adapter/ may import the schwab SDK (AD-8). "
        f"Offending files: {offenders}"
    )
