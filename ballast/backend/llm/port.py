"""The LLM Gateway Port — the sole boundary between Ballast and the Anthropic API.

AD-6 ("sole caller"): every model call in the product flows through this port.
Only :mod:`llm.anthropic_adapter` may import the ``anthropic`` SDK — every other
consumer (the Coach Engine in 4.2/4.3, and beyond) depends ONLY on this port.
The concrete adapter (fake default vs anthropic real) is chosen by config and
swapped without touching a single caller, mirroring :mod:`marketdata`.

Three invariants are enforced STRUCTURALLY here (not merely discouraged):

- **Sole caller (AD-6):** the ``anthropic`` SDK is imported nowhere but
  ``llm/anthropic_adapter.py`` — verified by a structural test that scans the
  source tree. Callers hold a :class:`LLMGateway`, never a vendor client.
- **Structured output required (NFR2):** every :class:`LLMRequest` MUST carry a
  non-empty JSON ``output_schema``. A request without one is rejected by
  :func:`require_output_schema` BEFORE any model call, so an unstructured LLM
  call is physically un-issuable through the gateway.
- **No raw vendor/parse exception escapes the port (AD-6/NFR2):** a runtime
  failure inside any adapter's :meth:`LLMGateway.complete` — an
  ``anthropic.*`` SDK error (transport/timeout/rate-limit/status/overloaded), a
  ``json.JSONDecodeError``, or a bare ``StopIteration`` — is caught and
  re-raised as a typed, vendor-neutral :class:`LLMError` subtype (chained via
  ``from``). Callers depend on this taxonomy, never on the SDK's, so they can
  name a gateway failure without importing ``anthropic``.

The vendor-neutral runtime error taxonomy (:class:`LLMError` and its subtypes)
plus the message precondition (:class:`EmptyMessagesError`,
:func:`require_valid_messages`) live HERE — not in the adapter — precisely so a
caller like ``coach/pipeline.py`` can reference :class:`LLMError` without
importing the ``anthropic`` SDK (AD-6). :class:`LLMNotConfiguredError` is a
CONFIG-time error and deliberately stays in the adapter (out of scope here).

The gateway is a thin transport that is GENERIC over the output schema. Prompt
assembly, citation/evidence validation, and the Recommendation contract are NOT
its concern — they belong to the Coach Engine (Story 4.2/4.3).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class StructuredOutputRequiredError(ValueError):
    """Raised when an :class:`LLMRequest` lacks a non-empty ``output_schema``.

    Structured output is the structural teeth for NFR2 at this layer: a
    schema-less request is rejected BEFORE any adapter/model call, so an
    unstructured LLM call cannot be issued through the gateway.
    """


class EmptyMessagesError(ValueError):
    """Raised when an :class:`LLMRequest` carries no messages or an invalid role.

    A message precondition, checked BEFORE any model call (indeed before the
    vendor client is even constructed): ``messages`` must be non-empty and every
    ``role`` must be ``"user"`` or ``"assistant"``. A ``ValueError`` (not an
    :class:`LLMError`) because it is a caller-side precondition violation, not a
    runtime failure of the provider.
    """


class LLMError(RuntimeError):
    """Base for every VENDOR-NEUTRAL runtime failure of :meth:`LLMGateway.complete`.

    An adapter MUST NOT let a raw ``anthropic.*`` SDK exception, a
    ``json.JSONDecodeError``, or a bare ``StopIteration`` escape the port — each
    is caught and re-raised as one of the subtypes below (chained via ``from``).
    Callers (e.g. ``coach/pipeline.py``) catch this base to observe a typed
    gateway failure WITHOUT importing the SDK, preserving AD-6.
    """


class LLMTransportError(LLMError):
    """A provider/transport failure — timeout, connection, rate-limit, or status.

    The vendor-neutral wrapper for any ``anthropic.APIError`` (its base covers
    ``APITimeoutError``/``APIConnectionError``/``RateLimitError``/
    ``APIStatusError``/overloaded). The raw vendor exception is chained but NEVER
    re-raised past the port.
    """


class LLMMalformedResponseError(LLMError):
    """The provider returned an unusable response the gateway cannot parse.

    Covers truncation/incompletion (``stop_reason`` in ``max_tokens`` /
    ``model_context_window_exceeded`` / ``pause_turn``), a response with no text
    block (would otherwise be a bare ``StopIteration``), non-JSON text (a chained
    ``json.JSONDecodeError``), and a JSON root that is not an object.
    """


class LLMRefusalError(LLMError):
    """The provider refused the request (``stop_reason == "refusal"``).

    A distinct, typed signal so a caller can tell a refusal apart from a
    transport or parse failure. The refusal category/explanation may be
    summarized in a body-free log, but the raw response body is never raised.
    """


@dataclass(frozen=True)
class LLMMessage:
    """One turn in a conversation — a ``role`` ("user"/"assistant") and ``content``.

    Deliberately minimal and vendor-neutral: the gateway maps these onto the
    provider's message shape. It carries no prompt-assembly logic (that is the
    Coach Engine's concern).
    """

    role: str
    content: str


@dataclass(frozen=True)
class LLMRequest:
    """A single structured completion request — generic over the output schema.

    ``messages`` is the conversation (at least one turn). ``output_schema`` is a
    JSON Schema the response MUST conform to; it is REQUIRED (a request without
    one is rejected before any model call). ``system`` is an optional system
    prompt. ``hard_reasoning`` selects the model tier deterministically
    (Opus 4.8 vs Sonnet 4.6). ``max_tokens`` bounds the response.

    Frozen so a request is an immutable value that can be replayed to the fake
    adapter and yield byte-identical output.
    """

    messages: tuple[LLMMessage, ...]
    output_schema: dict[str, Any] = field(default_factory=dict)
    system: str | None = None
    hard_reasoning: bool = False
    max_tokens: int = 4096


@dataclass(frozen=True)
class LLMResponse:
    """The parsed result of a completion — a schema-conforming ``output`` dict.

    ``output`` is the JSON object the model (or the fake) produced, already
    parsed. ``model`` is the exact routed model-ID string that served the call.
    ``provider`` identifies the adapter ("fake" or "anthropic"). No raw response
    body is retained here.
    """

    output: dict[str, Any]
    model: str
    provider: str


def require_output_schema(request: LLMRequest) -> None:
    """Enforce the structured-output invariant shared by BOTH adapters.

    Raises :class:`StructuredOutputRequiredError` if ``request.output_schema`` is
    empty/absent. Called at the top of every ``complete()`` implementation BEFORE
    any model call, so an unstructured request is un-issuable through the gateway.
    """
    if not request.output_schema:
        raise StructuredOutputRequiredError(
            "Every LLM request must carry a non-empty JSON output_schema; a "
            "schema-less request is rejected before any model call (NFR2)."
        )


#: The roles a provider will accept in a conversation turn (vendor-neutral).
_VALID_ROLES = frozenset({"user", "assistant"})


def require_valid_messages(request: LLMRequest) -> None:
    """Enforce the message precondition shared by every adapter.

    Raises :class:`EmptyMessagesError` if ``request.messages`` is empty or any
    turn's ``role`` is not ``"user"``/``"assistant"``. PURE (no I/O): called at
    the top of ``complete()`` BEFORE any vendor client is constructed or called,
    so a malformed conversation is rejected pre-flight rather than surfacing as a
    raw vendor 400 mid-flight.
    """
    if not request.messages:
        raise EmptyMessagesError(
            "An LLM request must carry at least one message; an empty "
            "conversation is rejected before any model call."
        )
    for message in request.messages:
        if message.role not in _VALID_ROLES:
            raise EmptyMessagesError(
                f"Invalid message role {message.role!r}; every role must be one "
                "of 'user' or 'assistant'."
            )


class LLMGateway(ABC):
    """The abstract LLM boundary — the only model type callers depend on (AD-6).

    Implementations: :class:`~llm.fake_adapter.FakeLLMGateway` (local / dev /
    test, deterministic, zero credentials & zero network — the DEFAULT and tested
    path) and :class:`~llm.anthropic_adapter.AnthropicGateway` (real Claude calls,
    credential-gated). Swapping is a config change (``LLM_ADAPTER``), not a code
    change — callers touch only this port.
    """

    #: Identifies the concrete adapter; set on each subclass.
    provider: str

    @abstractmethod
    def complete(self, request: LLMRequest) -> LLMResponse:
        """Run one structured completion and return the parsed :class:`LLMResponse`.

        Synchronous (matching the existing adapter style). Implementations MUST
        call :func:`require_output_schema` before any model call and route the
        model deterministically from ``request.hard_reasoning``.
        """
        raise NotImplementedError
