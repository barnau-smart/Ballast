---
title: 'Story 10.3 — Fiduciary-advisor narration + never-invent-a-fact safeguard + the 5 good-lesson tests'
type: 'feature'
created: '2026-08-12'
status: 'done'
baseline_revision: a21901684da4f4cb5cb87091cfe2efc1cca1e634
final_revision: 5c459db8161ccd60bcf5dc011a159c3a906cf03f
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-10-context.md'
  - '{project-root}/_bmad-output/implementation-artifacts/spec-10-2-gap-to-target-deploy-cash-engine.md'
warnings: ['oversized']
---

<intent-contract>

## Intent

**Problem:** Story 10-2 hands back a deterministic "deploy my cash" plan (concrete BUYs toward target), but it is silent — a beginner sees numbers with no plain-English *why*. There is no advisor voice explaining what to do and why, and no safeguard stopping the AI from inventing a number or forecasting the market.

**Approach:** Add an advisor-persona narration layer over the 10-2 plan. The engine still computes every number; the LLM only narrates the *why / tradeoff / prioritization* as situational opinion. Reuse the existing evidence-record + `validate_recommendation` gate (the LLM cites only engine-provided evidence IDs), then add two NEW gates — a **never-invent-a-fact numeric validator** (every number the AI states must be in the engine-provided set) and a **no-forecast validator** — with a deterministic templated fallback that is authored to pass the **5 good-lesson tests** (principle-not-pick, why-generalizes, recognized-best-practice, teaches-the-tradeoff, facts-not-forecast). A new read-only `GET /api/allocation/narration` returns plan + narration; the coach console renders the narration card and still populates-don't-submits.

## Boundaries & Constraints

**Always:**
- **Never invent a fact.** Numbers come only from the 10-2 `Plan`. The narration passes through `validate_recommendation` (reuse: reasoning non-empty, ≥1 uncertainty, ≥1 cited evidence ID in the provided set) PLUS a numeric validator that rejects any figure stated in `reasoning`/`action_label` not numerically equal to an engine-provided fact, PLUS a no-forecast validator. **Any** rejection (or gateway/parse failure) degrades to the deterministic `_fallback_narration` — never a dead-end, never a surfaced unvalidated fact.
- **Opinion, not forecast.** The advisor may opine on the user's situation and settled principles; the no-forecast validator rejects prediction language. No market forecasts, ever.
- **"Nothing to do" is valid.** For every no-action status (`at_target`/`no_cash`/`no_target`/`decide_reserve`) the narration is the deterministic calm `plan.reason` passthrough — **no LLM call, no fabricated move**.
- **Populate, don't submit.** The endpoint places nothing and writes no `decision_record`; it calls `build_plan` (10-2) read-only + narrates. The human co-signs the unchanged `primary_order` through the existing `/approve` spine.
- **Deterministic reuse.** Evidence records are `EvidenceKind.STRATEGY` built purely from the `Plan` via `make_id` (content-addressed). Fake/degraded LLM mode routes to `_fallback_narration` for free (its placeholder cites an unbacked evidence ID → `UnbackedEvidenceError`), so fake mode yields real templated copy.
- **Calm/no-FOMO voice; suite green.** All new copy passes the digest FORBIDDEN-word bar. Money is `Decimal` → fixed-point wire strings (`format_money`); per-user scoped (AD-10); backend `pytest` (ballast_test DB) + frontend `vitest` stay green.

**Block If:**
- The 10-2 `Plan` contract (`allocation.engine.build_plan`/`Plan`/`ActionItem`), the `validate_recommendation` gate, the `EvidenceRecord`/`make_id` contract, or the LLM gateway (`llm.factory.get_llm_gateway`, `LLMRequest`/`complete`) is absent or shaped differently than this spec requires — HALT `blocked` (a real upstream-contract gap, not an unattended guess).

