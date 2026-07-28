---
title: 'Story 4.2 — Recommendation Object & Validation Gate'
type: 'feature'
created: '2026-07-28'
status: 'done'
baseline_revision: '2f104ea394215a131542f07ffabafce6ebae1564'
final_revision: '33fcb8efb2869fa3ad0843c291be271a16f6b2eb'
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-4-context.md'
warnings: [oversized]
---

<intent-contract>

## Intent

**Problem:** Epic 4's trust invariants (FR12 reasoning, FR13 real-evidence backing, FR14 explicit uncertainties) must be *structurally* enforced (NFR2): an unbacked or black-box recommendation has to be physically un-surfaceable, not merely discouraged. There is no Recommendation type and no validation gate yet — the Coach Engine has nothing to bless.

**Approach:** Add the canonical Recommendation value object to `backend/coach/` and a pure, synchronous **validation gate** that is the *only* producer of a `BlessedRecommendation`. The gate takes a composed candidate plus the retrieved evidence set and rejects any candidate missing reasoning, missing uncertainties, or citing an evidence ID absent from the retrieved set — otherwise it resolves the cited IDs to their real `EvidenceRecord`s and returns a frozen blessed object. Because the blessed type cannot be constructed outside the gate, downstream surface/execution code (4.3+) can accept *only* blessed output.

## Boundaries & Constraints

