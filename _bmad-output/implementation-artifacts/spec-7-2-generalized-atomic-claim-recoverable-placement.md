---
title: 'Generalized atomic-claim primitive & recoverable placement'
type: 'refactor'
created: '2026-08-01'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: ['oversized']
baseline_revision: '20519b06114fdabd82e323d58dca150d8eedbf75'
final_revision: '461945f569505f63e0682237b0d08d8e522be8a5'
---

<intent-contract>

## Intent

**Problem:** The Story 6.1 atomic-claim (conditional rowcount-gated `UPDATE … WHERE`) proved the pattern at the `/approve` seam ONLY. Every new Epic 6 seam re-opened the same atomicity class as a hand-rolled read-modify-write: the placement path persists `broker_ref` in the *cosign* commit (so a cosign/commit failure after a real order strands the row `cosigning` with a live order and no `broker_ref` — unrecoverable even by 6.7, which keys on `broker_ref`); the two-table balance reconcile (6.5) is a lock-free TOCTOU that can interleave/regress cash; the reconcile endpoint (6.7) has no claim, so two concurrent reconciles last-writer-wins and a transiently-worse read can regress persisted money truth; and a `cosigning` row orphaned by a mid-claim crash has NO reclamation path (invisible to both history and the pruner — a permanent zombie). These are the go-live-blocking atomicity items on the deferred-work ledger.

**Approach:** Extract the 6.1 pattern into ONE reusable, tested atomic-claim primitive (an optimistic rowcount-gated conditional `UPDATE` executor plus a pessimistic scoped `SELECT … FOR UPDATE` row-lock, both model-agnostic so the sole-writer modules keep issuing their own statements), then apply it at all three seams. Reshape the placement path so `broker_ref` is persisted DURABLY the instant placement returns — in its own atomic step BEFORE cosign — so a later cosign/commit failure leaves a recoverable `broker_ref`-bearing record. Add a bounded, age-gated reclamation path (keyed on a new `cosigning_at` stamp) that forward-recovers orphaned `cosigning` rows to a re-reconcilable state, never re-placing (ambiguous placements stay pending for explicit human re-confirmation). No new functional requirements; no new order behavior.

## Boundaries & Constraints

**Always:**
- The generalized primitive is the SINGLE shared mechanism used at the placement claim, the balance reconcile, and the reconcile endpoint — no seam hand-rolls its own concurrency guard afterward. The primitive is model-agnostic (takes a statement / model+ids), so `update(DecisionRecord)` / `update(PortfolioBalance)` text stays inside the respective sole-writer modules (AD-6/AD-14; the `DecisionRecord(` construction canary and single-writer conventions keep holding).
- Reconcile-wins is a conditional-update / serialized claim, NEVER a bare read-modify-write: two concurrent reconciles of the same decision (6.7) or two concurrent balance reconciles (6.5) can never regress persisted money truth (a `timeout` never overwrites a `filled`; older cash never clobbers newer). The existing in-writer monotonic terminal-guard in `record_reconciliation` must stay and be made effective across sessions by the claim/lock.
- The `portfolio_balance` + `portfolio_cache` reconcile stays ONE atomic unit: the winning balance conditional-update and the holdings replace commit together with no interleave and no dropped/duplicate holdings; a loser (stale/older snapshot) writes nothing.
- `broker_ref` is persisted durably in its own atomic step immediately after a successful placement and BEFORE cosign, so a cosign/commit failure leaves a `cosigning` row that still carries `broker_ref` (recoverable), never a `broker_ref`-NULL zombie.
- A decision with a NULL `idempotency_key` is refused BEFORE placement (pre-fill 422, claim released) — never a post-fill crash. (Already implemented at `api/coach.py`; this story pins it with a regression test and must not weaken it.)
- Reclamation is BOUNDED (only `cosigning` rows whose `cosigning_at` is older than a window), idempotent (re-run changes nothing new), rowcount-gated (a racing live cosign wins; the reclaimer no-ops), and NEVER re-places an order or releases a possibly-placed row back to `proposed`. An ambiguous (`broker_ref`-NULL) orphan is surfaced as a cosigned, indeterminate, needs-reconfirmation record for explicit human re-confirmation — never auto-placed and never presented as confirmed.
- Post-placement, the claim is still NEVER released (that would risk a double-place) — recoverability comes from the durable `broker_ref` + the reclaimer, not from releasing.

