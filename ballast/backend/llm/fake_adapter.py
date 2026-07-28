"""FakeLLMGateway — the credential-free implementation of :class:`LLMGateway`.

This is the DEFAULT adapter (``LLM_ADAPTER=fake``). It makes the entire coach
path runnable and testable locally with ZERO credentials and ZERO network calls,
and it NEVER imports the ``anthropic`` SDK (the sole-caller invariant, AD-6).

Determinism is LOAD-BEARING: the coach stories build on top of the gateway and
assert reproducible behaviour, so the fake's output MUST be a pure function of
the request. It generates a JSON object that CONFORMS to the request's
``output_schema`` (object/array/string/integer/number/boolean/null), filling
required fields with typed placeholders seeded by the field name via a stable
hash — NO wall-clock, NO ``random`` module, NO network. The same request always
yields byte-identical output. When an Anthropic key is available, flipping
``LLM_ADAPTER=anthropic`` swaps in the real source with no caller changes (AD-6).
"""

from __future__ import annotations

import hashlib
from typing import Any

from llm.models import route_model
from llm.port import LLMGateway, LLMRequest, LLMResponse, require_output_schema


def _seed(field_name: str) -> int:
    """A stable non-negative integer seed derived purely from a field name.

    Uses a hash (not Python's salted ``hash()``) so the value is identical across
    processes and runs — determinism is load-bearing here.
    """
    digest = hashlib.sha256(field_name.encode()).digest()
    return int.from_bytes(digest[:8], "big")


def _placeholder(schema: dict[str, Any], field_name: str) -> Any:
    """Build a deterministic value conforming to a JSON-Schema node.

    Pure function of (``schema``, ``field_name``): supports object/array/string/
    integer/number/boolean/null and fills required object fields recursively with
    typed placeholders seeded by field name. No wall-clock, no RNG.
    """
    schema_type = schema.get("type")

    # A schema may express its type as a list (e.g. ["string", "null"]); pick the
    # first non-null option deterministically so output stays reproducible.
    if isinstance(schema_type, list):
        non_null = [t for t in schema_type if t != "null"]
        schema_type = non_null[0] if non_null else "null"

    if schema_type == "object" or (schema_type is None and "properties" in schema):
        properties: dict[str, Any] = schema.get("properties", {})
        required = schema.get("required")
        # If the schema declares required fields, fill exactly those; otherwise
        # fill every declared property so callers get a fully-populated object.
        keys = list(required) if required else list(properties.keys())
        obj: dict[str, Any] = {}
        for key in keys:
            child = properties.get(key, {})
            obj[key] = _placeholder(child, key)
        return obj

    if schema_type == "array":
        items = schema.get("items", {})
        if isinstance(items, list):  # tuple-style items schema
            return [_placeholder(item, f"{field_name}[{i}]") for i, item in enumerate(items)]
        # Emit a single deterministic element so the array is non-empty and typed.
        return [_placeholder(items, f"{field_name}[0]")]

    if schema_type == "string":
        enum = schema.get("enum")
        if enum:
            return enum[_seed(field_name) % len(enum)]
        return f"fake-{field_name}"

    if schema_type == "integer":
        return _seed(field_name) % 100

    if schema_type == "number":
        return float(_seed(field_name) % 100)

    if schema_type == "boolean":
        return _seed(field_name) % 2 == 0

    if schema_type == "null":
        return None

    # Untyped/unknown node: fall back to a deterministic string so we always
    # return SOMETHING conforming to "any".
    return f"fake-{field_name}"


class FakeLLMGateway(LLMGateway):
    """A deterministic, offline stand-in for the real Anthropic gateway.

    Never uses wall-clock time, never touches the network, and never imports the
    ``anthropic`` SDK. Rejects a schema-less request (structured-output invariant)
    and otherwise returns a schema-conforming, byte-reproducible response.
    """

    provider = "fake"

    def complete(self, request: LLMRequest) -> LLMResponse:
        """Return a deterministic schema-conforming :class:`LLMResponse`. No network.

        Enforces the structured-output requirement first (raises
        :class:`~llm.port.StructuredOutputRequiredError` on a schema-less
        request), then builds an ``output`` conforming to ``request.output_schema``
        and routes the model from ``request.hard_reasoning``.
        """
        require_output_schema(request)
        output = _placeholder(request.output_schema, "root")
        if not isinstance(output, dict):
            # A top-level non-object schema still round-trips through the dict
            # ``output`` field by wrapping it under a stable key.
            output = {"value": output}
        return LLMResponse(
            output=output,
            model=route_model(request.hard_reasoning),
            provider=self.provider,
        )
