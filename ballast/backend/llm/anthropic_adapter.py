"""AnthropicGateway — the real :class:`LLMGateway` implementation via Claude.

Code-shaped but CREDENTIAL-GATED (per the story's fake-first strategy), mirroring
:class:`~marketdata.tiingo_adapter.TiingoAdapter`'s fail-loud posture:

- Importing this module NEVER imports the ``anthropic`` SDK and NEVER crashes,
  even with no credentials. The SDK is imported LAZILY inside :meth:`complete`.
  AD-6 ("sole caller"): this adapter is the ONLY place in the codebase that may
  touch the ``anthropic`` SDK — enforced by a structural test.
- Constructing/using the adapter without ``ANTHROPIC_API_KEY`` raises a clear
  :class:`LLMNotConfiguredError` — a configuration error, not an import crash and
  not a network error. The key is checked BEFORE the SDK is imported.
- Real network calls happen ONLY when the adapter is properly configured (i.e.
  never in tests / the default fake path).
- The API key and the raw prompt/response bodies are NEVER logged.

The gateway is a thin transport: it applies deterministic model routing, enforces
structured output via ``output_config.format`` (the ``claude-api`` Python
reference — NOT prefills or the deprecated ``output_format``), and returns the
parsed ``output`` dict. Adaptive thinking only — it never passes
``budget_tokens``, ``temperature``, ``top_p``, or ``top_k`` (they 400 on these
models). It never references any Recommendation fields.

When an Anthropic key is available, set ``ANTHROPIC_API_KEY`` and
``LLM_ADAPTER=anthropic``; nothing else changes (AD-6).
"""

from __future__ import annotations

import json

from api.config import get_settings
from llm.models import route_model
from llm.port import (
    LLMGateway,
    LLMMalformedResponseError,
    LLMRefusalError,
    LLMRequest,
    LLMResponse,
    LLMTransportError,
    require_output_schema,
    require_valid_messages,
)

#: Above this ``max_tokens`` the SDK's non-streaming ``messages.create`` refuses
#: the request (it may exceed the SDK's ~10-minute non-streaming estimate), so
#: such requests are routed through the streaming helper
#: (``with client.messages.stream(...) as s: s.get_final_message()``) — which
#: returns a full Message of identical shape, parsed by the same helper. The v1
#: coach uses ``max_tokens=4096``, so this path is DEFENSIVE (but tested).
STREAMING_MAX_TOKENS = 16000


class LLMNotConfiguredError(RuntimeError):
    """Raised when the Anthropic adapter is used without required credentials.

    This is a configuration error (fail-loud), deliberately distinct from an
    import failure or a network error. Also raised if the ``anthropic`` package
    is not installed (a missing SDK is a configuration problem, not a crash).
    """


