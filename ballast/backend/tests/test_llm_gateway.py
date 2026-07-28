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

import os
import re
import sys
from pathlib import Path

import pytest

from llm.factory import UnknownLLMAdapterError, get_llm_gateway
from llm.fake_adapter import FakeLLMGateway
from llm.models import DEFAULT_MODEL, HARD_REASONING_MODEL, route_model
from llm.port import (
    LLMGateway,
    LLMMessage,
    LLMRequest,
    LLMResponse,
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
