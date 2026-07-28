"""LLM Gateway factory (AD-6).

Selects the concrete :class:`~llm.port.LLMGateway` implementation from config
(``LLM_ADAPTER``) and returns it typed as the port. Callers (the Coach Engine)
depend on the port only; swapping fake <-> anthropic is a config change, not a
code change.

- ``LLM_ADAPTER=fake`` (default): :class:`FakeLLMGateway` — no creds, no network,
  never imports the ``anthropic`` SDK.
- ``LLM_ADAPTER=anthropic``: :class:`~llm.anthropic_adapter.AnthropicGateway` —
  raises a clear :class:`~llm.anthropic_adapter.LLMNotConfiguredError` if
  ``ANTHROPIC_API_KEY`` is absent (or the SDK is not installed).
"""

from __future__ import annotations

from api.config import get_settings
from llm.fake_adapter import FakeLLMGateway
from llm.port import LLMGateway


class UnknownLLMAdapterError(RuntimeError):
    """Raised when ``LLM_ADAPTER`` names an adapter that does not exist."""


def get_llm_gateway() -> LLMGateway:
    """Return the configured LLM gateway as an :class:`LLMGateway`.

    The ``anthropic`` adapter is imported lazily (only when selected), so the
    default fake path never touches the SDK.
    """
    adapter = (get_settings().LLM_ADAPTER or "fake").strip().lower()

    if adapter == "fake":
        return FakeLLMGateway()

    if adapter == "anthropic":
        # Import lazily so selecting fake never loads the anthropic adapter module.
        from llm.anthropic_adapter import AnthropicGateway

        return AnthropicGateway()

    raise UnknownLLMAdapterError(
        f"Unknown LLM_ADAPTER '{adapter}'. Expected 'fake' or 'anthropic'."
    )
