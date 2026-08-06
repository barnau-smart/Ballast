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

import threading

from api.config import get_settings
from llm.fake_adapter import FakeLLMGateway
from llm.port import LLMGateway


class UnknownLLMAdapterError(RuntimeError):
    """Raised when ``LLM_ADAPTER`` names an adapter that does not exist."""


#: Single-slot memo of the anthropic gateway as ``(cache_key, gateway)`` so the
#: same instance — and thus its pooled httpx client — serves successive
#: ``/recommend`` requests (connection reuse, Story 7.4). The cache key is the
#: full transport identity ``(api_key, timeout, max_retries)``: a rotated key OR a
#: retuned timeout/retry budget rebuilds a fresh gateway/client rather than
#: serving a stale-config pool (the client bakes those in at build). A single slot
#: means only the current config is pooled; ``get_settings()`` stays deliberately
#: uncached so the config is always read live. The fake path is never cached.
_ANTHROPIC_GATEWAY_CACHE: dict[str, object] = {}

#: Guards the read-modify-write of the single-slot cache above. ``get_llm_gateway``
#: is called from the async request path via a worker THREAD, so concurrent
#: first-callers could otherwise each build an ``AnthropicGateway`` and race the
#: single-slot swap (duplicate gateways, duplicate leaked httpx pools). The lock
#: makes the build-and-swap happen once; the steady-state cache hit stays lock-free.
_ANTHROPIC_GATEWAY_LOCK = threading.Lock()


def _reset_llm_gateway_cache() -> None:
    """Clear the memoized anthropic gateway — for test isolation only."""
    with _ANTHROPIC_GATEWAY_LOCK:
        _ANTHROPIC_GATEWAY_CACHE.clear()


def get_llm_gateway() -> LLMGateway:
    """Return the configured LLM gateway as an :class:`LLMGateway`.

    The ``anthropic`` adapter is imported lazily (only when selected), so the
    default fake path never touches the SDK. The anthropic gateway is memoized by
    its transport identity (``ANTHROPIC_API_KEY`` + the timeout/retry budget) so
    its pooled client is reused across requests; a key or budget change rebuilds.
    The fake gateway is always constructed fresh.
    """
    settings = get_settings()
    adapter = (settings.LLM_ADAPTER or "fake").strip().lower()

    if adapter == "fake":
        return FakeLLMGateway()

    if adapter == "anthropic":
        # Reuse the pooled instance while the full transport identity is stable;
        # rebuild if the key or the timeout/retry budget changed.
        cache_key = (
            f"{settings.ANTHROPIC_API_KEY}\x00"
            f"{settings.LLM_REQUEST_TIMEOUT_SECONDS}\x00"
            f"{settings.LLM_MAX_RETRIES}"
        )
        # Lock-free fast path: an already-pooled instance for this exact transport
        # identity serves immediately without contending on the lock.
        cached = _ANTHROPIC_GATEWAY_CACHE.get(cache_key)
        if cached is not None:
            return cached

        # Import lazily so selecting fake never loads the anthropic adapter module.
        from llm.anthropic_adapter import AnthropicGateway

        # Double-checked under the lock: only the first, racing cold-start callers
        # serialize here so the gateway (and its httpx pool) is built exactly once.
        with _ANTHROPIC_GATEWAY_LOCK:
            cached = _ANTHROPIC_GATEWAY_CACHE.get(cache_key)
            if cached is not None:
                return cached
            # Build FIRST, then swap the single slot — so a construction that
            # raises (e.g. a blank/undecryptable key) never evicts a healthy pooled
            # gateway that other requests are still reusing.
            gateway = AnthropicGateway()
            _ANTHROPIC_GATEWAY_CACHE.clear()
            _ANTHROPIC_GATEWAY_CACHE[cache_key] = gateway
            return gateway

    raise UnknownLLMAdapterError(
        f"Unknown LLM_ADAPTER '{adapter}'. Expected 'fake' or 'anthropic'."
    )
