---
title: 'Live-read robustness & multi-account safety'
type: 'bugfix'
created: '2026-08-01'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: ['oversized']
baseline_revision: 'e8b859d50d44ad6237d70c20ac03a2fa876b2aeb'
final_revision: '08847538e0ad5bf4b47cb86e2c14916ac3375c33'
---

<intent-contract>

## Intent

**Problem:** Three go-live-blocking gaps remain on the credential-gated live Schwab read/placement path. (1) The real `SchwabAdapter.fetch_portfolio()` is a **blocking** network read (`adapter.py:210` `client.get_account(...)`) called directly inside the `async` single-writer `reconcile_portfolio` (`brokers/portfolio.py:146`), which serves both `/portfolio/refresh` and import-on-connect — a slow Schwab read **stalls the event loop** for every concurrent request. (2) `_account_hash` **silently picks `accounts[0]`** (`adapter.py:604`) with an explicit `# v1 uses the first account` — a login exposing more than one account will place a **taxable buy into an IRA** with no selection, no refusal, and no record of which account was hit. (3) A **re-link** replaces the token rows (`api/brokerage.py:228-238`) but never clears the two-table portfolio projection (`portfolio_cache` + `portfolio_balance`); because balance reconcile is `as_of`-gated (`portfolio.py:154-188`) and import-on-connect is best-effort-swallowed (`brokerage.py:251`), a **new account inherits the prior account's cash/holdings** whenever the import fails or is skipped as stale.

**Approach:** Offload the blocking `fetch_portfolio()` read off the event loop with `anyio.to_thread.run_sync` (the network read only — the `AsyncSession` is never touched off-thread). Make account selection **explicit**: add `Settings.SCHWAB_ACCOUNT_ID` (the stable Schwab account **number**); `_account_hash` selects the matching account, uses the sole account when there is exactly one, and **refuses** (never `accounts[0]`) when a login exposes more than one and no id is configured — the resolved account hash rides back on `OrderOutcome.account_ref` and is persisted into the decision record's `cosign_snapshot`. On re-link, **clear both projection tables in the same transaction that replaces the token rows**, so a new account starts from an empty (honest) projection and the subsequent import repopulates via a first-ever reconcile — no stale cross-account cash. No new functional requirements; the existing FR2–FR11/AD-8/AD-14 contract is made true against real money.

## Boundaries & Constraints

**Always:** Offload only the pure `fetch_portfolio()` network read to the worker thread; all DB/`AsyncSession` work stays on the event loop. A login exposing >1 account with no `SCHWAB_ACCOUNT_ID` **refuses** (calm, pre-placement/pre-read, no order) — never `accounts[0]`. When `SCHWAB_ACCOUNT_ID` is set it must match a returned `accountNumber` or the operation refuses. The resolved account hash is carried on `OrderOutcome.account_ref` and persisted in the `cosign_snapshot`. Re-link clears `portfolio_cache` AND `portfolio_balance` for the user in the SAME transaction as the token replacement (scoped, fail-closed per `owner_id`). Broker errors never leak a raw exception past the port; config/selection faults stay distinct from a transport `TIMEOUT`. schwab-py stays lazily imported; no token/secret is logged.

**Block If:** the chosen selection mechanism would require a human product decision beyond a config value (it does not — `SCHWAB_ACCOUNT_ID` mirrors the existing `SCHWAB_*` gated-config pattern); the re-link clear cannot be made atomic with the token replacement within the existing callback transaction.

