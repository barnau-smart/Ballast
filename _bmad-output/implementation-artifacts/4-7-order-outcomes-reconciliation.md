---
title: 'Story 4.7 — Order Outcomes & Reconciliation'
type: 'feature'
created: '2026-07-28'
status: 'done'
baseline_revision: 'a71cf33ee04bc5db4314d29d9903a7881d29baff'
final_revision: 'a44534324c291eb4b7c4aaa48e03997add11c5b0'
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-4-context.md'
warnings: ['oversized']
---

<intent-contract>

## Intent

**Problem:** Story 4.6 gave the coach a single execution path (`/api/coach/approve → Coach Engine → BrokerPort.place_order`) that mints an idempotency key and returns a single `OrderOutcome`, but that outcome is **unreconciled**: the fake always returns `FILLED`, there is no `get_order_status`, and an indeterminate placement (a `timeout` or `pending`) would be surfaced as-is with no way to learn the true state — and a naive retry could double-place. The user is not yet guaranteed to always see what really happened to their money (FR22, NFR3, AD-13).

**Approach:** Complete the Broker Port reconciliation contract. Add `get_order_status(idempotency_key) -> OrderOutcome` to the port; the Coach Engine execution owner, after its single `place_order`, reconciles an **indeterminate** placement (`timeout`/`pending`) by reading the authoritative state via `get_order_status` **reusing the same idempotency key** — never assuming, never re-placing. Definitive placements (`filled`/`partial`/`rejected`) are surfaced directly. The `/approve` response carries the true reconciled status for all five outcomes honestly (no phantom success, no duplicate order). Fake adapter becomes idempotency-keyed so a re-place with the same key returns the recorded order and `get_order_status` reports it; Schwab reconciliation stays a credential-gated stub.

## Boundaries & Constraints

**Always:**
- **Reconcile indeterminate placements via `get_order_status`, never optimistically (AD-13/NFR3).** When `place_order` returns `timeout` or `pending`, the Coach Engine calls `get_order_status(idempotency_key)` — reusing the **same** key `place_order` used — exactly once and surfaces whatever it authoritatively reports (which may still be `pending`). It never assumes a timeout filled or failed.
- **A timeout never double-places (FR22/NFR3).** Per approval, `place_order` is called **at most once**; reconciliation is read-only through `get_order_status`. The fake adapter is idempotency-keyed: a `place_order` re-invoked with an already-seen key returns the recorded outcome and records the order once.
- **Honest true state for all five statuses (FR22).** `filled|partial|rejected|timeout|pending` each surface truthfully in the `/approve` response body (HTTP 200 for any resolved outcome — a broker `rejected` is a truthful data result, not an app error). `filled_qty`/`avg_price` reflect the reconciled outcome; a non-`filled` status is never coerced into a phantom fill.
- **Single owner (AD-7).** `coach/execution.py` is the **sole caller** of both `BrokerPort.place_order` and `BrokerPort.get_order_status`. The API handler calls the Coach Engine, never the broker directly.
- **Execution invariants from 4.6 hold unchanged:** explicit-approval-only, live-session gate (`require_live_broker_session`, calm 409 on non-live), v1 order-scope gate (`is_index_core`, finite `amount > 0`), per-user auth via `get_scope`, money as `Decimal` serialized as decimal strings, structured logs never logging tokens/secrets.
- **All ACs pass offline** with `BROKER_ADAPTER=fake` and `LLM_ADAPTER=fake` — zero network, zero credentials. Existing tests stay green.

**Block If:**
- A stakeholder wants a `timeout`/`pending` to be **assumed** filled (or assumed not-placed) instead of reconciled via `get_order_status` — contradicts AD-13 ("never optimistic assumptions"). HALT.
- A stakeholder wants to **re-place** (call `place_order` again) on a timeout instead of reading state read-only — risks a duplicate order, contradicts FR22/NFR3. HALT.
- A stakeholder wants a non-`filled` outcome coerced into a success / phantom fill, or hidden from the user — contradicts FR22. HALT.
- Satisfying any AC would require a live Schwab or Anthropic call (real key/network/paid tokens). HALT.

