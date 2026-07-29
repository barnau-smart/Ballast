---
title: 'Story 4.5 — Self-Destructive-Move Warnings'
type: 'feature'
created: '2026-07-28'
status: 'done'
baseline_revision: '1616d15a1ca897f525da1f6b81881e80533ceb5e'
final_revision: 'f8e64a20de95cd83f567aa05b861bf216af5ecdf'
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-4-context.md'
warnings: ['oversized']
---

<intent-contract>

## Intent

**Problem:** The coach does not yet honor FR11: when a user initiates a rash, self-destructive move — a panic sell (selling into a live drawdown), over-concentration (a buy that pushes one holding to a large share of the portfolio), or an oversized lump (a contribution that dwarfs the current portfolio) — nothing detects the risk or warns about it. The coach must flag the risk honestly and explain it, but **never block**: it advises, the user decides.

**Approach:** Detect self-destructive moves deterministically from the user's decision plus the retrieved precedent and (when available) a portfolio snapshot, then make the coach WARN about them within the existing single `reasoning` field — via (a) an FR11 rule in `COACH_SYSTEM_PROMPT` and a risk signal added to the composed request (LLM path) and (b) code-authored warning content in the deterministic default plan (fallback path). No new `Recommendation` field, no new gate rule, no block, and any `order_intent` carries through unchanged. Detection is a pure function; the pipeline accepts an optional portfolio snapshot so warnings surface when context is provided, staying backend-only and offline-tested like Stories 4.1–4.4.

## Boundaries & Constraints

**Always:**
- The warning lives as CONTENT in the existing `Recommendation.reasoning` (and may lead `action_label` with a calm caution) — the one-field subsystem from 4.4. No new schema field; `RECOMMENDATION_OUTPUT_SCHEMA` and `validate_recommendation` are unchanged (the gate stays structural).
- **Never block.** A detected self-destructive move only adds honest warning content; it never refuses, never rejects, never strips or mutates a present `order_intent`, and never prevents blessing. FR11: the coach advises, the user decides.
- Warnings are **honest and calm, never alarmist** and never condescending — coach voice (patient, warm, plain). The warning explains *why* the move is risky in plain English, then leaves the choice to the user.
- Detection is **pure and deterministic**: identical `(decision, retrieved, portfolio)` → identical warnings and, through them, an identical frozen default plan (no I/O, wall-clock, or randomness). Money is `Decimal`, never binary float.
- The default plan still cites every retrieved evidence ID, carries `order_intent=None`, and carries ≥1 explicit honest uncertainty; when warning thresholds are used, an uncertainty states they are coach heuristics, not guarantees. Warning content does NOT go in the `uncertainties` slot (that slot states genuine unknowns only — carried lesson from 4.4).
- All ACs pass offline with the default fake gateway (zero network, zero credentials); backward compatible — a `CoachDecision` with no `side`/no portfolio yields no warnings and behaves exactly as before.

**Block If:**
- A stakeholder wants a self-destructive move to BLOCK, refuse, or gate execution — that contradicts FR11 ("never block") and the architecture ("the coach advises, the user decides"). HALT.
- A stakeholder wants warnings enforced via a NEW `Recommendation` field, a new gate rule, or a separate warning subsystem — that contradicts the frozen structural gate (4.2/4.3) and the one-field mandate (4.4). HALT.
- Satisfying any AC would require a live Anthropic call (real key/network/paid tokens). HALT.