**Always:**
- The Recommendation contract is exactly `{action_label, order_intent?, reasoning, evidence[], uncertainties[]}`; `order_intent` is the optional typed payload `{symbol, side, amount}`. All value objects are `@dataclass(frozen=True)`; money (`amount`) is `Decimal`; `side` is an `OrderSide` enum. Match the existing `precedent/evidence.py` / `llm/port.py` dataclass style.
- `evidence[]` on a candidate is a list of **cited evidence IDs (str)** — the coach cites only IDs it was handed; it never invents a record. The gate resolves each cited ID against the retrieved set and stores the resolved `EvidenceRecord`s on the blessed object (the snapshot 4.9 will persist).
- The gate rejects (raising a typed error, never returning) when: `reasoning` is empty/whitespace (`MissingReasoningError`); `uncertainties` is empty (`MissingUncertaintiesError`); `evidence` is empty OR any cited ID is not in the retrieved set (`UnbackedEvidenceError`). All subclass a `RecommendationValidationError(ValueError)`.
- `BlessedRecommendation` is producible **only** by `validate_recommendation()` — enforced at runtime via a module-private sentinel its `__post_init__` requires, so direct construction elsewhere raises. This is the NFR2 structural teeth (mirrors 4.1's schema-required enforcement).
- The gate is pure and synchronous: no I/O, no DB, no network, no LLM, no wall-clock, no randomness. Fully offline and deterministic; identical inputs give an identical (equal, frozen) blessed object.
- Provide `RECOMMENDATION_OUTPUT_SCHEMA` (a non-empty JSON-Schema `object`, `additionalProperties: false`, required = action_label/reasoning/evidence/uncertainties) as the canonical contract the LLM will emit, plus a tolerant `recommendation_from_output(dict) -> Recommendation` mapper. The mapper never raises on missing keys — it maps to empty fields so the **gate** stays the single rejection point.

**Block If:**
- A stakeholder wants the mandatory-invariant set changed (e.g. `evidence[]` or `uncertainties[]` made optional) — that weakens NFR2 and is a product decision, not an unattended call. HALT.

**Never:**
- No pipeline orchestration, no prompt assembly, no gateway call, no `find_precedent` invocation — those are Story 4.3 (`retrieve → compose → validate → surface`). The gate only *receives* an already-retrieved evidence set.
- No decision-record persistence / co-sign / replay (4.9/4.10), no execution or order-semantics validation of `order_intent` (4.6–4.8), no FastAPI route, no UI.
- No async, no DB access in this story's code or its tests (construct `EvidenceRecord` fixtures in-memory).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Valid recommendation | candidate with non-empty reasoning, ≥1 uncertainty, evidence IDs all present in retrieved set | returns `BlessedRecommendation` with cited IDs resolved to their `EvidenceRecord`s; `action_label`/`order_intent`/`reasoning`/`uncertainties` carried through | No error expected |
| Determinism | same candidate + retrieved set, blessed twice | two `BlessedRecommendation`s compare equal (frozen, order-preserving) | No error expected |
| Missing reasoning | `reasoning` is `""` or whitespace | rejected, no blessed object produced | raise `MissingReasoningError` |
| Missing uncertainties | `uncertainties` is empty | rejected | raise `MissingUncertaintiesError` |
| Fabricated evidence | a cited ID not in retrieved set | rejected | raise `UnbackedEvidenceError` |
| No backing | `evidence` empty | rejected (a recommendation must cite ≥1 real record; the strategy default is always available) | raise `UnbackedEvidenceError` |
| Direct bless attempt | construct `BlessedRecommendation(...)` outside the gate (no sentinel) | construction fails — physically un-surfaceable | raise `RecommendationValidationError` |
| Parse LLM output | valid `LLMResponse.output` dict | `Recommendation` with tuple `evidence`/`uncertainties`, `order_intent` parsed if present else `None` | mapper does not raise |
| Malformed LLM output | dict missing `reasoning`/`uncertainties` | maps to empty fields → subsequent gate call rejects | rejection via gate (see above) |

</intent-contract>

## Code Map

- `ballast/backend/precedent/evidence.py` -- REFERENCE: `EvidenceRecord{id,kind,statement,stats,source,as_of}` (frozen), `EvidenceKind` enum, `to_dict()`. The retrieved set the gate validates against; cited IDs resolve to these.
- `ballast/backend/llm/port.py` -- REFERENCE: frozen-dataclass + `require_output_schema` / `StructuredOutputRequiredError` structural-enforcement style to mirror; `RECOMMENDATION_OUTPUT_SCHEMA` is the schema a future `LLMRequest.output_schema` will use.
- `ballast/backend/coach/__init__.py` -- existing package docstring only; the target module for this story.
- `ballast/backend/coach/recommendation.py` -- NEW: `OrderSide` enum, `OrderIntent`, `Recommendation` (unvalidated candidate), `RECOMMENDATION_OUTPUT_SCHEMA`, `recommendation_from_output()`.
- `ballast/backend/coach/validation.py` -- NEW: `BlessedRecommendation`, module-private gate sentinel, `RecommendationValidationError` + `MissingReasoningError`/`MissingUncertaintiesError`/`UnbackedEvidenceError`, `validate_recommendation(candidate, retrieved) -> BlessedRecommendation`.
- `ballast/backend/tests/test_recommendation_gate.py` -- NEW: every I/O-matrix row + the structural direct-construction guard.

## Tasks & Acceptance

**Execution:**
- [x] `ballast/backend/coach/recommendation.py` -- define `OrderSide(str, Enum)` (`BUY="buy"`, `SELL="sell"`), frozen `OrderIntent(symbol: str, side: OrderSide, amount: Decimal)`, frozen `Recommendation(action_label: str, reasoning: str, evidence: tuple[str, ...], uncertainties: tuple[str, ...], order_intent: OrderIntent | None = None)` (candidate; `evidence` = cited IDs). Add `RECOMMENDATION_OUTPUT_SCHEMA` (JSON-Schema object, `additionalProperties: false`, required `["action_label","reasoning","evidence","uncertainties"]`, `evidence`/`uncertainties` as string arrays, nested optional `order_intent`) and tolerant `recommendation_from_output(output: dict) -> Recommendation` (`.get` with empty defaults, tuple-ize lists, parse `order_intent` to `OrderIntent` only when the key is a dict; never raise on missing keys).
- [x] `ballast/backend/coach/validation.py` -- define `RecommendationValidationError(ValueError)` and the three subclasses; a module-private sentinel (`_GATE_KEY = object()`); frozen `BlessedRecommendation(action_label, order_intent, reasoning, evidence: tuple[EvidenceRecord, ...], uncertainties, _gate_key=None)` whose `__post_init__` raises `RecommendationValidationError` unless `_gate_key is _GATE_KEY` (`_gate_key` field `repr=False, compare=False`); and `validate_recommendation(candidate: Recommendation, retrieved: Sequence[EvidenceRecord]) -> BlessedRecommendation` that checks reasoning → uncertainties → evidence (non-empty + every cited ID in `{r.id for r in retrieved}`), resolves cited IDs to their records preserving cite order, and constructs the blessed object with `_gate_key=_GATE_KEY`.
- [x] `ballast/backend/tests/test_recommendation_gate.py` -- cover every I/O-matrix row: valid→blessed (evidence resolved to `EvidenceRecord`s, order preserved, fields carried), determinism/equality, each of the three rejection errors (incl. empty-evidence and whitespace-reasoning), direct-construction guard raises, `recommendation_from_output` round-trip (valid + malformed→gate-rejects), and that `RECOMMENDATION_OUTPUT_SCHEMA` is a non-empty `object` schema with the four required fields. Build `EvidenceRecord` fixtures in-memory (no DB).

**Acceptance Criteria:**
- Given a composed Recommendation, when it passes through the validation gate, then any candidate missing reasoning, missing uncertainties, or citing evidence not in the retrieved set is rejected (typed error, no object returned) and cannot be surfaced (FR12, FR13, FR14, NFR2, AD-2, AD-3).
- Given a valid candidate, when blessed, then the returned `BlessedRecommendation` carries the cited evidence resolved to the real retrieved `EvidenceRecord`s (the snapshot 4.9 persists), and blessing is pure/deterministic (equal on repeat, offline, no DB/network/LLM).
- Given any module other than the gate, when it attempts to construct a `BlessedRecommendation`, then construction raises — the blessed type is producible only by `validate_recommendation()` (structural NFR2 enforcement).

## Spec Change Log

(No bad_spec loopbacks — empty.)

## Review Triage Log

### 2026-07-28 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 3 (high 0, medium 2, low 1)
- defer: 0
- reject: 10
- addressed_findings:
  - `[medium]` `[patch]` `recommendation_from_output` raised on malformed *values* (unknown `side` → `ValueError`, non-numeric `amount` → `decimal.InvalidOperation`, bare-string `evidence` char-split, non-iterable `evidence`/`uncertainties` → `TypeError`), contradicting the spec's stated design (tolerant mapper; the gate is the single rejection point). Fixed: added `_as_str_tuple` (wraps a bare string, drops non-iterables to `()`), wrapped `OrderIntent` coercion in `try/except (ValueError, ArithmeticError)` dropping a malformed intent to `None`, and coerced scalar fields with `or ""` so `None`→`""`. Now never raises; malformed output reaches the gate as empty fields.
  - `[medium]` `[patch]` The uncertainties gate tested only tuple non-emptiness, so `("  ", "")` was blessed — weaker FR14 teeth than `reasoning` (which is `.strip()`-checked). Fixed: require `any(u and u.strip() ...)`.
  - `[low]` `[patch]` Duplicate cited evidence IDs were multiplied into the resolved evidence tuple (inflating the 4.9 snapshot). Fixed: resolve over `dict.fromkeys(candidate.evidence)` to dedupe while preserving first-cite order.
  - Rejected (10, dropped): action_label non-emptiness (outside the three spec-scoped invariants); duplicate-`retrieved`-ID last-writer-wins and gate-side non-string/unhashable cited values (unreachable — `find_precedent` returns one record and evidence arrives schema-validated); unhashability of blessed/`EvidenceRecord` (no consumer hashes them; pre-existing); schema lacks `minItems`/`minLength` (by design — the runtime gate is the teeth); error-message interpolation and strategy-default wording (cosmetic); missing kind/relevance checks (4.3/4.6 scope); `dataclasses.replace`/`deepcopy`/`pickle` re-bless bypass (not reachable by the LLM — NFR2's threat actor — and closing it needs an out-of-band-identity redesign beyond the spec's field-sentinel mechanism).

## Design Notes

The un-surfaceable teeth are the private-sentinel guard, mirroring how 4.1 made a schema-less LLM call un-issuable: only code holding `_GATE_KEY` (i.e. the gate, same module) can build a blessed object.

```python
_GATE_KEY = object()  # module-private; never exported

@dataclass(frozen=True)
class BlessedRecommendation:
    action_label: str
    order_intent: OrderIntent | None
    reasoning: str
    evidence: tuple[EvidenceRecord, ...]
    uncertainties: tuple[str, ...]
    _gate_key: object = field(default=None, repr=False, compare=False)
    def __post_init__(self):
        if self._gate_key is not _GATE_KEY:
            raise RecommendationValidationError("only validate_recommendation() may bless")
```

The gate is the single rejection point: `recommendation_from_output` maps tolerantly (malformed LLM JSON → empty fields → gate rejects), so there is exactly one place trust invariants are enforced. `evidence` must be non-empty because the Precedent Engine always returns at least a `strategy` record (never a dead-end), so a legitimate recommendation always has ≥1 backing to cite.

## Verification

**Commands:**
- `cd ballast/backend && python -m pytest tests/test_recommendation_gate.py -q` -- expected: all tests pass, zero network, zero DB, zero credentials.
- `cd ballast/backend && python -m pytest -q` -- expected: full suite still green (no regressions).

**Manual checks:**
- Confirm the direct-construction guard: instantiating `BlessedRecommendation(...)` from any module other than `coach/validation.py` raises `RecommendationValidationError` (covered by the structural test).

## Auto Run Result

Status: done

**Summary of implemented change:** Added the Coach Engine's Recommendation contract and its validation gate — the NFR2 structural teeth for FR12/FR13/FR14. `coach/recommendation.py` defines the frozen value objects (`OrderSide`, `OrderIntent`, the unvalidated `Recommendation` candidate whose `evidence` is a tuple of cited evidence-ID strings), the canonical `RECOMMENDATION_OUTPUT_SCHEMA` the LLM will emit against, and a tolerant `recommendation_from_output` mapper. `coach/validation.py` defines `BlessedRecommendation` (producible ONLY through the gate via a module-private sentinel checked in `__post_init__`), the `RecommendationValidationError` hierarchy, and the pure/synchronous `validate_recommendation` gate that rejects a candidate missing reasoning, missing an explicit uncertainty, or citing an evidence ID absent from the retrieved set — otherwise resolving cited IDs to their real `EvidenceRecord`s (deduped, cite-order preserved) for the 4.9 snapshot. No DB, network, LLM, or async.

**Files changed:**
- `ballast/backend/coach/recommendation.py` (new) — `OrderSide`/`OrderIntent`/`Recommendation` frozen dataclasses, `RECOMMENDATION_OUTPUT_SCHEMA`, tolerant `recommendation_from_output` + `_as_str_tuple` helper.
- `ballast/backend/coach/validation.py` (new) — `BlessedRecommendation` (sentinel-guarded), `RecommendationValidationError` + `MissingReasoningError`/`MissingUncertaintiesError`/`UnbackedEvidenceError`, `validate_recommendation` gate.
- `ballast/backend/tests/test_recommendation_gate.py` (new) — 22 tests: all I/O-matrix rows, the structural direct-construction guard, and the three review patches (malformed-value tolerance, blank-uncertainty rejection, cited-ID dedup).

**Review findings breakdown:** 3 patches applied (medium: `recommendation_from_output` value-tolerance so it never raises and the gate stays the single rejection point; medium: reject a blank-only uncertainty tuple to match FR14's intent; low: dedupe cited evidence IDs so the snapshot isn't inflated). 0 deferred. 10 rejected (out-of-scope invariants, unreachable-given-upstream-contract paths, by-design schema-vs-gate split, cosmetic wording, and a non-LLM-reachable `replace`/`deepcopy` guard bypass that would need a redesign). 0 intent gaps, 0 spec loopbacks.

**Follow-up review recommended:** false — the three fixes are localized to two small new files, each covered by a new test, with no behavior/API/security/data change beyond input hardening and a slightly stronger FR14 check.

**Verification performed:**
- `python -m pytest tests/test_recommendation_gate.py -q` → 22 passed (zero network, zero DB, zero credentials).
- Full suite `python -m pytest -q` → 184 passed, 1 pre-existing Starlette deprecation warning (no regressions; was 179 before this story).
- Confirmed the gate is the single trust chokepoint: malformed LLM output now maps to empty fields (never raises) and is rejected by the gate; direct `BlessedRecommendation` construction raises.

**Residual risks:** The `BlessedRecommendation` sentinel guard blocks direct construction but not `dataclasses.replace`/`deepcopy`/`pickle` re-blessing — acceptable because the LLM (NFR2's threat actor) has no route to those calls; if a future story hardens the guard against internal misuse it would need out-of-band identity tracking. Order-intent *semantics* (side/amount validity, v1 scope) are intentionally unvalidated here — that is Story 4.6–4.8. The prompt assembly, gateway call, and `find_precedent` wiring that produce and retrieve real candidates are Story 4.3.