**Never:**
- No selling/trimming, no concentration or cost/fee buckets (Story 10-4). No new order types, no live-broker call, no order placement, no `decision_record` write, no auto-submit. No change to the 10-2 engine math or the `GET /api/allocation/plan` endpoint (10-2 tests depend on it). No teaching layer beyond the single `reasoning` field. No LLM computing or altering any number.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Narrate deploy | `Plan.status="deploy"`; live/fake gateway | `narration` = `{action_label, reasoning, uncertainties[], evidence[]}`; every stated number ∈ engine facts; no forecast language; `plan` unchanged (primary_order populated by UI) | LLM path used |
| Invented number | LLM states a figure not in the engine set | numeric validator rejects → `_fallback_narration` returned (deterministic, all facts real) | graceful fallback |
| Forecast language | LLM predicts the market ("will rise", "outperform") | no-forecast validator rejects → `_fallback_narration` returned | graceful fallback |
| Gateway outage / fake mode | `complete` raises, or fake placeholder cites unbacked id | `validate_recommendation`/exception → `_fallback_narration` (real templated copy) | graceful fallback |
| No-action status | `at_target`/`no_cash`/`no_target`/`decide_reserve` | deterministic narration = calm `plan.reason`; empty `evidence`; NO LLM call, no fabricated move | none |
| Endpoint no-writes | `GET /api/allocation/narration` | returns `{plan, narration}`; writes no `decision_record`, places no order | none |
| Isolation | user A requests narration | only A's scoped plan/holdings used | scoped repo fail-closed |

</intent-contract>

## Code Map

- `ballast/backend/allocation/narrate.py` — **NEW**: pure `build_narration_facts(plan)` (→ `tuple[EvidenceRecord,...]`, STRATEGY kind), `allowed_facts(plan)` (→ numeric allow-set), `check_no_invented_numbers(text, allowed)` + `check_no_forecast(text)` (raise on violation), `_fallback_narration(plan, evidence)` (deterministic templated copy), `compose_narration_request(plan, evidence)`, `narrate_plan(gateway, plan) -> AllocationNarration`, and the `AllocationNarration` dataclass.
- `ballast/backend/allocation/engine.py` — CONSUME: `Plan`, `ActionItem`, `build_plan`, status consts. Do not modify.
- `ballast/backend/coach/validation.py` — CONSUME: `validate_recommendation`, `RecommendationValidationError`. Reuse the gate verbatim.
- `ballast/backend/coach/recommendation.py` — CONSUME: `recommendation_from_output`, `RECOMMENDATION_OUTPUT_SCHEMA` shape (narration schema = same minus `order_intent`).
- `ballast/backend/precedent/evidence.py` — CONSUME: `EvidenceRecord`, `EvidenceKind.STRATEGY`, `make_id`.
- `ballast/backend/strategy/target_allocation.py` — CONSUME: `ASSET_CLASS_LABEL`, `resolve_target` (target weights for the facts).
- `ballast/backend/llm/factory.py` / `llm/port.py` — CONSUME: `get_llm_gateway`, `LLMRequest`, `LLMMessage`. Mirror `coach/suggest.py`'s resilient `narrate_suggestion`.
- `ballast/backend/api/allocation.py` — MODIFY: add `GET /api/allocation/narration` returning `{plan, narration}` (reuse `_plan_out`; serialize evidence via `record.to_dict()`); `gateway = get_llm_gateway()` inside the handler (mirror `api/coach.py:877`).
- `ballast/frontend/src/components/CoachConsult.jsx` — MODIFY: `onDeploy` fetches `/api/allocation/narration`; populate from `data.plan.primary_order` (unchanged logic); render `data.narration` via `<CoachCard recommendation={narration} />`; no-action shows `narration.reasoning`.
- `ballast/frontend/src/components/CoachCard.jsx` — CONSUME: renders `{action_label, reasoning, evidence[], uncertainties[]}` (already handles STRATEGY evidence).
- `ballast/backend/tests/test_allocation_narrate.py` — **NEW**. `ballast/frontend/src/test/deploy-cash.test.jsx` — MODIFY (endpoint → `/narration`, add narration render assertions).

## Tasks & Acceptance

