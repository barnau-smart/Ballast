---
title: 'Story 4.4 — Just-in-Time Teaching'
type: 'feature'
created: '2026-07-28'
status: 'done'
baseline_revision: '198e8b9588d3e24730e0193932d2b4cacb373311'
final_revision: '56cb1454710c6a2734d15f31119a5265c55605a7'
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-4-context.md'
warnings: ['oversized']
---

<intent-contract>

## Intent

**Problem:** Story 4.3 wired the coach pipeline so every recommendation carries a non-empty `reasoning` field (FR12), but the reasoning does not yet fulfil FR18: it does not reliably teach the *principle and mechanics behind the action* tied to the live decision ("why consistent index buying, why this isn't market timing"). The code-authored default plan says "stick to your plan" without teaching *why staying the course works*. Per the architecture, teaching (FR18) and reasoning (FR12) are one subsystem — the single `reasoning` field — not two.

**Approach:** Strengthen the existing one-field reasoning subsystem so `reasoning` always teaches the principle + mechanics in plain English tied to the live decision, written in layers (the immediate "why" leads; the deeper lesson follows) so a future coach card can progressively disclose it ("explain more") without interrupting. Concretely: add an FR18 teaching directive to `COACH_SYSTEM_PROMPT`, and enrich the deterministic `build_default_plan` reasoning to teach the principle + mechanics — with no new schema field, subsystem, route, second LLM call, or UI (the coach card + "explain more" expander are Story 4.6's ask→approve surface).

## Boundaries & Constraints

**Always:**
- Teaching lives in the ONE `reasoning` field of the existing Recommendation subsystem — architecture: "reasoning IS the just-in-time teaching — one field, not a separate subsystem"; FR18 and FR12 are "the same engine viewed two ways ... one subsystem, not two."
- `reasoning` must teach the *principle and mechanics behind the recommended action* in plain English tied to THIS decision (FR18), leading with the immediate "why" then the deeper lesson, so progressive disclosure over the same field is possible without interrupting.
- The code-authored `build_default_plan` reasoning stays pure and deterministic (identical retrieved set → equal frozen blessed object; no I/O, wall-clock, or randomness), coach-voice (patient, warm, honest, plain; never hype, condescending, or alarmist), cites every retrieved evidence ID, `order_intent=None`, carries ≥1 explicit uncertainty, and still passes the gate.
- FR18 teaching is enforced via the prompt (LLM path) and the code-authored content (default plan) — NOT via a new gate rule. The validation gate and `RECOMMENDATION_OUTPUT_SCHEMA` are unchanged.
- All ACs pass offline with the default fake gateway (zero network, zero credentials); the fake still fails the gate on placeholder IDs and falls back to the default plan.

**Block If:**
- A stakeholder wants FR18 teaching to require a NEW recommendation field or a separate teaching subsystem / second LLM roundtrip — that contradicts the architecture ("one field, one subsystem") and is a product/architecture decision. HALT.
- Satisfying any AC would require a live Anthropic call (real key/network/paid tokens). HALT.

**Never:**
- No new `Recommendation` field; no change to `RECOMMENDATION_OUTPUT_SCHEMA` or `validate_recommendation` (the gate stays structural — reasoning/uncertainty non-empty, evidence backed); no new evidence kinds.
- No coach-card UI, no "explain more" expander component, and no recommendation-serving route/endpoint — those are Story 4.6's ask→approve surface (which renders reasoning-as-teaching with progressive disclosure, the precedent data-block, the uncertainty callout, and the co-sign zone). 4.4 is backend content, consistent with 4.1–4.3 staying route-less; 4.4/4.5 precede 4.6, so no recommendation route exists yet.
- No self-destructive-move warnings (4.5); no decision-record persistence, co-sign, or replay (4.9/4.10); no order-intent *semantics* validation or Broker Port call — carry any LLM-emitted `order_intent` through unchanged, exactly as 4.3 left it.
- No second LLM roundtrip for "explain more"; teaching is produced within the single existing coach call.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Default plan teaches | any retrieved set; LLM path fails to bless | default plan blessed; `reasoning` teaches the principle + mechanics (the *why it works* — not merely "stick to your plan"); ≥1 uncertainty; cites all retrieved IDs; `order_intent=None` | Fallback, no raise |
| Determinism preserved | same retrieved set through `build_default_plan` twice | two equal (frozen) `BlessedRecommendation`s (enriched reasoning is static; no wall-clock/RNG) | No error expected |
| LLM teaching surfaced | stub gateway emits reasoning citing a real retrieved ID with teaching content + an uncertainty | the LLM's blessed recommendation is surfaced (not the default); `reasoning` carried through verbatim | No error expected |
| Prompt carries FR18 directive | `compose_request(decision, retrieved).system` | contains the principle-and-mechanics teaching instruction tied to the live decision | No error expected |
| No regression | full suite | all prior coach / gate / pipeline tests still green; gate + schema unchanged | No error expected |

</intent-contract>

## Code Map

- `ballast/backend/coach/pipeline.py` -- MODIFY: `COACH_SYSTEM_PROMPT` (add the FR18 teaching directive as an additional rule) and `build_default_plan` (enrich the code-authored `reasoning` to teach principle + mechanics; keep determinism, gate-pass, the uncertainty, `order_intent=None`, and citing all retrieved IDs). No signature or control-flow changes; `surface`/`run_coach_pipeline`/`is_hard_reasoning`/`compose_request` untouched.
- `ballast/backend/coach/recommendation.py` -- REFERENCE: frozen `Recommendation(action_label, reasoning, evidence, uncertainties, order_intent=None)` + `RECOMMENDATION_OUTPUT_SCHEMA` (required: action_label/reasoning/evidence/uncertainties). Unchanged.
- `ballast/backend/coach/validation.py` -- REFERENCE: `validate_recommendation` + `BlessedRecommendation` + gate sentinel. Unchanged (gate stays structural; no taught-quality check).
- `ballast/backend/llm/port.py`, `factory.py`, `fake_adapter.py` -- REFERENCE: `LLMGateway` (sync `complete`), `get_llm_gateway()` (default `FakeLLMGateway`), fake emits placeholder evidence IDs → gate reject → default plan. Unchanged.
- `ballast/backend/precedent/__init__.py` -- REFERENCE: `find_precedent`, `EvidenceRecord` (frozen; `to_dict()`), `EvidenceKind`. Unchanged.
- `ballast/backend/tests/test_coach_pipeline.py` -- MODIFY/EXTEND: add FR18 teaching assertions (default-plan reasoning teaches principle+mechanics; `compose_request(...).system` carries the FR18 directive) alongside/strengthening the existing default-plan test; keep all existing tests green. Reuse the existing `_strategy_record()`/`_event_record()`/`_decision()` fixtures and `_CitingGateway` stub style.

## Tasks & Acceptance

**Execution:**
- [x] `ballast/backend/coach/pipeline.py` -- In `COACH_SYSTEM_PROMPT`, add an FR18 teaching rule (append to the existing numbered rules; do NOT alter the "cite only provided IDs / never invent numbers / always state ≥1 uncertainty / never a black box" rules): the reasoning must explain, in plain English, the *principle and mechanics behind the recommended action* tied to THIS decision (e.g. why consistent index investing, why this is not market timing), leading with the immediate "why" and then the deeper lesson so the user can read on without being interrupted — patient and warm, never jargon, hype, or alarm. -- Makes the LLM-path reasoning fulfil FR18 within the single reasoning subsystem.
- [x] `ballast/backend/coach/pipeline.py` -- Enrich `build_default_plan`'s code-authored `reasoning` so it teaches the principle + mechanics of the default plan (the *why it works*: time in the market tends to beat timing it; steady, regular contributions compound; reacting to short-term noise usually costs more than it helps) in plain, warm, layered prose that leads with the immediate call then teaches — while keeping it deterministic (no wall-clock/RNG), coach-voice, gate-passing, `order_intent=None`, citing every retrieved evidence ID, and preserving ≥1 explicit uncertainty (keep or extend the existing uncertainty string, still deterministic). -- Fulfils FR18 for the always-available default plan; never a bare "stick to your plan."
- [x] `ballast/backend/tests/test_coach_pipeline.py` -- Extend coverage: (a) the default-plan `reasoning` teaches principle+mechanics — assert it references the mechanics (e.g. timing / time-in-market / consistent-contribution wording), is materially longer than a bare directive, still blesses, cites all retrieved IDs, `order_intent=None`, ≥1 non-blank uncertainty; (b) determinism — two `build_default_plan(retrieved)` results are equal; (c) `compose_request(decision, retrieved).system` contains the FR18 principle-and-mechanics teaching directive; (d) an `LLMGateway` stub emitting teaching-shaped reasoning citing a real retrieved ID is surfaced (not the default) with `reasoning` carried through. Keep all existing tests green. -- Locks FR18 teaching structurally without touching the gate.

**Acceptance Criteria:**
- Given a user reads the coach's LLM-blessed recommendation, when it is surfaced, then its `reasoning` teaches the principle and mechanics behind the action in plain English tied to the live decision (FR18) — because `COACH_SYSTEM_PROMPT` now directs it — with the validation gate and `RECOMMENDATION_OUTPUT_SCHEMA` unchanged and teaching living in the single `reasoning` field.
- Given no confident special call (or any LLM-path failure), when the deterministic default plan is returned, then its `reasoning` teaches *why* staying the course works (principle + mechanics), not merely "stick to your plan," while remaining deterministic, coach-voice, gate-passing, citing every retrieved evidence ID with `order_intent=None` and an explicit uncertainty.
- Given the default (fake gateway, offline) configuration, when the whole suite runs, then all coach ACs pass with zero credentials and zero network, the Recommendation schema and validation gate are unchanged, and no new field, subsystem, route, or UI was added (FR18/FR12 remain one subsystem — one `reasoning` field).

## Spec Change Log

## Review Triage Log

### 2026-07-28 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 3: (high 0, medium 0, low 3)
- defer: 0
- reject: 10
- addressed_findings:
  - `[low]` `[patch]` The enriched default-plan uncertainty smuggled a favorable comparative ("staying the course reduces the odds of a costly mistake") into the FR14 uncertainty slot, which exists to state what the coach does NOT know. Reworded to state only the genuine unknown ("...staying invested cannot promise a positive return"), keeping the uncertainty honest and free of an unbacked benefit claim.
  - `[low]` `[patch]` No test locked AC3's central negative guarantee (no new field / no schema change under FR18). Added `test_teaching_adds_no_new_field_or_schema_change` canary pinning `RECOMMENDATION_OUTPUT_SCHEMA["required"]` and `Recommendation.__dataclass_fields__`, so a future FR18-branded schema widening fails loudly.
  - `[low]` `[patch]` The test module docstring still said "Story 4.3 tests," hiding 4.4's FR18 coverage from a traceability audit. Updated it to note the 4.4/FR18 rows (default-plan teaching, prompt directive, verbatim surfacing, AC3 canary).
- rejected (not this story's problem / noise): equity/index assumption in the default plan (v1 order scope is broad index funds/ETFs — the market-DCA teaching is scope-appropriate); teaching assertions are keyword/length checks (legitimate change-detectors for a deterministic code-authored string; rewriting the test philosophy is churn, not correctness); magic-400 length threshold; prompt-substring "tautology" assertions; the LLM-teaching-surfaced test being close to 4.3's pass-through test (it still pins that 4.4 added no reasoning post-processing); LLM-path FR18 enforced only by the prompt with no gate (the documented, accepted design — 4.3 froze the structural gate); rule-5 prompt verbosity; `build_default_plan(())` empty-retrieved dead-end (already adjudicated/rejected in 4.3 — `find_precedent` guarantees ≥1 and a zero-evidence recommendation must be un-blessable); redundant `timing`/`market timing` assertions; `uv.lock` drift (environmental `uv run` artifact, not in the diff; tree confirmed clean).

## Design Notes

- Architecture mandate (epic-4 context, verbatim): *"reasoning IS the just-in-time teaching — one field, not a separate subsystem"*; PRD addendum: FR18 and FR12 are *"the same engine viewed two ways ... architect them as one subsystem, not two."* So 4.4 enriches the existing reasoning content; it does not add a field, subsystem, route, or second LLM call.
- **Hand-off to Story 4.6 (ask→approve surface):** the coach card renders `reasoning` as always-visible teaching with an "explain more" progressive-disclosure expander that pulls (never interrupts) over this same reasoning field — plus the precedent data-block, uncertainty callout, and co-sign zone, and the recommendation-serving endpoint. 4.4 makes the reasoning teach *in layers* so 4.6's expander has real depth to reveal; 4.6 builds the expander UI and the endpoint. Because 4.4/4.5 precede 4.6, no recommendation route exists yet — 4.4 stays backend-only, consistent with 4.1–4.3.
- The gate is deliberately NOT taught-quality-aware: teaching quality cannot be enforced structurally/deterministically, and 4.3 froze the gate. FR18 is enforced by the prompt (LLM path) and code-authored content (default plan), leaving the structural gate (non-empty reasoning/uncertainty, backed evidence) intact.

## Verification

**Commands:**
- `cd ballast/backend && python -m pytest tests/test_coach_pipeline.py -q` -- expected: all pass; offline, no network/credentials.
- `cd ballast/backend && python -m pytest -q` -- expected: full suite green (was 200 passing after 4.3; no regressions).
- `cd ballast/backend && python -c "import coach.pipeline, sys; assert 'anthropic' not in sys.modules"` -- expected: exit 0 (no SDK import on the fake/default path).

## Auto Run Result

Status: done

**Summary of implemented change:** Fulfilled FR18 (just-in-time teaching) entirely within the single existing `reasoning` field of the coach subsystem — no new field, subsystem, route, second LLM call, or UI (the coach card + "explain more" expander remain Story 4.6's ask→approve surface). Added an FR18 teaching rule to `COACH_SYSTEM_PROMPT` directing the LLM to explain the *principle and mechanics behind the action* tied to the live decision (leading with the immediate "why," then the deeper lesson so it can be read on demand without interrupting), and enriched the deterministic, code-authored `build_default_plan` reasoning so it teaches *why staying the course works* (time in the market vs. timing it; steady regular contributions compounding; reacting to noise usually costs more) instead of a bare "stick to your plan." The Recommendation schema and structural validation gate are unchanged; the default plan stays pure/deterministic, coach-voice, gate-passing, cites every retrieved evidence ID, `order_intent=None`, with ≥1 explicit (honest, non-selling) uncertainty. All ACs pass offline with the fake gateway (zero network, zero credentials).

**Files changed:**
- `ballast/backend/coach/pipeline.py` (MODIFIED) — appended FR18 teaching rule 5 to `COACH_SYSTEM_PROMPT`; enriched `build_default_plan` reasoning to teach principle+mechanics; reworded its uncertainty to state only the genuine unknown (review patch). No signature/control-flow change.
- `ballast/backend/tests/test_coach_pipeline.py` (MODIFIED) — added 4 tests: default-plan reasoning teaches principle+mechanics; `compose_request(...).system` carries the FR18 directive; LLM teaching reasoning surfaced verbatim (not default); AC3 canary pinning no new field / no schema change. Updated the module docstring to note 4.4/FR18 coverage.

**Review findings breakdown:** 3 patches applied (all low: honesty rewording of the default-plan uncertainty so it does not smuggle a favorable comparative into the FR14 slot; an AC3 canary locking "no new field / no schema change"; a stale-docstring/traceability fix). 0 intent gaps, 0 bad-spec loopbacks, 0 deferred. 10 findings rejected as v1-scope-appropriate, already-adjudicated (empty-`retrieved` dead-end, decided in 4.3), accepted-and-documented design (LLM-path FR18 via prompt, gate frozen in 4.3), or test-style opinion on a deterministic code-authored string.

**Verification performed:**
- `.venv/bin/python -m pytest tests/test_coach_pipeline.py -q` → 20 passed.
- `.venv/bin/python -m pytest -q` (full suite) → 204 passed, 0 failed (was 200 after 4.3; +4 tests; zero regressions; 1 pre-existing unrelated Starlette deprecation warning).
- `.venv/bin/python -c "import coach.pipeline, sys; assert 'anthropic' not in sys.modules"` → exit 0 (no SDK import on the fake/default path).
- `git status --short` → only `coach/pipeline.py` and `tests/test_coach_pipeline.py` modified; `uv.lock` clean (a reviewer's `uv run` drift was reverted).

**Follow-up review recommendation:** false — the review pass made three localized, low-consequence fixes (a wording refinement in a deterministic string, one additive canary test, a docstring update); no behavior, API, security, or data change to the core logic.

**Residual risks:** FR18 on the LLM path is enforced by the system prompt, not the structural gate (an accepted, documented design decision carried from 4.3 — the gate stays taught-quality-agnostic); a model that ignores rule 5 would still bless, though the deterministic default plan is guaranteed to teach. The visible "explain more" progressive-disclosure expander and the recommendation-serving endpoint are deliberately deferred to Story 4.6's ask→approve surface (documented hand-off in Design Notes) — 4.4 makes the reasoning teach *in layers* so that expander has real depth to reveal.
