---
title: 'Story 6.2 — Live LLM Gateway Enablement & Hardening'
type: 'feature'
created: '2026-08-01'
status: 'done'
baseline_revision: 'baef052450e8b1e0fd1139a4ff58391b561956fd'
final_revision: 'bbf0968563045347d3a0e379ea5c2885055f3aed'
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-6-context.md'
warnings: [oversized]
---

<intent-contract>

## Intent

**Problem:** The real `AnthropicGateway` (Story 4.1) is code-shaped but unexercised: its `complete()` has zero runtime hardening — it leaks raw `anthropic.*` SDK exceptions, `json.JSONDecodeError`, and bare `StopIteration` past the `LLMGateway` port (breaking the AD-6 abstraction), never inspects `stop_reason`, never validates messages, and always uses non-streaming `messages.create` (which `ValueError`s at large `max_tokens`). Until it is hardened and proven, flipping `LLM_ADAPTER=anthropic` risks a dead-end instead of a graceful degrade to the default plan.

**Approach:** Harden the real adapter so every runtime failure surfaces as a typed, vendor-neutral `LLMError` (never a raw SDK/parse exception), inspect `stop_reason` (refusal/truncation), guard text-block extraction and JSON parsing, validate messages, and route large-`max_tokens` calls through streaming. Prove the full real code path exhaustively with a **mocked** Anthropic client (including one end-to-end pass where a real-shaped `order_intent` citing a retrieved evidence ID clears the 4.2 gate and surfaces), and promote `anthropic` to a pinned, locked dependency. The coach's existing never-a-dead-end degradation net stays intact.

## Boundaries & Constraints

**Always:**
- Only `llm/anthropic_adapter.py` imports the `anthropic` SDK (AD-6 sole caller — the existing structural test must stay green after hardening).
- **No raw vendor or parse exception escapes the `LLMGateway` port.** Every `anthropic.APIError` (timeout/connection/rate-limit/status/overloaded), `json.JSONDecodeError`, and `StopIteration` from `complete()` is caught and re-raised as a typed `LLMError` subtype (chained via `from`). The API key and raw prompt/response bodies are never logged.
- Structured output stays enforced (schema-less request rejected before any model call) and model routing stays deterministic (`claude-opus-4-8` hard reasoning / `claude-sonnet-4-6` default, no date suffixes) — unchanged from 4.1.
- Use `client.messages.create(model, max_tokens, system?, messages, output_config={"format": {"type": "json_schema", "schema": ...}})`; adaptive-thinking defaults only — never pass `budget_tokens`/`temperature`/`top_p`/`top_k`. Route a request whose `max_tokens` exceeds the streaming threshold through `client.messages.stream(...).get_final_message()` (same parsing path).
- `complete()` stays **synchronous** and returns an `LLMResponse` with `output` as a `dict`; the fake adapter remains the default and the fully-offline tested path.
- All acceptance criteria pass offline with zero credentials and zero network (mocked SDK client). `anthropic` is a pinned dependency present in `uv.lock`.

**Block If:**
- A live Anthropic API call (real key / real network / paid tokens) is required to satisfy any acceptance criterion. All ACs are offline (mocked client). The one-time **live** paid smoke-test against `api.anthropic.com` is a documented manual go-live step behind a real `ANTHROPIC_API_KEY` — a credentials decision. If an AC cannot be met without it, HALT.

