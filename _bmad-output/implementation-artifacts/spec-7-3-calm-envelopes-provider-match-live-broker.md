---
title: 'Calm envelopes & provider-match on the live broker seams'
type: 'bugfix'
created: '2026-08-01'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: ['oversized']
baseline_revision: '53e492fc6538fa5e2bfa6ac4d90731e279b35190'
final_revision: '1565bc5b27a86023aa12374c66c36976196535e4'
---

<intent-contract>

## Intent

**Problem:** Three live-broker failure modes still breach the calm/honest/never-red voice (NFR8) on the credential-gated Schwab path. (1) A token that cannot be decrypted (rotated `TOKEN_ENCRYPTION_KEY` / corrupt ciphertext) raises `TokenEncryptionError` inside `_bind_user_token` DURING FastAPI dependency resolution for `/approve` (`get_execution_broker`) and `/refresh` (`get_reading_broker`) — before any handler `try/except` — so it escapes as a raw 500 through the app's `Exception` handler instead of a calm 409 reconnect. (2) On reconcile, `SchwabAdapter.get_order_status_by_ref` wraps the client build (`_trading_client`/`_account_hash`) in the SAME bare `except Exception` fence as the transport read, so a deterministic config/auth fault (`SchwabNotConfiguredError`) is laundered into `OrderStatus.TIMEOUT` + `needs_reconfirmation` — a soft dead-end where every retry re-launders the same fault with no honest "reconnect" signal. (3) The provider-match refusal (`session.provider == broker.provider` before placement) is implemented in `_assert_session_integrity` (Story 4.8) but is not pinned by a regression test at both seams.

