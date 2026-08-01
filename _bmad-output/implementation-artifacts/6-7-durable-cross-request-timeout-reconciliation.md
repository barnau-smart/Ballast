---
title: 'Story 6.7 — Durable Cross-Request Timeout Reconciliation'
type: 'feature'
created: '2026-08-01'
status: 'done'
baseline_revision: '31999828c740791d4e8ed3ad5bc94b2f99ab8040'
final_revision: '0c6dd6389ca969bfdf7d8545cc86d49799ef5dfa'
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-6-context.md'
warnings: [oversized]
---

<intent-contract>

## Intent

**Problem:** When a placement is surfaced `pending`/`timeout` (Story 6.3), the only reconciliation is in-request: `execute_approved_order` → `_reconcile` reads once via `get_order_status(idempotency_key)` against the adapter's **in-instance** `self._orders` cache, which dies with the request. A cosigned decision left `pending`/`timeout` — with its authoritative `broker_ref` (order id) already persisted as a queryable column — has **no way to be resolved in a later request**: a fresh adapter has an empty cache, so `get_order_status` just returns `PENDING` again. The pending state is stranded, and because Schwab honors no client idempotency key, the system must never guess or re-place.

**Approach:** Add a durable, read-only, cross-request reconcile path keyed on the **persisted `broker_ref`/order id** (never the dead in-request cache, never fuzzy attribute-matching). A new port method `get_order_status_by_ref(broker_ref)` reads one order directly by its id (`client.get_order`); the Coach Engine's `reconcile_pending_decision` is its sole caller. An explicit user/status action — `POST /api/coach/decisions/{id}/reconcile` — loads the scoped decision, reads the true `OrderOutcome`, and persists it additively to new `reconciliation_snapshot`/`reconciled_at` columns (the immutable `recommendation_snapshot`/`cosign_snapshot` are never mutated, so verbatim replay stays byte-identical). It **never** calls `place_order`. When the order cannot be positively confirmed — `broker_ref is None`, or the read is still indeterminate — the decision stays `pending` and the response signals `needs_reconfirmation` so a human can decide; the system never auto-resolves or re-places.

## Boundaries & Constraints

**Always:**
- Reconcile authoritative state **only** by the persisted queryable `broker_ref` (order id) via a single `client.get_order(order_id, hash)` read. Only `brokers/schwab_adapter/` imports `schwab-py`, lazily (AD-8 sole-caller structural test stays green).
- `coach/execution.py`'s `reconcile_pending_decision` is the **sole caller** of `get_order_status_by_ref` (AD-7, mirroring the `place_order`/`get_order_status` ownership). It asserts the same placement-time session integrity as `execute_approved_order` (session live AND provider match, case/whitespace-insensitive) before any broker call, raising `SessionIntegrityError`.
- The reconcile path is **read-only**: it NEVER calls `place_order`, never mints a new key, never loops/polls (one `get_order` read), never touches the broker when `broker_ref is None`.
- Map broker outcomes truthfully via the existing `_map_order`; transport/SDK exceptions on the read → `OrderStatus.TIMEOUT` (indeterminate) with the `broker_ref` **preserved** and **no raw exception leaking the port** (mirror the `get_order_status` fence). Money stays `Decimal` / fixed-point strings (`format_money`); no `float`, no `E+` on the wire.
- Persist the reconciled outcome **additively**: `record_reconciliation` writes `reconciliation_snapshot` (outcome as fixed-point strings, same shape as `cosign_snapshot.outcome`) + `reconciled_at` (tz-aware UTC). It asserts `record.status == "cosigned"` and NEVER mutates `recommendation_snapshot`, `cosign_snapshot`, `status`, or `broker_ref`. Reconciliation may run repeatedly (latest-known-truth), so these fields are write-latest, not write-once.
- Per-user isolation: the reconcile endpoint loads the decision via the fail-closed scoped `load_decision(decision_id, scope)` → a foreign/unknown id 404s; no foreign row is ever read or reconciled.
- When the true outcome cannot be positively confirmed (`broker_ref is None`, OR the reconciled read is still `pending`/`timeout`), leave it `pending`, set `needs_reconfirmation=true`, and surface a calm honest message — never optimistic, never a re-place.

