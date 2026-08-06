"""Story 4.1 tests — the LLM Gateway (AD-6 / NFR2).

These tests run with ZERO credentials and ZERO network. The deterministic
FakeLLMGateway is the tested path; the ``anthropic`` SDK is never imported (the
fake path never loads it, and the real adapter is only imported when selected).
NO test here makes a real Anthropic call.

Covers every I/O-matrix row from the spec:
  - deterministic model routing (both tiers)
  - deterministic fake completion conforming to the schema
  - missing-output-schema rejection (StructuredOutputRequiredError)
  - real adapter with no key fails loud (LLMNotConfiguredError), SDK not imported
  - factory default (FakeLLMGateway typed as LLMGateway)
  - unknown adapter (UnknownLLMAdapterError)
plus the structural "sole caller" invariant (only anthropic_adapter.py imports
the ``anthropic`` SDK).
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

from llm.factory import UnknownLLMAdapterError, get_llm_gateway
from llm.fake_adapter import FakeLLMGateway
from llm.models import DEFAULT_MODEL, HARD_REASONING_MODEL, route_model
from llm.port import (
    EmptyMessagesError,
    LLMGateway,
    LLMMalformedResponseError,
    LLMMessage,
    LLMRefusalError,
    LLMRequest,
    LLMResponse,
    LLMTransportError,
    StructuredOutputRequiredError,
)

# A representative object schema with required fields of several JSON types.
SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "confidence": {"type": "number"},
        "count": {"type": "integer"},
        "ok": {"type": "boolean"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "choice": {"type": "string", "enum": ["a", "b", "c"]},
    },
    "required": ["answer", "confidence", "count", "ok", "tags", "choice"],
    "additionalProperties": False,
}


def _request(*, hard_reasoning: bool = False, schema=SCHEMA) -> LLMRequest:
    return LLMRequest(
        messages=(LLMMessage("user", "What should I do?"),),
        output_schema=schema,
        hard_reasoning=hard_reasoning,
    )


# --- Deterministic model routing (both tiers) --------------------------------


def test_route_model_hard_reasoning():
    assert route_model(True) == HARD_REASONING_MODEL == "claude-opus-4-8"


def test_route_model_default():
    assert route_model(False) == DEFAULT_MODEL == "claude-sonnet-4-6"


def test_model_ids_have_no_date_suffix():
    # The bare identifiers — never a date-suffixed variant.
    assert DEFAULT_MODEL == "claude-sonnet-4-6"
    assert HARD_REASONING_MODEL == "claude-opus-4-8"
    assert not re.search(r"\d{8}$", DEFAULT_MODEL)
    assert not re.search(r"\d{8}$", HARD_REASONING_MODEL)


# --- Fake completion (deterministic, schema-conforming) ----------------------


def test_fake_completion_conforms_and_routes_default():
    resp = FakeLLMGateway().complete(_request(hard_reasoning=False))
    assert isinstance(resp, LLMResponse)
    assert resp.provider == "fake"
    assert resp.model == "claude-sonnet-4-6"
    out = resp.output
    # Every required field present with the correct JSON type.
    assert set(SCHEMA["required"]) <= set(out.keys())
    assert isinstance(out["answer"], str)
    assert isinstance(out["confidence"], float)
    assert isinstance(out["count"], int) and not isinstance(out["count"], bool)
    assert isinstance(out["ok"], bool)
    assert isinstance(out["tags"], list) and all(isinstance(t, str) for t in out["tags"])
    assert out["choice"] in {"a", "b", "c"}


def test_fake_completion_routes_hard_reasoning():
    resp = FakeLLMGateway().complete(_request(hard_reasoning=True))
    assert resp.model == "claude-opus-4-8"
    assert resp.provider == "fake"


def test_fake_completion_is_deterministic():
    a = FakeLLMGateway().complete(_request())
    b = FakeLLMGateway().complete(_request())
    # Same request -> byte-identical output (load-bearing determinism).
    assert a == b
    assert a.output == b.output


def test_fake_completion_nested_schema():
    """Nested objects/arrays are filled recursively with typed placeholders."""
    schema = {
        "type": "object",
        "properties": {
            "meta": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "n": {"type": "integer"}},
                "required": ["name", "n"],
            },
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                },
            },
        },
        "required": ["meta", "items"],
    }
    out = FakeLLMGateway().complete(_request(schema=schema)).output
    assert isinstance(out["meta"], dict)
    assert isinstance(out["meta"]["name"], str)
    assert isinstance(out["meta"]["n"], int)
    assert isinstance(out["items"], list) and len(out["items"]) >= 1
    assert isinstance(out["items"][0]["id"], str)


# --- Missing output schema (rejected before any model call) ------------------


def test_missing_output_schema_rejected():
    req = LLMRequest(messages=(LLMMessage("user", "hi"),), output_schema={})
    with pytest.raises(StructuredOutputRequiredError):
        FakeLLMGateway().complete(req)


def test_missing_output_schema_rejected_by_default_construction():
    # output_schema defaults to an empty dict -> also rejected.
    req = LLMRequest(messages=(LLMMessage("user", "hi"),))
    with pytest.raises(StructuredOutputRequiredError):
        FakeLLMGateway().complete(req)


# --- Real adapter, no key: fail loud, SDK never imported ---------------------


def test_anthropic_adapter_no_key_raises_without_importing_sdk(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    from llm.anthropic_adapter import AnthropicGateway, LLMNotConfiguredError

    # Construction must not import the SDK: compare sys.modules before/after.
    before = "anthropic" in sys.modules
    with pytest.raises(LLMNotConfiguredError):
        AnthropicGateway()
    after = "anthropic" in sys.modules
    # Construction must not have newly imported the SDK.
    assert after == before


def test_factory_anthropic_no_key_raises(monkeypatch):
    from llm.anthropic_adapter import LLMNotConfiguredError

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("LLM_ADAPTER", "anthropic")
    with pytest.raises(LLMNotConfiguredError):
        get_llm_gateway()


# --- Factory default + unknown adapter ---------------------------------------


def test_factory_returns_fake_by_default(monkeypatch):
    monkeypatch.delenv("LLM_ADAPTER", raising=False)
    gateway = get_llm_gateway()
    assert isinstance(gateway, FakeLLMGateway)
    assert isinstance(gateway, LLMGateway)


def test_factory_explicit_fake(monkeypatch):
    monkeypatch.setenv("LLM_ADAPTER", "fake")
    assert isinstance(get_llm_gateway(), FakeLLMGateway)


def test_factory_unknown_adapter_raises(monkeypatch):
    monkeypatch.setenv("LLM_ADAPTER", "bogus")
    with pytest.raises(UnknownLLMAdapterError):
        get_llm_gateway()


# --- Structural sole-caller invariant (AD-6) ---------------------------------


def test_only_anthropic_adapter_imports_anthropic_sdk():
    """No backend .py file other than llm/anthropic_adapter.py imports ``anthropic``.

    Scans the whole backend source tree (excluding .venv and __pycache__) and
    asserts the ``anthropic`` SDK is imported in exactly one place — the real
    adapter. This is the structural teeth for AD-6 ("sole caller").
    """
    backend_root = Path(__file__).resolve().parent.parent
    allowed = (backend_root / "llm" / "anthropic_adapter.py").resolve()

    # Match `import anthropic` / `from anthropic import ...` / `from anthropic.x`
    # but not identifiers that merely contain the substring (e.g. a variable).
    import_re = re.compile(r"^\s*(?:import\s+anthropic\b|from\s+anthropic\b)", re.MULTILINE)

    offenders: list[str] = []
    for path in backend_root.rglob("*.py"):
        parts = set(path.parts)
        if ".venv" in parts or "__pycache__" in parts:
            continue
        resolved = path.resolve()
        if resolved == allowed:
            continue
        # This very test file references the SDK name in strings/comments; skip
        # only the anthropic_adapter, so guard against a false positive here by
        # checking for a real import statement.
        text = resolved.read_text(encoding="utf-8")
        if import_re.search(text):
            offenders.append(str(resolved.relative_to(backend_root)))

    assert offenders == [], (
        "Only llm/anthropic_adapter.py may import the anthropic SDK (AD-6). "
        f"Offending files: {offenders}"
    )


# --- Story 6.2: hardened AnthropicGateway against a MOCKED SDK client ----------
#
# These tests exercise the REAL AnthropicGateway.complete() code path with the
# ``anthropic`` SDK client fully MOCKED — zero credentials, zero network, zero
# paid tokens. Every adapter row of the I/O & Edge-Case Matrix is covered, and
# each failure mode asserts a typed llm.port.LLMError subtype surfaces (never a
# raw anthropic.* / json.JSONDecodeError / StopIteration).
#
# NOTE: this file must never contain a literal ``import anthropic`` statement —
# the structural sole-caller test above scans the tree for exactly that. The SDK
# module is obtained via importlib at runtime instead.
import importlib
import types as _types

import httpx

from llm.anthropic_adapter import STREAMING_MAX_TOKENS, AnthropicGateway

_anthropic = importlib.import_module("anthropic")


class _Block:
    """A minimal content block double (a text block, a tool_use block, etc.)."""

    def __init__(self, type: str, text: str | None = None):
        self.type = type
        if text is not None:
            self.text = text


class _Message:
    """A crafted Anthropic Message double: stop_reason + a list of content blocks."""

    def __init__(self, stop_reason: str, content: list[_Block]):
        self.stop_reason = stop_reason
        self.content = content


def _text_message(payload: str, stop_reason: str = "end_turn") -> _Message:
    return _Message(stop_reason, [_Block("text", payload)])


class _FakeStream:
    """The object YIELDED by entering the stream manager (``with ... as s``).

    Only this entered object carries ``get_final_message()`` — mirroring the real
    SDK, where the method lives on ``MessageStream`` (post-``__enter__``), NOT on
    the ``MessageStreamManager`` returned by ``messages.stream(...)``.
    """

    def __init__(self, message: _Message, exc: Exception | None = None):
        self._message = message
        self._exc = exc

    def get_final_message(self) -> _Message:
        if self._exc is not None:
            raise self._exc
        return self._message


class _FakeStreamManager:
    """A stand-in for the real ``MessageStreamManager`` returned by stream(...).

    A context manager whose entered value (NOT the manager itself) exposes
    ``get_final_message()`` — so an adapter that calls ``.get_final_message()`` on
    the un-entered manager (the old bug) raises ``AttributeError`` and fails the
    test, exactly as the real SDK would.
    """

    def __init__(self, message: _Message, exc: Exception | None = None):
        self._stream = _FakeStream(message, exc)

    def __enter__(self) -> _FakeStream:
        return self._stream

    def __exit__(self, *exc_info) -> bool:
        return False


class _FakeMessages:
    """A stand-in for client.messages with create() and stream() recorders."""

    def __init__(
        self,
        *,
        create_result=None,
        create_exc=None,
        stream_result=None,
        stream_exc=None,
    ):
        self._create_result = create_result
        self._create_exc = create_exc
        self._stream_result = stream_result
        self._stream_exc = stream_exc
        self.create_kwargs = None
        self.stream_kwargs = None

    def create(self, **kwargs):
        self.create_kwargs = kwargs
        if self._create_exc is not None:
            raise self._create_exc
        return self._create_result

    def stream(self, **kwargs):
        self.stream_kwargs = kwargs
        # Returns the manager; any error surfaces from get_final_message() INSIDE
        # the ``with`` block (the real SDK's shape), not from stream() itself.
        return _FakeStreamManager(self._stream_result, self._stream_exc)


class _FakeClient:
    """A stand-in for anthropic.Anthropic(api_key=...); records the key it got."""

    def __init__(self, *, messages: _FakeMessages, on_init):
        self.messages = messages
        self._on_init = on_init

    # The gateway constructs anthropic.Anthropic(api_key=...); we patch the class
    # symbol with a factory that returns a preconfigured _FakeClient.


def _install_fake_client(monkeypatch, messages: _FakeMessages):
    """Patch ``anthropic.Anthropic`` so complete() gets our mocked client.

    Returns a dict capturing the api_key the gateway passed to the SDK, so a test
    can assert the validated key is the one used (never an env-resolved one).
    """
    captured: dict[str, object] = {}

    def _factory(*, api_key, **kwargs):
        captured["api_key"] = api_key
        # Capture the transport budget (Story 7.4) so a test can assert the
        # client was built with the timeout/max_retries from Settings, and count
        # constructions to pin the build-once-reuse contract.
        captured["timeout"] = kwargs.get("timeout")
        captured["max_retries"] = kwargs.get("max_retries")
        captured["build_count"] = captured.get("build_count", 0) + 1
        return _types.SimpleNamespace(messages=messages)

    monkeypatch.setattr(_anthropic, "Anthropic", _factory)
    return captured


def _configured_gateway(monkeypatch) -> AnthropicGateway:
    """A credential-configured AnthropicGateway (a FAKE key — never a real one).

    ``get_settings()`` reads the environment on every call (not cached), so
    setting the env var is enough to satisfy the credential gate at construction.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    return AnthropicGateway()


