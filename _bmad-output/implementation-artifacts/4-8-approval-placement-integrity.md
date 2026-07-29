---
title: 'Story 4.8 — Approval→Placement Integrity'
type: 'feature'
created: '2026-07-28'
status: 'done'
baseline_revision: '2e6b2cb4a2420c2967011bb682e495ec34f48e40'
final_revision: 'e6277719f7020396a53c5e1871a38a208b97c565'
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-4-context.md'
warnings: ['oversized']
---

<intent-contract>

## Intent

**Problem:** The approval→placement window is unguarded at placement time. `require_live_broker_session` checks liveness ONCE, at request-entry dependency resolution; `execute_approved_order` then mints an idempotency key and calls `place_order` with no session re-validation and — critically — no check that the authenticated session's `provider` matches the adapter actually placing the order. So a session that lapses between the entry gate and placement, or one whose `provider` disagrees with the config-selected `BROKER_ADAPTER`, could still drive a real placement — a stale or mismatched order the user never truly stands behind (FR23, AD-11; this also closes the provider-integrity gap deferred from Story 4.6).

**Approach:** Make the sole order placer (`execute_approved_order`) self-defending. It now REQUIRES the `BrokerageSession` and, as its FIRST action (before scope gate, key mint, or `place_order`), asserts placement-time integrity: the session is live AND `session.provider == broker.provider`. On any failure it raises a typed `SessionIntegrityError` and the broker is never touched; the API maps it to the same calm 409 reconnect envelope the entry gate uses. Because v1 re-auth is client-driven (no silent server refresh, AD-11), "re-establish a live session and re-confirm intent" is realized honestly: refuse to place, signal reconnect; the user re-links via the existing 2.1/2.2 flow and re-submits `/approve` with an explicit intent — that re-submission IS the re-confirmation. No silent auto-re-place path exists.

## Boundaries & Constraints

**Always:**
- **The sole placer refuses on a non-live OR provider-mismatched session (FR23/AD-11).** `execute_approved_order` requires `broker_session` and asserts `broker_session.is_live` AND `broker_session.provider == broker.provider` BEFORE minting a key or calling `place_order`. On failure it raises `SessionIntegrityError` and the broker is never touched — no stale/partial order, no phantom idempotency key.
- **Provider integrity (closes the 4.6-deferred gap).** The session's `provider` (from the authenticated token row) must equal the placing adapter's `provider`; otherwise the "live" session and the adapter that would place are decoupled — refuse and prompt reconnect.
- **Honest calm surfacing.** A placement-time integrity failure surfaces as the existing calm 409 + `RECONNECT_MESSAGE` (same envelope as the entry gate); the broker is untouched. Read/coach/`/recommend` stay usable in degraded mode (unchanged, AD-11).
- **Re-confirm is explicit and cross-request.** The ONLY way an order places is a `/approve` call carrying an explicit `order_intent` on a placement-time-verified live + provider-matched session. After a refusal the user reconnects (existing 2.1/2.2 link flow) and re-submits — that re-submission IS the re-confirmed intent.
- **4.6/4.7 invariants hold unchanged:** explicit-approval-only, the `require_live_broker_session` entry gate, the v1-scope gate, per-user auth via `get_scope`, a single `place_order` + indeterminate reconciliation, idempotency-key reuse, money as `Decimal`→decimal string, structured logs never logging tokens/secrets, and the AD-7 sole-caller canary.
- **All ACs pass offline** with `BROKER_ADAPTER=fake`, `LLM_ADAPTER=fake` — zero network, zero credentials. Existing tests stay green.

**Block If:**
- A stakeholder wants a silent server-side token refresh or an auto-re-place after expiry (placing without a fresh explicit approval) — contradicts AD-11 ("prompt re-auth, never refresh behind the user's back") and FR23. HALT.
- A stakeholder wants a non-live or provider-mismatched session to place anyway (e.g. "trust the entry gate, skip the placement-time check") — contradicts FR23/AD-11. HALT.
- Satisfying any AC would require a live Schwab or Anthropic call (real key/network/paid tokens). HALT.

