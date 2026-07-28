---
title: 'Story 4.1 — LLM Gateway'
type: 'feature'
created: '2026-07-28'
status: 'done'
baseline_revision: '80a1ea0412a88d79e6a2d05fb600d1965d72f59c'
final_revision: 'e8d3886e256bbe2f96ea49dbad1c0f935181b6e7'
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-4-context.md'
warnings: [oversized]
---

<intent-contract>

## Intent

**Problem:** Epic 4's Coach Engine needs language from Claude, but there is no controlled path to the Anthropic API yet. Without a single owner, model calls would be scattered, unstructured, and impossible to enforce the trust invariants (NFR2/AD-6) against.

**Approach:** Build a hexagonal **LLM Gateway** — the sole caller of the Anthropic API — behind a `LLMGateway` port with a config-selected adapter (fake default / anthropic real), mirroring the existing `marketdata/` module. The gateway is a thin transport that (a) enforces structured output on every call, and (b) applies deterministic model routing (Opus 4.8 for flagged hard-reasoning, Sonnet 4.6 otherwise). It is generic over the output schema; prompt assembly, citation checks, and the Recommendation contract are NOT its concern (they belong to the Coach Engine in 4.2/4.3).

## Boundaries & Constraints

**Always:**
- Only `llm/anthropic_adapter.py` may import the `anthropic` SDK (AD-6 "sole caller" — enforced by a structural test). All other callers depend only on the `LLMGateway` port.
- Every request MUST carry a non-empty JSON output schema; a request without one is rejected before any model call (structured output is structurally enforced, not optional).
- Model routing is a pure, deterministic function of the request's reasoning tier — no wall-clock, no randomness. Use the exact model-ID strings `claude-opus-4-8` (hard reasoning) and `claude-sonnet-4-6` (default); never append date suffixes.
- The fake adapter is the default and the tested path: fully offline, zero credentials, zero network, deterministic output that conforms to the requested schema.
- The real adapter is credential-gated like `TiingoAdapter`: importing it never crashes and never imports the SDK at module load; constructing/using it without `ANTHROPIC_API_KEY` raises a clear `LLMNotConfiguredError`. The API key and raw prompt/response bodies are never logged.
- Use the Anthropic Python SDK per the `claude-api` reference: synchronous `client.messages.create(...)` with `output_config={"format": {"type": "json_schema", "schema": ...}}`. Adaptive thinking only — never pass `budget_tokens`, `temperature`, `top_p`, or `top_k`.

**Block If:**
- A live Anthropic API call (real key / real network / paid tokens) is required to satisfy any acceptance criterion. (Fake-first: all ACs must pass offline. A real key is a credentials decision — HALT.)

**Never:**
- No prompt assembly, no citation/evidence validation, no Recommendation `{action_label, order_intent?, reasoning, evidence[], uncertainties[]}` schema or validation gate — those are Story 4.2/4.3. The gateway must stay generic over output schema.
- No FastAPI route/endpoint (the gateway is system-facing, invoked by the Coach Engine later).
- No streaming, batching, tool-runner, or agent surfaces in v1 — a single structured `complete()` call only.
- No async: `complete()` is synchronous, matching the existing adapter style (`MarketDataPort.fetch_eod`).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Route hard reasoning | request with `hard_reasoning=True` | `route_model` → `"claude-opus-4-8"` | No error expected |
| Route default | request with `hard_reasoning=False` | `route_model` → `"claude-sonnet-4-6"` | No error expected |
| Fake completion | `LLM_ADAPTER=fake`, valid request with an output schema | Deterministic `LLMResponse` whose `output` conforms to the schema; identical request → identical output; `provider == "fake"`, `model` = routed model | No error expected |
| Missing output schema | request with empty/absent `output_schema` | Rejected before any adapter call | raise `StructuredOutputRequiredError` |
| Real adapter, no key | `LLM_ADAPTER=anthropic`, `ANTHROPIC_API_KEY` unset | Fail loud at construction; SDK never imported | raise `LLMNotConfiguredError` |
| Factory default | no `LLM_ADAPTER` set | returns `FakeLLMGateway` typed as `LLMGateway` | No error expected |
| Unknown adapter | `LLM_ADAPTER=bogus` | — | raise `UnknownLLMAdapterError` |

</intent-contract>

## Code Map

- `ballast/backend/marketdata/{port,factory,fake_adapter,tiingo_adapter}.py` -- REFERENCE pattern to mirror exactly (port + config-selected factory + fake default + credential-gated real adapter).
- `ballast/backend/llm/__init__.py` -- existing empty package to populate.
- `ballast/backend/llm/port.py` -- NEW: `LLMGateway` ABC, `LLMMessage`/`LLMRequest`/`LLMResponse` dataclasses, `StructuredOutputRequiredError`.
- `ballast/backend/llm/models.py` -- NEW: model-ID constants + deterministic `route_model(hard_reasoning)`.
- `ballast/backend/llm/fake_adapter.py` -- NEW: deterministic offline `FakeLLMGateway`.
- `ballast/backend/llm/anthropic_adapter.py` -- NEW: credential-gated real adapter; sole Anthropic caller; `LLMNotConfiguredError`.
- `ballast/backend/llm/factory.py` -- NEW: `get_llm_gateway()`, `UnknownLLMAdapterError`.
- `ballast/backend/api/config.py` -- add `LLM_ADAPTER` (default `"fake"`) and `ANTHROPIC_API_KEY` (default `""`) settings.
- `ballast/backend/.env.example` -- document the two new vars under an `--- LLM / Story 4.1 ---` block.
- `ballast/backend/pyproject.toml` -- add `anthropic` to `dependencies`.
- `ballast/backend/tests/test_llm_gateway.py` -- NEW: tests for all I/O-matrix cases + the structural "sole caller" invariant.