**Never:**
- No new `Recommendation` field; no change to `RECOMMENDATION_OUTPUT_SCHEMA` or `validate_recommendation`; no new evidence kinds; no taught/warned-quality gate check.
- No live portfolio fetch, `Scope` threading, or degraded-mode/all-cash handling inside the coach pipeline — the pipeline reads an OPTIONAL portfolio snapshot passed by the caller; sourcing/threading the live portfolio is Story 4.6's ask→approve surface concern.
- No coach-card UI, "warning callout" component, or recommendation-serving route — 4.5 is backend content, consistent with 4.1–4.4 staying route-less; 4.6 renders the warning.
- No order execution, Broker Port call, `order_intent` *semantics* validation, decision-record persistence, co-sign, or replay (4.6–4.10). Carry any LLM-emitted `order_intent` through unchanged.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Panic-sell warned (fallback) | `side="sell"`, retrieved set contains an event-precedent (live drawdown); LLM path fails | default plan blessed; `reasoning` warns honestly about selling into a downturn and explains the risk (calm, not alarmist); cites all retrieved IDs; ≥1 uncertainty; `order_intent=None`; **not blocked** | Fallback, no raise |
| Over-concentration warned | `side="buy"`, portfolio where this `symbol` would exceed the concentration threshold post-trade | warning content present in `reasoning`; recommendation still blessed; **not blocked** | No error expected |
| Oversized-lump warned | `side="buy"`, `amount` exceeds the oversized-lump threshold vs. portfolio value | warning content present in `reasoning`; still blessed; **not blocked** | No error expected |
| No rash move | `side="buy"`, small amount, diversified portfolio (or no portfolio + no drawdown) | no warning content added; behavior identical to pre-4.5 | No error expected |
| Never blocks with order_intent | any detected move + LLM emits a valid `order_intent` | blessed recommendation surfaced with `order_intent` intact and unchanged; nothing refused/stripped | No error expected |
| Prompt carries FR11 directive | `compose_request(decision, retrieved).system` | contains the "warn honestly and explain the risk but never block" rule | No error expected |
| Request carries risk signal | `compose_request(decision, retrieved, warnings=<detected>).user_content` | includes the detected risk descriptors so the LLM can warn | No error expected |
| Determinism | same `(decision, retrieved, portfolio)` through detection + `build_default_plan` twice | equal detected warnings; two equal (frozen) `BlessedRecommendation`s | No error expected |

</intent-contract>

## Code Map

- `ballast/backend/coach/pipeline.py` -- MODIFY: extend `CoachDecision` with an optional `side: Literal["buy","sell"] | None = None` (the direction of the action the user is contemplating; backward compatible). Add pure, deterministic `detect_self_destructive_moves(decision, retrieved, portfolio=None) -> tuple[...]` plus named threshold constants (concentration share, oversized-lump multiple) with documented rationale. Add an FR11 warning rule to `COACH_SYSTEM_PROMPT`. Thread detected warnings into `compose_request(decision, retrieved, warnings=())` (embed a risk signal in `user_content`) and `build_default_plan(retrieved, warnings=())` (author calm warning content into `reasoning`; add the heuristic-uncertainty when thresholds fire). Compute warnings in `run_coach_pipeline` (new optional `portfolio` kw-arg) and pass them to compose + fallback. No gate/schema change; `surface`/`is_hard_reasoning`/`validate_recommendation` control flow otherwise untouched.
- `ballast/backend/brokers/portfolio.py` -- REFERENCE: `PortfolioView(holdings: list[PortfolioCache], cash: Decimal, as_of)` and `PortfolioCache(symbol, quantity, market_value, cost_basis, ...)`. Coach READS these fields for concentration/lump math (type import only; no live fetch inside the pipeline). Unchanged.
- `ballast/backend/coach/recommendation.py` -- REFERENCE: frozen `Recommendation(action_label, reasoning, evidence, uncertainties, order_intent=None)` + `RECOMMENDATION_OUTPUT_SCHEMA`. Unchanged.
- `ballast/backend/coach/validation.py` -- REFERENCE: `validate_recommendation` + `BlessedRecommendation` (structural gate). Unchanged.
- `ballast/backend/precedent/__init__.py` -- REFERENCE: `find_precedent`, `EvidenceRecord`, `EvidenceKind` (event-precedent presence = live drawdown signal for panic-sell). Unchanged.
- `ballast/backend/tests/test_coach_pipeline.py` -- MODIFY/EXTEND: add FR11 tests using existing `_strategy_record`/`_event_record`/`_decision`/`_CitingGateway` fixtures. Reuse `PortfolioView`/`PortfolioCache` (build a small fixture) for concentration/lump cases. Keep all existing tests green.

## Tasks & Acceptance