**Block If:**
- A live Schwab API call (real creds / real network / real money) is required to satisfy any acceptance criterion. All ACs are offline against the mocked schwab-py client and the fake adapter (the Story 6.2/6.3 injection pattern). The one-time live reconcile against a real stranded order is a documented manual go-live step behind real `SCHWAB_*` creds + a live session — a credentials decision. If an AC cannot be met without it, HALT.

**Never:**
- No `get_orders_for_account`, no attribute/amount/time fuzzy-matching, no auto-search when `broker_ref is None`.
- No re-placement on ambiguity, and no automatic re-confirmation workflow — the response only *signals* `needs_reconfirmation`; a human choosing to act is out of scope (a fresh recommend→approve).
- No mutation of the immutable `recommendation_snapshot`/`cosign_snapshot` or the `status` machine (`proposed`/`cosigning`/`cosigned` unchanged); verbatim replay of those snapshots must stay byte-identical.
- Out of scope (remain deferred-work ledger items — a distinct write-path atomicity mechanism, not reconciliation): the post-placement `cosign`/`commit` atomicity gap that strands a record in `cosigning` with a live order and **no** persisted `broker_ref`; and the released-claim approve-retry double-place residual. Both are unreachable by a `broker_ref`-keyed reconcile (the ref was never persisted in those escapes). Do not attempt them here.
- No changes to `OrderIntent`, `OrderOutcome`/`OrderStatus` shapes, `place_order`/`get_order_status` signatures, or the fake adapter's existing behavior.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Resolves to filled | cosigned decision, `cosign_snapshot.outcome.status=pending`, `broker_ref="42"`; `get_order(42)` → FILLED qty2 px100 | `get_order_status_by_ref` → `OrderOutcome{FILLED,2,100,"42"}`; `reconciliation_snapshot`+`reconciled_at` persisted; response `{status:filled, needs_reconfirmation:false}` | none; single read |
| Still working | `broker_ref="42"`; `get_order(42)` → WORKING/QUEUED | `OrderOutcome{PENDING,0,ref}`; persisted; response `{status:pending, needs_reconfirmation:false}` (retryable) | none |
| No order id (true timeout) | outcome `pending`/`timeout`, `broker_ref is None` | broker **never called**; stays pending; response `{status:pending, needs_reconfirmation:true}`; no persist needed | never search/guess |
| Transport error on read | `broker_ref="42"`; `get_order` raises `httpx.TimeoutException` | `OrderOutcome{TIMEOUT, broker_ref="42"}`; no raw exception escapes port; stays pending; `needs_reconfirmation:true` | fenced → TIMEOUT |
| Non-numeric ref | `broker_ref="not-an-id"` | honest indeterminate (PENDING/TIMEOUT), never `get_orders_for_account` | degrades safely |
| Already terminal | `cosign_snapshot.outcome.status=filled` (or rejected) | broker **never called**; returns the recorded terminal outcome; idempotent | none |
| Foreign / unknown id | `decision_id` not owned by caller / missing | 404; broker never touched | scoped load |
| Not cosigned | decision `status` in {`proposed`,`cosigning`} | 422 calm (nothing placed to reconcile); broker never touched | typed |
| Session not live | `require_live_broker_session` fails | 409 calm reconnect; broker never touched | entry gate |
| Never re-places | any reconcile path | `place_order` is NEVER called | invariant |

</intent-contract>

## Code Map