**Never:** Do NOT offload `LLMGateway.complete()` or the placement-write path (`place_order`/quote/`get_order`) in this story — offloading `complete()` would activate the unlocked module-level LLM factory cache + shared-client race deferred from Story 7.4; both are logged as deferred, not fixed here. No schema migration or new DB column (the account hash lives in the existing JSON `cosign_snapshot`). No new order semantics, no fractional/multi-account fan-out, no account-picker UI. Do not change reconcile-wins/`as_of` gating or the single-writer projection ownership (AD-14).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Read offloaded | `/refresh`, real adapter, `snapshot is None` | `fetch_portfolio()` runs via `anyio.to_thread.run_sync`; event loop not blocked; snapshot reconciled on-loop | Read failure still raises `SchwabReadError` (unchanged) |
| Single account | login returns 1 account, no `SCHWAB_ACCOUNT_ID` | that account's hash used (unambiguous); `account_ref` set | — |
| Multi-account, unselected | login returns >1 account, `SCHWAB_ACCOUNT_ID=""` | REFUSE before any place/read; no order; no `accounts[0]` | `SchwabAccountSelectionError` → calm 422 on approve (claim released); calm config-fault on read |
| Multi-account, selected | >1 account, `SCHWAB_ACCOUNT_ID` matches an `accountNumber` | that account's hash resolved + used; persisted on `cosign_snapshot` | selection not found → `SchwabAccountSelectionError` refuse |
| Re-link new account | user re-links a different Schwab login | old `portfolio_cache`+`portfolio_balance` deleted in the token-replacement commit; new import populates fresh (first-ever reconcile) | import failure ⇒ empty projection (honest), never prior account's cash |
| Re-link import fails | re-link, then `fetch_portfolio` raises | link survives (existing best-effort); projection stays empty | swallowed as today; no stale rows remain to mislead |

</intent-contract>

## Code Map

- `ballast/backend/brokers/portfolio.py` -- `reconcile_portfolio` (line 146): when `snapshot is None`, call `broker.fetch_portfolio` via `anyio.to_thread.run_sync(broker.fetch_portfolio)` so the blocking read is off the event loop. Only this call moves off-thread; all `session`/repo work stays on-loop.
- `ballast/backend/brokers/schwab_adapter/adapter.py` -- `_account_hash` (589-618): selection-aware. Build `(accountNumber, hashValue)` pairs defensively; if `Settings.SCHWAB_ACCOUNT_ID` set → pick the matching `accountNumber` (refuse if none matches); elif exactly one account → use it; elif >1 → raise `SchwabAccountSelectionError`. Never `accounts[0]`. `place_order`/`get_order_status`/`get_order_status_by_ref` set `account_ref=<resolved hash>` on the returned `OrderOutcome`. Add `class SchwabAccountSelectionError(SchwabNotConfiguredError)` (line ~46 block) + export it.
- `ballast/backend/brokers/schwab_adapter/__init__.py` -- export `SchwabAccountSelectionError` alongside `SchwabNotConfiguredError`.
- `ballast/backend/api/config.py` -- add `SCHWAB_ACCOUNT_ID: str = ""` to the `SCHWAB_*` block (line ~53) with a docstring: the stable Schwab account **number** selecting which account to trade/read when a login exposes more than one; empty = require exactly one account.
- `ballast/backend/brokers/port.py` -- `OrderOutcome` (96-111): add `account_ref: str | None = None` (broker-neutral; the account the order was placed/read against; `None` for the fake / when unknown).
- `ballast/backend/coach/decision_record.py` -- `cosign` (567-577): add `"account_ref": outcome.account_ref` to the `cosign_snapshot["outcome"]` dict; `_recovery_cosign_snapshot` (409-435): add `"account_ref": None` for shape parity.
- `ballast/backend/api/coach.py` -- approve handler (522-557): add `except SchwabAccountSelectionError` BEFORE the broad `except Exception`, mapping to a calm 422 with a clear message and `release_claim` (mirrors the `OrderNotPlaceableError` branch — pre-placement, no order, retryable). Import `SchwabAccountSelectionError`.
- `ballast/backend/api/brokerage.py` -- `callback` (227-238): BEFORE `session.commit()`, delete all `portfolio_cache` and `portfolio_balance` rows for the user via `ScopedRepository(...).list()` + `session.delete(...)`, so the projection clear commits atomically with the token replacement.
- `ballast/backend/pyproject.toml` -- add `anyio` to `dependencies` (already present transitively via Starlette; make the direct import explicit).
- `ballast/backend/tests/test_portfolio.py`, `test_schwab_adapter.py`, `test_brokerage.py`, `test_coach_api.py` -- add coverage per Tasks below.