**Execution:**
- [x] `ballast/backend/coach/pipeline.py` -- Add optional `side` to `CoachDecision` (frozen, default `None`); implement `detect_self_destructive_moves(decision, retrieved, portfolio=None)` as a pure function returning deterministic warning descriptors for panic-sell (`side="sell"` while an event-precedent/live drawdown is present), over-concentration (`side="buy"` pushing `symbol` past the concentration threshold of portfolio value), and oversized-lump (`side="buy"` with `amount` past the lump threshold vs. portfolio value); define named threshold constants with rationale comments. -- Deterministic FR11 detection with no I/O.
- [x] `ballast/backend/coach/pipeline.py` -- Append an FR11 rule to `COACH_SYSTEM_PROMPT`: when the request flags a potentially self-destructive move, warn honestly and explain the risk in plain, calm English, then leave the decision to the user — never refuse or block, never alarmist. Thread detected warnings into `compose_request` (risk signal in `user_content`) and into `build_default_plan` (calm code-authored warning in `reasoning`; add the heuristic-limitation uncertainty when a threshold fired, keeping the uncertainty slot honest). Compute warnings in `run_coach_pipeline` via a new optional `portfolio` kw-arg and pass them to both paths. -- Surfaces the warning on both LLM and fallback paths without blocking, without a new field, and without touching the gate.
- [x] `ballast/backend/tests/test_coach_pipeline.py` -- Add coverage: (a) panic-sell default-plan reasoning warns about selling into a downturn, still blesses, cites all IDs, `order_intent=None`, ≥1 honest uncertainty; (b) over-concentration and (c) oversized-lump produce warning content given a `PortfolioView` fixture; (d) no rash move → no warning content (pre-4.5 behavior); (e) never-block canary — a detected move plus a `_CitingGateway` emitting a valid `order_intent` still yields a blessed recommendation with `order_intent` intact; (f) `compose_request(...).system` carries the FR11 directive and `compose_request(..., warnings=<detected>).user_content` carries the risk signal; (g) determinism — equal warnings and equal frozen default plans for identical inputs; (h) a no-new-field/no-schema-change canary. Keep all existing tests green. -- Locks FR11 structurally.

**Acceptance Criteria:**
- Given a user initiates a rash action (panic sell, over-concentration, or oversized lump), when the coach reviews it, then the surfaced recommendation warns and explains the risk honestly and calmly in its `reasoning` while **never blocking** — any `order_intent` carries through unchanged, the recommendation is still blessed, and the user decides (FR11).
- Given no confident special call and/or an LLM-path failure, when the deterministic default plan is returned for a detected self-destructive move, then its `reasoning` carries the calm honest warning, it stays deterministic and coach-voiced, cites every retrieved evidence ID with `order_intent=None`, and carries ≥1 explicit honest uncertainty (thresholds noted as heuristics, never smuggled benefit claims).
- Given the default (fake gateway, offline) configuration, when the whole suite runs, then all coach ACs pass with zero credentials and zero network, the Recommendation schema and validation gate are unchanged, no new field/subsystem/route/UI was added, and a `CoachDecision` without `side`/portfolio behaves exactly as it did before 4.5.

## Design Notes

- **Why detection is deterministic and prompt+content-driven, not a gate rule:** the structural gate was frozen in 4.2/4.3 and is deliberately quality-agnostic (it checks reasoning/uncertainty non-empty and evidence backed — not *what* the reasoning says). Like 4.4's FR18 teaching, FR11 warning is enforced on the LLM path by the prompt + a risk signal in the request, and on the fallback path by code-authored content — so a warning always surfaces even when the model is bypassed, without widening the gate.
- **Threshold heuristics (v1, tunable):** over-concentration and oversized-lump use named `Decimal` constants (e.g. a single-position share ceiling and a lump-vs-portfolio-value multiple). They are coach heuristics, not guarantees — the default plan states this as an honest uncertainty when a threshold fires. Panic-sell needs no portfolio: `side="sell"` while a live drawdown (event-precedent) is present is the signal.
- **Golden shape (illustrative, not prescriptive) — panic-sell default-plan reasoning:** leads calm and honest, e.g. "Before you sell: markets are down right now, and selling after a fall locks in the loss and takes you out of the recovery the record shows tends to follow. Here's what the precedent says… This is your call — here's the risk so you can weigh it." Warning in `reasoning`/`action_label`; the uncertainty slot stays a genuine unknown ("past recoveries never guarantee a future one").
- **Hand-off to Story 4.6 (ask→approve surface):** 4.6 assembles the full user action (structured `order_intent`) + live portfolio/scope and renders the warning in the coach card. 4.5 makes the warning *content* exist deterministically so 4.6 has something honest to render; it does not fetch the live portfolio or build UI. Because 4.5 precedes 4.6, no recommendation route exists yet — 4.5 stays backend-only.