**Never:**
- No decision-record persistence, co-sign, or snapshot (4.9); no cross-request persisted idempotency key (4.9); no replay (4.10); no React UI (honest surfacing is the 409 / response body).
- No silent refresh-token rotation / server-side re-auth; no auto re-place; no retry loop or wait-until-live poll.
- No change to `OrderStatus`/`OrderOutcome`/`Recommendation`/`OrderIntent` field shapes, the coach pipeline, or the entry-gate contract. This ADDS a placement-time self-defense; it does NOT remove or weaken the entry gate.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Approve, live + provider match | authed; session `live`, `provider="fake"`; broker fake; in-scope `order_intent` | order placed once and reconciled per 4.7 (true state surfaced); integrity gate passes silently | No error (HTTP 200) |
| Approve, expired at entry gate | authed; session `expired` | entry gate raises 409 `RECONNECT_MESSAGE`; broker never called (unchanged 4.6/4.7 behavior) | 409 (entry gate) |
| Placement-time session not live | `execute_approved_order` handed a non-live `broker_session` | raises `SessionIntegrityError` before key mint / `place_order`; neither broker method called | typed error → 409 |
| Approve, provider mismatch | session `live`, `provider="schwab"`; broker `provider="fake"` (or vice-versa) | refuse: `SessionIntegrityError` before `place_order`; broker untouched | 409 `RECONNECT_MESSAGE` |
| Integrity-before-scope | non-live session AND out-of-scope symbol | integrity check fails first → `SessionIntegrityError` (409); scope check not reached; broker untouched | 409 (integrity wins) |
| Approve, out-of-scope + good session | session `live`, matched provider; non-index symbol | integrity passes; `OrderScopeError` → 422 BEFORE any broker call (unchanged 4.6) | 422 (scope) |
| Sole-placer canary | grep backend tree | `.place_order(` / `.get_order_status(` still only in `coach/execution.py` (+ port/adapters) | No error |

</intent-contract>

## Code Map

- `ballast/backend/coach/execution.py` -- MODIFY: add `class SessionIntegrityError(ValueError)` alongside `OrderScopeError`. Add a required `broker_session: BrokerageSession` keyword param to `execute_approved_order`; as its FIRST action (before the v1-scope gate, key mint, and `place_order`) assert `broker_session.is_live` and `broker_session.provider == broker.provider`, raising `SessionIntegrityError` on either failure (broker never touched). Import `BrokerageSession` from `brokers.session`. Update the module + function docstrings that currently say "No approval→placement session-integrity … (Story 4.8); the live-session gate is enforced upstream" to state placement-time integrity lands HERE in 4.8. `_reconcile`, key minting, and scope logic are otherwise unchanged.
- `ballast/backend/brokers/port.py` -- MODIFY: declare `provider: str` on the `BrokerPort` ABC (a documented contract-level attribute; every subclass already sets it) so `broker.provider` is guaranteed by the port rather than incidental. No method or `OrderOutcome`/`OrderStatus` shape changes.
- `ballast/backend/api/coach.py` -- MODIFY: pass `broker_session=broker_session` into `execute_approved_order`; add an `except SessionIntegrityError` arm mapping to `HTTPException(status_code=409, detail=RECONNECT_MESSAGE)` (import `RECONNECT_MESSAGE` from `api.deps`). Keep the `require_live_broker_session` entry gate and the `OrderScopeError`→422 mapping. Reword the `approve()` docstring to note the placement-time integrity + provider check.
- `ballast/backend/api/deps.py` -- REFERENCE: `require_live_broker_session` (entry gate, unchanged), `RECONNECT_MESSAGE`, `_RECONNECT_STATUS` (=409). Reused, unchanged.
- `ballast/backend/brokers/session.py` -- REFERENCE: `BrokerageSession{state, expires_at, provider}` (frozen), `is_live == (state == "live")`. Unchanged.
- `ballast/backend/brokers/fake_adapter.py` / `ballast/backend/brokers/schwab_adapter/adapter.py` -- REFERENCE: `provider = "fake"` / `provider = "schwab"` class attributes. Unchanged.
- `ballast/backend/tests/test_coach_api.py` -- MODIFY: update the four direct `execute_approved_order(...)` calls (≈ lines 593, 602, 823, 845) to pass a live, provider-matched `broker_session`; add a `_live_session(provider="fake")` helper returning `BrokerageSession(state="live", expires_at=_live(), provider=provider)`; add the integrity tests (see Tasks). `_SpyAdapter`/`_ScriptedAdapter` already expose `provider = "fake"`. Keep all existing tests green.