**Execution:**
- [x] `ballast/backend/allocation/narrate.py` -- build the pure narration layer: `build_narration_facts` (one STRATEGY `EvidenceRecord` per deploy `action_item` — statement names the asset-class label + amount, `stats` holds the raw `Decimal`s; plus a portfolio-level record for investable cash / current vs target — `as_of` from `plan.as_of`, `symbol` the fund or `"PORTFOLIO"`); `allowed_facts` (the numeric allow-set: each amount, investable/undeployed cash, each current market_value, and each current/target weight as both fraction and 0–100 percent, compared by `Decimal` value); `check_no_invented_numbers` (regex-extract `$`/comma/decimal/`%` tokens, normalize, raise if any ∉ allow-set); `check_no_forecast` (raise on a named forecast-word list); `_fallback_narration` (deterministic templated copy authored to pass all gates + the 5 good-lesson tests); `compose_narration_request` (advisor-persona system prompt: prioritize the why/tradeoff, cite only provided IDs, state numbers only as given, NEVER forecast); `narrate_plan` (deploy → LLM → `validate_recommendation` → numeric + forecast checks, fallback on ANY exception; no-action → deterministic `plan.reason` passthrough). -- the load-bearing safeguard; pure + resilient like `narrate_suggestion`.
- [x] `ballast/backend/api/allocation.py` -- add `GET /api/allocation/narration`: `build_plan` (scoped, read-only) → `narrate_plan(get_llm_gateway(), plan)`; return `{"plan": _plan_out(plan), "narration": {action_label, reasoning, uncertainties[], evidence: [r.to_dict() for r]}}`. Leave `/plan` untouched. -- read-only, degraded-safe, places nothing.
- [x] `ballast/frontend/src/components/CoachConsult.jsx` -- point `onDeploy` at `/api/allocation/narration`; read `primary_order` from `data.plan` (keep the exact populate/validation/guards); on `deploy` render `data.narration` through `<CoachCard>` (why + cited data + uncertainty); on any no-action status show `data.narration.reason`/`reasoning` and populate nothing. -- populate-don't-submit; add the advisor card.
- [x] `ballast/backend/tests/test_allocation_narrate.py` -- cover every I/O row + the **5 good-lesson tests as 5 named assertions** against `_fallback_narration` (principle-not-pick: asset-class/target framing, no stock-pick words; why-generalizes; recognized-best-practice: diversification/rebalance keyword; teaches-the-tradeoff: ≥1 real uncertainty + tradeoff phrase; facts-not-forecast: `check_no_forecast` + `check_no_invented_numbers` pass); plus `check_no_invented_numbers` rejects an injected fabricated number, `check_no_forecast` rejects prediction language, fake-mode → fallback template, determinism, calm-copy FORBIDDEN bar, no-action passthrough, and a real-DB endpoint test asserting `{plan,narration}` shape, NO `decision_record` write, and scoped isolation. Use the `ballast_test` DB per the live-link guard. -- pins the safeguard + lesson quality.
- [x] `ballast/frontend/src/test/deploy-cash.test.jsx` -- update mocks to `/api/allocation/narration` returning `{plan,narration}`; assert the narration `action_label`/`reasoning` render (coach-card testid) on `deploy` and the controls still populate with no submit; a no-action status shows the calm reason and populates nothing; calm copy passes the FORBIDDEN bar. -- covers the UI narration + populate-don't-submit.

**Acceptance Criteria:**
- Given a `deploy` plan and a live gateway, when `GET /api/allocation/narration` is called, then it returns the unchanged `plan` plus a `narration` whose every stated number is numerically equal to an engine-provided fact, contains no forecast language, cites only engine-provided evidence IDs, and carries ≥1 explicit uncertainty.
- Given the LLM states a number not in the engine set OR predicts the market OR the gateway fails, when narration is produced, then the deterministic `_fallback_narration` is returned instead — no unvalidated fact ever reaches the user, and the response never errors.
- Given any no-action status, when narration is requested, then the narration is the calm deterministic `plan.reason` with no LLM call and no fabricated move ("nothing to do" is honest).
- Given `_fallback_narration` for a deploy plan, when the 5 good-lesson tests run, then it passes all five (principle-not-pick, why-generalizes, recognized-best-practice, teaches-the-tradeoff, facts-not-forecast).
- Given the coach console on a `deploy` plan, when the user triggers "Deploy your cash toward your target", then the advisor narration card renders (action_label, why, cited data, uncertainty) AND the order controls are populated with the primary MARKET BUY with nothing submitted — the human co-signs via the existing approve spine.
- Given user B's data, when user A requests narration, then only A's scoped plan is used; the endpoint writes no `decision_record` and places no order; the full backend `pytest` and frontend `vitest` suites pass.

## Design Notes

**Reuse maximizes the existing gate.** The narration LLM emits `{action_label, reasoning, evidence[], uncertainties[]}` (the `RECOMMENDATION_OUTPUT_SCHEMA` minus `order_intent`). We build STRATEGY `EvidenceRecord`s from the `Plan` and run `recommendation_from_output(...)` → `validate_recommendation(cand, evidence)` verbatim. This is why fake mode self-heals: `FakeLLMGateway` fills `evidence` with `["fake-evidence"]`, which is unbacked → `UnbackedEvidenceError` → `_fallback_narration`. So the "fake fallback = deterministic templated copy" guarantee comes for free, exactly as the coach pipeline gets it via `build_default_plan`.