## Verification

**Commands:**
- `cd ballast/backend && python -m pytest tests/test_coach_pipeline.py -q` -- expected: all pass; offline, no network/credentials.
- `cd ballast/backend && python -m pytest -q` -- expected: full suite green (was 204 passing after 4.4; no regressions; new FR11 tests added).
- `cd ballast/backend && python -c "import coach.pipeline, sys; assert 'anthropic' not in sys.modules"` -- expected: exit 0 (no SDK import on the fake/default path).

## Spec Change Log

## Review Triage Log

### 2026-07-28 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 2: (high 0, medium 0, low 2)
- defer: 0
- reject: 20
- addressed_findings:
  - `[low]` `[patch]` The default-plan heuristic uncertainty enumerated both "concentration and lump-size thresholds" even when only one threshold fired, naming a threshold that didn't trip. Reworded to "The thresholds behind this caution are coach heuristics, not guarantees…" so it no longer references a non-firing threshold (uncertainty still states a genuine unknown only).
  - `[low]` `[patch]` `run_coach_pipeline`'s new optional `portfolio` param — the public entrypoint seam (portfolio→detect→surface) that Story 4.6 will call — had no test; the individual pieces were covered but not the wiring. Added `test_run_coach_pipeline_threads_portfolio_into_fr11_warning` (offline, DB-seeded like the existing e2e tests): a concentrated buy through the fake gateway surfaces the over-concentration warning end to end, and the same call with no portfolio surfaces none — pinning the seam both directions.
