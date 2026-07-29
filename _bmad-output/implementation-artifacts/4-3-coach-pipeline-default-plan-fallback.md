---
title: 'Story 4.3 — Coach Pipeline & Default-Plan Fallback'
type: 'feature'
created: '2026-07-28'
status: 'draft'
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-4-context.md'
warnings: []
---

<intent-contract>

## Intent

**Problem:** Stories 4.1 (LLM Gateway) and 4.2 (Recommendation object + validation gate) built the pieces, but nothing wires them to the Precedent Engine. The Coach Engine has no pipeline that turns a user decision into a blessed recommendation, and no guarantee it is *never a dead-end* (FR7, AD-4): when there is no confident special call, the user must still get the strategy-backed default plan plus a plain reason.

**Approach:** Add a Coach Engine pipeline (`backend/coach/pipeline.py`) that orchestrates `retrieve → compose → validate → surface`: retrieve evidence via the Precedent Engine's `find_precedent`, compose a coach-voice prompt (owned by the Coach Engine, not the Gateway) instructing the LLM to cite only the retrieved evidence IDs, call the LLM Gateway for a structured candidate, and pass it through the 4.2 validation gate. If the LLM path fails for any reason (gateway error, unparseable output, or gate rejection — including the fake adapter's placeholder IDs), the pipeline falls back to a deterministic, code-authored **default plan** built from the same retrieved set and blessed through the gate — so a `BlessedRecommendation` is *always* returned.

## Boundaries & Constraints

**Always:**
- The pipeline is the single orchestrator of `retrieve → compose → validate → surface`. Retrieval goes only through `find_precedent`; the LLM call goes only through the injected `LLMGateway` (default `get_llm_gateway()`); blessing goes only through `validate_recommendation`. No module bypasses an owner.
- Prompt assembly and the "cite only provided IDs, never invent numbers" instruction live in the Coach Engine, not the Gateway. The composed `LLMRequest` always carries `RECOMMENDATION_OUTPUT_SCHEMA` and a deterministic `hard_reasoning` flag.
- `hard_reasoning` routing is a pure function of the retrieved set: `True` when any retrieved record is an `EVENT_PRECEDENT` (a tactical special call), `False` for the always-available `STRATEGY` default. No wall-clock, no randomness.
- **Never a dead-end (FR7/AD-4):** `surface` returns the LLM's `BlessedRecommendation` when it validates; otherwise it returns the deterministic default plan. The default plan is code-authored (not LLM-derived), cites every retrieved evidence ID, carries non-empty coach-voice reasoning and ≥1 explicit uncertainty, `order_intent=None`, and always passes the gate.
- The default-plan builder is pure and deterministic: identical retrieved set → equal (frozen) blessed object; no I/O, no LLM, no wall-clock, no randomness.
- Coach voice in the code-authored default plan and system prompt: patient, warm, honest, plain-spoken, explicit about uncertainty; never hype, condescending, or alarmist ("stick to your plan").
- Match existing style: frozen `@dataclass` for the decision input; async only where retrieval requires it (`find_precedent` is async); reuse the fake gateway as the default/tested path; money as `Decimal`.

**Block If:**
- A live Anthropic call (real key/network/paid tokens) is required to satisfy any AC — all ACs must pass offline with the fake gateway. HALT.
- A stakeholder wants "never a dead-end" weakened (e.g. allow the pipeline to raise/return nothing when the LLM fails) — that breaks FR7/AD-4 and is a product decision. HALT.