- `ballast/backend/brokers/port.py` -- ADD abstract `async def get_order_status_by_ref(self, broker_ref: str) -> OrderOutcome` to `BrokerPort`. Document: reconcile a previously-placed order **by its persisted broker order id** (durable cross-request path, Story 6.7); read-only, single `get_order`, NEVER places, NEVER `get_orders_for_account`; an unknown/empty/unusable ref → honest `PENDING` (`filled_qty=0`, `broker_ref=None`); transport/parse error → `TIMEOUT` with the ref preserved, no raw exception leaking the port. No changes to `OrderOutcome`/`OrderStatus`/`OrderIntent` or existing method signatures.
- `ballast/backend/brokers/schwab_adapter/adapter.py` -- IMPLEMENT `get_order_status_by_ref(broker_ref)`: guard empty/non-usable ref → `OrderOutcome{PENDING, 0, broker_ref=None}` (never call the SDK); else build client → resolve hash → `client.get_order(int(broker_ref), hash)` → `_map_order(json, broker_ref=broker_ref)`. Wrap the whole build+read in the SAME `except Exception:` fence used by `get_order_status` → return `OrderOutcome{TIMEOUT, 0, broker_ref=broker_ref}` (id preserved), never let a raw exception escape, never `get_orders_for_account`. Reuse `_trading_client`/`_account_hash`/`_map_order` unchanged.
- `ballast/backend/brokers/fake_adapter.py` -- IMPLEMENT `get_order_status_by_ref(broker_ref)`: maintain a `self._orders_by_ref: dict[str, OrderOutcome]` populated in `place_order` whenever the recorded outcome carries a non-`None` `broker_ref` (plus a small test-seed helper to register a `broker_ref → OrderOutcome` a later reconcile should observe). Unknown/`None` ref → honest `PENDING` (`filled_qty=0`, `broker_ref=None`), deterministic, no search. Keep existing `place_order`/`get_order_status` behavior unchanged.
- `ballast/backend/db/models.py` -- ADD to `DecisionRecord`: `reconciliation_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)` and `reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)`. Additive, nullable; docstring caveat: like `broker_ref`/`idempotency_key`, `create_all` will NOT `ALTER` an existing table — a go-live schema step.
- `ballast/backend/coach/decision_record.py` -- ADD `record_reconciliation(record, *, outcome: OrderOutcome, now: datetime | None = None) -> None`: assert `record.status == "cosigned"`; set `record.reconciliation_snapshot = {"outcome": {status, filled_qty(str), avg_price(str|None), broker_ref}}` (fixed-point strings via the same money formatter `cosign` uses) and `record.reconciled_at = now or datetime.now(UTC)`. NEVER touch `recommendation_snapshot`/`cosign_snapshot`/`status`/`broker_ref` (caller commits). ADD `effective_outcome_status(record) -> str` returning `reconciliation_snapshot.outcome.status` if present else `cosign_snapshot.outcome.status` (the current truthful status), and a small `_is_terminal(status)` helper (terminal = filled/partial/rejected).
- `ballast/backend/coach/execution.py` -- ADD `async def reconcile_pending_decision(record, *, broker, broker_session) -> ReconcileResult` (a small frozen dataclass `{outcome: OrderOutcome, needs_reconfirmation: bool, reconciled: bool}`). Sole caller of `get_order_status_by_ref`. Assert session integrity FIRST (reuse the exact live+provider-match check from `execute_approved_order`; factor it into a shared `_assert_session_integrity(broker, broker_session)` helper both call). If `effective_outcome_status` is terminal → return the recorded outcome, `reconciled=False`, `needs_reconfirmation=False`, no broker call. If non-terminal and `record.broker_ref is None` → no broker call, `needs_reconfirmation=True`. Else `outcome = await broker.get_order_status_by_ref(record.broker_ref)`; `needs_reconfirmation = outcome.status in INDETERMINATE`; `reconciled=True`. NEVER call `place_order`.
- `ballast/backend/api/coach.py` -- ADD `POST /decisions/{decision_id}/reconcile` (deps: `get_scope`, `require_live_broker_session`, `get_execution_broker`, `get_async_session`). Load via `load_decision(decision_id, scope)` → 404 on foreign/unknown. If `record.status != "cosigned"` → 422 calm ("This decision has no placed order to reconcile yet."). Call `reconcile_pending_decision`; when it returns `reconciled=True`, `record_reconciliation(...)` + `await session.commit()`. Return new `ReconcileResponse{status, filled_qty, avg_price, broker_ref, needs_reconfirmation}` (money via `format_money`). Map `SessionIntegrityError` to the calm 409 `RECONNECT_MESSAGE` exactly as `approve()` does. ALSO extend `DecisionDetailResponse` additively with `reconciliation_snapshot: dict | None = None` and `reconciled_at: str | None = None` (pass-through), and `DecisionSummaryOut.outcome_status` to reflect `effective_outcome_status` — the immutable snapshots are untouched so verbatim replay stays byte-identical.
- `ballast/backend/tests/test_schwab_adapter.py` -- ADD mocked-SDK tests for `get_order_status_by_ref`: happy resolve→FILLED (`get_order(int(ref),hash)`), working→PENDING, transport→TIMEOUT with ref preserved & no raw leak, empty/non-numeric ref → indeterminate with NO SDK call, and assert `get_orders_for_account` is NEVER called. Keep the AD-8 sole-caller structural test green.
- `ballast/backend/tests/test_coach_api.py` -- ADD reconcile-endpoint tests: pending+`broker_ref`→FILLED persists `reconciliation_snapshot`/`reconciled_at` and surfaces `status=filled, needs_reconfirmation=false`; pending+`broker_ref is None`→stays pending, `needs_reconfirmation=true`, broker `place_order`/`get_order` NEVER called; already-terminal→idempotent, no broker call; foreign id→404; non-cosigned→422; session-not-live→409; `place_order` is never called on any reconcile path; `/decisions/{id}` replay stays byte-identical after adding the additive fields; per-user isolation holds.
- `ballast/backend/tests/test_brokerage.py` -- ADD fake-adapter `get_order_status_by_ref`: seeded `broker_ref`→outcome resolves; unknown ref→PENDING; and a sole-caller structural test that only `coach/execution.py` calls `get_order_status_by_ref`.