**Never:**
- No prompt assembly, no Recommendation `{action_label, order_intent?, reasoning, evidence[], uncertainties[]}` schema or citation/validation logic here — those are Coach 4.2/4.3 and stay unchanged. The gateway stays generic over `output_schema`.
- No token-by-token streaming surface exposed to callers; streaming is an internal transport detail for large `max_tokens` only.
- No custom retry/backoff — the SDK already auto-retries 429/5xx; do not reimplement it.
- Do not narrow the coach pipeline's `except Exception` degradation net in any way that could stop catching `RecommendationValidationError` (the "valid JSON, fake evidence id" path) — the never-a-dead-end guarantee must hold.
- No changes to the `FakeLLMGateway` schema generator (its exotic-node fidelity gap stays deferred).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Live structured completion | mocked client returns a Message with a text block of object-JSON, `stop_reason="end_turn"` | `LLMResponse{output=<dict>, model=routed, provider="anthropic"}` | No error |
| Provider refusal | `stop_reason="refusal"` (`stop_details` present) | raise `LLMRefusalError` | typed; no vendor leak |
| Truncated output | `stop_reason="max_tokens"` | raise `LLMMalformedResponseError` | typed |
| No text block | `content` has only `tool_use`/`thinking` blocks | raise `LLMMalformedResponseError` | no bare `StopIteration` |
| Non-JSON / non-dict text | first text block is not valid JSON, or JSON root is not an object | raise `LLMMalformedResponseError` | guarded `json.loads` |
| Provider transport error | `messages.create` raises `anthropic.APITimeoutError` / `RateLimitError` / `APIError` | raise `LLMTransportError` (chained) | raw `anthropic.*` never escapes the port |
| Large max_tokens | `request.max_tokens > STREAMING_MAX_TOKENS` | routed via `client.messages.stream(...).get_final_message()`, then same parse | avoids the SDK non-streaming `ValueError` |
| Invalid messages | empty `messages`, or a role ∉ {`user`,`assistant`} | raise `EmptyMessagesError` before any client construction/call | pre-flight precondition |
| End-to-end order_intent | mocked real gateway emits Recommendation JSON with `order_intent` citing a **retrieved** evidence id | clears the 4.2 gate, surfaces via `/recommend` with `order_intent` intact | No error |

</intent-contract>

## Code Map