- rejected (not this story's problem / noise): panic-sell "selling into a downturn" copy called factually wrong (MISREAD — the Precedent Engine only surfaces an `EVENT_PRECEDENT` when the *current* drawdown matches a historical band, so its presence IS the live-drawdown signal, consistent with the recovery view); tiny-dip event-precedents overstating the downturn (the deferred 3-2 magnitude-band-floor item, already logged); all-cash / `total_value == 0` suppressing the lump warning (the deferred AD-14 cash-only cache gap, already logged; a buy into an empty portfolio has nothing to dwarf); negative `amount`/`market_value`/`cash`, margin/short accounts (out of v1 long-only index-fund scope — no shorting/margin); `market_value` None-crash (the column is non-null `Mapped[Decimal]`); detection running outside `surface`'s try/except (detection is pure over frozen/validated inputs and cannot raise for valid input — matches the module's deliberate "let real bugs surface" design for `build_default_plan`); symbol case/whitespace mismatch (symbols are canonical uppercase system-wide; normalization is the caller/cache contract); strict-`>` threshold boundary untested (measure-zero equality, immaterial to the user); LLM-path warning language untested (a model obeying prompt rule 6 is not offline-testable — the accepted 4.4-style prompt-enforced design; we test the directive + risk signal are present); multi-warning run-on prose and prompt-whitespace artifacts (acceptable calm prose / cosmetic, LLM-insensitive); `PANIC_SELL from_threshold=False` (already hedged by the base "past patterns never guarantee" uncertainty; `from_threshold` is specifically the numeric-heuristic caveat); `WarningKind` string values unused (harmless, forward-looking for 4.6 rendering); determinism sum-order (Decimal addition is commutative in value); empty `w.risk` dangling text (all risk strings are hardcoded non-empty, unreachable).

## Auto Run Result

Status: done

**Summary of implemented change:** Delivered FR11 (self-destructive-move warnings) entirely within the coach's existing single `reasoning` field — no new `Recommendation` field, no schema/gate change, no new route or UI, and **never a block**. Added an optional `side` to `CoachDecision`; a pure, deterministic `detect_self_destructive_moves(decision, retrieved, portfolio=None)` that flags panic-sell (a sell while a live drawdown / `EVENT_PRECEDENT` is present — no portfolio needed), over-concentration (a buy pushing one holding past a 40% share of portfolio value), and oversized-lump (a buy amount exceeding 50% of portfolio value), with named `Decimal` thresholds; an FR11 rule in `COACH_SYSTEM_PROMPT`; and threading of detected warnings into both `compose_request` (a calm risk signal in `user_content`) and the code-authored `build_default_plan` (a calm warning that leads `reasoning`/`action_label`, plus an honest heuristic-limitation uncertainty when a numeric threshold fires). `run_coach_pipeline` gained an optional caller-supplied `portfolio` snapshot; it never fetches live portfolio/Scope or handles degraded/all-cash (Story 4.6's ask→approve concern). Any LLM-emitted `order_intent` carries through unchanged. Backward compatible: a decision with no `side`/portfolio behaves exactly as pre-4.5.

**Files changed:**
- `ballast/backend/coach/pipeline.py` (MODIFIED) — `CoachDecision.side`; `WarningKind`/`MoveWarning`; `detect_self_destructive_moves` + threshold constants; FR11 rule appended to `COACH_SYSTEM_PROMPT`; `compose_request`/`build_default_plan`/`surface` take detected `warnings`; `run_coach_pipeline` takes an optional `portfolio`. Gate/schema untouched. (Review patch: generalized the heuristic-uncertainty wording.)
- `ballast/backend/tests/test_coach_pipeline.py` (MODIFIED) — 14 FR11 tests: panic-sell/over-concentration/oversized-lump warning content, no-rash-move + backward-compat no-warning parity, portfolio-required guard, never-block canary (order_intent intact), prompt directive + request risk signal, determinism, honest-uncertainty (no smuggled benefit), no-new-field/no-schema canary, Decimal thresholds, and the `run_coach_pipeline` portfolio-seam integration test (review patch).

**Review findings breakdown:** 2 low patches applied (heuristic-uncertainty wording no longer names a non-firing threshold; added the untested public `run_coach_pipeline` portfolio-seam test). 0 intent gaps, 0 bad-spec loopbacks, 0 deferred. 20 findings rejected — chiefly a misread of the panic-sell "downturn" signal (the `EVENT_PRECEDENT` presence IS the current-drawdown signal), items belonging to already-deferred gaps (3-2 band floor, AD-14 cash-only), out-of-v1-scope inputs (margin/short/negative amounts), a non-null column (no crash), and accepted prompt-enforced-LLM design (not offline-testable).

**Verification performed:**
- `.venv/bin/python -m pytest tests/test_coach_pipeline.py -q` → 34 passed (was 20 after 4.4; +14).
- `.venv/bin/python -m pytest -q` (full suite) → 218 passed, 0 failed (was 204 after 4.4; +14; zero regressions; 1 pre-existing unrelated Starlette deprecation warning).
- `.venv/bin/python -c "import coach.pipeline, sys; assert 'anthropic' not in sys.modules"` → offline OK (no SDK import on the fake/default path).
- `git status --short` → only `coach/pipeline.py` and `tests/test_coach_pipeline.py` modified; `uv.lock` reverted after reviewers' `uv run` drift (per the known drift gotcha).

**Follow-up review recommendation:** false — the review pass made two localized, low-consequence fixes (a wording generalization in a deterministic string and one additive offline test); no behavior, API, security, or data change to the core logic.

**Residual risks:** FR11 on the LLM path is enforced by the system prompt (rule 6), not the structural gate — a model ignoring it would still bless, though the deterministic default plan is guaranteed to warn. The over-concentration/oversized-lump thresholds (40% / 50%) are v1 coach heuristics (documented, tunable) and only fire when the caller supplies a portfolio snapshot; the panic-sell signal inherits the Precedent Engine's deferred magnitude-band-floor sensitivity (3-2). Live portfolio/Scope fetching, degraded/all-cash handling (AD-14), and rendering the warning in the coach card are deliberately Story 4.6's ask→approve surface.