**Never:**
- No FastAPI route/endpoint and no UI — the pipeline is system-facing, invoked by the ask→approve surface in Story 4.6 (which adds approval + co-sign). Consistent with 4.1/4.2 staying route-less.
- No decision-record persistence, co-sign, or replay (4.9/4.10); no order execution, `order_intent` **semantics** validation, or Broker Port call (4.6–4.8) — carry any LLM-emitted `order_intent` through unvalidated, exactly as 4.2 left it.
- No self-destructive-move warnings (4.5) and no just-in-time-teaching subsystem (4.4 — `reasoning` already IS the teaching).
- No changes to `find_precedent`, the Gateway, or the validation gate. No new evidence kinds; no invented contribution-amount/plan feature.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| LLM valid → surface | injected gateway returns output citing a real retrieved ID; non-empty reasoning + uncertainty | returns the LLM's `BlessedRecommendation` (evidence resolved to real records); default plan NOT used | No error expected |
| No confident call (strategy) | `find_precedent` returns a `STRATEGY` record; LLM path fails to bless | returns default plan citing the strategy record, "stick to your plan" reasoning, `order_intent=None` | Fallback (no raise) |
| Gate rejects LLM output | candidate cites an ID not in retrieved set / empty reasoning / empty uncertainties (e.g. fake adapter's placeholder IDs) | returns deterministic default plan (never a dead-end) | `RecommendationValidationError` caught → fallback |
| Gateway raises | injected gateway `.complete()` raises (network/config/parse) | returns deterministic default plan | Exception caught at surface boundary → fallback |
| Determinism | same retrieved set through the default-plan builder twice | two equal (frozen) `BlessedRecommendation`s | No error expected |
| Hard-reasoning routing | retrieved set has an `EVENT_PRECEDENT` vs only `STRATEGY` | composed `LLMRequest.hard_reasoning` is `True` vs `False` | No error expected |
| End-to-end (offline) | real fake gateway + real `find_precedent` over seeded data | returns a `BlessedRecommendation` (the default plan, since the fake cannot cite real IDs); zero network, zero credentials | Fallback path |

</intent-contract>

## Code Map

- `ballast/backend/precedent/engine.py` -- REFERENCE: `async find_precedent(session, symbol=DEFAULT_BENCHMARK, as_of=None) -> list[EvidenceRecord]` (always length ≥1; strategy fallback guarantees non-empty). `DEFAULT_BENCHMARK = "VTI"`. Exported via `precedent` package (`find_precedent`, `EvidenceRecord`, `EvidenceKind`).
- `ballast/backend/coach/recommendation.py` -- REFERENCE: `Recommendation`, `RECOMMENDATION_OUTPUT_SCHEMA`, tolerant `recommendation_from_output(dict)`.
- `ballast/backend/coach/validation.py` -- REFERENCE: `validate_recommendation(candidate, retrieved) -> BlessedRecommendation`, `RecommendationValidationError`. Gate is the sole producer of a blessed object.
- `ballast/backend/llm/{port,factory}.py` -- REFERENCE: `LLMMessage`/`LLMRequest`/`LLMResponse`, `LLMGateway` ABC, `get_llm_gateway()`.
- `ballast/backend/coach/pipeline.py` -- NEW: `CoachDecision` input, `COACH_SYSTEM_PROMPT`, `is_hard_reasoning`, `compose_request`, `build_default_plan`, `surface`, `async run_coach_pipeline`.
- `ballast/backend/coach/__init__.py` -- export the pipeline's public entrypoints.
- `ballast/backend/tests/test_coach_pipeline.py` -- NEW: every I/O-matrix row (mostly pure/offline with in-memory `EvidenceRecord` fixtures + stub/fake gateways) plus one async DB-backed end-to-end test (mirrors `tests/test_precedent.py` harness).

## Tasks & Acceptance

**Execution:**
- [ ] `ballast/backend/coach/pipeline.py` -- add: frozen `CoachDecision(symbol: str = DEFAULT_BENCHMARK, question: str = "", amount: Decimal | None = None, as_of: date | None = None)`; `COACH_SYSTEM_PROMPT` (coach voice + "cite only the evidence IDs provided, never invent numbers, always state what's uncertain"); pure `is_hard_reasoning(retrieved) -> bool` (`True` iff any record kind is `EVENT_PRECEDENT`); pure `compose_request(decision, retrieved) -> LLMRequest` (user message embeds the decision + each retrieved record via `to_dict()`; `output_schema=RECOMMENDATION_OUTPUT_SCHEMA`, `system=COACH_SYSTEM_PROMPT`, `hard_reasoning=is_hard_reasoning(retrieved)`); pure `build_default_plan(retrieved) -> BlessedRecommendation` (constructs a `Recommendation` citing every retrieved ID, coach-voice "stick to your plan" reasoning + ≥1 uncertainty, `order_intent=None`, then returns `validate_recommendation(candidate, retrieved)`); `surface(gateway, decision, retrieved) -> BlessedRecommendation` (compose → `gateway.complete` → `recommendation_from_output` → `validate_recommendation`; on `RecommendationValidationError` or any gateway `Exception`, return `build_default_plan(retrieved)`); `async run_coach_pipeline(session, decision, *, gateway=None) -> BlessedRecommendation` (`retrieved = tuple(await find_precedent(session, symbol=decision.symbol, as_of=decision.as_of))`; `gateway = gateway or get_llm_gateway()`; `return surface(gateway, decision, retrieved)`).
- [ ] `ballast/backend/coach/__init__.py` -- export `CoachDecision`, `run_coach_pipeline`, `build_default_plan`, `surface`, `compose_request`, `is_hard_reasoning`.
- [ ] `ballast/backend/tests/test_coach_pipeline.py` -- cover every I/O-matrix row. Pure/offline tests with in-memory `EvidenceRecord` fixtures (event-precedent + strategy) and small `LLMGateway` stubs: LLM-valid → LLM rec surfaced (not default); gate-reject → default plan; gateway-raises → default plan; default-plan blesses + cites all retrieved IDs + `order_intent=None` + non-empty reasoning/uncertainty; determinism (equal on repeat); `is_hard_reasoning`/`compose_request` routing both tiers; the real `FakeLLMGateway` falls back to the default plan (placeholder IDs don't match). Plus one `@pytest.mark.asyncio` end-to-end test seeding `market_daily` and running `run_coach_pipeline` with the fake gateway (mirror `tests/test_precedent.py` via `async_session_maker()`), asserting a `BlessedRecommendation` is returned with zero network/credentials.

**Acceptance Criteria:**
- Given a user initiates a decision, when `run_coach_pipeline` executes `retrieve → compose → validate → surface`, then it returns a `BlessedRecommendation` — the LLM's when it validates against the retrieved evidence, otherwise the strategy-backed default plan — and never raises or returns nothing (FR7, AD-4).
- Given the LLM path fails (gateway error, unparseable output, or gate rejection), when `surface` runs, then the deterministic code-authored default plan is returned, citing the retrieved evidence with a plain coach-voice reason and an explicit uncertainty — proving the coach is never a dead-end.
- Given the default configuration (fake gateway, offline), when the whole suite runs, then all coach-pipeline ACs pass with zero credentials and zero network, and prompt assembly / evidence citation remain owned by the Coach Engine (the Gateway stays generic over schema).

## Design Notes

The fake gateway is schema-driven and emits placeholder evidence IDs (`"fake-evidence[0]"`) that never match a real retrieved ID, so offline the LLM path *always* fails the gate and the default plan is returned. This is by design: the same fallback that guarantees FR7 on gateway/validation failure also delivers "no confident special call → default plan," and it makes never-a-dead-end fully testable offline. The LLM happy path is proven with a tiny in-test `LLMGateway` stub that cites the real retrieved ID (dependency injection via `run_coach_pipeline(..., gateway=...)`).

The `surface` broad-`Exception` catch is the intentional resilience boundary — it wraps only the LLM-happy-path (compose/complete/map/validate); `build_default_plan` runs outside the `try`, so a bug there still surfaces rather than being masked.

```python
def surface(gateway, decision, retrieved):
    try:
        resp = gateway.complete(compose_request(decision, retrieved))
        return validate_recommendation(recommendation_from_output(resp.output), retrieved)
    except (RecommendationValidationError, Exception):
        return build_default_plan(retrieved)   # never a dead-end
```

## Verification

**Commands:**
- `cd ballast/backend && python -m pytest tests/test_coach_pipeline.py -q` -- expected: all pass; pure tests need no network/credentials; the one async test needs only the local DB (no network, no Anthropic key).
- `cd ballast/backend && python -m pytest -q` -- expected: full suite still green (no regressions; was 184 passing after 4.2).
- `cd ballast/backend && python -c "import coach.pipeline, sys; assert 'anthropic' not in sys.modules"` -- expected: exits 0 (the pipeline never imports the SDK on the fake/default path).