## Tasks & Acceptance

**Execution:**
- [x] `ballast/backend/brokers/port.py` -- add abstract `get_order_status_by_ref`; document read-only-by-order-id / no-search / transport→TIMEOUT-preserve-ref contract.
- [x] `ballast/backend/brokers/schwab_adapter/adapter.py` -- implement `get_order_status_by_ref` (empty-ref guard → PENDING; `get_order(int(ref),hash)` → `_map_order`; fence → TIMEOUT with ref preserved; never `get_orders_for_account`).
- [x] `ballast/backend/brokers/fake_adapter.py` -- implement `get_order_status_by_ref` via a `broker_ref → OrderOutcome` map + seed helper; unknown → PENDING.
- [x] `ballast/backend/db/models.py` -- add nullable `reconciliation_snapshot` (JSON) + `reconciled_at` (tz DateTime) to `DecisionRecord`; create_all-caveat docstring.
- [x] `ballast/backend/coach/decision_record.py` -- add `record_reconciliation` (write-latest, cosigned-only, immutable snapshots untouched) + `effective_outcome_status`/`_is_terminal`.
- [x] `ballast/backend/coach/execution.py` -- add `reconcile_pending_decision` (sole caller of `get_order_status_by_ref`; shared `_assert_session_integrity`; terminal short-circuit; `broker_ref is None` → needs_reconfirmation, no broker call; never places).
- [x] `ballast/backend/api/coach.py` -- add `POST /decisions/{id}/reconcile` + `ReconcileResponse`; persist on `reconciled`; additive `reconciliation_snapshot`/`reconciled_at` on detail and `effective_outcome_status` on the summary; SessionIntegrityError→409.
- [x] `ballast/backend/tests/test_schwab_adapter.py` + `tests/test_coach_api.py` + `tests/test_brokerage.py` -- add the I/O-matrix + endpoint + fake + sole-caller tests.

