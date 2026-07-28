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
from llm.port import LLMGateway, LLMRequest, LLMResponse, require_output_schema


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

        Enforces the structured-output requirement first (raises before any model
        call), imports the ``anthropic`` SDK lazily (AD-6), routes the model, and
        calls the synchronous ``messages.create`` with ``output_config.format``.
        Parses the first text block as JSON into ``output``. The API key and raw
        bodies are never logged.
        """
        require_output_schema(request)
        self._require_configured()

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

        resp = client.messages.create(**kwargs)

        # Parse the first text block as JSON.
        text = next(b.text for b in resp.content if b.type == "text")
        output = json.loads(text)

        return LLMResponse(output=output, model=model, provider=self.provider)