## Tasks & Acceptance

**Execution:**
- [x] `ballast/backend/brokers/port.py` -- Declare `provider: str` on the `BrokerPort` ABC so the placing adapter's provider is a guaranteed contract attribute. -- Enables the provider-integrity assertion against a contract, not an incidental attribute.
- [x] `ballast/backend/coach/execution.py` -- Add `SessionIntegrityError`; require `broker_session`; assert `is_live` + `provider == broker.provider` FIRST (before scope/key/`place_order`), raising on failure; update docstrings. -- Makes the sole placer self-defending at placement time (FR23/AD-11) and closes the 4.6 provider gap.
- [x] `ballast/backend/api/coach.py` -- Pass `broker_session` into the execution owner; map `SessionIntegrityError` → 409 `RECONNECT_MESSAGE`; keep the entry gate and `OrderScopeError`→422; reword the docstring. -- Honest calm surfacing; broker untouched on any integrity failure.
- [x] `ballast/backend/tests/test_coach_api.py` -- Update the four direct `execute_approved_order` calls to pass a live matched session; add `_live_session` helper; add integrity tests covering the I/O matrix: (a) unit — non-live `broker_session` → `SessionIntegrityError`, `place_order`/`get_order_status` never called; (b) unit — provider mismatch (`session.provider="schwab"` vs broker `"fake"`) → `SessionIntegrityError`, broker untouched; (c) API — provider-mismatch at `/approve` (override the session to live `provider="schwab"`, fake broker spy) → 409 `RECONNECT_MESSAGE`, `spy.calls == []`; (d) API — happy path live+matched still places once and reconciles (regression); (e) integrity-before-scope — non-live + out-of-scope intent → integrity error (409) wins, broker untouched. Keep the existing entry-gate expired-session 409 test and the sole-caller canary green. -- Locks FR23/AD-11 + provider integrity structurally, offline.

**Acceptance Criteria:**
- Given a user approval where the brokerage session is not live at placement time (it lapsed between approval and placement) OR the session's `provider` does not match the placing adapter, when the Coach Engine execution owner runs, then it raises `SessionIntegrityError` BEFORE minting an idempotency key or calling `place_order` — the broker is never touched, no stale/partial order and no phantom key — and `/api/coach/approve` returns the calm 409 `RECONNECT_MESSAGE` (FR23, AD-11).
- Given a live, provider-matched session and an in-scope approved intent, when `/approve` runs, then the order is placed exactly once and reconciled per Story 4.7 (true state surfaced), and both broker methods are still called only through the execution owner (AD-7) — the integrity gate does not regress the happy path or the sole-caller canary.
- Given the default offline configuration (`BROKER_ADAPTER=fake`, `LLM_ADAPTER=fake`), when the whole suite runs, then all ACs pass with zero credentials and zero network; the placer requires a `BrokerageSession` and enforces live + provider integrity at placement time; the entry-gate 409, degraded-mode reads, v1-scope 422, and 4.7 reconciliation all still hold; and no persistence/co-sign (4.9), replay (4.10), silent refresh, or UI was added.

## Spec Change Log

## Review Triage Log

### 2026-07-29 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 1: (high 0, medium 0, low 1)
- defer: 0
- reject: 7
- addressed_findings:
  - `[low]` `[patch]` Hardened the placement-time provider comparison in `coach/execution.py`: normalized case/whitespace on both sides and read the adapter provider via `getattr(broker, "provider", None)`, so a stored-provider casing drift, a `None` on either side, or a misconfigured adapter missing `provider` now refuses with the calm 409 instead of false-refusing or raising a raw `AttributeError` (HTTP 500) — the placer never places on doubt. Added a unit test locking the normalized-match behavior (`" FAKE "` still matches `"fake"` and places once). Consolidates review findings F3/F4/F7 + the edge-case provider-None/missing-attr/casing branches; all are unreachable in the v1 offline scope (every adapter sets `provider="fake"`) but latent, so hardened defensively.