- `ballast/backend/llm/port.py` -- ADD vendor-neutral runtime error taxonomy: `LLMError(RuntimeError)` base + `LLMTransportError`, `LLMMalformedResponseError`, `LLMRefusalError` subtypes; ADD `EmptyMessagesError(ValueError)` and a pure `require_valid_messages(request)` precondition (non-empty `messages`; each `role in {"user","assistant"}`). Document the "no raw vendor/parse exception escapes the port" invariant alongside the existing two. These live here (not in the adapter) so `coach/pipeline.py` can reference `LLMError` without importing the SDK (AD-6).
- `ballast/backend/llm/anthropic_adapter.py` -- HARDEN `complete()`: after `require_output_schema`/`_require_configured`, call `require_valid_messages(request)`; lazily import `anthropic` (ImportError → `LLMNotConfiguredError`, unchanged); build kwargs as today; if `request.max_tokens > STREAMING_MAX_TOKENS` obtain the Message via `client.messages.stream(**kwargs).get_final_message()`, else `client.messages.create(**kwargs)` — wrap that call in `try/except anthropic.APIError` (and the SDK's large-`max_tokens` `ValueError`) → `raise LLMTransportError(...) from exc`; factor a `_parse_message(resp, model) -> LLMResponse` helper that: checks `stop_reason` (`"refusal"` → `LLMRefusalError`; `"max_tokens"` → `LLMMalformedResponseError`), safely finds the first `type=="text"` block (none → `LLMMalformedResponseError`, no bare `StopIteration`), `json.loads` guarded (`JSONDecodeError` → `LLMMalformedResponseError`), and requires a `dict` root (else `LLMMalformedResponseError`). ADD module const `STREAMING_MAX_TOKENS = 16000`. Never log key/bodies. Keep `provider="anthropic"` and `route_model` usage.
- `ballast/backend/coach/pipeline.py` -- In `surface()`: KEEP the `except Exception` net (never-a-dead-end) but import `LLMError` from `llm.port` and make the observability log name the typed failure (e.g. `isinstance(exc, LLMError)`); confirm both `LLMError` subtypes and `RecommendationValidationError` degrade to `build_default_plan`. No narrowing that could drop `RecommendationValidationError`.
- `ballast/backend/pyproject.toml` -- Pin `anthropic` (currently unpinned) to a range compatible with the installed 0.120.2 (e.g. `anthropic>=0.120,<0.121`) — it becomes a committed go-live dependency.
- `ballast/backend/uv.lock` -- Lock `anthropic` (resolves the long-standing "uv.lock re-adds anthropic then gets reverted" drift; from 6.2 on it is a permanent locked dependency).
- `ballast/backend/.env.example` -- Update the `LLM / Story 4.1` block comment to note `LLM_ADAPTER=anthropic` + a real `ANTHROPIC_API_KEY` is the go-live path.
- `ballast/backend/tests/test_llm_gateway.py` -- ADD mocked-SDK tests (monkeypatch `anthropic.Anthropic` so `messages.create`/`messages.stream().get_final_message()` return crafted Message doubles): happy dict parse (+ provider/model), refusal→`LLMRefusalError`, max_tokens→`LLMMalformedResponseError`, no-text-block→`LLMMalformedResponseError`, non-JSON/non-dict→`LLMMalformedResponseError`, `anthropic.APIError`/timeout→`LLMTransportError` (assert raw vendor type does NOT escape), large-`max_tokens` uses the stream path, invalid/empty messages→`EmptyMessagesError` before any client call. Keep the structural sole-caller test green.
- `ballast/backend/tests/test_coach_pipeline.py` -- ADD a gateway double raising each `LLMError` subtype → assert pipeline returns `build_default_plan(...)` (never dead-end); ADD an end-to-end test with a mocked real `AnthropicGateway` returning a Recommendation JSON whose `order_intent` cites a real retrieved evidence id → assert the blessed recommendation surfaces with `order_intent` intact through the 4.2 gate.

## Tasks & Acceptance

**Execution:**
- [x] `ballast/backend/llm/port.py` -- add `LLMError` base + `LLMTransportError`/`LLMMalformedResponseError`/`LLMRefusalError`, `EmptyMessagesError`, `require_valid_messages`; document the no-vendor-leak invariant.
- [x] `ballast/backend/llm/anthropic_adapter.py` -- harden `complete()` per Code Map: message precondition, streaming route for large `max_tokens`, `_parse_message` with `stop_reason`/text-block/JSON/dict guards, wrap SDK exceptions into `LLMTransportError`, add `STREAMING_MAX_TOKENS`.
- [x] `ballast/backend/coach/pipeline.py` -- keep the broad degradation net; improve the typed-error log; verify both error families degrade to the default plan.
- [x] `ballast/backend/pyproject.toml` + `ballast/backend/uv.lock` -- pin and lock `anthropic`.
- [x] `ballast/backend/.env.example` -- document the `LLM_ADAPTER=anthropic` go-live path.
- [x] `ballast/backend/tests/test_llm_gateway.py` -- add the mocked-SDK hardening tests (every I/O-matrix adapter row); keep the sole-caller structural test green.
- [x] `ballast/backend/tests/test_coach_pipeline.py` -- add the typed-error degradation tests and the end-to-end mocked-real-gateway order_intent test.

**Acceptance Criteria:**
- Given `LLM_ADAPTER=anthropic` and a hardened real adapter, when `complete()` hits any runtime failure (transport/timeout, refusal, truncation, no text block, non-JSON/non-dict), then it raises a typed `LLMError` subtype and no raw `anthropic.*` / `json.JSONDecodeError` / `StopIteration` escapes the `LLMGateway` port (AD-6, NFR2).
- Given any such gateway failure during a coach run, when `run_coach_pipeline` executes `retrieve → compose → validate → surface`, then it degrades to `build_default_plan` and `/recommend` still returns a valid recommendation — never a dead-end (FR7, FR12–FR14).
- Given a mocked live client returning a real-shaped `order_intent` recommendation that cites a retrieved evidence id, when the coach runs end-to-end behind the existing gates, then the 4.2 validation gate passes and the recommendation surfaces with `order_intent` intact.
- Given the default config, when the whole suite runs, then it passes with zero credentials and zero network, only `anthropic_adapter.py` imports the SDK, `anthropic` is pinned and present in `uv.lock`, and model routing + structured-output enforcement are unchanged (AD-6).

## Spec Change Log

(No bad_spec loopbacks — empty.)

## Review Triage Log

### 2026-08-01 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 5 (high 0, medium 3, low 2)
- defer: 1
- reject: 5
- addressed_findings:
  - `[medium]` `[patch]` Both reviewers (via pinned-SDK source) found the streaming path called `client.messages.stream(**kwargs).get_final_message()` on the un-entered `MessageStreamManager` — `get_final_message()` only exists on the object yielded by `__enter__`, so the real path would raise a raw `AttributeError` that escapes the `except (anthropic.APIError, ValueError)` net (violating the no-raw-leak invariant); the test mock hid it. Fixed to `with client.messages.stream(**kwargs) as stream: resp = stream.get_final_message()` and rebuilt the test double as a faithful manager/stream context-manager split so the `with`-idiom is genuinely proven.
  - `[medium]` `[patch]` `except (anthropic.APIError, ValueError)` over-caught legitimate/programming `ValueError`s as transport failures, and its intended target (the SDK's large-`max_tokens` `ValueError` on the non-streaming path) is unreachable since large requests route to streaming. Narrowed to `except anthropic.APIError`; corrected the adapter comment and the `LLMTransportError` docstring (dropped the ValueError-wrapping claim).
  - `[medium]` `[patch]` `stop_reason == "model_context_window_exceeded"` (a real SDK terminal reason) was unhandled — a context-blown partial response would be blessed as complete. Folded it into the truncation guard alongside `max_tokens`.
  - `[low]` `[patch]` `stop_reason == "pause_turn"` (incomplete/paused turn) was unhandled and would parse a partial body; added it to the incomplete-response guard → `LLMMalformedResponseError`.
  - `[low]` `[patch]` `next(b.text for b in resp.content ...)` assumed `resp.content` is iterable; guarded with `resp.content or []` so a `content=None` shape cannot raise a raw `TypeError` past the port.

### 2026-08-01 — Review pass (follow-up, independent)
- intent_gap: 0
- bad_spec: 0
- patch: 0
- defer: 1
- reject: 9
- addressed_findings:
  - none
- notes: Independent second-pass review (Blind Hunter + Edge Case Hunter). Full suite green (313 passed; targeted gateway+pipeline 68 passed). No patch/bad_spec/intent_gap. Both reviewers converged on one real forward-looking robustness item → deferred (see below). All other findings rejected as unreachable-against-the-real-SDK, out-of-spec, or already-deliberately-decided in the prior pass:
    - `[defer]` The port's no-raw-leak invariant on the non-streaming path holds only by an unpinned coincidence (`STREAMING_MAX_TOKENS=16000` < the SDK's ~21333 time-based `ValueError` threshold; routed models absent from the SDK per-model cap dict), untested. Verified UNREACHABLE at current config against pinned SDK 0.120.2 → deferred as a go-live robustness/coupling item, not a current bug.
    - `[reject]` Widen `except anthropic.APIError` to also catch `ValueError`/`AttributeError`/`TypeError`: the `ValueError` arm is unreachable and was deliberately narrowed last pass (over-catching programming errors is worse); `AttributeError`/`TypeError` on malformed vendor blocks cannot occur against the real pydantic SDK (`TextBlock.text` is a required str, every block carries `.type`).
    - `[reject]` `stop_reason=None` blessed as complete: `messages.create()` / `stream().get_final_message()` always return a terminal `stop_reason`; `None` is a streaming-in-progress-only state, not a returned-message shape.
    - `[reject]` `pause_turn` message wording ("truncated or incomplete"): behavior (degrade) is correct and intentional for a tool-less single-shot gateway, and the message already embeds the actual `stop_reason={reason!r}`.
    - `[reject]` `require_valid_messages` should also reject empty `content`: out of the spec's scoped precondition (non-empty messages + valid role); an empty-content turn would return a vendor 400 that wraps to `LLMTransportError` (not a raw leak) and degrades.
    - `[reject]` Role case/whitespace strictness, narrow `<0.121` pin (matches spec exactly), `STREAMING_MAX_TOKENS` comment "fabrication" (comment already hedges with "may" and cites the ~10-min estimate), a degradation-log observability enhancement, and a CI "no real key" confirmation (verified: fake default, importlib, zero network) — all noise for this story.

## Design Notes

- **Why the error taxonomy lives in `llm/port.py`.** `coach/pipeline.py` must be able to name `LLMError` in its degradation log without importing `anthropic` (AD-6 sole-caller). Putting the base + subtypes in the vendor-neutral port keeps the SDK import confined to the adapter while giving callers a typed handle. `LLMNotConfiguredError` stays where it is (config-time, not a runtime failure) — out of scope to move.
- **Hardening is in the adapter; degradation stays in the pipeline.** `surface()` already wraps the gateway call in `except Exception` and returns the default plan, so degradation works today even for raw errors. The value 6.2 adds is that the adapter no longer *leaks* raw vendor/parse types past the port — the pipeline keeps its broad, deliberately-blunt net (it must still catch `RecommendationValidationError`, the "valid JSON but fabricated evidence id" arm).
- **Streaming is a transport detail.** Per the SDK, non-streaming `create()` refuses `max_tokens` above its ~10-minute estimate; route large requests through the streaming helper — `with client.messages.stream(**kwargs) as s: resp = s.get_final_message()` (`stream()` returns a *context manager*; `get_final_message()` lives on the entered object, not the manager) — which returns a full Message of identical shape, so `_parse_message` is shared across both paths. The v1 coach uses `max_tokens=4096`, so this path is defensive but must be tested against a faithful manager/stream double.
- **`output_config.format` is the structured-output mechanism** (per the `claude-api` reference), already used in 4.1 — a request lacking a schema is still rejected before any model call. On refusal, `stop_details.category/explanation` may be summarized in the (body-free) log but never raised with content.
- **uv.lock drift ends here.** Prior stories reverted the `anthropic` line to keep the tree clean; 6.2 is the go-live story that legitimately makes it a pinned, committed dependency — do not revert it after this story.
- Golden shape of the shared parser:
```python
def _parse_message(self, resp, model) -> LLMResponse:
    if resp.stop_reason == "refusal":
        raise LLMRefusalError("model refused the request")
    if resp.stop_reason == "max_tokens":
        raise LLMMalformedResponseError("response truncated at max_tokens")
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if text is None:
        raise LLMMalformedResponseError("no text block in response")
    try:
        output = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMMalformedResponseError("non-JSON structured output") from exc
    if not isinstance(output, dict):
        raise LLMMalformedResponseError("structured output root is not an object")
    return LLMResponse(output=output, model=model, provider=self.provider)
```

## Verification

**Commands:**
- `cd ballast/backend && python -m pytest tests/test_llm_gateway.py tests/test_coach_pipeline.py -q` -- expected: all pass, zero network, zero credentials.
- `cd ballast/backend && python -m pytest -q` -- expected: full suite green (no regressions).
- `cd ballast/backend && python -c "import anthropic; print(anthropic.__version__)"` -- expected: prints the pinned version (SDK importable).
- `grep -n '^name = "anthropic"' ballast/backend/uv.lock` -- expected: a match (anthropic locked).
- Confirm the structural sole-caller test in `test_llm_gateway.py` still passes after hardening (only `anthropic_adapter.py` imports the SDK).

**Manual checks (go-live, out of AC scope):**
- With a real `ANTHROPIC_API_KEY` + `LLM_ADAPTER=anthropic`, run one live `/recommend` and confirm a real structured recommendation surfaces (or degrades cleanly). This paid call is a credentials decision, performed manually at go-live — not part of the automated suite.

## Auto Run Result

Status: done (follow-up independent review pass; no code changes required)

### Summary of implemented change
Story 6.2 hardens the real `AnthropicGateway.complete()` so every runtime failure surfaces as a typed, vendor-neutral `LLMError` subtype and no raw `anthropic.*` / `json.JSONDecodeError` / `StopIteration` escapes the `LLMGateway` port (AD-6/NFR2): a vendor-neutral error taxonomy + message precondition in `llm/port.py`, a shared `_parse_message` with `stop_reason` / text-block / JSON / dict guards, `except anthropic.APIError → LLMTransportError`, streaming routing for large `max_tokens`, a typed-error degradation log in `coach/pipeline.py` (broad never-a-dead-end net preserved), and `anthropic` promoted to a pinned + locked dependency. This run was a fresh independent review of the already-committed implementation; it applied no patches.

### Files changed this pass
- `_bmad-output/implementation-artifacts/6-2-live-llm-gateway-enablement-hardening.md` — appended the follow-up review-triage entry; set `status: done`, `followup_review_recommended: false`.
- `_bmad-output/implementation-artifacts/deferred-work.md` — appended ONE new deferred entry (the unpinned `STREAMING_MAX_TOKENS`↔SDK-`ValueError`-threshold coupling). No existing entries modified.

(No `ballast/` source changed — the review produced zero patches and zero spec loopbacks.)

### Review findings breakdown
- Reviewers: Blind Hunter (`bmad-review-adversarial-general`) + Edge Case Hunter (`bmad-review-edge-case-hunter`), run in parallel with no prior context.
- Patches applied: 0
- Deferred: 1 — the port's no-raw-leak invariant on the non-streaming path holds only by an unpinned numeric coincidence (`STREAMING_MAX_TOKENS=16000` below the SDK's ~21333 time-based `ValueError` threshold; routed models absent from the SDK per-model cap dict), untested. Verified unreachable at current config → forward-looking go-live robustness item.
- Rejected: 9 — unreachable-against-the-real-SDK shape guards (`AttributeError`/`TypeError`/`stop_reason=None`), a deliberately-decided-last-pass `except`-widening, out-of-spec precondition additions, intentional strictness, and cosmetic/observability/confirmation notes.