**Approach:** Give every live-seam fault the existing calm 409 reconnect envelope, and surface deterministic config/auth faults DISTINCTLY from a transport `TIMEOUT`. Wrap the two `decrypt_token` calls in the shared `_bind_user_token` and raise `HTTPException(409, RECONNECT_MESSAGE)` (one choke point covers `/approve`, reconcile, and `/refresh`). In `get_order_status_by_ref`, let `SchwabNotConfiguredError` propagate (add `except SchwabNotConfiguredError: raise` before the fence — READ-ONLY, safe, mirrors `place_order`'s deliberate non-masking) and map it to a calm 409 at the `reconcile_decision` endpoint, distinct from the transport→`TIMEOUT` path. Pin provider-match at both `/approve` and reconcile with regression tests. No schema change, no new order behavior, no new functional requirements.

## Boundaries & Constraints

**Always:**
- A calm 409 reconnect envelope (`RECONNECT_MESSAGE`) — never a raw 500 — for a decrypt failure during dependency resolution on `/approve`, `/refresh`, or reconcile, and for a deterministic config/auth fault (`SchwabNotConfiguredError`) surfaced by the reconcile read.
- Config/auth faults are surfaced DISTINCTLY from a transport `TIMEOUT`: a config fault yields an actionable 409 reconnect; a genuine transport indeterminacy still flows through as a normal reconcile result (`TIMEOUT` + `needs_reconfirmation`, `broker_ref` preserved) so the order stays reconcilable — no retry dead-end that re-launders a config fault as an infinitely-retryable timeout.
- `get_order_status_by_ref` stays READ-ONLY: surfacing `SchwabNotConfiguredError` never re-places, never searches, never introduces order-status ambiguity (the read simply could not happen). Its short-circuits (empty/None/non-numeric ref → honest `PENDING` without touching the SDK) are unchanged.
- The decrypt guard covers ONLY `TokenEncryptionError` from the two `decrypt_token` calls in `_bind_user_token`; the existing `SchwabNotConfiguredError(missing_message)` for an unlinked user (already refused by the entry gate) is untouched.
- Provider-match refusal (`session.provider == broker.provider`) remains enforced by `_assert_session_integrity` BEFORE any broker call at BOTH `execute_approved_order` and `reconcile_pending_decision`; a mismatch is refused with the same calm 409 (broker never touched, claim released at `/approve`). This story pins that with tests; it must not be weakened.
- The fake/non-Schwab path is unaffected: `_bind_user_token` passes a non-`SchwabAdapter` straight through, and the fake `get_order_status_by_ref` never raises `SchwabNotConfiguredError`.

**Block If:**
- Surfacing `SchwabNotConfiguredError` from `get_order_status_by_ref` would require changing the READ-ONLY reconcile contract or the terminal/monotonic reconcile semantics — HALT rather than redefine reconcile behavior.

**Never:**
- Never map a config/auth fault to `TIMEOUT` (the laundering being fixed), and never map a genuine transport indeterminacy to a 409 (that would wrongly tell a user to reconnect on a transient blip and could mask an unreconciled live order).
- Never leak a raw exception past the port for a transport/parse/shape failure — the bare `except Exception → TIMEOUT` fence for the actual `client.get_order` read stays.
- Never introduce a new `OrderStatus`/`DecisionRecord.status` value, a schema change, a migration, or a background scheduler.
- Never log or echo token/secret material, key values, or raw internal exception text in the calm envelope.
- Never broaden the decrypt guard into an app-wide `TokenEncryptionError` handler (keep it localized to the dependency site so an encrypt failure elsewhere is not silently reclassified as a 409).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Undecryptable token on `/approve` | `BROKER_ADAPTER=schwab`, linked token, ciphertext undecryptable (rotated key) | Calm 409 `RECONNECT_MESSAGE`; broker never built; no claim taken (fault precedes handler) | `TokenEncryptionError` → `HTTPException(409)` in `_bind_user_token` |
| Undecryptable token on `/refresh` | Same, via `get_reading_broker` | Calm 409 `RECONNECT_MESSAGE`; portfolio not read | Same choke point (shared `_bind_user_token`) |
| Undecryptable token on reconcile | Same, via `get_execution_broker` on `/decisions/{id}/reconcile` | Calm 409 `RECONNECT_MESSAGE`; nothing persisted | Same choke point |
| Reconcile hits config/auth fault | `cosigned` record w/ `broker_ref`; live+provider-matched session; adapter raises `SchwabNotConfiguredError` at client build | Calm 409 `RECONNECT_MESSAGE`; NOT `timeout`; nothing persisted | `SchwabNotConfiguredError` propagates → 409 at endpoint |
| Reconcile hits transport blip | Same, but `client.get_order` raises a transport/SDK error | 200 with `status=timeout`, `needs_reconfirmation=true`, `broker_ref` preserved (reconcilable) | Transport fenced → `TIMEOUT`; NOT a 409 |
| Provider mismatch at `/approve` | Live session `provider="schwab"`, adapter `provider="fake"` (or vice-versa) | Calm 409 `RECONNECT_MESSAGE`; broker never touched; claim released (`cosigning→proposed`) | `SessionIntegrityError` → 409 (existing) |
| Provider mismatch at reconcile | Same mismatch on `/decisions/{id}/reconcile` | Calm 409 `RECONNECT_MESSAGE`; broker never read | `SessionIntegrityError` → 409 (existing) |
| Empty/non-numeric ref on reconcile read | `broker_ref` "" / None / "not-an-id" | Honest `PENDING`, no SDK call (unchanged) | Short-circuit, no config-fault path reached |

</intent-contract>

## Code Map

- `ballast/backend/brokers/factory.py` -- `_bind_user_token` (shared by `get_execution_broker` + `get_reading_broker`): wrap the two `decrypt_token(...)` calls; on `TokenEncryptionError` raise `HTTPException(409, RECONNECT_MESSAGE)`. Add imports (`HTTPException`, `RECONNECT_MESSAGE`, `TokenEncryptionError`).
- `ballast/backend/brokers/schwab_adapter/adapter.py` -- `get_order_status_by_ref`: add `except SchwabNotConfiguredError: raise` immediately BEFORE the bare `except Exception:` fence so a client-build config fault surfaces distinctly; transport/parse read failures still → `TIMEOUT`.
- `ballast/backend/api/coach.py` -- `reconcile_decision`: after the `SessionIntegrityError` catch, add `except SchwabNotConfiguredError` → `HTTPException(409, RECONNECT_MESSAGE)`. Import `SchwabNotConfiguredError` from `brokers.schwab_adapter` (module import does NOT load schwab-py SDK). The `approve` provider-mismatch mapping already exists (unchanged).
- `ballast/backend/brokers/crypto.py` -- `TokenEncryptionError` / `decrypt_token` (referenced; unchanged).
- `ballast/backend/api/deps.py` -- `RECONNECT_MESSAGE` / `_RECONNECT_STATUS` (409) constant (referenced; unchanged).
- `ballast/backend/coach/execution.py` -- `_assert_session_integrity` (provider-match, unchanged); `reconcile_pending_decision` (must let `SchwabNotConfiguredError` propagate — verify no swallowing try/except is added).
- `ballast/backend/tests/test_brokerage.py` -- **add** decrypt-failure-in-dependency → 409 coverage for `get_execution_broker` + `get_reading_broker`.
- `ballast/backend/tests/test_schwab_adapter.py` -- **add** `get_order_status_by_ref` config-fault-surfaces-`SchwabNotConfiguredError` (not `TIMEOUT`); existing transport→`TIMEOUT` tests must stay green.
- `ballast/backend/tests/test_coach_api.py` -- **add** reconcile config-fault → 409; reconcile transport → 200/`timeout`; provider-mismatch regression at both `/approve` and reconcile.

## Tasks & Acceptance

**Execution:**
- [x] `ballast/backend/brokers/factory.py` -- import `HTTPException` (fastapi), `RECONNECT_MESSAGE` (api.deps), and `TokenEncryptionError` (brokers.crypto); wrap the `BrokerTokens(access_token=decrypt_token(...), refresh_token=decrypt_token(...))` construction in `_bind_user_token` in `try/except TokenEncryptionError` → `raise HTTPException(status_code=409, detail=RECONNECT_MESSAGE) from exc`. -- turns an undecryptable token into a calm 409 across `/approve`, reconcile, and `/refresh` at one shared choke point; never a raw 500.
- [x] `ballast/backend/brokers/schwab_adapter/adapter.py` -- in `get_order_status_by_ref`, insert `except SchwabNotConfiguredError:\n    raise` immediately before the existing `except Exception:` fence. -- a deterministic config/auth fault (from `_trading_client`/`_account_hash`) surfaces distinctly instead of being laundered into `TIMEOUT`; the READ-ONLY method still never re-places/searches, and a real transport failure on `client.get_order` still → `TIMEOUT` with the ref preserved.
- [x] `ballast/backend/api/coach.py` -- import `SchwabNotConfiguredError` from `brokers.schwab_adapter`; in `reconcile_decision`, add `except SchwabNotConfiguredError as exc:\n    raise HTTPException(status_code=409, detail=RECONNECT_MESSAGE) from exc` after the `SessionIntegrityError` catch (before the `if result.reconciled:` persist). -- a surfaced config fault becomes a calm 409 reconnect distinct from the transport→`TIMEOUT` result; nothing is persisted (the fault precedes `record_reconciliation`).
- [x] `ballast/backend/tests/test_brokerage.py` -- add tests: with a configured `SchwabAdapter` and a stored token row whose ciphertext cannot be decrypted, `get_execution_broker` AND `get_reading_broker` raise `HTTPException` with `status_code == 409` and `detail == RECONNECT_MESSAGE` (broker never returned); a non-Schwab adapter passes through untouched. -- pins the calm-409 decrypt guard at the shared dependency for both paths.
- [x] `ballast/backend/tests/test_schwab_adapter.py` -- add a test: a configured-but-tokenless `SchwabAdapter` (`token_read_func=None`) calling `get_order_status_by_ref("42")` raises `SchwabNotConfiguredError` (NOT a `TIMEOUT` outcome); confirm the existing transport-error and non-enumerated-fence tests still return `TIMEOUT`. -- proves config faults are surfaced distinctly from transport indeterminacy at the port.
- [x] `ballast/backend/tests/test_coach_api.py` -- add tests via the API: (a) an adapter whose `get_order_status_by_ref` raises `SchwabNotConfiguredError` → reconcile endpoint returns 409 with `RECONNECT_MESSAGE` and persists nothing; (b) an adapter whose read is a transport `TIMEOUT` → reconcile returns 200 `status=timeout` + `needs_reconfirmation=true` (distinct from the 409; pinned by the existing `test_reconcile_timeout_read_needs_reconfirmation`); (c) provider-mismatch regression: a live session whose `provider` disagrees with the adapter → 409 (broker never touched) at BOTH `/approve` (claim released back to `proposed`) and `/decisions/{id}/reconcile`. -- locks the calm-vs-timeout taxonomy and the provider-match invariant at both seams.

**Acceptance Criteria:**
- Given `BROKER_ADAPTER=schwab` and a linked token whose ciphertext cannot be decrypted, when `/approve`, `/refresh`, or `/decisions/{id}/reconcile` resolves its broker dependency, then the caller gets a calm 409 `RECONNECT_MESSAGE` (never a raw 500) and no broker call is made.
- Given a `cosigned` decision with a `broker_ref` and a live provider-matched session, when the reconcile read hits a deterministic config/auth fault (`SchwabNotConfiguredError`), then the endpoint returns a calm 409 reconnect, persists nothing, and does NOT surface it as `timeout` — whereas a genuine transport failure still returns 200 `timeout`/`needs_reconfirmation` with the `broker_ref` preserved.
- Given a live brokerage session whose `provider` does not match the configured adapter's `provider`, when `/approve` or reconcile runs, then the order/read is refused before any broker call with a calm 409, and at `/approve` the claim is released (`cosigning → proposed`, retryable).
- Given the full backend suite, when it runs, then the new envelopes are green and no existing test (transport→`TIMEOUT`, fake-path reconcile, entry-gate 409, sole-writer/canary) regresses.

## Spec Change Log

_No bad_spec loopbacks — the intent contract and spec held through review; all review findings were pre-existing defers or rejects, no code re-derivation._

## Review Triage Log

### 2026-08-01 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 0
- defer: 1: (high 0, medium 1, low 0)
- reject: 6
- addressed_findings:
  - none
- notes: Blind Hunter + Edge Case Hunter on the committed-to-be 7.3 diff. All three AC triggers verified correct and tested (decrypt→409 at the shared `_bind_user_token` choke point covering `/approve`+reconcile+`/refresh`; `SchwabNotConfiguredError`→409 distinct from transport→TIMEOUT at the reconcile read; provider-mismatch→409 before any broker call at both `/approve` and reconcile, broker untouched). **1 defer (medium):** the SAME config-class fault (`SchwabNotConfiguredError`) still surfaces as a raw 500 at the OTHER live seams this story's AC did not enumerate — `/approve` placement (`place_order`'s `_account_hash` empty-account/missing-hash body, caught by the handler's bare `except Exception`→release+re-raise→500) and `/refresh` read (`fetch_portfolio`, no handler try/except), plus the unlinked-user missing-token `SchwabNotConfiguredError` raised in `_bind_user_token` during dependency resolution (shadowed by `require_live_broker_session` but TOCTOU-reachable). All are PRE-EXISTING (unchanged by this diff) and outside the AC's enumerated triggers (which scope `/approve`+`/refresh` to the *decrypt* fault and the config fault to the *reconcile read*), so appended to the ledger for a focused calm-mapping follow-up rather than scope-creeping this story. **6 rejects:** (a) `_account_hash` empty/missing-hash body→409 on reconcile is INTENTIONAL and correct per the adapter's own documented config-vs-transport taxonomy — it improves the prior soft dead-end (TIMEOUT that re-launders forever) into an actionable calm 409, nothing persisted, self-heals; (b) non-`str` ciphertext→`AttributeError` escaping the `TokenEncryptionError` guard is unreachable given `BrokerageToken.access_token/refresh_token` are `Mapped[str]` (pre-existing `decrypt_token` type nit); (c) `HTTPException` raised in `brokers/factory.py` is acceptable — `get_execution_broker`/`get_reading_broker` are FastAPI dependency callables already importing from `api.deps`, this matches the ledger's own recommended fix, and a domain-error-then-map alternative cannot handle a dependency-resolution-time fault (the handler try/except never sees it); (d) the ordering invariant ("`SchwabNotConfiguredError` raised only at client build, before `client.get_order`") is already stated in the new comment; (e)+(f) the `object()` scope/session test doubles and a suggested extra invariant comment are cosmetic test-style nits — the tests correctly exercise the branches. Verification: `pytest tests/test_brokerage.py tests/test_schwab_adapter.py tests/test_coach_api.py` → 157 passed; full backend suite → 440 passed.

## Design Notes

- **One decrypt choke point.** `/approve` and `/decisions/{id}/reconcile` both depend on `get_execution_broker`, and `/refresh` on `get_reading_broker`; both delegate to `_bind_user_token`, so wrapping the decrypt there once fixes all three ledger sites (6.3 `get_execution_broker`, 6.5 `_bind_user_token`/`get_reading_broker`, and the reconcile path). Raising `HTTPException` inside a FastAPI dependency is idiomatic — it is caught by the app's `StarletteHTTPException` handler and rendered as the calm envelope, exactly like the `require_live_broker_session` entry gate.
- **Config fault vs. transport, at the port.** `SchwabNotConfiguredError` is only raised at client build (`_trading_client`/`_account_hash`), never by the actual `client.get_order` read. Adding `except SchwabNotConfiguredError: raise` before the bare fence therefore surfaces exactly the deterministic config class while leaving transport/parse/shape failures fenced to `TIMEOUT`. This mirrors `place_order` (which deliberately keeps `SchwabNotConfiguredError` OUT of its pre-placement `except` tuple) and `fetch_portfolio` (which builds the client before its `try`). It is safe here because the method is READ-ONLY — a surfaced config fault means the read never happened, so there is no phantom fill and no order ambiguity, unlike the post-placement fence which must never leak.
- **Provider-match is already enforced — this pins it.** `_assert_session_integrity` (Story 4.8) checks `session.provider == broker.provider` as the FIRST action of both `execute_approved_order` and `reconcile_pending_decision`, and both endpoints map `SessionIntegrityError → 409`. The deferred-ledger item that flagged `require_live_broker_session` for not checking provider is effectively closed by that execution-layer gate; 7.3 adds the missing regression coverage rather than re-implementing.

## Verification

**Commands:**
- `cd ballast/backend && python -m pytest tests/test_brokerage.py tests/test_schwab_adapter.py tests/test_coach_api.py -v` -- expected: new decrypt-409, config-fault-vs-timeout, and provider-mismatch tests pass; existing transport→`TIMEOUT` and entry-gate tests stay green (requires `docker compose up -d db`).
- `cd ballast/backend && python -m pytest` -- expected: full suite green (no regression to the sole-writer/canary or 6.7 reconcile behavior).

## Auto Run Result

Status: done

**Summary of implemented change:** Story 7.3 — every credential-gated Schwab failure mode now yields the calm 409 reconnect envelope (`RECONNECT_MESSAGE`) instead of a raw 500, and deterministic config/auth faults are surfaced DISTINCTLY from transport indeterminacy. (1) `_bind_user_token` (the single dependency-resolution choke point shared by `/approve`, `/decisions/{id}/reconcile`, and `/refresh`) wraps the two `decrypt_token` calls and converts a `TokenEncryptionError` (rotated key / corrupt ciphertext) into `HTTPException(409, RECONNECT_MESSAGE)`. (2) `SchwabAdapter.get_order_status_by_ref` lets a `SchwabNotConfiguredError` (raised only at client build, before the read) propagate via `except SchwabNotConfiguredError: raise` ahead of the bare transport fence, and `reconcile_decision` maps it to a calm 409 — distinct from a genuine transport `TIMEOUT`, which still returns 200 with `needs_reconfirmation` and the `broker_ref` preserved. (3) Provider-match (`session.provider == broker.provider`, already enforced by `_assert_session_integrity` since Story 4.8) is pinned by new regression tests at both `/approve` and reconcile. No schema/migration/new-status/scheduler changes.

**Files changed:**
- `brokers/factory.py` — `_bind_user_token`: `try/except TokenEncryptionError` → calm 409; added `HTTPException`, `RECONNECT_MESSAGE`, `TokenEncryptionError` imports.
- `brokers/schwab_adapter/adapter.py` — `get_order_status_by_ref`: `except SchwabNotConfiguredError: raise` before the `except Exception → TIMEOUT` fence (READ-ONLY, safe; mirrors `place_order`).
- `api/coach.py` — `reconcile_decision`: `except SchwabNotConfiguredError → HTTPException(409, RECONNECT_MESSAGE)`; module-level `SchwabNotConfiguredError` import (no SDK load).
- `tests/test_brokerage.py` — decrypt-failure → calm 409 at `get_execution_broker` + `get_reading_broker`; fake passthrough.
- `tests/test_schwab_adapter.py` — config-fault surfaces `SchwabNotConfiguredError` (not `TIMEOUT`) from the by-ref read.
- `tests/test_coach_api.py` — reconcile config-fault → 409 (persists nothing); reconcile provider-mismatch → 409 (broker untouched).

**Review findings breakdown:** 0 intent_gap, 0 bad_spec, 0 patch, 1 defer (medium), 6 reject. The defer: the same config-class `SchwabNotConfiguredError` still surfaces as a raw 500 at the live seams the AC did not enumerate — `/approve` placement (`_account_hash` body faults via `place_order`), `/refresh` read (`fetch_portfolio`), and the tokenless-user dependency-resolution path — all PRE-EXISTING and out of this story's AC scope; logged for a focused calm-mapping follow-up. Rejects were an intentional-and-correct config classification (`_account_hash` empty-body → 409 improves the prior soft dead-end), an unreachable non-`str`-ciphertext nit, an acceptable `HTTPException`-in-dependency layering call, and cosmetic comment/test-style nits.

**Verification:** `python -m pytest tests/test_brokerage.py tests/test_schwab_adapter.py tests/test_coach_api.py` → 157 passed; full backend suite `python -m pytest` → 440 passed (against docker Postgres). No regressions (6 new tests over the 434 baseline).

**Residual risks:** The one deferred item (config-class faults → raw 500 at `/approve` placement, `/refresh` read, and the tokenless dependency-resolution path) leaves an NFR8 calm-voice gap on the real-money placement seam. It is credential-gated/unexercised today and carries no double-place / persisted-money-truth risk (pre-placement, self-healing, nothing persisted), but it should be closed before or alongside the Story 7.6 real-money exercise.

**Follow-up review recommended:** false — this review pass made no code changes (all findings were defers/rejects); the implementation held as committed.