**Block If:**
- The deployed `decision_record` table diverges such that `cosigning_at` cannot be added additively (e.g. a conflicting existing column) — HALT rather than guess a destructive ALTER.
- Making the balance reconcile a conditional update would require changing the reconcile-wins semantics (the `as_of` staleness key) itself — that is a product decision; HALT rather than silently redefine what "wins".

**Never:**
- Never introduce a new `DecisionRecord.status` value or a new order type/behavior; reuse `proposed → cosigning → cosigned` and the existing `OrderStatus`/`OrderOutcome` shapes.
- Never hold a DB row lock across the `place_order` network round-trip (the whole point of 6.1's commit-before-network); the reconcile endpoint reads the broker FIRST (unlocked), then serializes only the local write.
- Never auto-release a `cosigning` row to `proposed` for a crash-orphaned row (could double-place); never re-place, search, or guess an order in the reclaimer.
- Never introduce Alembic or a background scheduler here — extend the existing idempotent-startup migration (7.1) for the one additive column; wiring the reclaimer to a periodic runner is out of scope (it is an invocable function like `prune_stale_proposed_decisions`).
- Never mutate the immutable `recommendation_snapshot`/`schema_version`/`created_at`, and never re-write a stable `idempotency_key`.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Placement, then cosign/commit fails | Won claim; `place_order` returns `broker_ref=R`; the subsequent cosign/commit raises | `broker_ref=R` is already persisted (its own prior commit); row stays `cosigning` WITH `R` — recoverable by the reclaimer/reconcile | Original error surfaces; NO claim release; NO double-place |
| Two concurrent reconciles of one decision | Decision `cosigned`; reconcile A reads broker `filled`, reconcile B reads `timeout` | Whichever serializes second re-checks the committed state; the terminal `filled` is never regressed to `timeout` | No error; monotonic guard holds across sessions |
| Two concurrent balance reconciles | Snapshots as_of T1 (older) and T2 (newer) race | T2 wins (conditional `as_of < :incoming` update); T1 is a no-op; holdings + balance for T2 commit as one unit, no interleave | No error; T1 writes nothing |
| First-ever balance reconcile races itself | No `portfolio_balance` row; two concurrent inserts | One inserts; the other hits `uq_portfolio_balance_owner` → treated as a lost race (skip / re-read), not a crash | IntegrityError caught → rollback → no duplicate row |
| NULL idempotency_key at approve | Won claim; `record.idempotency_key IS NULL` | Refused with 422 BEFORE any placement; claim released (`cosigning → proposed`); broker never touched | Pre-fill refusal, retryable |
| Reclaim orphaned cosigning, broker_ref present | `cosigning` row, `cosigning_at` older than window, `broker_ref=R` | Forward-recovered to `cosigned` with an indeterminate (pending/timeout) outcome carrying `R` → re-reconcilable via 6.7 | Rowcount-gated; a racing real cosign wins → reclaimer no-ops |
| Reclaim orphaned cosigning, broker_ref NULL | `cosigning` row, `cosigning_at` older than window, `broker_ref IS NULL` | Surfaced as `cosigned` + indeterminate + needs-reconfirmation (human re-confirm); NEVER re-placed, NEVER released to `proposed` | Never auto-place/guess |
| Reclaim, row still in-flight | `cosigning` row, `cosigning_at` within the window | NOT touched (a legitimately in-flight approve is never reclaimed) | No-op |
| Reclaim re-run | Same rows after a prior reclaim (now `cosigned`) | No `cosigning` rows match → full no-op | Idempotent |

</intent-contract>

## Code Map

- `ballast/backend/db/atomic.py` -- **NEW** the generalized atomic-claim primitive: `conditional_claim(session, statement) -> bool` (rowcount==1) and `lock_row(session, model, *, entity_id, owner_id)` (scoped `SELECT … FOR UPDATE`). Model-agnostic; takes caller-built statements so writes stay in the sole-writer modules.
- `ballast/backend/coach/decision_record.py` -- SOLE writer of `DecisionRecord` (AD-6). Refactor `claim_for_cosign`/`release_claim` onto the primitive; stamp/clear `cosigning_at`; ADD `persist_broker_ref`, `lock_decision`, and `reclaim_orphaned_cosigning`. `record_reconciliation`'s monotonic guard stays.
- `ballast/backend/brokers/portfolio.py` -- SOLE writer of `portfolio_cache`+`portfolio_balance` (AD-14). Reshape `reconcile_portfolio` balance upsert into a conditional `as_of`-gated claim (its row lock serializes the two-table unit).
- `ballast/backend/api/coach.py` -- `approve` persists `broker_ref` durably (new step) between placement and cosign; `reconcile_decision` reads the broker first, then serializes the local persist via `lock_decision`. NULL-key guard stays.
- `ballast/backend/db/models.py` -- `DecisionRecord`: ADD nullable `cosigning_at TIMESTAMPTZ` (when the current claim entered `cosigning`; the reclaimer's age key).
- `ballast/backend/db/migrations.py` -- extend `run_startup_migrations` with `ALTER TABLE decision_record ADD COLUMN IF NOT EXISTS cosigning_at TIMESTAMPTZ` (additive, idempotent — the 7.1 pattern).
- `ballast/backend/coach/execution.py` -- `execute_approved_order`/`reconcile_pending_decision` (unchanged callers of the port; referenced for outcome shape).
- `ballast/backend/tests/test_coach_api.py` -- `ensure_tables` fixture: add the same `ADD COLUMN IF NOT EXISTS cosigning_at` so tests run against a table with the column; existing 6.1 claim/NULL-key tests to extend.
- `ballast/backend/tests/test_atomic.py` -- **NEW** primitive unit tests.
- `ballast/backend/tests/test_recoverable_placement.py` -- **NEW** placement-recoverability, reconcile/balance concurrency, and reclamation tests.

## Tasks & Acceptance

**Execution:**
- [x] `ballast/backend/db/atomic.py` -- add `conditional_claim(session, statement) -> bool` (execute the caller-built `update(...)` statement, return `result.rowcount == 1`; does NOT commit — the caller owns the commit boundary because the placement claim must commit-before-network while a reconcile commits-with-read) and `lock_row(session, model, *, entity_id, owner_id) -> model | None` (`select(model).where(model.id == entity_id, model.owner_id == owner_id).with_for_update()`, scoped, returns the locked instance or None). -- the ONE shared primitive both claim strategies live in.
- [x] `ballast/backend/db/models.py` -- add `cosigning_at: Mapped[datetime|None]` (`DateTime(timezone=True)`, nullable) to `DecisionRecord` with a comment: stamped when the CURRENT claim entered `cosigning`; the reclaimer's bounded-age key; not part of the immutable snapshot. -- gives the reclaimer a safe age signal.
- [x] `ballast/backend/db/migrations.py` -- append `ALTER TABLE decision_record ADD COLUMN IF NOT EXISTS cosigning_at TIMESTAMPTZ` to the ordered column-add list (before the index list), with a per-step label. -- provisions the new column on an already-deployed DB, idempotently.
- [x] `ballast/backend/coach/decision_record.py` -- refactor `claim_for_cosign` and `release_claim` to build their `update(...)` statement and go through `conditional_claim` (claim also sets `cosigning_at=now`; release also clears `cosigning_at=None`), preserving their commit + scope-guard behavior. ADD `persist_broker_ref(decision_id, broker_ref, *, scope, session) -> bool` (conditional `UPDATE … SET broker_ref WHERE id AND owner_id AND status='cosigning'`, commit, return won). ADD `lock_decision(decision_id, *, scope, session) -> DecisionRecord | None` (via `lock_row`, scoped). ADD `reclaim_orphaned_cosigning(*, session, older_than, now=None) -> int`: SYSTEM-scope; select `cosigning` rows with `cosigning_at < now - older_than`; for each, transition `cosigning → cosigned` via a rowcount-gated conditional update that stamps `co_signed_at=now` and a recovery `cosign_snapshot` (executed intent = the proposed `order_intent` from the record's snapshot; outcome = an indeterminate/non-terminal status carrying the persisted `broker_ref`, filled_qty 0, avg_price None — money fixed-point), so the row becomes a re-reconcilable, needs-reconfirmation cosigned record; commit; return the count reclaimed. Reject a negative `older_than`. Keep `record_reconciliation`'s status assertion + monotonic terminal no-op. -- generalizes the primitive at the placement claim and adds durable-ref + bounded recovery.
- [x] `ballast/backend/brokers/portfolio.py` -- reshape `reconcile_portfolio`: when a balance row exists, advance it via a conditional claim `update(PortfolioBalance).where(owner_id == scope.user_id, PortfolioBalance.as_of < incoming_as_of).values(cash=…, as_of=…)` through `conditional_claim`; only on a win (rowcount==1) replace the holdings (delete-then-add) in the SAME transaction and commit; on a loss (stale/equal) leave both tables untouched and return the existing view. When no balance row exists, INSERT it (the first reconcile) guarded by `uq_portfolio_balance_owner`, catching a concurrent-insert `IntegrityError` as a lost race (rollback → re-read → skip). Keep it the sole writer of both tables. -- reconcile-wins becomes a conditional update whose row lock serializes the two-table unit.
- [x] `ballast/backend/api/coach.py` -- in `approve`, after `execute_approved_order` returns successfully and BEFORE `cosign`, when `outcome.broker_ref` is present call `persist_broker_ref(...)` (its own commit) so the ref is durable independent of the cosign commit; then `cosign` + commit as today (cosign still sets `broker_ref` idempotently). Do NOT release the claim on a post-placement cosign/commit failure. In `reconcile_decision`, call `reconcile_pending_decision` (broker read, UNLOCKED) first; only if `result.reconciled`, `lock_decision(...)` the row, then `record_reconciliation` on the locked instance and commit (lock serializes concurrent persists so the monotonic guard holds across sessions). Keep the NULL-`idempotency_key` pre-flight 422. -- durable-ref recoverable placement + serialized reconcile persist.
- [x] `ballast/backend/tests/test_coach_api.py` -- extend `ensure_tables` with `ALTER TABLE decision_record ADD COLUMN IF NOT EXISTS cosigning_at TIMESTAMPTZ`; add a regression test pinning the NULL-`idempotency_key` pre-placement 422 + claim release (broker untouched). -- keeps the suite runnable against the new column and locks the pre-fill refusal.
- [x] `ballast/backend/tests/test_atomic.py` -- unit-test the primitive: `conditional_claim` returns True on a 1-row match and False on 0 rows (concurrent-claim semantics), and `lock_row` returns only the owner's row (foreign row invisible → None) and issues a `FOR UPDATE` select. -- proves the shared primitive in isolation.
- [x] `ballast/backend/tests/test_recoverable_placement.py` -- cover the I/O matrix: (a) a cosign/commit failure after placement leaves `status='cosigning'` WITH the persisted `broker_ref` (recoverable) and no double-place; (b) two concurrent same-decision reconciles never regress a terminal outcome (filled not overwritten by timeout); (c) two concurrent balance reconciles → newest `as_of` wins, holdings+balance atomic, no interleave, stale writes nothing; (d) reclaimer forward-recovers a `broker_ref`-present orphan to a re-reconcilable cosigned record; (e) reclaimer surfaces a `broker_ref`-NULL orphan as cosigned+needs-reconfirmation without re-placing or releasing to `proposed`; (f) a within-window `cosigning` row is untouched; (g) reclaim re-run is a no-op. -- proves every atomicity/recoverability guarantee.

**Acceptance Criteria:**
- Given a won claim whose `place_order` succeeded with a `broker_ref`, when the subsequent cosign/commit fails, then the record stays `cosigning` carrying the persisted `broker_ref` (not NULL), the claim is not released, and no second order is placed.
- Given the placement claim, the balance reconcile, and the reconcile endpoint, when concurrency is exercised, then all three route their write through the single `db/atomic.py` primitive and none regress persisted money truth (a terminal outcome is never walked back; older cash never clobbers newer).
- Given an orphaned `cosigning` row older than the reclamation window, when `reclaim_orphaned_cosigning` runs, then it is forward-recovered to a cosigned, needs-reconfirmation, re-reconcilable record without re-placing any order or releasing to `proposed`; a within-window row is untouched; a re-run is a no-op.
- Given a deployed `decision_record` table without `cosigning_at`, when the startup migration runs, then the column is added idempotently (a re-run is a clean no-op), and the sole-writer / `DecisionRecord(` construction canary still passes.

## Spec Change Log

_No bad_spec loopbacks — the intent contract and spec held through review; all review findings were localized patches or defers._

## Review Triage Log

### 2026-08-01 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 6: (high 0, medium 4, low 2)
- defer: 2: (high 0, medium 1, low 1)
- reject: 7
- addressed_findings:
  - `[medium]` `[patch]` Concurrent-reconcile holdings interleave — `reconcile_portfolio` read the existing holdings BEFORE winning the `as_of`-gated balance claim, so a newer-wins race deleted a stale snapshot and orphaned the racing winner's rows. Moved the holdings read to AFTER the claim win (under the balance row lock held until commit), so the two-table replace can never interleave. Added a genuinely-concurrent (`asyncio.gather`) regression test — verified it fails on the pre-fix read and passes on the fix.
  - `[medium]` `[patch]` Stale-instance cosign in `/approve` — `persist_broker_ref`'s return was discarded and `cosign` ran on the pre-network in-memory record (stale `cosigning` under `expire_on_commit=False`). Now re-loads the record after the durable-ref commit and only cosigns if it is still `cosigning`; if a concurrent forward-recovery moved it, surfaces the honest outcome without a spurious second cosign (never releasing).
  - `[medium]` `[patch]` First-insert `IntegrityError` over-catch — the balance first-insert swallowed ANY `IntegrityError` as a benign lost race, masking a real NOT NULL/FK fault. Narrowed to re-raise unless the failure is `uq_portfolio_balance_owner`.
  - `[medium]` `[patch]` Carried-over `cosigning` orphans permanently unreclaimable — a pre-7.2 `cosigning` row gets `cosigning_at = NULL` and the reclaimer excludes NULL, so historical orphans could never be recovered. Added a NULL-only migration backfill (`cosigning_at = created_at WHERE status='cosigning'`, matching 7.1's backfill discipline) + a migration test; a carried-over orphan is restarted-by-definition so it is safely eligible.
  - `[low]` `[patch]` Reclaimer tz-naive `now` — an injected naive `now` would compare a naive cutoff against tz-aware `cosigning_at` and stamp a naive `co_signed_at`. Normalized `now` to UTC (matching the codebase's tz discipline).
  - `[low]` `[patch]` Added the P2 concurrency regression test (counted with the interleave fix above) plus the migration-backfill test as explicit coverage.

### 2026-08-01 — Review pass (follow-up, iteration reset)
- intent_gap: 0
- bad_spec: 0
- patch: 0
- defer: 2: (high 0, medium 1, low 1)
- reject: 9
- addressed_findings:
  - none
- notes: Fresh follow-up review (Blind Hunter + Edge Case Hunter) on the committed 7.2 diff. No patch or spec loopback this pass — the implementation held. Two NEW defers appended to the ledger: (medium) the reconcile endpoint protects PERSISTED money truth via the lock+monotonic guard but still builds its HTTP response from the loser's freshly-read `result.outcome`, so a concurrent double-reconcile can surface `timeout`/`needs_reconfirmation` for an already-`filled` decision (self-heals on next reconcile; no DB regression / no double-place); (low) `reclaim_orphaned_cosigning` commits the whole candidate batch at once with no per-row error isolation, so one poison row would roll back the batch. Two further real findings (broker_ref VARCHAR(64) length gap; reclaimer not scheduler-wired / no-LIMIT paging) were already present in the ledger and were NOT re-appended per the NEW-entries-only directive. Rejected 9 items as noise/non-reachable: duplicate-balance-row race (prevented by `uq_portfolio_balance_owner`), `lock_decision` None on a never-deleted cosigned record, non-dict `recommendation_snapshot` (always a dict), narrow snapshot-vs-column `broker_ref` cosmetic mismatch (column keeps the ref; reconcile keys on it), first-insert flush-then-commit (flush does not commit → both roll back together), `release_claim`/`persist_broker_ref` rowcount observability nits (safe re-load guard already covers), reclaimer `co_signed_at=now` (matches spec), and the migration phase-ordering comment (columns→backfill run ordered in one tx). Verification: `tests/test_atomic.py tests/test_recoverable_placement.py tests/test_migrations.py` → 22 passed; full backend suite → 434 passed.

## Design Notes

- **One primitive, two strategies — by necessity, not preference.** The three seams cannot all use the same guard: the placement claim must COMMIT before the `place_order` network call (so it can't hold a lock), and its guard (`status`) is a scalar → optimistic `conditional_claim`. The reconcile endpoint's monotonic guard lives in a JSON column (`reconciliation_snapshot.outcome.status`) that can't be expressed in a portable `WHERE` → pessimistic `lock_row` makes the existing in-writer terminal-monotonic guard effective across sessions. The balance reconcile's guard IS scalar (`as_of`) → `conditional_claim`, and the conditional `UPDATE`'s own row lock serializes the two-table replace (a concurrent reconcile blocks on that row, then re-evaluates `as_of < :incoming` → no-op). Both live in `db/atomic.py` as the shared atomic-claim toolkit.
- **Why durable `broker_ref` first.** Ordering becomes: claim (commit) → `place_order` (network) → persist `broker_ref` (commit) → cosign (commit). The extra commit makes `broker_ref` durable the instant the order exists, so any later failure leaves a `broker_ref`-keyed recoverable row instead of the current place-then-persist-in-one-commit zombie. `cosign` still writes `broker_ref` (idempotent — same value).
- **Reclamation is forward-only and never re-places.** A crash-orphaned `cosigning` row may correspond to a real order (esp. `broker_ref`-present). Because Schwab honors no client idempotency key, releasing to `proposed` (which permits re-place) is unsafe. The reclaimer instead completes the record to `cosigned` with an indeterminate outcome + `needs_reconfirmation`, so the ordinary 6.7 reconcile (by `broker_ref`) or a human resolves it — matching the LOCKED policy "ambiguous placement stays pending / never guess / never re-place." The rowcount-gated transition + age window ensure a live in-flight approve is never stolen or double-completed.
- **Canary preservation.** The primitive takes caller-built statements, so `update(DecisionRecord)` / `DecisionRecord(...)` text stays in `coach/decision_record.py` and `update(PortfolioBalance)` in `brokers/portfolio.py` — the AD-6 `DecisionRecord(` construction canary and the single-writer conventions are untouched.

## Verification

**Commands:**
- `cd ballast/backend && python -m pytest tests/test_atomic.py tests/test_recoverable_placement.py tests/test_migrations.py -v` -- expected: all primitive, recoverability/concurrency, reclamation, and migration tests pass (requires `docker compose up -d db`).
- `cd ballast/backend && python -m pytest` -- expected: full suite green (no regression; the sole-writer/canary and 6.1 claim tests still pass).


## Auto Run Result

Status: done (follow-up review pass on a completed story)

**Summary of change reviewed:** Story 7.2 — the generalized atomic-claim primitive (`db/atomic.py`: `conditional_claim` + `lock_row`) applied at all three concurrency seams (placement claim, balance reconcile, reconcile endpoint), recoverable placement (durable `broker_ref` persisted in its own commit before cosign), the new `cosigning_at` reclaimer age key (+ idempotent additive migration and NULL-only backfill), and the bounded, forward-only `reclaim_orphaned_cosigning` reclaimer. This pass made NO code changes — it was a fresh independent review of the already-committed diff (baseline `20519b0`).

**Files reviewed (unchanged this pass):**
- `db/atomic.py` — new shared primitive (optimistic conditional claim + pessimistic row lock).
- `coach/decision_record.py` — claim/release refactored onto the primitive; `persist_broker_ref`, `lock_decision`, `reclaim_orphaned_cosigning` added; monotonic reconcile guard retained.
- `brokers/portfolio.py` — balance reconcile reshaped into an `as_of`-gated conditional claim; holdings read AFTER the claim win; first-insert `IntegrityError` narrowed to the owner-uniqueness backstop.
- `api/coach.py` — durable `broker_ref` step + stale-instance re-load before cosign; reconcile persist serialized under `lock_decision`.
- `db/models.py`, `db/migrations.py` — additive `cosigning_at` column + idempotent migration/backfill.
- `tests/test_atomic.py`, `tests/test_recoverable_placement.py`, `tests/test_coach_api.py`, `tests/test_migrations.py` — primitive/recoverability/concurrency/reclamation/migration coverage.

**Review findings breakdown:** patches applied 0; bad_spec/intent_gap 0; deferred 2 NEW (medium: reconcile RESPONSE surface can walk back a terminal outcome under a concurrent double-reconcile even though PERSISTED truth is protected; low: reclaimer batch-commit lacks per-row error isolation); 2 real findings already tracked in the ledger and not re-appended (broker_ref VARCHAR(64) length; reclaimer not scheduler-wired / no-LIMIT paging); rejected 9 as noise/non-reachable.

**Verification:** `python -m pytest tests/test_atomic.py tests/test_recoverable_placement.py tests/test_migrations.py` → 22 passed; full backend suite `python -m pytest` → 434 passed (against docker Postgres). No regressions.

**Residual risks:** The two new defers (reconcile response-surface regression; reclaimer batch-atomicity) plus the two pre-existing ledger items (broker_ref length; reclaimer runner/paging) remain open for go-live hardening. None are double-place / persisted-money-truth regressions — all are surface/robustness/observability items with no current single-user-dev impact.

**Follow-up review recommended:** false — this pass made no review-driven code changes.