**Acceptance Criteria:**
- Given a cosigned decision surfaced `pending` whose queryable `broker_ref` is set, when the user calls `POST /decisions/{id}/reconcile` and the broker now reports the order FILLED, then the Coach Engine reads it by `broker_ref` via a single `get_order`, persists the true outcome to `reconciliation_snapshot`/`reconciled_at`, and returns the honest `filled` outcome with `needs_reconfirmation=false` — no `place_order`, no `get_orders_for_account` (FR22, NFR3, AD-13).
- Given a cosigned decision surfaced `pending`/`timeout` with **no** confirmed `order_id` (`broker_ref is None`), when it is reconciled, then the broker is never touched, the decision stays `pending`, and the response sets `needs_reconfirmation=true` prompting explicit human re-confirmation — never guessing, never re-placing (upholds never-double-place / never-phantom-fill).
- Given a reconcile read that raises a transport error (→ `timeout`), when reconciliation runs, then no raw exception escapes the port, `broker_ref` is preserved, `needs_reconfirmation=true`, and no phantom or duplicate order occurs. Given instead a read that reports a still-working order (→ `pending`), then it is surfaced honestly with `needs_reconfirmation=false` — a successful read is a positive confirmation (the I/O matrix "Still working" row; the epic 6.7 "cannot be positively confirmed" test), retryable-without-a-human, so it never nudges toward re-placing a live order.
- Given a decision id that is foreign/unknown, or a decision not in `cosigned` status, or a broker session that is not live, when reconcile is called, then it returns 404 / 422 / 409 (calm reconnect) respectively and the broker is never touched.
- Given `/decisions` and `/decisions/{id}`, when replayed after this change, then the immutable `recommendation_snapshot`/`cosign_snapshot` payloads are byte-identical to pre-change verbatim replay, per-user isolation holds, and the additive reconciliation fields surface truthfully.
- Given the default config, when the whole suite runs, then it passes with zero credentials and zero network, only `schwab_adapter` imports the SDK (AD-8), `coach/execution.py` is the sole caller of `get_order_status_by_ref` (AD-7), the fake path is unchanged, and no `float`/`E+` crosses any money path.

## Design Notes

