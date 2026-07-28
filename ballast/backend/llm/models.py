"""Deterministic model routing for the LLM Gateway (AD-6).

The gateway routes to exactly two Claude models by reasoning tier. The model-ID
strings are the EXACT bare identifiers — never append a date suffix. Routing is a
pure function of the request's reasoning tier: no wall-clock, no randomness, so
the same request always resolves to the same model.
"""

from __future__ import annotations

#: Default tier — used for ordinary coach language.
DEFAULT_MODEL = "claude-sonnet-4-6"

#: Hard-reasoning tier — used when a request is flagged ``hard_reasoning=True``.
HARD_REASONING_MODEL = "claude-opus-4-8"


def route_model(hard_reasoning: bool) -> str:
    """Return the model-ID for the given reasoning tier.

    Pure and deterministic: ``True`` → :data:`HARD_REASONING_MODEL`
    (``"claude-opus-4-8"``), ``False`` → :data:`DEFAULT_MODEL`
    (``"claude-sonnet-4-6"``). No wall-clock, no RNG.
    """
    return HARD_REASONING_MODEL if hard_reasoning else DEFAULT_MODEL