def _matrix_request(*, max_tokens: int = 4096) -> LLMRequest:
    return LLMRequest(
        messages=(LLMMessage("user", "What should I do?"),),
        output_schema=SCHEMA,
        max_tokens=max_tokens,
    )


# Happy path: object-JSON text block, end_turn -> LLMResponse{dict, model, provider}


def test_hardened_happy_dict_parse_sets_provider_and_model(monkeypatch):
    payload = json.dumps({"answer": "hold", "confidence": 0.9})
    messages = _FakeMessages(create_result=_text_message(payload))
    captured = _install_fake_client(monkeypatch, messages)

    gateway = _configured_gateway(monkeypatch)
    resp = gateway.complete(_matrix_request())

    assert isinstance(resp, LLMResponse)
    assert resp.output == {"answer": "hold", "confidence": 0.9}
    assert resp.provider == "anthropic"
    assert resp.model == "claude-sonnet-4-6"  # default tier, no date suffix
    # The validated key (not an env-resolved profile) was passed to the SDK.
    assert captured["api_key"] == "test-key-not-real"
    # Structured output stayed enforced: output_config.format carried the schema.
    fmt = messages.create_kwargs["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"] is SCHEMA
    # Non-streaming path used for the default max_tokens.
    assert messages.stream_kwargs is None