class AnthropicGateway(LLMGateway):
    """Real LLM gateway backed by the Anthropic API. Gated on credentials."""

    provider = "anthropic"

    def __init__(self) -> None:
        # Read the key via Settings (pydantic-settings, .env-aware) — the same
        # source the rest of the app uses and mirroring TiingoAdapter. The value
        # is passed explicitly to the SDK client in complete(), so the key that
        # passes this gate is exactly the key used for the call.
        self._api_key = get_settings().ANTHROPIC_API_KEY
        # Fail loudly at construction if the key is missing, so the factory's
        # gating is unambiguous. The anthropic SDK is NOT imported here.
        self._require_configured()

    def _require_configured(self) -> None:
        if not self._api_key:
            raise LLMNotConfiguredError(
                "Anthropic is not configured. Set ANTHROPIC_API_KEY (and "
                "LLM_ADAPTER=anthropic) to enable the Anthropic gateway."
            )

    def complete(self, request: LLMRequest) -> LLMResponse:
        """Run one structured completion against Claude (network call).

        Enforces the structured-output requirement and the message precondition
        first (both raise BEFORE any model call — the latter before the client is
        even constructed), imports the ``anthropic`` SDK lazily (AD-6), routes the
        model, and issues the call with ``output_config.format``. A request whose
        ``max_tokens`` exceeds :data:`STREAMING_MAX_TOKENS` is routed through
        ``messages.stream(...).get_final_message()`` (which avoids the SDK's
        non-streaming large-``max_tokens`` ``ValueError``); both paths return a
        Message parsed by the shared :meth:`_parse_message`.

        HARDENING (AD-6/NFR2): every runtime failure surfaces as a typed,
        vendor-neutral :class:`~llm.port.LLMError` subtype — a raw ``anthropic.*``
        SDK exception, ``json.JSONDecodeError``, or bare ``StopIteration`` NEVER
        escapes this port. The API key and raw prompt/response bodies are never
        logged.
        """
        require_output_schema(request)
        self._require_configured()
        # Pre-flight precondition — rejects an empty/invalid conversation BEFORE
        # the SDK is imported or any client is constructed.
        require_valid_messages(request)

        # Lazy import: selecting the fake adapter never loads the anthropic SDK.
        # A missing SDK fails loud and clear like the rest of the adapter.
        try:
            import anthropic
        except ImportError as exc:
            raise LLMNotConfiguredError(
                "The 'anthropic' package is not installed. Install it (and set "
                "ANTHROPIC_API_KEY, LLM_ADAPTER=anthropic) to use the Anthropic "
                "gateway."
            ) from exc

        model = route_model(request.hard_reasoning)

        # Pass the validated key explicitly so the SDK uses the same credential
        # that passed the gate (not an independently-resolved env var / profile).
        client = anthropic.Anthropic(api_key=self._api_key)

        # Build kwargs — omit ``system`` when None (None-safe). Adaptive thinking
        # only: no budget_tokens/temperature/top_p/top_k (they 400 on these models).
        kwargs: dict[str, object] = {
            "model": model,
            "max_tokens": request.max_tokens,
            "messages": [
                {"role": m.role, "content": m.content} for m in request.messages
            ],
            "output_config": {
                "format": {
                    "type": "json_schema",
                    "schema": request.output_schema,
                }
            },
        }
        if request.system is not None:
            kwargs["system"] = request.system

        # Wrap the vendor call so no raw ``anthropic.*`` (its ``APIError`` base
        # covers timeout/connection/rate-limit/status/overloaded) escapes the
        # port; chain via ``from``.
        try:
            if request.max_tokens > STREAMING_MAX_TOKENS:
                # Large requests: stream and coalesce to a full Message of the
                # same shape (avoids the non-streaming large-``max_tokens``
                # refusal). ``messages.stream(...)`` returns a CONTEXT MANAGER;
                # ``get_final_message()`` is only available on the object yielded
                # by entering it — so we must use ``with``. Streaming is an
                # INTERNAL transport detail — no token-by-token surface leaks.
                with client.messages.stream(**kwargs) as stream:
                    resp = stream.get_final_message()
            else:
                resp = client.messages.create(**kwargs)
        except anthropic.APIError as exc:
            raise LLMTransportError(
                "The Anthropic API call failed at transport level."
            ) from exc

        return self._parse_message(resp, model)

    def _parse_message(self, resp: object, model: str) -> LLMResponse:
        """Parse a provider Message into an :class:`LLMResponse`, or raise typed.

        Shared by BOTH the streaming and non-streaming paths (they return an
        identically-shaped Message). Inspects ``stop_reason`` (refusal /
        truncation), safely extracts the first ``type == "text"`` block (no bare
        :class:`StopIteration`), parses its JSON guardedly (no raw
        ``json.JSONDecodeError``), and requires a ``dict`` root — every failure a
        typed :class:`~llm.port.LLMError` subtype. No response body is logged.
        """
        if resp.stop_reason == "refusal":
            raise LLMRefusalError("The model refused the request.")
        # Truncated / incomplete terminal reasons: a partial (or absent) body must
        # never be blessed as a complete response. ``max_tokens`` and
        # ``model_context_window_exceeded`` are output-truncation; ``pause_turn``
        # is an incomplete/paused turn. All degrade — none are parsed.
        if resp.stop_reason in (
            "max_tokens",
            "model_context_window_exceeded",
            "pause_turn",
        ):
            raise LLMMalformedResponseError(
                "The response was truncated or incomplete "
                f"(stop_reason={resp.stop_reason!r})."
            )
        # ``resp.content or []`` — None-safe; ``next(..., None)`` — no bare
        # StopIteration if there is no text block.
        text = next(
            (b.text for b in (resp.content or []) if b.type == "text"), None
        )
        if text is None:
            raise LLMMalformedResponseError("No text block in the response.")
        try:
            output = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMMalformedResponseError(
                "The structured output was not valid JSON."
            ) from exc
        if not isinstance(output, dict):
            raise LLMMalformedResponseError(
                "The structured output root is not an object."
            )
        return LLMResponse(output=output, model=model, provider=self.provider)