**Never:**
- No **unbounded polling / retry loop**. Reconciliation is a **single** `get_order_status` read on an indeterminate placement; if it still reports `pending`/`timeout`, that honest state is surfaced. No wait-until-terminal loop, no re-placement, no wall-clock sleeps.
- No **cross-request / persisted** idempotency. Storing the key on a decision record so a later, separate request reuses it is enabled by the immutable co-signed record — **Story 4.9**; the approval→placement session-expiry race and re-confirm are **Story 4.8**. 4.7 reconciles **within a single approve flow** only.
- No decision-record persistence, co-sign, or snapshot (**4.9**); no replay (**4.10**); no React coach-card / outcome UI (honest surfacing is the `/approve` response body). Real Schwab `place_order`/`get_order_status` remain credential-gated raising stubs.
- No change to `Recommendation`/`RECOMMENDATION_OUTPUT_SCHEMA`/`validate_recommendation`, the coach pipeline, `OrderIntent`/`OrderSide`, or the `OrderStatus`/`OrderOutcome` field shapes (already frozen in 4.6). Do not add fields to `OrderOutcome`.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Approve, placement definitive `filled` | authed, live, in-scope `order_intent`; broker `place_order → filled` | reconciled outcome = `filled` surfaced; `get_order_status` **not** called (definitive); `place_order` called once | No error expected |
| Approve, placement `partial` | broker `place_order → partial` (partial `filled_qty`) | surfaced honestly as `partial` with true `filled_qty`/`avg_price`; not coerced to `filled`; `get_order_status` not called | No error (HTTP 200) |
| Approve, placement `rejected` | broker `place_order → rejected` | surfaced honestly as `rejected`, `filled_qty` 0; no phantom fill; `place_order` once, `get_order_status` not called | HTTP 200, honest body |
| Approve, placement `timeout` → reconciles | broker `place_order → timeout`; `get_order_status(key) → filled` | engine calls `get_order_status` once **with the same key**; surfaces reconciled `filled`; `place_order` still called exactly once (no double-place) | No error expected |
| Approve, placement `pending` → still pending | broker `place_order → pending`; `get_order_status(key) → pending` | reconciles once; honestly surfaces `pending` (no phantom success, no re-place) | HTTP 200, honest body |
| Idempotent re-place (fake) | fake `place_order(intent, key)` then `place_order(intent, key)` again (same key) | second call returns the **identical** recorded `OrderOutcome`; order recorded once (no duplicate) | No error expected |
| `get_order_status` for placed key (fake) | fake after `place_order(intent, key)` | returns the recorded outcome for that key | No error expected |
| `get_order_status` for unknown key (fake) | fake, key never placed | honest `pending` outcome (`filled_qty` 0, no `broker_ref`) — never invents a fill | No error expected |
| Reuse-key reconciliation canary | stub where `place_order → timeout`; records keys seen by both methods | `place_order` called once, `get_order_status` called once, and the key passed to both is identical | No error expected |
| Schwab `get_order_status` without creds | `BROKER_ADAPTER=schwab`, no credentials | raises `SchwabNotConfiguredError` ("not wired"), mirroring `place_order`/`fetch_portfolio` stubs | Raise, credential-gated |

</intent-contract>

## Code Map