**Never-invent-a-fact is a safety net, not a hard error surface.** Its failure mode is *degrade to the deterministic template*, never a 500 and never a surfaced bad number. So an over-strict numeric match is safe — a false positive just yields honest templated copy. Extraction: regex `\$?\d[\d,]*(?:\.\d+)?%?`; normalize (strip `$`/commas, keep `%` face value); compare by `Decimal` value against the allow-set (amounts as their value; weights as BOTH the fraction e.g. `0.60` and the percent `60`). Example allow-set for growth deploy {VXUS $3,000, BND $1,000; target 60/30/10}: `{3000, 1000, 0.60, 0.30, 0.10, 60, 30, 10, investable, undeployed, current market_values...}`. `"$3,000.00"`→`3000`∈set ✓; `"60%"`→`60`∈set ✓; a stray `"2026"`∉set → fallback (safe).

**No-forecast list** (named consts, tuning-friendly): e.g. `will rise/fall/grow`, `going to`, `expect`, `forecast`, `predict`, `outperform`, `rally`, `crash`, `bull/bear market`, `guaranteed`, `next year`, `target price`. Applied case-insensitively to `reasoning`+`action_label`.

**`_fallback_narration` worked shape** (deploy, growth): action_label `"Put your idle cash to work toward your target mix"`; reasoning teaches the *principle* — you're light on International stocks and Bonds versus your chosen mix, so buying the underweight classes (not chasing a winner) moves you toward balance; states the tradeoff (this doesn't try to time the market; leftover cash stays put); cites every evidence id; ≥1 uncertainty ("markets move; a fill isn't guaranteed and this isn't a prediction"). Authored to pass all five lesson tests by construction, calm-word-list clean, numbers drawn only from `allowed_facts`.

## Verification

**Commands:**
- `cd ballast/backend && DATABASE_URL=postgresql://ballast:ballast@localhost:5432/ballast_test BALLAST_ALLOW_DIRTY_BROKERAGE_DB=1 uv run pytest -q` -- expected: all pass incl. new `test_allocation_narrate.py` (⚠️ `ballast_test` DB only — never `ballast`, which holds the live Schwab link the suite deletes).
- `cd ballast/frontend && npm test` -- expected: all pass incl. updated `deploy-cash.test.jsx`.

## Review Triage Log

### 2026-08-12 — Review pass (follow-up)
- intent_gap: 0
- bad_spec: 0
- patch: 3: (high 0, medium 2, low 1)
- defer: 0
- reject: 14: (high 0, medium 3, low 11)
- addressed_findings:
  - `[medium]` `[patch]` (EC-follow) Numeric gate false-ACCEPT of a sign-flipped value: `_NUMBER_TOKEN_RE` didn't capture a leading `-`, so `"-30%"` was laundered to the unsigned `30` and matched a legit engine weight — a fabricated negative claim slipping the never-invent gate. Fixed by capturing the sign in the regex (`-?\$?…`) and preserving it in `_normalize_number_token` (compare signed, bare `-`/`+` ignored); added `test_check_no_invented_numbers_rejects_sign_flipped_value`.
  - `[medium]` `[patch]` (BH/EC-follow) No-forecast denylist had concrete, verified misses: `\bexpect\b` never matched `expected`/`expects`; `tends to` was listed but `tend to` was not; several return-promise/magnitude forms were absent. Expanded `FORECAST_TERMS` with `expected`, `expects`, `tend to`, `will double`, `will make`, `will appreciate`, `make money`, `double your`, `triple your`, `headed higher`, `headed lower` — kept narrow so the honest `"a fill isn't guaranteed"` hedge still passes (verified by the calm/lesson tests).
  - `[low]` `[patch]` (BH-follow) Accept-branch coverage gap: the only accept-path test used number-free prose, leaving the numeric gate's happy path (an LLM restating real engine figures like `$3,000.00`/`30%`) unverified — a silent over-strict regression would ship the feature as always-fallback undetected. Added `test_narrate_plan_accepts_narration_restating_engine_numbers`.