## Tasks & Acceptance

**Execution:**
- [x] `ballast/backend/pyproject.toml` -- add `anyio` to `dependencies`. -- makes the direct `anyio.to_thread` import explicit rather than relying on a transitive dep.
- [x] `ballast/backend/brokers/portfolio.py` -- offload `broker.fetch_portfolio()` via `anyio.to_thread.run_sync` when no `snapshot` is passed. -- a slow live read no longer stalls the event loop for concurrent requests; the `AsyncSession` never crosses the thread boundary.
- [x] `ballast/backend/api/config.py` -- add `SCHWAB_ACCOUNT_ID: str = ""` with docstring. -- env-tunable explicit account selection, consistent with the gated `SCHWAB_*` pattern.
- [x] `ballast/backend/brokers/schwab_adapter/adapter.py` -- add `SchwabAccountSelectionError(SchwabNotConfiguredError)`; make `_account_hash` selection-aware (match `SCHWAB_ACCOUNT_ID`; sole account ok; refuse on ambiguous multi-account; never `accounts[0]`); set `account_ref` on the outcomes returned by `place_order`/`get_order_status`/`get_order_status_by_ref`. -- closes the wrong-account placement surface and records which account was used.
- [x] `ballast/backend/brokers/schwab_adapter/__init__.py` -- export `SchwabAccountSelectionError`. -- lets the API layer catch it without importing schwab-py (AD-8, existing precedent).
- [x] `ballast/backend/brokers/port.py` -- add `OrderOutcome.account_ref: str | None = None`. -- broker-neutral field carrying the placed/read account back to the persistence layer.
- [x] `ballast/backend/coach/decision_record.py` -- persist `account_ref` in `cosign` and `_recovery_cosign_snapshot`. -- the chosen account hash is durable on the decision record's `cosign_snapshot` (no schema change).
- [x] `ballast/backend/api/coach.py` -- catch `SchwabAccountSelectionError` in the approve handler → calm 422 + `release_claim`. -- an ambiguous-account refusal is a calm, retryable, pre-placement 422, symmetric with `OrderNotPlaceableError`, never a raw 500.
- [x] `ballast/backend/api/brokerage.py` -- in `callback`, clear both projection tables (scoped, per `owner_id`) in the same transaction as the token replacement, before commit. -- a re-linked (different) account can never inherit the prior account's cash/holdings under a staleness skip or import failure.
- [x] `ballast/backend/tests/*` -- add the tests enumerated in Acceptance (offload, account selection, account_ref persistence, re-link clear). -- pins each edge case in the I/O matrix offline (mocked schwab client, injected snapshot).