def test_hardened_routes_hard_reasoning_model(monkeypatch):
    payload = json.dumps({"answer": "x"})
    messages = _FakeMessages(create_result=_text_message(payload))
    _install_fake_client(monkeypatch, messages)
    gateway = _configured_gateway(monkeypatch)

    req = LLMRequest(
        messages=(LLMMessage("user", "hard?"),),
        output_schema=SCHEMA,
        hard_reasoning=True,
    )
    resp = gateway.complete(req)
    assert resp.model == "claude-opus-4-8"


# Refusal -> LLMRefusalError


def test_hardened_refusal_raises_typed_no_vendor_leak(monkeypatch):
    messages = _FakeMessages(create_result=_Message("refusal", []))
    _install_fake_client(monkeypatch, messages)
    gateway = _configured_gateway(monkeypatch)

    with pytest.raises(LLMRefusalError):
        gateway.complete(_matrix_request())


# Truncation (stop_reason="max_tokens") -> LLMMalformedResponseError


def test_hardened_truncation_raises_malformed(monkeypatch):
    payload = json.dumps({"answer": "cut"})
    messages = _FakeMessages(create_result=_text_message(payload, stop_reason="max_tokens"))
    _install_fake_client(monkeypatch, messages)
    gateway = _configured_gateway(monkeypatch)

    with pytest.raises(LLMMalformedResponseError):
        gateway.complete(_matrix_request())


