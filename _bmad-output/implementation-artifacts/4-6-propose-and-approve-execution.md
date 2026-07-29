---
title: 'Story 4.6 — Propose-and-Approve Execution'
type: 'feature'
created: '2026-07-28'
status: 'done'
baseline_revision: 'a5f1acbaac0817018922a6c5a55ae5b877287df6'
final_revision: '2e0061a8c32376dece29bd40a9e487e9d252fe43'
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-4-context.md'
warnings: ['oversized']
---

<intent-contract>

## Intent

**Problem:** The coach can produce a blessed `Recommendation` carrying an `order_intent`, but nothing surfaces it over HTTP and nothing executes it (FR8/FR9/FR10, AD-7). Coach is still route-less (deferred here by Stories 4.1–4.5), the Broker Port has no order-placement contract, and there is no user-driven path from "here is my call" to a placed order.

**Approach:** Establish the single execution path `propose → user-approve → Coach Engine → Broker Port`. Give the coach its first HTTP surface: `POST /api/coach/recommend` (propose — runs the existing pipeline with the user's portfolio and returns the blessed recommendation) and `POST /api/coach/approve` (execute — on an explicit approval carrying the `order_intent`, the Coach Engine validates v1 order scope and places the order through the Broker Port). Extend the Broker Port with a minimal `place_order → OrderOutcome` contract (fake deterministic; Schwab credential-gated stub). Nothing places an order except the approve endpoint, and only through the Coach Engine, and only on a live session.

## Boundaries & Constraints

**Always:**
- **Nothing executes without explicit user approval (FR8/FR9).** An order is placed only by `POST /api/coach/approve` on an explicit request carrying an `order_intent`; no other route, job, or code path places orders. The `recommend` endpoint never places anything.
- **Single execution path (AD-7):** `propose → user-approve → Coach Engine → Broker Port`. The **Coach Engine is the sole caller** of `BrokerPort.place_order` — the API handler calls the Coach Engine, never the broker directly. No module bypasses the owner.
- **Execution requires a live session (AD-11):** `approve` depends on the existing `require_live_broker_session`; an `expired`/`unlinked` session yields the calm 409 reconnect response and **no order is attempted**. `recommend` (advice) works in degraded mode and does **not** gate on a live session.
- **v1 order scope (FR10/AD-7):** an order may be placed only for a broad index fund/ETF — `strategy.index_core.is_index_core(symbol)` must be true — with `amount > 0` and `side ∈ {buy, sell}` (`OrderSide` already forecloses options/shorting/complex). A blessed `order_intent` outside this scope is rejected before any broker call, through the app's error envelope; the broker is never called.
- **Per-user isolation & auth (AD-10/Story 1.4):** both endpoints require an authenticated user and act only under that user's `Scope` via the existing `get_scope` funnel. Unauthenticated → 401 before the handler runs.
- **Money is `Decimal`, never float**, integer-clean on the wire (amounts serialized as decimal strings, matching `RECOMMENDATION_OUTPUT_SCHEMA`). UUID keys and ISO-8601 UTC timestamps where applicable. Structured logs never log tokens/secrets.
- **All ACs pass offline** with `BROKER_ADAPTER=fake` and `LLM_ADAPTER=fake` — zero network, zero credentials — matching Stories 4.1–4.5. Existing tests stay green.

**Block If:**
- A stakeholder wants any auto-execute / one-click-without-explicit-approval / "approve on the user's behalf" path — contradicts FR8/FR9 ("nothing executes without explicit approval"). HALT.
- A stakeholder wants v1 to place orders outside the broad index-fund/ETF core (individual stocks, options, shorting, complex/conditional orders) — contradicts FR10/AD-7. HALT.
- A stakeholder wants the approve endpoint to place an order on a non-live session, or to skip the live-session gate — contradicts AD-11. HALT.
- Satisfying any AC would require a live Schwab or Anthropic call (real key/network/paid tokens). HALT.

**Never:**
- No order reconciliation logic — partial/rejected/timeout/pending handling, `get_order_status`, and idempotency **retry-reuse** semantics are **Story 4.7**. `place_order` returns a single `OrderOutcome`; 4.6 does not poll, retry, or re-derive state. (The `OrderStatus` enum defines all five values as the fixed contract; the fake returns `filled`.)
- No approval→placement session-integrity handling — re-auth/re-confirm when a session expires **between** approve and placement, and double-place prevention, are **Story 4.8**. 4.6 gates on a live session at request time only.
- No decision-record persistence, co-sign, or immutable snapshot — persisting the blessed recommendation on approval is **Story 4.9**; replay is **4.10**. 4.6 places the order and returns the outcome; it does not write a `decision_record`.
- No React coach-card UI (the co-sign zone is 4.9, the replay chip 4.10). 4.6 is backend + API, consistent with 4.1–4.5's backend-first pattern; the `approve` POST is the explicit-approval gate.
- No change to `Recommendation`/`RECOMMENDATION_OUTPUT_SCHEMA`/`validate_recommendation` or the coach pipeline logic — the structural gate and pipeline stay frozen (4.2/4.3). No real Schwab order placement (credential-gated stub only). Do not add `get_order_status` to the port here.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Approve in-scope order (happy path) | authed user, **live** session, body `order_intent={symbol:"VTI", side:"buy", amount:"500"}` | Coach Engine validates scope, mints an idempotency key, calls `broker.place_order` exactly once; returns the `OrderOutcome` (fake → `status:"filled"`, non-negative `filled_qty`/`avg_price`, stable `broker_ref`) | No error expected |
| Approve out-of-v1-scope symbol | authed, live, `order_intent.symbol="AAPL"` (not index-core) | rejected before any broker call; error-envelope response; `broker.place_order` **never called** | 422/400 via envelope |
| Approve, non-positive amount | authed, live, `amount<=0` | rejected before broker call; broker never called | 422/400 via envelope |
| Approve on expired/unlinked session | authed, session **not live** | calm 409 reconnect response (`RECONNECT_MESSAGE`); **no order attempted**; broker never called | 409 via `require_live_broker_session` |
| Approve unauthenticated | no/invalid token | 401 before handler; no scope built, no broker call | 401 |
| Recommend (propose) | authed user (session may be expired), body decision `{symbol, question, amount?, side?}` | runs `run_coach_pipeline` with the user's `PortfolioView`; returns blessed recommendation `{action_label, reasoning, evidence[], uncertainties[], order_intent?}`; **never** places an order; works in degraded mode | Pipeline never dead-ends (default plan) |
| Recommend surfaces FR11 warning | authed, a decision + portfolio that trips over-concentration/panic-sell | warning content present in returned `reasoning` (4.5 behavior), still blessed, not blocked | No error expected |
| Fake broker determinism | same `(order_intent, idempotency_key)` placed twice | equal `OrderOutcome`s (no wall-clock/randomness inside the fake) | No error expected |
| Schwab place_order without creds | `BROKER_ADAPTER=schwab`, no credentials | raises `SchwabNotConfiguredError` (mirrors `fetch_portfolio` stub); real placement lands with live Schwab | Raise, credential-gated |

</intent-contract>

## Code Map

- `ballast/backend/brokers/port.py` -- MODIFY: extend the `BrokerPort` ABC with the execution contract. Add `OrderStatus` enum (`filled|partial|rejected|timeout|pending` — the full fixed contract) and a frozen `OrderOutcome{status: OrderStatus, filled_qty: Decimal, avg_price: Decimal | None, broker_ref: str | None}`. Add abstract `async place_order(self, order_intent: OrderIntent, *, idempotency_key: str) -> OrderOutcome`. Update the "arrives with execution in Epic 4" docstring note: `place_order` lands here (4.6); **`get_order_status` + reconciliation are 4.7**. Import `OrderIntent` from `coach.recommendation` (or accept the structural triple) — keep the port free of coach *logic*.
- `ballast/backend/brokers/fake_adapter.py` -- MODIFY: implement `place_order` deterministically — return `OrderOutcome(status=FILLED, filled_qty=<deterministic, ≥0>, avg_price=<deterministic fake price, ≥0>, broker_ref=<stable, derived from idempotency_key>)`. No wall-clock/randomness. Offline, credential-free.
- `ballast/backend/brokers/schwab_adapter/adapter.py` -- MODIFY: implement `place_order` as a credential-gated stub that raises `SchwabNotConfiguredError` (mirror the existing `fetch_portfolio` stub + note); real `schwab-py` order placement lands with live Schwab access.
- `ballast/backend/coach/execution.py` -- NEW: the Coach Engine's order-execution owner (the sole caller of `broker.place_order`). `async def execute_approved_order(order_intent, *, broker, idempotency_key=None) -> OrderOutcome`: validate v1 scope (`is_index_core(symbol)`, `amount > 0`; `side` guaranteed by `OrderSide`), raise a typed `OrderScopeError` (out-of-v1-scope) otherwise, mint an idempotency key if not supplied, then `await broker.place_order(order_intent, idempotency_key=...)`. Pure orchestration + gate; no persistence (4.9).
- `ballast/backend/api/coach.py` -- NEW: `APIRouter` with two endpoints. `POST /recommend`: body `RecommendRequest{symbol, question, amount?, side?}`; deps `get_scope`, `get_async_session`; fetch the user's `PortfolioView` via `get_portfolio(scope, session)`; call `run_coach_pipeline(session, CoachDecision(...), portfolio=view)`; return `RecommendResponse` (serialize blessed rec incl. evidence records + optional order_intent; Decimal→string). `POST /approve`: body `ApproveRequest{order_intent{symbol, side, amount}}`; deps `get_scope`, `require_live_broker_session`, `get_broker`, `get_async_session`; call `execute_approved_order(order_intent, broker=broker)`; return `ApproveResponse(OrderOutcome serialized)`. Map `OrderScopeError` to a 422/400 through the app envelope.
- `ballast/backend/api/app.py` -- MODIFY: import and `include_router(coach_router, prefix="/api/coach", tags=["coach"])` alongside the existing routers.
- `ballast/backend/strategy/index_core.py` -- REFERENCE: `is_index_core(symbol)` / `INDEX_CORE_SYMBOLS` = the v1 order-scope allowlist (FR10). Unchanged.
- `ballast/backend/api/deps.py` -- REFERENCE: `get_scope` (auth funnel) and `require_live_broker_session` (409 + `RECONNECT_MESSAGE` on non-live). Reused, unchanged.
- `ballast/backend/coach/recommendation.py` -- REFERENCE: `OrderIntent{symbol, side: OrderSide, amount: Decimal}`, `Recommendation`. Unchanged.
- `ballast/backend/coach/validation.py` -- REFERENCE: `BlessedRecommendation{action_label, order_intent, reasoning, evidence: tuple[EvidenceRecord,...], uncertainties}`. Unchanged.
- `ballast/backend/coach/pipeline.py` -- REFERENCE: `run_coach_pipeline(session, decision, *, gateway=None, portfolio=None)`, `CoachDecision{symbol, question, amount?, as_of?, side?}`. Unchanged.
- `ballast/backend/brokers/portfolio.py` -- REFERENCE: `get_portfolio(scope, session) -> PortfolioView`. Unchanged.
- `ballast/backend/precedent/evidence.py` -- REFERENCE: `EvidenceRecord{id, kind, statement, stats, source, as_of}` shape for serializing `evidence[]` in the recommend response. Unchanged.
- `ballast/backend/tests/test_coach_api.py` -- NEW: endpoint + execution-owner + adapter tests (see Tasks). Follow the `tests/test_brokerage.py` TestClient + register/login pattern; offline fake adapters.

## Tasks & Acceptance

**Execution:**
- [x] `ballast/backend/brokers/port.py` -- Add `OrderStatus` enum (all five values), frozen `OrderOutcome`, and abstract `place_order(order_intent, *, idempotency_key) -> OrderOutcome`; update the Epic-4 docstring to scope `place_order` to 4.6 and `get_order_status`/reconciliation to 4.7. -- Establishes the Broker Port execution contract without over-reaching into 4.7.
- [x] `ballast/backend/brokers/fake_adapter.py` -- Implement deterministic `place_order` returning a `FILLED` `OrderOutcome` (≥0 qty/price, stable `broker_ref`, no wall-clock/randomness). -- Offline, credential-free execution for tests.
- [x] `ballast/backend/brokers/schwab_adapter/adapter.py` -- Implement `place_order` as a credential-gated stub raising `SchwabNotConfiguredError` (mirror `fetch_portfolio`). -- Keeps the real path honest without live creds; real placement is a later credential-gated pass.
- [x] `ballast/backend/coach/execution.py` -- NEW Coach-Engine execution owner: v1-scope gate (`is_index_core`, `amount > 0`) raising typed `OrderScopeError`, idempotency-key minting, and the sole `broker.place_order` call. -- Single execution path; no other module places orders.
- [x] `ballast/backend/api/coach.py` + `ballast/backend/api/app.py` -- NEW coach router (`POST /recommend` propose via `run_coach_pipeline` + user portfolio, degraded-ok; `POST /approve` execute via `require_live_broker_session` + Coach Engine), Decimal→string serialization, `OrderScopeError`→envelope mapping; register at `/api/coach`. -- The propose-and-approve HTTP surface and the explicit-approval execution gate.
- [x] `ballast/backend/tests/test_coach_api.py` -- NEW offline tests covering the I/O matrix: (a) approve in-scope → one `place_order` call + returned outcome; (b) approve out-of-scope symbol and (c) non-positive amount → rejected, broker never called; (d) approve on expired/unlinked session → 409, no order; (e) approve unauthenticated → 401; (f) recommend returns a blessed recommendation with `order_intent` and never places an order (assert broker not called); (g) recommend works while session expired (degraded); (h) recommend surfaces an FR11 warning given a tripping portfolio; (i) fake `place_order` determinism; (j) Schwab `place_order` stub raises without creds; (k) sole-execution-path canary — `place_order` is reachable only through the Coach Engine execution owner (recommend path never calls it). Keep all existing tests green. -- Locks FR8/FR9/FR10/AD-7/AD-11 structurally, offline.

**Acceptance Criteria:**
- Given a blessed recommendation carrying an in-v1-scope `order_intent` and a live brokerage session, when the user calls `POST /api/coach/approve` with that `order_intent`, then the Coach Engine (the sole caller) validates v1 scope and places the order through the Broker Port exactly once and the reconciled `OrderOutcome` is returned — and no order is ever placed by any path other than an explicit approve on a live session (FR8/FR9, AD-7).
- Given an approval whose `order_intent` is outside v1 scope (not a broad index fund/ETF, or non-positive amount), when `approve` is called, then it is rejected through the app error envelope and the Broker Port is never called; and given a non-live (expired/unlinked) session, when `approve` is called, then the calm 409 reconnect response is returned and no order is attempted (FR10/AD-7, AD-11).
- Given the default offline configuration (`BROKER_ADAPTER=fake`, `LLM_ADAPTER=fake`), when the whole suite runs, then all coach/execution ACs pass with zero credentials and zero network, the coach gains its `/api/coach/recommend` + `/api/coach/approve` surface, the `recommend` endpoint returns a blessed recommendation (with any `order_intent`) and never places an order and works in degraded mode, the `Recommendation` schema and validation gate are unchanged, and no reconciliation/persistence/UI (4.7–4.10) was added.

## Design Notes

- **Why both propose and approve live here:** Stories 4.1–4.5 were deliberately backend-only and explicitly deferred "the recommendation-serving route" and "rendering in the coach card" to 4.6 (see 4.5's hand-off). The story is *propose-and-approve*: `/recommend` is the propose half (also finally surfacing the FR11 warnings 4.5 authored), `/approve` is the approve half. The React coach card itself is deferred because its co-sign zone (4.9) and replay chip (4.10) are downstream; the `approve` POST is a sufficient explicit-approval gate for FR8/FR9.
- **Integrity boundary (what 4.6 does NOT guarantee):** `approve` accepts a client-supplied `order_intent`. 4.6's guarantees are: explicit approval required, live session required, and the v1-scope gate (only broad index funds/ETFs, positive amount) — so even a malformed/fabricated request is bounded to the safe v1 universe and rejected otherwise. **Binding an approval to the exact blessed recommendation via the immutable co-signed record is Story 4.9; the approve↔placement session-integrity race and double-place prevention are Story 4.8.** This split is intentional and matches the epic's cross-story dependencies (4.6 precedes 4.7/4.8; 4.9's record is written in 4.6's flow *later*). Do not pull 4.8/4.9 forward.
- **Idempotency key:** minting the client idempotency key at the single execution path (here) is part of establishing that path — the key is genuinely passed to the broker. Its **reuse-on-retry** and `get_order_status` **reconciliation** are 4.7; 4.6 mints and passes it, nothing more.
- **Serialization:** amounts and prices are `Decimal` end-to-end and cross the wire as decimal strings (consistent with `RECOMMENDATION_OUTPUT_SCHEMA`'s string `amount`), never binary float. `evidence[]` in the recommend response serializes the `EvidenceRecord` fields so a later UI can render the precedent data-block.
- **Coach Engine as sole executor:** the API handler must call `coach.execution.execute_approved_order`, which is the only code that calls `broker.place_order`. A test asserts the broker's `place_order` is unreachable from `/recommend` and only reachable through the execution owner — this is the AD-7 "single execution path / one owner" invariant made structural.

## Verification

**Commands:**
- `cd ballast/backend && .venv/bin/python -m pytest tests/test_coach_api.py -q` -- expected: all pass; offline, no network/credentials.
- `cd ballast/backend && .venv/bin/python -m pytest -q` -- expected: full suite green (was 218 passing after 4.5; new tests added, zero regressions).
- `cd ballast/backend && .venv/bin/python -c "import coach.execution, coach.pipeline, sys; assert 'anthropic' not in sys.modules"` -- expected: exit 0 (no SDK import on the fake/default path).
- `cd ballast/backend && git status --short` -- expected: only the files in the Code Map changed; revert any `uv.lock` drift before finishing (known drift gotcha).

## Spec Change Log

## Review Triage Log

### 2026-07-28 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 7: (high 0, medium 1, low 6)
- defer: 1
- reject: 11
- addressed_findings:
  - `[medium]` `[patch]` Symbol/scope disagreement: the v1-scope gate normalizes (`is_index_core` strips+uppercases) but the raw `order_intent.symbol` was passed to `place_order`, so `"  vti  "` passed the gate yet a non-canonical string would reach the real broker. `execute_approved_order` now canonicalizes the symbol once (strip+upper) and places the canonical `OrderIntent`; added `test_approve_normalizes_symbol_before_placing`.
  - `[low]` `[patch]` `amount` gate could be bypassed by a non-finite `Decimal` (`NaN <= 0` is False) on a direct internal call. Gate now rejects `not amount.is_finite()` as well as `amount <= 0` (HTTP path already blocked by Pydantic; this hardens direct callers).
  - `[low]` `[patch]` `/recommend` used `body.symbol or CoachDecision.symbol`, which passed a whitespace-only symbol straight through and reached into another module's class default. Now strips the symbol and falls back to `CoachDecision`'s own default only when blank.
  - `[low]` `[patch]` Misleading docstrings called the returned `OrderOutcome` "reconciled" — reconciliation is Story 4.7. Reworded `ApproveResponse`/`approve()` to "placed-order outcome (unreconciled)".
  - `[low]` `[patch]` `test_schwab_place_order_stub_raises_without_creds` passed for the wrong reason (a `monkeypatch.setenv` after construction is a no-op; the captured cred was unchanged), so it would not catch a silent phantom-fill regression. Rewritten to pin the creds-present "not wired" raise (`match="not wired"`) and to strip the captured `_client_id` for the guard path.
  - `[low]` `[patch]` `test_approve_in_scope...` did `Decimal(body["avg_price"])` which would `TypeError` (not assert) if `avg_price` were ever `None`. Now asserts `avg_price is not None` first.
  - `[low]` `[patch]` No test proved the client-trusted `/approve` body boundary. Added `test_approve_malformed_body_rejected_broker_never_called` (missing `order_intent`, invalid `side`) asserting 422 and `spy.calls == []`.
- deferred:
  - `[defer]` The live-session gate (`require_live_broker_session`) keys only on `expires_at`; it does not assert the session token's `provider` matches the configured `BROKER_ADAPTER`. Masked in 4.6 (all-fake) but a real session↔placement integrity concern for Story 4.8. Logged to deferred-work.
- rejected (not this story's problem / deferred by design / noise): `/approve` does not bind the executed intent to a specific blessed recommendation (requires the immutable decision record — **Story 4.9**; documented integrity boundary in Design Notes); double-submit places two orders (idempotency retry-reuse is 4.7, double-place prevention 4.8, and real placement is a *raising* stub so no real order is possible in 4.6); fake `filled_qty` is unrounded (share quantization is a real-adapter/4.7 concern, fake is test-only); sole-execution-path canary is a substring grep (acceptable smoke check; AST enforcement is over-engineering for 4.6); `OrderScopeError→422` typed as generic `http_error` (envelope is consistent; a domain error type needs a custom handler, not required by spec); no upper bound on `amount` (a blocking size cap would contradict FR11 "never block"; oversized is a *warning*, 4.5); `EvidenceOut(**to_dict())` brittle to schema drift (the AD-12 evidence shape is fixed and tests pass); non-FILLED status serialized as 200 (only FILLED exists in 4.6; honest partial/rejected/timeout/pending reconciliation is 4.7); body-422-before-session-409 ordering (standard FastAPI, no sensitive data leaked); side-only `/recommend` runs FR11 detectors with no amount (intended 4.5 behavior — oversized-lump simply does not fire); adapter-level "non-negative filled_qty" not asserted inside the fake (the execution owner is the only real caller and now guards finiteness+positivity upstream).

## Auto Run Result

Status: done

**Summary of implemented change:** Established the coach's single execution path `propose → user-approve → Coach Engine → Broker Port` (FR8/FR9/FR10, AD-7/AD-11) — the coach's first HTTP surface. Added `POST /api/coach/recommend` (propose: runs the existing pipeline over the user's portfolio, degraded-mode-safe, surfaces FR11 warnings, **never** executes) and `POST /api/coach/approve` (execute: requires an authenticated user **and** a live brokerage session via the existing `require_live_broker_session`, then delegates to a new Coach Engine execution owner). Extended the Broker Port with a minimal execution contract — `OrderStatus` (all five values, fixed contract), a frozen broker-neutral `OrderOutcome`, and abstract `place_order(order_intent, *, idempotency_key) -> OrderOutcome` — implemented deterministically in the fake adapter and as a credential-gated raising stub in the Schwab adapter. `coach.execution.execute_approved_order` is the SOLE caller of `place_order`: it validates the v1 order scope (`is_index_core` + finite `amount > 0`), canonicalizes the symbol, mints an idempotency key, and places exactly one order. Money is `Decimal` end-to-end and serialized as decimal strings. No reconciliation (4.7), session-integrity/double-place (4.8), persistence/co-sign (4.9), replay (4.10), UI, or real Schwab placement — all deliberately downstream.

**Files changed:**
- `ballast/backend/brokers/port.py` (MODIFIED) — `OrderStatus` enum, frozen `OrderOutcome`, abstract `place_order`; docstring scopes `place_order` to 4.6 and `get_order_status`/reconciliation to 4.7.
- `ballast/backend/brokers/fake_adapter.py` (MODIFIED) — deterministic `place_order` returning a `FILLED` outcome (no wall-clock/randomness; stable `broker_ref`).
- `ballast/backend/brokers/schwab_adapter/adapter.py` (MODIFIED) — credential-gated `place_order` stub raising `SchwabNotConfiguredError` (mirrors `fetch_portfolio`).
- `ballast/backend/coach/execution.py` (NEW) — the Coach Engine execution owner: v1-scope gate (`OrderScopeError`), symbol canonicalization, idempotency-key minting, sole `place_order` caller.
- `ballast/backend/api/coach.py` (NEW) — the `/api/coach` router: `/recommend` (propose, degraded-ok) and `/approve` (execute, live-session-gated), Pydantic models, Decimal→string serialization, `OrderScopeError`→422.
- `ballast/backend/api/app.py` (MODIFIED) — registered the coach router.
- `ballast/backend/tests/test_coach_api.py` (NEW) — 16 offline tests covering the full I/O & Edge-Case Matrix plus review-hardening (symbol normalization, malformed body, strengthened Schwab-stub + avg_price assertions).
- `ballast/backend/tests/test_portfolio.py` (MODIFIED) — added a delegating `place_order` to an existing test-double adapter (required by the new abstract port method).

**Review findings breakdown:** 7 patches applied (1 medium: symbol canonicalization so the scope gate and the placed order agree; 6 low: non-finite-amount guard, blank-symbol strip, "reconciled" docstring wording, and 3 test-quality fixes). 1 deferred (live-session gate does not verify token `provider` matches the configured broker adapter — a Story 4.8 session-integrity concern). 11 rejected — chiefly items explicitly deferred by design (proposal↔placement binding → 4.9; double-place prevention → 4.7/4.8; honest non-FILLED reconciliation → 4.7) and noise (blocking on order size would violate FR11 "never block"; canary-grep limits; error-type cosmetics). 0 intent gaps, 0 bad-spec loopbacks.

**Verification performed:**
- `.venv/bin/python -m pytest tests/test_coach_api.py -q` → 16 passed (14 + 2 review-hardening tests).
- `.venv/bin/python -m pytest -q` (full suite) → 234 passed, 0 failed (was 218 after 4.5; +16; zero regressions; 1 pre-existing Starlette deprecation warning).
- `.venv/bin/python -c "import coach.execution, coach.pipeline, sys; assert 'anthropic' not in sys.modules"` → offline OK (no SDK import on the fake/default path).
- `git status --short` → only Code-Map files changed; `uv.lock` clean (no drift).

**Follow-up review recommendation:** false — the review pass made seven localized, low-consequence fixes (one small correctness improvement plus docstring/test hardening); no change to the core execution behavior, API surface, security model, or data handling.

**Residual risks:** (1) `/approve` trusts a client-supplied `order_intent` bounded only by the v1-scope gate — binding it to a specific blessed recommendation requires the immutable decision record and is **Story 4.9**; the approval↔placement session-expiry race and double-place prevention are **Story 4.8**. (2) A double-submit would mint two keys and, against a real broker, place two orders — but real placement is a *raising* credential-gated stub, so no real order can be placed until the Schwab adapter is wired (with 4.7/4.8 by then). (3) The live-session gate does not yet verify the session `provider` matches the configured broker adapter (deferred). (4) FR11 over-concentration/oversized-lump warnings on `/recommend` only fire when a portfolio snapshot is present and inherit the deferred AD-14 cash-only and 3-2 magnitude-band-floor sensitivities.