**Acceptance Criteria:**
- Given the real adapter and no injected snapshot, when `reconcile_portfolio` runs, then `broker.fetch_portfolio` is invoked through `anyio.to_thread.run_sync` (asserted via a spy/patch), and reconcile still writes both tables correctly on the event loop.
- Given a login exposing more than one account and `SCHWAB_ACCOUNT_ID` unset, when `_account_hash` is resolved (place or read), then it raises `SchwabAccountSelectionError` and no `accounts[0]` hash is ever returned; the approve handler surfaces a calm 422 and releases the claim (decision retryable, broker order never placed).
- Given `SCHWAB_ACCOUNT_ID` set, when it matches a returned `accountNumber`, then that account's `hashValue` is used and no other account is touched; when it matches none, then `SchwabAccountSelectionError` is raised (refuse, no order).
- Given exactly one account, when `_account_hash` is resolved, then that account is used unchanged (no regression of the single-account path).
- Given a placed order on the real adapter, when it co-signs, then `cosign_snapshot["outcome"]["account_ref"]` equals the resolved account hash used for the placement.
- Given a user with existing `portfolio_cache` + `portfolio_balance` rows, when they re-link, then both tables are emptied for that user in the same committed transaction as the token replacement; a subsequent import populates from the new account, and if the import fails the projection is empty (never the prior account's data).
- Given the full backend suite, when it runs, then all new coverage is green and nothing regresses (existing portfolio reconcile, schwab adapter, brokerage callback, approve/cosign, fake-path tests all stay green).

## Spec Change Log

_No bad_spec loopbacks — the intent contract and spec held through review. Every review finding was a caused-by-this-change patch (auto-fixed in the review pass), a forward-looking defer, or a reject; no code re-derivation from an amended spec._

## Review Triage Log

### 2026-08-01 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 5: (high 0, medium 3, low 2)
- defer: 2: (high 0, medium 1, low 1)
- reject: 4
- addressed_findings:
  - `[medium]` `[patch]` The new `SchwabAccountSelectionError` raised from `_account_hash` on the READ path (`fetch_portfolio` → `reconcile_portfolio` → `/portfolio/refresh`) leaked as a raw 500 — the endpoint had no handler — contradicting the spec I/O matrix's "calm config-fault on read". Added a `try/except SchwabAccountSelectionError` in `api/portfolio.py::refresh_portfolio` mapping it to a calm 422 with the actionable "set SCHWAB_ACCOUNT_ID" message (symmetric with the approve path), plus an endpoint test asserting 422 (not 500).
  - `[medium]` `[patch]` A MULTI-account login with one malformed (non-dict) entry left exactly one well-formed pair, so the `len(pairs) == 1` branch silently traded the sole survivor — a residual never-`accounts[0]` hole. Reworked `_account_hash` to judge ambiguity on the RAW `len(accounts)` (not the survivor count), so any >1-account body with no `SCHWAB_ACCOUNT_ID` refuses; folded in the all-non-dict case. Added a regression test.
  - `[medium]` `[patch]` A duplicate `accountNumber == SCHWAB_ACCOUNT_ID` made `next(...)` silently pick the first duplicate's hash (wrong-account risk). Added a `len(matches) > 1` ambiguity refusal + test.
  - `[low]` `[patch]` A selected account whose `hashValue` was null was reported via the misleading "does not match any account" message. Distinguished matched-but-null-hash → a clear `SchwabNotConfiguredError("selected account is missing its trading hash")` + test.
  - `[low]` `[patch]` `accountNumber` was compared raw against the `str` config value (a numeric `accountNumber` from the live API would never match). `str()`-normalized the comparison so live payloads compare equal.
- notes: Blind Hunter + Edge Case Hunter on the committed-to-be 7.5 diff. **2 defers:** (medium) the re-link projection clear fires on EVERY callback including a routine same-account re-auth, so a transient import failure leaves a temporarily-empty projection — gate the clear on a detected account change (self-heals on next `/refresh`; the spec deliberately chose empty-over-wrong, so this is a refinement, not a regression of intent); (low-medium) the `anyio.to_thread` offload shares Starlette's ~40-token threadpool, a scaling ceiling under many concurrent slow Schwab reads — isolate behind a dedicated `CapacityLimiter` if concurrency grows. Both logged to the ledger. **4 rejects:** (a) a null `accountNumber` on the SOLE account is silently accepted — safe, a single account is unambiguous regardless of its number; (b) `get_order_status` launders `SchwabAccountSelectionError` into `TIMEOUT` while `get_order_status_by_ref` surfaces it — pre-existing asymmetry AND moot on the approve path (`place_order`'s `_account_hash` fires the selection refusal first, before any indeterminate-reconcile read); (c) `import anyio.to_thread` grouping nit — cosmetic; (d) `anyio` added unpinned — matches the repo's majority-unpinned dependency style and `uv.lock` pins it to 4.14.2. Verification: `pytest tests/test_schwab_adapter.py tests/test_portfolio.py tests/test_brokerage.py tests/test_coach_api.py` → 189 passed; full backend suite → 461 passed.