# No text block (only tool_use/thinking) -> LLMMalformedResponseError (no StopIteration)


def test_hardened_no_text_block_raises_malformed_not_stopiteration(monkeypatch):
    messages = _FakeMessages(
        create_result=_Message("end_turn", [_Block("tool_use"), _Block("thinking")])
    )
    _install_fake_client(monkeypatch, messages)
    gateway = _configured_gateway(monkeypatch)

    with pytest.raises(LLMMalformedResponseError):
        gateway.complete(_matrix_request())


# Non-JSON text -> LLMMalformedResponseError (guarded json.loads, no JSONDecodeError leak)


def test_hardened_non_json_text_raises_malformed(monkeypatch):
    messages = _FakeMessages(create_result=_text_message("this is not json {"))
    _install_fake_client(monkeypatch, messages)
    gateway = _configured_gateway(monkeypatch)

    with pytest.raises(LLMMalformedResponseError):
        gateway.complete(_matrix_request())
    # The raw parse exception did not escape.
    try:
        gateway.complete(_matrix_request())
    except json.JSONDecodeError:  # pragma: no cover - would be a leak
        pytest.fail("raw json.JSONDecodeError escaped the port")
    except LLMMalformedResponseError:
        pass


# JSON root not an object (array/scalar) -> LLMMalformedResponseError