- rejected (degrade-safe-by-design / design-accepted best-effort / already-deferred / test-only / cosmetic): over-degradation from bare integers/years/`S&P 500`→500/`401k`→401 (degrade-safe, prior-rejected); the 4-dp percent render "over-degrade" (invalid — `check_no_invented_numbers` compares by `Decimal` VALUE, so `33.33` == `33.3300`, no degrade); the sync blocking `gateway.complete()` in the async handler (duplicate of already-deferred F8 — untouched per ledger ownership); no-action empty-`plan.reason` defensive guard (engine invariant always sets a non-empty reason); evidence `statement`s not re-gated (engine-authored, trusted-by-construction, no LLM number); `allocation-engine · as of` provenance label (cosmetic); `_ScriptedGateway` skipping `super().__init__()` (test-only, works); two overlapping denylists `FORECAST_TERMS`/`_FORBIDDEN` drift (distinct intents, maintenance-only); fully-general spelled-out-magnitude & qualitative forecasts (design-accepted best-effort denylist per prior F3/F5 — numeric gate is digit-form best-effort + calm system prompt; the concrete verified stems were patched above); scale-suffix `$3M`→3 (degrade-safe, prior-rejected); current combined stock/bond split not admitted & rounded-weight forms (degrade-safe, prior-rejected); frontend partial/blank narration card (backend `NarrationOut` Pydantic + always-well-formed fallback guarantee shape, prior-rejected); narration/plan status divergence (server-side consistent — one `build_plan`, one narration).