### 2026-08-01 — Review pass (follow-up)
- intent_gap: 0
- bad_spec: 0
- patch: 2: (high 0, medium 1, low 1)
- defer: 0
- reject: 12
- addressed_findings:
  - `[medium]` `[patch]` `record_reconciliation` built `reconciliation_snapshot.outcome` with `status`/`filled_qty`/`avg_price`/`broker_ref` but NOT `account_ref`, even though the reconcile read paths (`get_order_status`/`get_order_status_by_ref`) now resolve and carry `account_ref` — violating that snapshot's documented "SAME shape as `cosign_snapshot.outcome`" invariant (cosign_snapshot.outcome gained `account_ref` this story) and dropping the which-account audit on the durable reconcile path. Added `"account_ref": outcome.account_ref` to the reconciliation snapshot (`coach/decision_record.py`) for shape parity + audit completeness.
  - `[low]` `[patch]` `SCHWAB_ACCOUNT_ID` was matched raw, so a whitespace-only env value (a stray blank in an env file) was truthy and entered the match branch, raising the misleading "does not match any account" instead of being treated as unset. Added `.strip()` so a blank value falls through to the count-based selection (`brokers/schwab_adapter/adapter.py`).
- notes: Follow-up review pass (spec was `done` with `followup_review_recommended: true`). Blind Hunter + Edge Case Hunter re-ran on the full baseline diff. Both flagged the same two headline concerns as the original pass — (1) the re-link clear firing on same-account re-auth and (2) the shared-threadpool scaling ceiling — but **both are already in the deferred-work ledger** from the original 7.5 pass, so no new ledger entries were added (per the NEW-entries-only instruction). **2 new patches** applied above. **12 rejects:** `_recovery_cosign_snapshot` hardcoding `account_ref=None` (by-design — the account hash is never persisted queryably, and the no-schema-change boundary forbids fixing it; `broker_ref` still identifies the order); per-request adapter lifecycle makes the alleged off-thread `_account_hash_cache` race and the "SCHWAB_ACCOUNT_ID changed after cache" staleness both unreachable (fresh adapter per request); the callback delete-before-commit "aborts re-link on delete failure" is the intended fail-closed atomicity, not a bug; the swallowed post-commit import is the pre-existing best-effort pattern over a request-scoped session; `SCHWAB_ACCOUNT_ID="None"` literal-string matching a null `accountNumber` is contrived; duplicate-`accountNumber` unreachability and duplicate-refusal message nuance are by-design safe refusals; `anyio` unpin / import-group nits already adjudicated in the prior pass. Verification: `pytest tests/test_schwab_adapter.py tests/test_portfolio.py tests/test_coach_api.py tests/test_brokerage.py` → 189 passed; `tests/test_recoverable_placement.py tests/test_migrations.py` (reconciliation-shape coverage) → 17 passed.

## Design Notes

- **Offload the read only, not the write.** `reconcile_portfolio` owns the `AsyncSession`; SQLAlchemy async sessions are not thread-safe. So only `broker.fetch_portfolio()` (a pure network→`PortfolioSnapshot`, no DB) crosses to the worker thread via `anyio.to_thread.run_sync`; the thread returns the snapshot and all reconcile writes run back on the event loop. `anyio.to_thread` uses the same worker pool Starlette runs on (its default ~40-token limiter) — this fixes the event-loop stall; isolating slow broker reads behind a dedicated `CapacityLimiter` so they can't contend with other sync work at scale is a logged defer.
- **Why not offload `complete()` too.** Story 7.4's review explicitly deferred to 7.5 the fact that the module-level LLM factory cache and the shared gateway client have no locking — safe only because nothing offloads them today. This story deliberately does NOT offload `complete()` or the placement writes, so that race stays dormant; it is re-logged as deferred, not silently activated.
- **Selection by stable account NUMBER, hash on the record.** Schwab `get_account_numbers()` returns `{accountNumber, hashValue}`; the `hashValue` is opaque and can rotate, while `accountNumber` is stable — so the CONFIG selector is the account number, and what we PERSIST (the audit of where the order actually landed) is the resolved `hashValue` via `account_ref`. Example refusal shape (multi-account, unset id): `SchwabAccountSelectionError("This Schwab login exposes more than one account; set SCHWAB_ACCOUNT_ID to choose which one to trade. No order was placed.")`.
- **Subclass keeps the read path calm for free.** `SchwabAccountSelectionError(SchwabNotConfiguredError)` means the read path (`fetch_portfolio` / reconcile) already treats it as a distinct config fault (not a `SchwabReadError`), and import-on-connect still swallows it (link survives). Only the approve handler needs the one new explicit `except` for the calm 422.
- **Clear-then-import, not clear-inside-reconcile.** Clearing both tables in the token-replacement transaction (before commit) means the balance row is gone, so the following import takes the first-ever-reconcile INSERT branch — no `as_of` gate can skip it. If the import then fails, an empty projection is the honest state; the prior account's cash is already gone.