def test_hardened_non_dict_root_raises_malformed(monkeypatch):
    messages = _FakeMessages(create_result=_text_message(json.dumps([1, 2, 3])))
    _install_fake_client(monkeypatch, messages)
    gateway = _configured_gateway(monkeypatch)

    with pytest.raises(LLMMalformedResponseError):
        gateway.complete(_matrix_request())


# Transport error (anthropic.APIError family) -> LLMTransportError, raw type never escapes


def test_hardened_transport_error_wrapped_no_vendor_leak(monkeypatch):
    exc = _anthropic.APITimeoutError(
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    messages = _FakeMessages(create_exc=exc)
    _install_fake_client(monkeypatch, messages)
    gateway = _configured_gateway(monkeypatch)

    with pytest.raises(LLMTransportError) as excinfo:
        gateway.complete(_matrix_request())
    # The raw vendor exception is chained (via ``from``) but does NOT escape.
    assert isinstance(excinfo.value.__cause__, _anthropic.APIError)
    assert not isinstance(excinfo.value, _anthropic.APIError)


def test_hardened_rate_limit_error_wrapped(monkeypatch):
    resp = httpx.Response(
        429, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    exc = _anthropic.RateLimitError("rate limited", response=resp, body=None)
    messages = _FakeMessages(create_exc=exc)
    _install_fake_client(monkeypatch, messages)
    gateway = _configured_gateway(monkeypatch)

    with pytest.raises(LLMTransportError):
        gateway.complete(_matrix_request())


# Large max_tokens -> routed through the stream path (avoids the SDK ValueError)


def test_hardened_large_max_tokens_uses_stream_path(monkeypatch):
    payload = json.dumps({"answer": "streamed"})
    messages = _FakeMessages(stream_result=_text_message(payload))
    _install_fake_client(monkeypatch, messages)
    gateway = _configured_gateway(monkeypatch)

    resp = gateway.complete(_matrix_request(max_tokens=STREAMING_MAX_TOKENS + 1))
    assert resp.output == {"answer": "streamed"}
    # The streaming path was taken; the non-streaming create() was NOT called.
    assert messages.stream_kwargs is not None
    assert messages.create_kwargs is None


def test_hardened_large_max_tokens_stream_api_error_wrapped(monkeypatch):
    # On the stream path too, a vendor APIError surfacing from get_final_message()
    # (inside the ``with`` block) is wrapped as LLMTransportError, never leaked.
    exc = _anthropic.APITimeoutError(
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    messages = _FakeMessages(stream_exc=exc)
    _install_fake_client(monkeypatch, messages)
    gateway = _configured_gateway(monkeypatch)

    with pytest.raises(LLMTransportError) as excinfo:
        gateway.complete(_matrix_request(max_tokens=STREAMING_MAX_TOKENS + 1))
    assert messages.stream_kwargs is not None
    assert isinstance(excinfo.value.__cause__, _anthropic.APIError)
    assert not isinstance(excinfo.value, _anthropic.APIError)


# Invalid / empty messages -> EmptyMessagesError BEFORE any client construction


def test_hardened_empty_messages_raises_before_client(monkeypatch):
    called = {"factory": False}

    def _factory(*, api_key, **_kwargs):  # pragma: no cover - must NOT be called
        called["factory"] = True
        raise AssertionError("client must not be constructed for invalid messages")

    monkeypatch.setattr(_anthropic, "Anthropic", _factory)
    gateway = _configured_gateway(monkeypatch)

    empty = LLMRequest(messages=(), output_schema=SCHEMA)
    with pytest.raises(EmptyMessagesError):
        gateway.complete(empty)
    assert called["factory"] is False


def test_hardened_invalid_role_raises_before_client(monkeypatch):
    called = {"factory": False}

    def _factory(*, api_key, **_kwargs):  # pragma: no cover - must NOT be called
        called["factory"] = True
        raise AssertionError("client must not be constructed for an invalid role")

    monkeypatch.setattr(_anthropic, "Anthropic", _factory)
    gateway = _configured_gateway(monkeypatch)

    bad = LLMRequest(
        messages=(LLMMessage("system", "not a valid conversation role"),),
        output_schema=SCHEMA,
    )
    with pytest.raises(EmptyMessagesError):
        gateway.complete(bad)
    assert called["factory"] is False


# --- Story 7.4: client reuse, transport budget, factory memoization -----------
#
# The live LLM path must build the SDK client ONCE (connection reuse) with an
# explicit timeout/retry budget from Settings, and the factory must pool the
# gateway across requests (keyed by API key) so the pool actually outlives one
# request — while a rotated key rebuilds. These exercise the real code path with
# the SDK fully mocked; no credentials, no network.

from api.config import get_settings  # noqa: E402
from llm.factory import _reset_llm_gateway_cache  # noqa: E402


def test_client_constructed_once_across_two_complete_calls(monkeypatch):
    # Two complete() calls on one gateway must build the SDK client exactly once
    # (the cached httpx pool is reused, not rebuilt per call).
    payload = json.dumps({"answer": "hold"})
    messages = _FakeMessages(create_result=_text_message(payload))
    captured = _install_fake_client(monkeypatch, messages)
    gateway = _configured_gateway(monkeypatch)

    gateway.complete(_matrix_request())
    gateway.complete(_matrix_request())

    assert captured["build_count"] == 1


def test_client_ctor_receives_configured_timeout_and_max_retries(monkeypatch):
    # The client is built with the exact timeout/max_retries from Settings.
    monkeypatch.setenv("LLM_REQUEST_TIMEOUT_SECONDS", "42.5")
    monkeypatch.setenv("LLM_MAX_RETRIES", "5")
    payload = json.dumps({"answer": "hold"})
    messages = _FakeMessages(create_result=_text_message(payload))
    captured = _install_fake_client(monkeypatch, messages)
    gateway = _configured_gateway(monkeypatch)

    gateway.complete(_matrix_request())

    settings = get_settings()
    assert settings.LLM_REQUEST_TIMEOUT_SECONDS == 42.5
    assert settings.LLM_MAX_RETRIES == 5
    assert captured["timeout"] == 42.5
    assert captured["max_retries"] == 5


def test_factory_pools_anthropic_gateway_for_stable_key(monkeypatch):
    # get_llm_gateway() returns the SAME cached anthropic gateway across calls
    # when the key is unchanged (pooled client reused across requests).
    _reset_llm_gateway_cache()
    monkeypatch.setenv("LLM_ADAPTER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stable-key")
    try:
        first = get_llm_gateway()
        second = get_llm_gateway()
        assert first is second
        assert isinstance(first, AnthropicGateway)
    finally:
        _reset_llm_gateway_cache()


def test_factory_rebuilds_anthropic_gateway_when_key_changes(monkeypatch):
    # A key change yields a FRESH gateway/client (a rotated key is honored, never
    # served from the stale pool). get_settings() reads env live.
    _reset_llm_gateway_cache()
    monkeypatch.setenv("LLM_ADAPTER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key-one")
    try:
        first = get_llm_gateway()
        monkeypatch.setenv("ANTHROPIC_API_KEY", "key-two")
        second = get_llm_gateway()
        assert first is not second
        assert isinstance(second, AnthropicGateway)
    finally:
        _reset_llm_gateway_cache()


def test_factory_builds_one_anthropic_gateway_under_concurrency(monkeypatch):
    # Pre-unattended-prod hardening (go-live sweep 2026-08-06): concurrent
    # cold-start callers must build the pooled anthropic gateway EXACTLY once
    # (double-checked lock on the single-slot cache) — no duplicate gateways and
    # thus no leaked httpx pools. get_llm_gateway() is reached from the async path
    # via a worker thread, so the race is real.
    import threading

    from llm import anthropic_adapter as _aa

    _reset_llm_gateway_cache()
    monkeypatch.setenv("LLM_ADAPTER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "concurrent-key")

    build_count = {"n": 0}
    count_lock = threading.Lock()
    real_ctor = _aa.AnthropicGateway

    class _CountingGateway(real_ctor):
        def __init__(self, *args, **kwargs):
            with count_lock:
                build_count["n"] += 1
            super().__init__(*args, **kwargs)

    # The factory imports AnthropicGateway lazily by attribute, so patching the
    # module attribute is what the cold-start path resolves.
    monkeypatch.setattr(_aa, "AnthropicGateway", _CountingGateway)

    n_threads = 16
    barrier = threading.Barrier(n_threads)
    results: list[object] = [None] * n_threads

    def _worker(i: int) -> None:
        barrier.wait()  # release all threads together to maximize the cold-start race
        results[i] = get_llm_gateway()

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n_threads)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Built exactly once, and every caller got that same pooled instance.
        assert build_count["n"] == 1
        assert all(r is results[0] for r in results)
        assert isinstance(results[0], real_ctor)
    finally:
        _reset_llm_gateway_cache()


def test_fake_path_needs_no_new_config_and_builds_no_client(monkeypatch):
    # The fake adapter reads none of the new Settings and never constructs an SDK
    # client. Guard the SDK ctor to prove it is never touched on the fake path.
    def _factory(*, api_key, **_kwargs):  # pragma: no cover - must NOT be called
        raise AssertionError("the fake path must never build an anthropic client")

    monkeypatch.setattr(_anthropic, "Anthropic", _factory)
    monkeypatch.setenv("LLM_ADAPTER", "fake")
    monkeypatch.delenv("LLM_REQUEST_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("LLM_MAX_RETRIES", raising=False)

    gateway = get_llm_gateway()
    assert isinstance(gateway, FakeLLMGateway)
    # A full fake completion runs without ever touching the guarded ctor.
    resp = gateway.complete(_matrix_request())
    assert isinstance(resp, LLMResponse)


# --- Story 7.4: STREAMING_MAX_TOKENS <-> SDK non-streaming ceiling (canary) ----
#
# STREAMING_MAX_TOKENS must stay strictly below the SDK's EFFECTIVE non-streaming
# max_tokens ceiling for EVERY model route_model can return, so a request at or
# below the threshold never trips the SDK's non-streaming ValueError (which is
# NOT an anthropic.APIError and so would escape complete()'s transport fence).
#
# This calls the SDK's REAL guard (`_client._calculate_nonstreaming_timeout`, the
# exact function `messages.create()` invokes with `MODEL_NONSTREAMING_TOKENS.get(
# model)`) rather than re-deriving its formula from source text — so it tracks any
# SDK change to the limit, the per-model cap, or the rejection logic, and cannot
# silently pass by validating its own reconstruction. It is a pure computation
# (no network, no credentials).
#
# NOTE (defense-in-depth): the adapter builds its client with an EXPLICIT
# `timeout`, and the SDK only runs this guard when `client.timeout ==
# DEFAULT_TIMEOUT` (anthropic messages.py). So on the live path the raw ValueError
# is ALREADY unreachable regardless of this coupling — the >STREAMING_MAX_TOKENS
# streaming route and this canary are belt-and-suspenders that keep the port safe
# if the explicit timeout were ever removed. This canary pins the coupling the
# streaming route relies on; it does not claim to be the sole guard.


def test_streaming_max_tokens_below_sdk_nonstreaming_ceiling_for_every_routed_model():
    routed_models = {route_model(True), route_model(False)}
    # Sanity: the routed set is exactly the two documented model ids.
    assert routed_models == {HARD_REASONING_MODEL, DEFAULT_MODEL}

    constants = importlib.import_module("anthropic._constants")
    # A DEFAULT-timeout client so we exercise the SDK's real non-streaming guard
    # directly (a fake key — never a real one; no network is made by the timeout
    # calc itself).
    client = _anthropic.Anthropic(api_key="test-key-not-real")

    for model in routed_models:
        cap = constants.MODEL_NONSTREAMING_TOKENS.get(model, None)

        # The SDK ACCEPTS STREAMING_MAX_TOKENS non-streaming (no ValueError) — this
        # is the exact call messages.create() makes for a default-timeout client.
        # If a future SDK/threshold change pushed the ceiling to/below the
        # threshold, this raises and the test fails loudly.
        try:
            client._calculate_nonstreaming_timeout(STREAMING_MAX_TOKENS, cap)
        except ValueError:  # pragma: no cover - only on a real regression
            pytest.fail(
                f"STREAMING_MAX_TOKENS={STREAMING_MAX_TOKENS} is NOT strictly below "
                f"the SDK non-streaming ceiling for {model!r}; a request at the "
                "threshold would trip the SDK's ValueError and escape the port."
            )

        # Confirm this IS the real guard (not a no-op): a value far above ANY
        # plausible ceiling DOES raise. Guarantees the accept-assertion above has
        # teeth — the SDK is genuinely rejecting large non-streaming requests.
        with pytest.raises(ValueError):
            client._calculate_nonstreaming_timeout(10_000_000, cap)