### 2026-08-12 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 6: (high 1, medium 2, low 3)
- defer: 1
- reject: 11: (high 0, medium 2, low 9)
- addressed_findings:
  - `[high]` `[patch]` (F1) The never-invent-a-fact + no-forecast gates ran only over `reasoning + action_label`, but LLM-authored `uncertainties[]` are surfaced to the user too (via `CoachCard`→`UncertaintyCallout`) — a fabricated number or forecast hiding in an uncertainty bypassed both gates. Now `narrate_plan` gates `reasoning + action_label + *uncertainties`; added two tests (a planted forecast and a planted invented number in an uncertainty each degrade to the template) plus a clean-scripted-accept test.
  - `[medium]` `[patch]` (F2/F6/F7) The allow-set admitted the UNION of every model's target weights, letting the AI state a wrong-but-plausible target % (e.g. "20%" for a growth user) as a fact — a false-accept the gate exists to stop. Fixed by threading the user's OWN resolved `target_weights` onto the `Plan` (new defaulted field, populated by `build_plan`) and building the allow-set from that model only (+ the recognized stock/bond split so "90% stocks, 10% bonds" stays citable); the prompt now renders weights as true percents with the target, not fractions-as-money. Deleted `_all_target_weights`; added a test proving a wrong-model % is rejected.
  - `[medium]` `[patch]` (F5) The no-forecast denylist had easy false-negatives ("likely to", "should grow", "projected", "beat the market", "poised to") and used raw substring matching (which mis-fires on "expectations"/"crashed"). Expanded `FORECAST_TERMS` and switched `check_no_forecast` to word-boundary matching; narrowed the over-broad `guaranteed` to `guaranteed return`/`guaranteed profit` so honest hedges ("a fill isn't guaranteed") are not flagged.
  - `[low]` `[patch]` (F3) Documented that the numeric gate is digit-form best-effort and instructed the model (system prompt) to write every quantity in digits as given, never spelled-out or rounded — closing the "spelled-out number" evasion at the source.
  - `[low]` `[patch]` (F7) `compose_narration_request` rendered weight fractions through `format_money` labelled "of your classified holdings" (a mis-typed, money-shaped fact) — now rendered as `NN% of your classified mix, target MM%`.
  - `[low]` `[patch]` (Edge#18) Softened the `read_narration` docstring's "never a 500" overclaim: narration failures degrade to the template; an upstream `build_plan` read error surfaces through the app's global calm-envelope handler exactly as it does for `/plan`.
- deferred:
  - `[low]` `[defer]` (F8) Synchronous blocking `gateway.complete()` runs inside the `async` `read_narration` handler — a pre-existing repo-wide pattern (mirrors `suggest_resting_order`), logged for a single focused `asyncio.to_thread` fix.
- rejected (unreachable / degrade-safe-by-design / not this story's problem): the value-based numeric compare admitting a unit-mismatched coincidence ("60" vs "60%") — intentional (the gate validates value, degrade-safe); the `_plan_as_of` 1970 sentinel — only fires if an upstream deploy-plan invariant is already broken (`build_plan` always sets `as_of` for deploy); duplicate `coach-card` testid — the deploy branch nulls `recommendation` before rendering, so the two cards are mutually exclusive today; bare years/ordinals/share-counts, rounded weights, "$3k" scale suffixes, and negative-sign tokens falsely rejected — all degrade-safe (fall to the honest template, never a wrong fact); empty-`action_items`-on-deploy and unknown-asset-class-label leakage — engine contract (10-2) guarantees ≥1 item and the 3-class universe; LLM citing a subset of evidence ids — allowed by design (≥1 backed id); frontend rendering a partial narration — backend `NarrationOut` Pydantic + the always-well-formed fallback guarantee the shape.

## Auto Run Result

Status: done (follow-up review pass on a `done` spec)

**Summary of change (this pass):** A fresh independent review (Blind Hunter + Edge Case Hunter) of the Story 10.3 narration layer. Three patches were applied to harden the never-invent-a-fact / no-forecast safeguards; the remaining findings were rejected as degrade-safe-by-design, design-accepted best-effort, already-deferred, test-only, or cosmetic. No `intent_gap` or `bad_spec` — no re-derivation loopback.

**Files changed (this pass):**
- `ballast/backend/allocation/narrate.py` — sign-aware number token (`-?\$?…`) + normalizer that preserves the sign so `"-30%"` is compared as `-30` (not laundered to `30`); expanded `FORECAST_TERMS` with concrete verified stems (`expected`/`expects`, `tend to`, `will double`/`will make`/`will appreciate`, `make money`, `double your`/`triple your`, `headed higher`/`headed lower`).
- `ballast/backend/tests/test_allocation_narrate.py` — added `test_narrate_plan_accepts_narration_restating_engine_numbers` (accept-branch numeric happy path with real figures) and `test_check_no_invented_numbers_rejects_sign_flipped_value`.

**Review findings breakdown:** patch 3 (medium 2, low 1); defer 0 (the sole pre-existing defer — F8 sync `gateway.complete()` in async handler — was already recorded in the prior pass; ledger untouched); reject 14 (medium 3, low 11).

**Verification:**
- `DATABASE_URL=…/ballast_test BALLAST_ALLOW_DIRTY_BROKERAGE_DB=1 uv run pytest -q` → **772 passed** (incl. `test_allocation_narrate.py` at 32, +2 new).
- Frontend `vitest` unaffected — no frontend file changed this pass (green at the prior `final_revision`).

**Residual risks:** The no-forecast gate remains a best-effort denylist by design (qualitative, number-free forecasts and fully-general spelled-out magnitudes are not provably caught) — bounded by the digit-form numeric gate, the calm system prompt, and the always-safe degrade-to-template. The async blocking `gateway.complete()` (F8) is still deferred for a focused `asyncio.to_thread` fix.

### Review Findings — Independent (2026-08-12)

_First independent human-requested review (loop self-review was auto-skipped on 10-2/10-3/10-4). Group A = narration guardrails. Blind Hunter + Edge Case Hunter + Acceptance Auditor._

- [x] [Review][Decision→Story] Numeric allow-set is value-based / unit-blind — a fabricated integer coinciding with an admitted weight-percent or amount (e.g. "30 companies" when 30% is a target weight) passes `check_no_invented_numbers` [narrate.py:330-345]. **RESOLVED 2026-08-12: new follow-up story** — make the gate unit-tagged / context-aware ($ vs % vs count), validating each number against the matching allowed set (supersedes the prior "accept as degrade-safe" call).
- [x] [Review][Patch] Forecast denylist misses modal-hedge predictions — "could double", "may rise", "should do well", "might grow" pass `check_no_forecast` [narrate.py:78-116, 348-361]. **APPLIED 2026-08-12:** added directional modal PHRASES (could/may/might + rise/grow/climb/double/gain, should outperform/do well/beat, on track to/for, poised for) — never bare modals (avoids over-degrade); +2 regression tests (`test_check_no_forecast_rejects_modal_hedge_predictions`, `test_check_no_forecast_allows_benign_modal_usage`).
- [x] [Review][Defer] Deterministic fallback prose + evidence `statement` bypass both honesty gates (authored to pass "by construction", not re-validated against future copy edits) [narrate.py:367-460, 545] — deferred, hardening only.
- [x] [Review][Defer] `narrate_plan` hardcodes `status="deploy"` on the LLM-success path vs `plan.status` on the fallback/no-action paths (latent divergence if ever called with a deploy-variant status) [narrate.py:~533] — deferred, low.