- rejected (not this story's problem / noise):
  - F2 "no test proves no-phantom-key": misread — the non-live and provider-mismatch UNIT tests use an in-scope symbol (`VTI`) and assert `spy.calls == []`/`status_calls == []`, already proving integrity fires before key-mint and placement.
  - F5 "API mismatch test overrides the entry gate": the override cleanly isolates 4.8's placement-time check; the DB→`provider` read path is Epic 2 code already covered by session-status tests — re-testing the session builder is out of scope.
  - F6 "provider limb is single-point": by design (AD-7 sole placer = sole enforcer), explicitly argued in the spec's Design Notes.
  - F1 "exception message ≠ RECONNECT_MESSAGE": the handler surfaces `RECONNECT_MESSAGE` to the user; the exception's own string is internal/log-only and reads calmly — no user-facing divergence.
  - F8 "lock adapter provider constants" and F9 "docstring line-wrap": cosmetic; the docstring renders as a continuous, readable sentence.
  - EC3 "'unknown' provider fallback in the link flow": pre-existing Epic 2 code, not in this diff, only reachable with a hypothetical broken adapter, and neutralized by the normalization patch anyway.

## Design Notes

- **Self-defending placer, not just the entry gate.** AD-7 makes `execute_approved_order` the sole placer; 4.8 makes it the sole ENFORCER of "may we place at all," so the integrity check executes at the placement decision point and no future caller can reach `place_order` on a non-live/mismatched session. The `require_live_broker_session` entry gate stays (fast fail, degraded-mode consistency); this ADDS a placement-time guard, it does not replace it.
- **Client-driven re-auth ⇒ honest "re-establish + re-confirm."** v1 has no silent server-side token refresh (AD-11: prompt re-auth, never refresh behind the user's back). The server therefore cannot re-establish a live session mid-request; the honest realization of FR23 is: detect at placement time, refuse (no stale/partial order), return the calm reconnect. The user re-links via the existing 2.1/2.2 link flow and re-submits `/approve` with an explicit intent — that re-submission IS the re-confirmation. A silent auto-re-place would violate explicit-approval and is a Block-If.
- **Provider integrity closes the 4.6-deferred gap.** `require_live_broker_session` authorized on `expires_at`/`is_live` alone; the session's `provider` and the config-selected adapter were decoupled. Asserting `session.provider == broker.provider` before placing prevents a live `fake` session from driving a real `schwab` placement (or vice-versa). This story resolves that deferred item (a future ledger sweep can close it); do not edit the append-only `deferred-work.md`.
- **Integrity before scope.** The integrity check ("may we act on this session at all") runs before the v1-scope check ("what may be placed"); neither touches the broker, so a failure of either leaves it untouched. When both fail, the 409 reconnect (integrity) is surfaced.

## Verification

**Commands:**
- `cd ballast/backend && .venv/bin/python -m pytest tests/test_coach_api.py -q` -- expected: all pass (existing + new integrity tests); offline, no network/credentials.
- `cd ballast/backend && .venv/bin/python -m pytest -q` -- expected: full suite green (was 245 passing after 4.7; new tests added, zero regressions).
- `cd ballast/backend && .venv/bin/python -c "import coach.execution, coach.pipeline, sys; assert 'anthropic' not in sys.modules"` -- expected: exit 0 (no SDK import on the fake/default path).
- `cd ballast/backend && git status --short` -- expected: only Code-Map files changed; revert any `uv.lock` drift before finishing (known drift gotcha).

## Auto Run Result

Status: done

**Summary of implemented change:** Made the Coach Engine's sole order placer (`execute_approved_order`) self-defending at placement time (FR23/AD-11). It now REQUIRES the `BrokerageSession` and, as its FIRST action — before the v1-scope gate, key minting, or `place_order` — asserts the session is live AND its `provider` matches the placing adapter's `provider`, raising a typed `SessionIntegrityError` on either failure with the broker NEVER touched (no stale/partial order, no phantom idempotency key). The API `/approve` maps that error to the same calm 409 `RECONNECT_MESSAGE` the request-entry gate uses. This closes the Story-4.6-deferred provider-integrity gap (a live `fake` session can no longer drive a real `schwab` placement, or vice-versa). Because v1 re-auth is client-driven (no silent server refresh, AD-11), "re-establish + re-confirm" is realized honestly: refuse to place and prompt reconnect; the user re-links via the existing 2.1/2.2 flow and re-submits `/approve` with an explicit intent (the re-confirmation). No decision-record/persistence (4.9), replay (4.10), silent refresh, auto-re-place, or UI was added.

**Files changed:**
- `ballast/backend/brokers/port.py` (MODIFIED) — declared `provider: str` on the `BrokerPort` ABC as a contract-level attribute so `broker.provider` is guaranteed. No method/`OrderOutcome`/`OrderStatus` shape changes.
- `ballast/backend/coach/execution.py` (MODIFIED) — added `SessionIntegrityError`; required `broker_session`; placement-time integrity assertion FIRST (integrity-before-scope), with a review-hardened provider comparison (case/whitespace-normalized + `getattr` so a missing/None/casing-drift provider refuses with the calm 409, not a 500); updated docstrings so 4.8 integrity lands here.
- `ballast/backend/api/coach.py` (MODIFIED) — passes `broker_session` into the execution owner; maps `SessionIntegrityError` → 409 `RECONNECT_MESSAGE`; kept the entry gate and `OrderScopeError`→422; reworded the `approve()` docstring.
- `ballast/backend/tests/test_coach_api.py` (MODIFIED) — `_live_session` helper; updated the four direct `execute_approved_order` calls to pass a live matched session; added integrity tests (unit non-live, unit provider-mismatch, API provider-mismatch 409, API happy-path regression, integrity-before-scope) plus a review-patch unit test locking normalized provider matching.

**Review findings breakdown:** 0 intent gaps, 0 bad-spec loopbacks, 1 patch (low; a single provider-comparison hardening consolidating findings F3/F4/F7 and the provider-None/missing-attr/casing edge cases — all latent/unreachable in the v1 offline scope but defended), 0 deferred, 7 rejected (misreads, out-of-scope Epic-2 coverage, by-design single-point enforcement, cosmetic/log-only copy).

**Verification performed (independently re-run by the orchestrator):**
- `.venv/bin/python -m pytest -q` (full suite) → 251 passed, 0 failed (was 245 after 4.7; +6 net; zero regressions; 1 pre-existing Starlette deprecation warning).
- `.venv/bin/python -c "import coach.execution, coach.pipeline, sys; assert 'anthropic' not in sys.modules"` → offline OK (no SDK import on the fake/default path).
- `git status --short` → only the 4 Code-Map files changed; `uv.lock` clean (no drift).

**Follow-up review recommendation:** false — the final pass produced one localized, low-consequence hardening patch (plus a lock test) on code unreachable in the v1 offline scope; the core integrity behavior, API surface, and honesty invariants reviewed clean.

**Residual risks:** (1) Provider integrity and the placement-time refusal are the honest v1 realization of FR23 given client-driven re-auth; a genuine wall-clock lapse strictly between dependency resolution and `place_order` within one request is sub-millisecond and, if the token truly expired, the session read already reflects it — the placer refuses regardless. (2) A realistic cross-provider mismatch (a `schwab` token row against a `fake` adapter, or vice-versa) is not reachable in v1 because linking always stores the active adapter's provider; the guard is exercised synthetically and will govern the real path once live Schwab lands. (3) Cross-request persisted idempotency key and the co-signed decision record remain Story 4.9; replay is Story 4.10. (4) Real Schwab `place_order`/`get_order_status` remain credential-gated raising stubs.