- `ballast/backend/brokers/port.py` -- MODIFY: add abstract `async def get_order_status(self, idempotency_key: str) -> OrderOutcome` to the `BrokerPort` ABC (the reconciliation primitive; reuse of the client key from `place_order`). Update the class/method docstrings that currently scope `get_order_status`+reconciliation to "**Story 4.7**, not added here" (lines ~14, ~85, ~122, ~158, ~174) to state it **lands here in 4.7**. Do not change `OrderStatus` or `OrderOutcome` shapes.
- `ballast/backend/brokers/fake_adapter.py` -- MODIFY: make the fake idempotency-keyed. Keep a per-instance `dict[str, OrderOutcome]`. `place_order`: if `idempotency_key` already recorded, return the stored outcome (idempotent — no double-place); else compute the deterministic `FILLED` outcome (unchanged formula: `filled_qty = amount / FAKE_FILL_PRICE`, `avg_price = FAKE_FILL_PRICE`, `broker_ref = f"fake-order-{idempotency_key}"`), record it, return it. `get_order_status(idempotency_key)`: return the recorded outcome; unknown key → honest `OrderOutcome(status=PENDING, filled_qty=Decimal("0"), avg_price=None, broker_ref=None)` (never a phantom fill). Stays offline/deterministic (no wall-clock/randomness).
- `ballast/backend/brokers/schwab_adapter/adapter.py` -- MODIFY: implement `get_order_status` as a credential-gated stub raising `SchwabNotConfiguredError` ("... not wired yet ...") mirroring the existing `place_order`/`fetch_portfolio` stubs (`self._require_configured()` then raise). Real `schwab-py` reconciliation lands with live Schwab.
- `ballast/backend/coach/execution.py` -- MODIFY: after the single `place_order`, reconcile. Define an indeterminate set `INDETERMINATE = {OrderStatus.TIMEOUT, OrderStatus.PENDING}`. If the placement outcome's status is in `INDETERMINATE`, call `await broker.get_order_status(key)` **once** (reusing the same `key`) and return that; otherwise return the placement outcome unchanged. Add a small private `reconcile` helper if it reads cleaner; keep `execute_approved_order` the **sole caller** of both `place_order` and `get_order_status`. No re-place, no loop. Keep the existing v1-scope gate, symbol canonicalization, and key minting.
- `ballast/backend/api/coach.py` -- MODIFY: minimal. `/approve` already returns `execute_approved_order`'s outcome via `_to_approve_response`, which serializes `status`/`filled_qty`/`avg_price`/`broker_ref` — now the **reconciled** outcome for any of the five statuses. Reword the 4.6 "placed-order outcome (unreconciled)" docstrings on `ApproveResponse`/`approve()` to "reconciled order outcome (true state)". Confirm a non-`filled` resolved status returns HTTP 200 with the honest body (no error-envelope coercion). No new endpoint, no `OrderScopeError` change.
- `ballast/backend/tests/test_portfolio.py` -- MODIFY: add `async def get_order_status(self, idempotency_key)` to the `_FetchFailsAdapter` test-double (line ~384) so the ABC stays satisfiable — delegate to its `FakeBrokerAdapter()` (mirrors its existing delegating `place_order`).
- `ballast/backend/tests/test_coach_api.py` -- MODIFY: add `get_order_status` to `_SpyAdapter` (record calls + delegate) and add the reconciliation tests (see Tasks). Keep the existing 16 tests green.
- `ballast/backend/brokers/port.py` (`OrderStatus`/`OrderOutcome`) -- REFERENCE: five-value `OrderStatus` and frozen `OrderOutcome{status, filled_qty: Decimal, avg_price: Decimal|None, broker_ref: str|None}` — already defined in 4.6; unchanged.
- `ballast/backend/coach/recommendation.py` -- REFERENCE: `OrderIntent{symbol, side: OrderSide, amount: Decimal}`. Unchanged.
- `ballast/backend/brokers/factory.py` -- REFERENCE: `get_broker()` returns a fresh adapter per call (so the fake's per-instance store lives for one request — sufficient for within-approve reconciliation). Unchanged.
- `ballast/backend/api/deps.py` -- REFERENCE: `get_scope`, `require_live_broker_session`, `get_broker`. Reused, unchanged.

## Tasks & Acceptance

**Execution:**
- [x] `ballast/backend/brokers/port.py` -- Add abstract `get_order_status(idempotency_key) -> OrderOutcome`; update the "4.7 not here" docstrings to "lands in 4.7". -- Completes the Broker Port reconciliation contract (AD-13).
- [x] `ballast/backend/brokers/fake_adapter.py` -- Make `place_order` idempotency-keyed (record once, re-place with same key returns the recorded outcome) and implement `get_order_status` (recorded outcome; unknown key → honest `pending`). -- Structural no-double-place + a reconcilable offline fake.
- [x] `ballast/backend/brokers/schwab_adapter/adapter.py` -- Add `get_order_status` credential-gated stub raising `SchwabNotConfiguredError` ("not wired"). -- Keeps the real path honest without live creds.
- [x] `ballast/backend/coach/execution.py` -- Reconcile indeterminate (`timeout`/`pending`) placements via a single same-key `get_order_status`; surface definitive placements directly; remain the sole caller of both broker methods. -- AD-13 reconciliation + AD-7 single owner; timeout never double-places.
- [x] `ballast/backend/api/coach.py` -- Reword the "unreconciled" docstrings to "reconciled true state"; confirm all five statuses serialize honestly at HTTP 200. -- Honest true-state surfacing (FR22).
- [x] `ballast/backend/tests/test_portfolio.py` -- Add delegating `get_order_status` to `_FetchFailsAdapter`. -- Keep the ABC satisfiable; no regression.
- [x] `ballast/backend/tests/test_coach_api.py` -- Add `get_order_status` to `_SpyAdapter`; add reconciliation tests covering the I/O matrix: (a) definitive `filled`/`partial`/`rejected` surfaced honestly, `get_order_status` not called; (b) `timeout`→`get_order_status`→reconciled `filled`, `place_order` called exactly once; (c) `pending`→still-`pending` surfaced honestly; (d) reconciliation reuses the same idempotency key `place_order` used; (e) fake `place_order` idempotent by key (re-place returns recorded outcome, recorded once); (f) fake `get_order_status` for placed vs unknown key (unknown → honest `pending`); (g) Schwab `get_order_status` stub raises without creds; (h) sole-execution-path canary extended so `.get_order_status(` is called only in `coach/execution.py`. Keep existing tests green. -- Locks FR22/NFR3/AD-13/AD-7 structurally, offline.

**Acceptance Criteria:**
- Given a placed order whose placement is indeterminate (the broker returns `timeout` or `pending`), when the Coach Engine handles it, then it reconciles the true state via `BrokerPort.get_order_status` **reusing the same idempotency key** exactly once (never re-placing, never assuming), `place_order` is called at most once for the approval, and the reconciled status is what the user sees (FR22, NFR3, AD-13).
- Given a resolved order of any status `filled|partial|rejected|timeout|pending`, when `/api/coach/approve` returns, then the response body carries that true status with reconciled `filled_qty`/`avg_price` (a non-`filled` outcome is never coerced into a phantom fill and a broker `rejected` returns HTTP 200 with an honest body), and both `place_order` and `get_order_status` are called only through the Coach Engine execution owner (AD-7).
- Given the default offline configuration (`BROKER_ADAPTER=fake`, `LLM_ADAPTER=fake`), when the whole suite runs, then all ACs pass with zero credentials and zero network; the fake adapter is idempotency-keyed (re-placing with the same key never records a duplicate) and answers `get_order_status`; the Schwab adapter's `get_order_status` is a credential-gated raising stub; and no persistence/co-sign (4.9), session-integrity/re-confirm (4.8), replay (4.10), or UI was added.

## Design Notes

- **Indeterminate-only reconciliation (not always).** AD-13's "reconciliation always uses `get_order_status`, never optimistic assumptions" governs the *indeterminate* case: a `timeout`/`pending` must be resolved by reading authoritative state, never by guessing. A definitive `place_order` return (`filled`/`partial`/`rejected`) IS the broker's authoritative answer to the placement call — surfacing it directly is honest and avoids a needless second round-trip (NFR3 responsiveness). This keeps the "few seconds" budget while still guaranteeing no optimistic assumption is ever made on an ambiguous result.
- **Why the fake becomes idempotency-keyed.** Reusing the key on retry "so a timeout never double-places" is the AD-13 invariant. The fake makes it structural: `place_order` records under the key and a second call with the same key returns the recorded order (records once) — a real broker enforces the same via the client key. Because `get_broker()` returns a fresh adapter per request, this store lives for one approve flow, which is exactly 4.7's scope (cross-request key reuse needs the persisted decision record — 4.9 — and the session-integrity race — 4.8).
- **Single reconciliation read, no loop.** 4.7 reads state once; if still `pending`, that honest state is surfaced. A wait-until-terminal poll (with sleeps/backoff) is deliberately out of scope — it would risk hanging the request and is not required to "show the true state."
- **HTTP status for non-`filled`.** A resolved order that the broker `rejected` (or left `pending`) is a truthful *result*, not an app error — so `/approve` returns HTTP 200 with the honest status in the body, consistent with 4.6's deliberate rejection of "non-FILLED status serialized as an error." Only scope/auth/session failures use the error envelope (4.6, unchanged).
- **Sole-owner canary extended.** 4.6 asserted `.place_order(` is called only in `coach/execution.py`; 4.7 extends the same grep canary to `.get_order_status(` so reconciliation cannot leak into the API handler or elsewhere — AD-7 made structural.

## Verification

**Commands:**
- `cd ballast/backend && .venv/bin/python -m pytest tests/test_coach_api.py -q` -- expected: all pass (existing 16 + new reconciliation tests); offline, no network/credentials.
- `cd ballast/backend && .venv/bin/python -m pytest -q` -- expected: full suite green (was 234 passing after 4.6; new tests added, zero regressions).
- `cd ballast/backend && .venv/bin/python -c "import coach.execution, coach.pipeline, sys; assert 'anthropic' not in sys.modules"` -- expected: exit 0 (no SDK import on the fake/default path).
- `cd ballast/backend && git status --short` -- expected: only Code-Map files changed; revert any `uv.lock` drift before finishing (known drift gotcha).

## Spec Change Log

## Review Triage Log

### 2026-07-28 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 0
- defer: 1
- reject: 1
- addressed_findings:
  - none
- deferred:
  - `[defer]` Money-string serialization can emit exponent notation: `_to_approve_response` serializes `filled_qty` via bare `str(Decimal)`, so an absurdly large order amount (~1E29) yields `"1E+27"` on the wire, which technically violates the "amounts serialized as plain decimal strings" convention. Pre-existing from Story 4.6 (the `str(outcome.filled_qty)` line is unchanged by 4.7) and it round-trips correctly through the documented `Decimal(str(...))` consumer, so every test passes; it also spans the `/recommend` amount serialization. Logged to deferred-work for a focused money-formatting pass (e.g. `format(value, "f")`).
- rejected (not this story's problem / noise):
  - The sole-execution-path canary is a raw substring grep (`.place_order(`/`.get_order_status(`) rather than an AST check — inherited 4.6 scaffolding that 4.7 extends verbatim; the reviewer confirmed it is "not a new bug," and 4.6 already rejected AST enforcement as over-engineering for an offline smoke check. No real breach is reachable in the offline scope.

## Auto Run Result

Status: done

**Summary of implemented change:** Completed the Broker Port reconciliation contract (FR22/NFR3/AD-13) so the user always sees the true state of an order. Added abstract `get_order_status(idempotency_key) -> OrderOutcome` to `BrokerPort`. The Coach Engine execution owner (`execute_approved_order`), after its single `place_order`, reconciles only **indeterminate** placements (`timeout`/`pending`) by reading authoritative state **once** via `get_order_status`, reusing the **same** idempotency key `place_order` used — never optimistic, never re-placing, never looping. Definitive placements (`filled`/`partial`/`rejected`) are surfaced directly. All five statuses serialize honestly at HTTP 200 (a broker `rejected`/`pending` is truthful data, not an app error; no phantom fill). The fake adapter became idempotency-keyed — a re-place with a seen key returns the recorded outcome (records once, structural no-double-place) and `get_order_status` returns the recorded outcome or an honest `pending` for an unknown key (never invents a fill). The Schwab adapter's `get_order_status` is a credential-gated raising stub. No persistence/co-sign (4.9), session-integrity/cross-request key reuse (4.8), replay (4.10), or UI.

**Files changed:**
- `ballast/backend/brokers/port.py` (MODIFIED) — abstract `get_order_status`; docstrings updated to scope reconciliation as landed in 4.7. `OrderStatus`/`OrderOutcome` shapes unchanged.
- `ballast/backend/brokers/fake_adapter.py` (MODIFIED) — per-instance idempotency-keyed store; idempotent `place_order`; `get_order_status` (recorded outcome, else honest `pending`). Default placement stays `filled`.
- `ballast/backend/brokers/schwab_adapter/adapter.py` (MODIFIED) — credential-gated `get_order_status` stub raising `SchwabNotConfiguredError` ("not wired").
- `ballast/backend/coach/execution.py` (MODIFIED) — `INDETERMINATE` set + `_reconcile` helper; single same-key `get_order_status` on indeterminate placements; sole caller of both broker methods.
- `ballast/backend/api/coach.py` (MODIFIED) — "reconciled true state" docstrings; confirmed all five statuses serialize honestly at HTTP 200. No new endpoint.
- `ballast/backend/tests/test_portfolio.py` (MODIFIED) — delegating `get_order_status` on `_FetchFailsAdapter`.
- `ballast/backend/tests/test_coach_api.py` (MODIFIED) — `get_order_status` on `_SpyAdapter`; new `_ScriptedAdapter` DI double for non-`filled` statuses; canary extended to `get_order_status`; reconciliation tests (a)–(h) from the I/O matrix.

**Review findings breakdown:** 0 intent gaps, 0 bad-spec loopbacks, 0 patches. 1 deferred (exponent-notation money-string serialization — pre-existing from 4.6, low consequence, spans multiple endpoints; logged to deferred-work). 1 rejected (substring canary vs AST — inherited scaffolding, confirmed not a new bug).

**Verification performed (independently re-run by the orchestrator):**
- `.venv/bin/python -m pytest tests/test_coach_api.py -q` → 27 passed (was 16 after 4.6; +11 reconciliation tests).
- `.venv/bin/python -m pytest -q` (full suite) → 245 passed, 0 failed (was 234 after 4.6; +11; zero regressions; 1 pre-existing Starlette deprecation warning).
- `.venv/bin/python -c "import coach.execution, coach.pipeline, sys; assert 'anthropic' not in sys.modules"` → offline OK (no SDK import on the fake/default path).
- `git status --short` → only the 7 Code-Map files changed; `uv.lock` clean (no drift).

**Follow-up review recommendation:** false — the review pass produced no code changes (0 patches, 0 loopbacks); one low-consequence pre-existing item was deferred and one nit rejected. The core reconciliation behavior, API surface, and honesty invariants were reviewed clean.

**Residual risks:** (1) The exponent-notation money-string edge (deferred) at absurd order magnitudes. (2) Cross-request idempotency-key reuse and the approval→placement session-expiry race remain **Story 4.8**; a persisted key/decision record is **Story 4.9** — within-approve reconciliation is all 4.7 guarantees. (3) The fake's per-request store cannot demonstrate cross-request double-place prevention (by design; a real broker enforces it via the client key). (4) Real Schwab `place_order`/`get_order_status` remain credential-gated raising stubs; live reconciliation mapping lands with real Schwab access. (5) The sole-execution-path canary is a substring smoke check (AST enforcement deferred as over-engineering).