- **Reconcile by the persisted order id, not the dead cache.** Story 6.3's `get_order_status(idempotency_key)` reconciles through the adapter's in-request `self._orders` cache — empty in any later request, so it can only answer `PENDING`. Durability requires keying on state that *survives* the request: the queryable `broker_ref` column 6.3 deliberately persisted for exactly this. `get_order_status_by_ref` is the new seam that reads one order directly by that id. Schwab honors no client idempotency key, so there is no broker-side dedupe to lean on — hence read-only, never re-place, and `broker_ref is None` is terminally unconfirmable (surface, don't guess).
- **Additive reconciliation, immutable decision preserved.** The co-sign is the immutable record of *what was decided and first surfaced* (verbatim-replay guarantee from 4.9/4.10/6.6). The broker's *later* truth is new live state, so it lands in separate `reconciliation_snapshot`/`reconciled_at` fields (write-latest — reconciliation can run repeatedly as an order progresses `pending → filled`). `cosign_snapshot`/`recommendation_snapshot` are never mutated, so the byte-identical replay tests stay green; `effective_outcome_status` layers the newest truth over the original for the history surface.
- **Sole-owner + integrity reuse.** `reconcile_pending_decision` lives in `coach/execution.py` (the placement/status owner, AD-7) and reuses the exact `execute_approved_order` session-integrity gate (extract `_assert_session_integrity`) so a reconcile can never read against a mismatched-provider or dead session. It shares the `INDETERMINATE` set and the `_map_order` fence discipline with the in-request path.
- **Out of scope, on purpose.** The post-placement `cosign`/`commit` atomicity gap (strands a live order with no persisted `broker_ref`) and the released-claim approve-retry double-place residual are a *write-path* atomicity problem, not reconciliation: a `broker_ref`-keyed reconcile literally cannot see an order whose ref was never persisted. They stay ledger items for a dedicated atomicity pass; conflating them here would balloon scope without closing them.

## Review Triage Log

### 2026-08-01 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 3: (high 0, medium 2, low 1)
- defer: 3
- reject: 9
- addressed_findings:
  - `[medium]` `[patch]` Both reviewers (needs_reconfirmation semantics): `reconcile_pending_decision` set `needs_reconfirmation = outcome.status in INDETERMINATE`, so a **successfully-read still-working** order (`get_order` → `PENDING`) was flagged `needs_reconfirmation=true` — contradicting the I/O matrix "Still working → false" row and the epic 6.7 AC "cannot be positively confirmed" (a working order read by id **is** positively confirmed). On real money this wrongly nudges a human toward re-confirming/re-placing a live order. Fixed: `needs_reconfirmation = outcome.status is OrderStatus.TIMEOUT` (only an indeterminate transport read, or `broker_ref is None`, is unconfirmable). Flipped the two still-working tests to `false`, added engine + endpoint transport-`TIMEOUT` tests asserting `true`, and aligned the outside-the-contract AC wording (the intent-contract's I/O matrix + epic govern; the loosely-worded "Always" bullet was resolvable to a single reading, so no HALT/intent-contract edit).
  - `[medium]` `[patch]` Edge Case Hunter (non-finite avg_price): `SchwabAdapter._extract_avg_price` (pre-existing from 6.3, an open deferred-work ledger item) returned `Decimal(str(val))` with **no** `is_finite()` guard, so a `NaN`/`Infinity` broker price mapped by `_map_order` — now reachable AND **durably persisted** via 6.7's reconcile path — would emit the literal `"NaN"`/`"Infinity"` on the wire and into `reconciliation_snapshot`. Fixed by mirroring the sibling `_decimal_or_zero` `is_finite()` guard on both the direct-field and weighted-average branches (non-finite → `None`), closing the ledger item; added a parametrized NaN/±Inf adapter test.
  - `[low]` `[patch]` Blind Hunter (writer monotonicity, AD-6 defense-in-depth): `record_reconciliation` (the sole reconciliation writer) trusted its one caller not to hand it a regression and had no terminal guard. Added a monotonic-toward-settlement guard: a no-op when the record's newest-known outcome is already terminal (`filled`/`rejected`) so a settled money truth is never walked backward; added a writer-level regression test. (Not exploitable through the single endpoint today — the engine short-circuits on terminal — but hardens the primitive.)
  - Deferred (3): (1) the reconcile endpoint has no atomic claim/row-lock, so two concurrent reconciles double-read the broker and last-writer-wins the snapshot — same atomicity class as the known Epic 4/6 gaps, read-only (no double-place), needs a row-lock/conditional-`UPDATE` design; (2) `get_order_status_by_ref`'s fence maps deterministic config/auth faults (`SchwabNotConfiguredError` — missing token / malformed account body) into `TIMEOUT`, creating a retry dead-end instead of a distinct reconnect signal on the reconcile path; (3) `partial` is classified terminal, so a partial fill that later completes to `filled` can never be re-reconciled — a genuine product decision about partial-fill terminality, not a clear-cut bug.
  - Rejected (9): unwrapped `session.commit()` (consistent with `approve`'s established convention, not a new deviation); `_recorded_outcome` reconstructing `OrderStatus(...)`/`Decimal(...)` unfenced (unreachable with valid rows — cosign writes valid enum + `format_money` strings; the avg_price patch removes the only NaN source); provider-mismatch → 409 "reconnect" wording (config-drift-only, broker safely untouched, consistent with `approve`); `broker_ref: str` vs `| None` typing (engine never passes `None`; adapters defensively guard it; cosmetic); source-grep vs behavioral `get_orders_for_account` test (behavioral `get_order_calls`/`ref_calls` asserts already cover it); `effective_outcome_status` `if status:` truthiness / indeterminate-shadow (writer always emits a valid enum; reconciliation only exists for non-terminal cosign states); 422 collapsing `proposed`/`cosigning` into one message (rare in-flight race, broker untouched, low consequence); non-numeric persisted `broker_ref` still `reconciled=True` persisting `PENDING` over a `pending` cosign (harmless pending-over-pending; new rows carry valid ids); fake blank-`""`-ref vs schwab blank-guard (both return `PENDING` — no observable divergence).

### 2026-08-01 — Review pass (follow-up)
- intent_gap: 0
- bad_spec: 0
- patch: 0
- defer: 0
- reject: 14
- addressed_findings:
  - none
- notes: A fresh independent follow-up review (Blind Hunter + Edge Case Hunter) on the full baseline→HEAD diff surfaced no new actionable defects. The three medium findings both reviewers ranked highest — (1) the reconcile endpoint's lack of an atomic claim/row-lock (concurrent last-writer-wins), (2) `get_order_status_by_ref`'s fence mapping deterministic `SchwabNotConfiguredError` config/auth faults into `TIMEOUT` (a retry dead-end vs. a distinct 409 reconnect), and (3) `partial` classified terminal (a partial→filled completion can never be re-reconciled) — are **already open deferred-work ledger entries** from the prior pass and remain orchestrator-owned; not re-added. The remaining findings were rejected as consistent with the prior pass's reasoning or unreachable/handled in code: `_recorded_outcome` reconstructs `OrderStatus(...)`/`Decimal(...)` unfenced but only on the terminal/no-ref short-circuit paths, which read snapshots that `cosign`/`record_reconciliation` write with a valid enum + `format_money` strings (the avg_price `is_finite()` patch removed the only NaN source), so unreachable with valid rows; empty `reconciliation_snapshot["outcome"]` cannot shadow the cosign outcome (the `or`-fallthrough in both `effective_outcome_status` and `_recorded_outcome` handles it); non-finite/negative `filled_qty`/`avg_price` are already sanitized at the adapter (`_decimal_or_zero`/`_extract_avg_price`); a blank/whitespace `broker_ref` is safely guarded to `PENDING` by both adapters; a negative/garbage-but-non-null persisted ref is a harmless fenced wasted read (rows this system writes carry valid positive integer ids); the carried-over-prod-table ALTER is the documented go-live schema step (identical pattern to `broker_ref`/`idempotency_key`); the 409-session-gate-before-404-isolation ordering leaks nothing and mirrors `approve`; `broker_ref: str` vs `| None` typing and the `cosigning`→generic-422 message are cosmetic (both rejected in the prior pass); and there is no `await`/yield point between the `reconciliation_snapshot` and `reconciled_at` writes, so no partial write is possible.

## Verification

**Commands:**
- `cd ballast/backend && uv run python -m pytest tests/test_schwab_adapter.py tests/test_coach_api.py tests/test_brokerage.py -q` -- expected: all pass, zero network, zero credentials.
- `cd ballast/backend && uv run python -m pytest -q` -- expected: full suite green (no regressions; fake path + verbatim replay unchanged).
- `cd ballast/backend && uv run python -c "import brokers.schwab_adapter"` -- expected: imports without loading `schwab-py` or crashing on missing creds.

**Manual checks (go-live, out of AC scope):**
- With real `SCHWAB_*` creds + `BROKER_ADAPTER=schwab` + a live session, `POST /decisions/{id}/reconcile` on a real order that timed out with a captured `broker_ref` and confirm it resolves truthfully (or stays `pending` with `needs_reconfirmation`). This live read is a credentials/real-money decision performed manually at go-live — not part of the automated suite.

## Auto Run Result

Status: done (follow-up review pass)

**Summary of change:** Story 6.7 (durable cross-request timeout reconciliation) was already implemented and committed at HEAD. This run was a fresh independent **follow-up review** (the `done` spec routed back into a review pass). No code was modified.

**Change under review (unchanged from HEAD):** a durable, read-only, cross-request reconcile path keyed on the persisted `broker_ref` — new port method `get_order_status_by_ref`, its schwab/fake adapter implementations, the Coach Engine's sole-caller `reconcile_pending_decision`, additive `reconciliation_snapshot`/`reconciled_at` columns + `record_reconciliation`/`effective_outcome_status`, and `POST /decisions/{id}/reconcile`.

**Review findings breakdown (this pass):**
- Patches applied: 0
- New items deferred: 0 (the three medium findings both reviewers ranked highest are already open, orchestrator-owned ledger entries from the prior pass — concurrent-reconcile atomicity, config/auth-fault→`TIMEOUT`, `partial`-classified-terminal — so they were not re-added)
- Items rejected: 14 (consistent with the prior pass's reasoning or unreachable/handled in code — see the follow-up triage-log entry for the full rationale)
- No intent_gap, no bad_spec, no repair loopback.

**Verification performed:** `import brokers.schwab_adapter` clean (no `schwab-py` load, no creds); `pytest tests/test_schwab_adapter.py tests/test_brokerage.py -q` → 67 passed (all new 6.7 adapter/fake/sole-caller tests included, zero network/creds). `tests/test_coach_api.py` requires a live Postgres and was not exercised this pass; since no code changed, the already-verified HEAD (`3bb7f99`) full-suite result stands.

**Follow-up review recommendation:** `false` — this pass made no review-driven code changes.

**Residual risks:** the three deferred-work items above (all read-only, no double-place / no phantom-fill; all on the credential-gated live-Schwab path unexercised offline) remain for the dedicated atomicity / go-live hardening pass. No new risk introduced.