## Tasks & Acceptance

**Execution:**
- [x] `ballast/backend/llm/port.py` -- define `LLMMessage(role, content)`, `LLMRequest(messages, output_schema, system=None, hard_reasoning=False, max_tokens=4096)`, `LLMResponse(output, model, provider)` (frozen dataclasses), the `LLMGateway` ABC with `complete(self, request) -> LLMResponse`, and `StructuredOutputRequiredError`. The port docstring states the two invariants (sole caller, structured-output-required).
- [x] `ballast/backend/llm/models.py` -- `DEFAULT_MODEL = "claude-sonnet-4-6"`, `HARD_REASONING_MODEL = "claude-opus-4-8"`, and pure `route_model(hard_reasoning: bool) -> str`.
- [x] `ballast/backend/llm/fake_adapter.py` -- `FakeLLMGateway(LLMGateway)` with `provider = "fake"`; rejects a request with no `output_schema` (`StructuredOutputRequiredError`); otherwise returns a deterministic instance conforming to the schema (fill required fields with typed placeholders seeded by field name; support object/array/string/integer/number/boolean/null), `model = route_model(request.hard_reasoning)`. No wall-clock, no RNG, no network.
- [x] `ballast/backend/llm/anthropic_adapter.py` -- `AnthropicGateway(LLMGateway)` with `provider = "anthropic"`; `__init__` reads `ANTHROPIC_API_KEY` and raises `LLMNotConfiguredError` if absent (SDK NOT imported here); `complete()` enforces the output-schema requirement, lazily imports `anthropic`, routes the model, calls sync `client.messages.create(model=..., max_tokens=..., system=..., messages=[...], output_config={"format": {"type": "json_schema", "schema": request.output_schema}})`, parses the first text block as JSON into `output`, and returns `LLMResponse`. Never log the key or bodies; a missing SDK raises `LLMNotConfiguredError`.
- [x] `ballast/backend/llm/factory.py` -- `get_llm_gateway() -> LLMGateway` selecting on `get_settings().LLM_ADAPTER` (`fake` default, `anthropic` lazily imported), raising `UnknownLLMAdapterError` otherwise.
- [x] `ballast/backend/api/config.py` + `.env.example` -- add `LLM_ADAPTER` and `ANTHROPIC_API_KEY` with safe empty/fake defaults and comments matching the existing Tiingo block style.
- [x] `ballast/backend/pyproject.toml` -- add `anthropic` to `dependencies`.
- [x] `ballast/backend/tests/test_llm_gateway.py` -- cover every I/O-matrix row (routing both tiers, deterministic fake output conforming to schema, missing-schema rejection, no-key gating, factory default, unknown adapter) PLUS a structural test asserting no backend module other than `llm/anthropic_adapter.py` imports `anthropic` (grep the source tree, excluding `.venv`/`__pycache__`).

**Acceptance Criteria:**
- Given the LLM Gateway, when the coach needs language, then it is the sole caller of the Anthropic API (structurally: only `anthropic_adapter.py` imports the SDK, verified by test) and enforces structured output (a request lacking an output schema is rejected before any model call) (AD-6, NFR2).
- Given a request flagged hard-reasoning vs not, when routed, then it deterministically resolves to `claude-opus-4-8` vs `claude-sonnet-4-6` respectively, with no wall-clock or randomness (AD-6).
- Given the default configuration, when `get_llm_gateway()` is called, then it returns the offline `FakeLLMGateway` and the whole suite passes with zero credentials and zero network.
- Given `LLM_ADAPTER=anthropic` without `ANTHROPIC_API_KEY`, when the adapter is constructed, then it raises a clear `LLMNotConfiguredError` without importing the SDK or hitting the network.

## Spec Change Log

(No bad_spec loopbacks — empty.)

## Review Triage Log

### 2026-07-28 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 2 (high 0, medium 1, low 1)
- defer: 2 (medium 1, low 1)
- reject: 7
- addressed_findings:
  - `[medium]` `[patch]` `AnthropicGateway` gated on `os.environ` while the story's own config/`.env` wires `ANTHROPIC_API_KEY` through pydantic `Settings`, and the spec said to mirror `TiingoAdapter` (which reads `get_settings()`); a `.env`-only key would fail the gate, and `anthropic.Anthropic()` re-resolved credentials independently of the validated key. Fixed: gate via `get_settings().ANTHROPIC_API_KEY` and pass `api_key=self._api_key` to the SDK client so the gated key is the one actually used.
  - `[low]` `[patch]` Removed a no-op tautology assertion (`assert "anthropic" not in sys.modules or True`) in the no-key test; kept the meaningful before/after `sys.modules` check.