### Verification performed
- `cd ballast/backend && uv run python -m pytest tests/test_llm_gateway.py tests/test_coach_pipeline.py -q` → **68 passed**, zero network, zero credentials.
- `cd ballast/backend && uv run python -m pytest -q` → **313 passed** (full suite, no regressions).
- `uv run python -c "import anthropic; print(anthropic.__version__)"` → `0.120.2` (SDK importable).
- `grep -n '^name = "anthropic"' ballast/backend/uv.lock` → match at line 28 (anthropic locked).
- Independently confirmed the reviewers' SDK claims against the pinned SDK: the routed model ids `claude-opus-4-8` / `claude-sonnet-4-6` are absent from `anthropic._constants.MODEL_NONSTREAMING_TOKENS`, and the large-`max_tokens` `ValueError` (`_base_client.py:774-775`) fires only above the ~10-minute (~21333-token) estimate — above `STREAMING_MAX_TOKENS=16000`, so the non-streaming `ValueError` is unreachable at current config.

### Residual risks
- The deferred coupling above: if `STREAMING_MAX_TOKENS` is ever raised toward the SDK boundary or `route_model` can return a capped model id, a raw `ValueError` would escape the port. Backstopped today by the coach pipeline's broad `except Exception` → default plan, and by `max_tokens=4096` in v1. Tracked in the deferred-work ledger.
- The prior-pass deferred items for 6.2 remain open (per-call client construction with no explicit timeout/retry; the NULL-`idempotency_key` and send-then-commit items from 6.1) — all go-live concerns, none blocking this story.