## Verification

**Commands:**
- `cd ballast/backend && uv run python -m pytest tests/test_portfolio.py tests/test_schwab_adapter.py tests/test_brokerage.py tests/test_coach_api.py -v` -- expected: new offload, account-selection, `account_ref`-persistence, and re-link-clear tests pass; existing tests stay green.
- `cd ballast/backend && uv run python -m pytest` -- expected: full backend suite green, no regression (requires `docker compose up -d db`).


## Auto Run Result

Status: done (follow-up review pass on a `done` spec that carried `followup_review_recommended: true`).

**Implemented change (this pass):** An independent Blind Hunter + Edge Case Hunter review of the full Story 7.5 diff (baseline `e8b859d`). The story itself — offload the blocking Schwab `fetch_portfolio` read off the event loop, explicit `SCHWAB_ACCOUNT_ID` account selection (refuse, never `accounts[0]`, on ambiguous multi-account), `account_ref` audit on `OrderOutcome` + `cosign_snapshot`, and the re-link two-table projection clear — was left intact. This pass applied two caused-by-the-change patches and adjudicated the rest.

**Files changed (this pass):**
- `ballast/backend/coach/decision_record.py` — added `"account_ref": outcome.account_ref` to `record_reconciliation`'s `reconciliation_snapshot.outcome`, restoring its documented "SAME shape as `cosign_snapshot.outcome`" parity and completing the which-account audit on the durable reconcile path (the reconcile reads already resolve `account_ref`).
- `ballast/backend/brokers/schwab_adapter/adapter.py` — `.strip()` on `SCHWAB_ACCOUNT_ID` so a whitespace-only env value is treated as unset (falls through to count-based selection) instead of raising a misleading "does not match any account".

**Review findings breakdown:** 2 patches applied (1 medium: reconcile-snapshot `account_ref` parity; 1 low: `SCHWAB_ACCOUNT_ID` strip). 0 new defers — the two headline concerns both reviewers raised (re-link clear on same-account re-auth; shared-threadpool scaling ceiling) are ALREADY in the deferred-work ledger from the original 7.5 pass, so no new ledger entries were appended (honoring NEW-entries-only). 12 rejects (by-design or unreachable: `_recovery_cosign_snapshot` `account_ref=None` bounded by the no-schema-change contract; per-request adapter lifecycle defeats the off-thread cache-race / stale-selection claims; fail-closed delete-before-commit atomicity; pre-existing best-effort import swallow; contrived literal-`"None"` match; by-design duplicate-account refusals; already-adjudicated `anyio` unpin/import nits).

**Verification:**
- `uv run pytest tests/test_schwab_adapter.py tests/test_portfolio.py tests/test_coach_api.py tests/test_brokerage.py -q` → 189 passed.
- `uv run pytest tests/test_recoverable_placement.py tests/test_migrations.py -q` (reconciliation-shape coverage for the patched snapshot) → 17 passed.
- `uv run pytest -q` (full backend suite) → 461 passed.

**Residual risks:** Both are already-logged defers, not blockers for the credential-gated single-account go-live path: (1) a routine same-account re-auth still clears the projection and relies on best-effort import repopulation (self-heals on the next `/refresh`); (2) the `anyio.to_thread` offload shares Starlette's ~40-token worker pool (a scaling ceiling, a net improvement over the prior event-loop stall). The `account_ref` audit remains unrecoverable on the crash-recovery snapshot path (`_recovery_cosign_snapshot`) by design, bounded by the story's no-schema-change constraint; `broker_ref` still identifies the order there.