## Design Notes

Mirror `marketdata/` decisions verbatim: config-selected factory with lazy import of the real adapter, `provider` class attribute per adapter, fail-loud credential gating identical in spirit to `TiingoAdapter`. Keep the gateway generic — it transports `{system, messages, output_schema, hard_reasoning}` and returns a parsed `output` dict; it must never reference Recommendation fields.

Structured-output enforcement is the structural teeth for NFR2 at this layer: the port rejects a schema-less request, so an unstructured LLM call is physically un-issuable through the gateway. The real adapter uses `output_config.format` (per the `claude-api` Python reference), not prefills or the deprecated `output_format`.

Golden shape:
```python
req = LLMRequest(
    messages=(LLMMessage("user", "..."),),
    output_schema={"type": "object",
                   "properties": {"answer": {"type": "string"}},
                   "required": ["answer"], "additionalProperties": False},
    hard_reasoning=True,
)
resp = get_llm_gateway().complete(req)   # fake: deterministic {"answer": "..."}, model == "claude-opus-4-8"
```

## Verification

**Commands:**
- `cd ballast/backend && python -m pytest tests/test_llm_gateway.py -q` -- expected: all tests pass, no network access, no credentials required.
- `cd ballast/backend && python -c "from llm.factory import get_llm_gateway; from llm.fake_adapter import FakeLLMGateway; assert isinstance(get_llm_gateway(), FakeLLMGateway)"` -- expected: exits 0 (factory default is the fake gateway, importable without the anthropic SDK installed).

## Auto Run Result

Status: done

**Summary of implemented change:** Added the hexagonal LLM Gateway (`ballast/backend/llm/`) — the sole controlled path to the Anthropic API, mirroring the existing `marketdata/` module. A `LLMGateway` port + config-selected factory (`LLM_ADAPTER`, fake default) fronts a deterministic offline `FakeLLMGateway` (the tested path) and a credential-gated `AnthropicGateway`. Structured output is enforced on every request (a schema-less request is rejected before any model call), and model routing is deterministic (`claude-opus-4-8` for hard reasoning, `claude-sonnet-4-6` otherwise). Only `anthropic_adapter.py` may import the SDK, enforced by a structural test.

**Files changed:**
- `ballast/backend/llm/port.py` (new) — `LLMGateway` ABC, `LLMMessage`/`LLMRequest`/`LLMResponse` frozen dataclasses, `require_output_schema`, `StructuredOutputRequiredError`.
- `ballast/backend/llm/models.py` (new) — model-ID constants + pure `route_model`.
- `ballast/backend/llm/fake_adapter.py` (new) — deterministic, offline, schema-conforming fake gateway.
- `ballast/backend/llm/anthropic_adapter.py` (new) — credential-gated real gateway; sole Anthropic SDK caller; `LLMNotConfiguredError`.
- `ballast/backend/llm/factory.py` (new) — `get_llm_gateway()`, `UnknownLLMAdapterError`.
- `ballast/backend/tests/test_llm_gateway.py` (new) — 15 tests: routing, deterministic fake output, schema enforcement, no-key gating, factory default/unknown, structural sole-caller scan.
- `ballast/backend/api/config.py` — added `LLM_ADAPTER` / `ANTHROPIC_API_KEY` settings.
- `ballast/backend/.env.example` — documented both vars.
- `ballast/backend/pyproject.toml` — added `anthropic` dependency.

**Review findings breakdown:** 2 patches applied (medium: config key-source consistency → gate via `get_settings()` + pass `api_key` to the SDK client; low: removed a no-op test assertion). 2 items deferred (real-adapter runtime hardening for when 4.2/4.3 first exercise the live path; fake-generator fidelity for exotic JSON-Schema nodes). 7 rejected as noise/by-design. 0 intent gaps, 0 spec loopbacks.

**Follow-up review recommended:** false — the final pass made only two small, localized patches (one config-consistency fix, one test cleanup); no behavior/API/security/data change of significance.

**Verification performed:**
- `python -m pytest tests/test_llm_gateway.py -q` → 15 passed (zero network, zero credentials).
- Full suite `python -m pytest -q` → 162 passed, 1 pre-existing Starlette deprecation warning (no regressions).
- Confirmed the default fake path works and never imports the `anthropic` SDK (not installed in the venv); no-key gating raises `LLMNotConfiguredError` without importing the SDK.

**Residual risks:** The real `AnthropicGateway` path is code-shaped but unexercised (no creds/network in this story); its runtime robustness is tracked in `deferred-work.md` and must be hardened before 4.2/4.3 flip `LLM_ADAPTER=anthropic`. The `anthropic` package is declared in `pyproject.toml` but not installed in the current venv — install it when enabling the real adapter.
