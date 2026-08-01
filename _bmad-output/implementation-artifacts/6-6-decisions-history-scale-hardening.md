---
title: 'Story 6.6 — Decisions History Scale Hardening'
type: 'feature'
created: '2026-08-01'
status: 'done'
baseline_revision: '15f98c53d4714743107114e8471e3e3a6009cb25'
final_revision: '64f770f25be5f3939834cf08ec909344fffe665c'
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-6-context.md'
warnings: [oversized]
---

<intent-contract>

## Intent

**Problem:** The Decisions history read is unbounded and index-blind. `list_cosigned_decisions` loads **every** `decision_record` row for the user into memory via `ScopedRepository.list()`, then filters `status == "cosigned"` and sorts by `co_signed_at` in Python — cost grows linearly with lifetime history, and `GET /api/coach/decisions` returns them all in one payload. Separately, never-approved `proposed` rows accumulate forever (one is inserted at every `/recommend`), bloating the table with records that will never co-sign. This is the Epic 4 deferred-work item (4.9/4.10 pagination + index + retention).

**Approach:** Push scoping, `cosigned` filtering, ordering, and a bounded page window into SQL backed by a new composite `(owner_id, co_signed_at)` index; paginate `GET /api/coach/decisions` with `limit`/`offset` query params and an additive `has_more`/`limit`/`offset` envelope; and add a SYSTEM-scope cron-style CLI prune job (mirroring `digest/job.py`) that deletes stale `proposed` records older than a configurable retention window. Verbatim replay (`/decisions/{id}`) and per-user isolation stay byte-for-byte unchanged.

## Boundaries & Constraints

**Always:**
- All per-user reads stay behind the fail-closed `ScopedRepository` (AD-10): the owner filter is applied in SQL for every user-scoped query; a foreign row is never visible.
- Ordering + limit + offset + the `status == "cosigned"` filter execute in SQL, not Python — the point is to stop loading the whole table.
- `coach/decision_record.py` remains the SOLE writer/reader of `DecisionRecord` (AD-6); the endpoint and prune CLI delegate to it and never touch the model directly.
- Cosigned records are immutable and permanent — pruning deletes ONLY `status == "proposed"` rows and can never touch a `cosigned` (on-the-record) row.
- Schema change is additive: ADD an `Index(...)`; never drop/alter existing columns. `create_all` builds the index on a fresh DB; carried-over DBs are reconciled with `CREATE INDEX IF NOT EXISTS` in the test fixture (and noted as a go-live caveat, exactly like `idempotency_key`/`broker_ref`).
- Money on any surfaced wire value stays fixed-point (no `float`, no `E+`); no new money paths introduced here.

**Block If:**
- The intended retention semantics require deleting anything other than never-co-signed `proposed` rows (e.g. a request to prune `cosigned` history) — HALT; that would violate immutability.

**Never:**
- No cursor/keyset pagination, no background scheduler infra, no new DB engine/Alembic — offset/limit + a cron-invoked CLI is the v1 shape.
- No pruning on the read path (`list_decisions` stays read-only; pruning is the CLI job's job only).
- No change to replay output, to the `cosign`/`claim`/`release` lifecycle, or to per-user isolation behavior.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Default page | 120 cosigned rows, no params | Newest 50 by `co_signed_at` desc; `has_more=true`, `limit=50`, `offset=0` | — |
| Deep page | `offset=100&limit=50` | Rows 101–120; `has_more=false` | — |
| Over-max limit | `limit=500` | 422 (bounded by `le` cap) | Reject, don't silently clamp-to-huge |
| Bad params | `limit=0` / `offset=-1` | 422 | FastAPI `ge` validation |
| Isolation under pagination | user A pages while user B has rows | Only A's rows ever returned/counted | Owner filter in SQL |
| Empty history | 0 cosigned rows | `{decisions:[], has_more:false, limit:50, offset:0}` | — |
| Replay unchanged | `GET /decisions/{id}` | Verbatim snapshot passthrough, identical to 4.10 | 404 on foreign/unknown id |
| Prune run | proposed rows: some older than window, some recent; plus cosigned rows | Deletes only `proposed` older than cutoff; returns count; cosigned + recent proposed untouched | Per-run commit; count logged |
| Prune isolation | proposed rows across multiple users | System-scope delete spans users but only stale `proposed`; no cosigned touched | — |

</intent-contract>

## Code Map

- `ballast/backend/db/models.py` -- `DecisionRecord` (~line 270). ADD `Index("ix_decision_record_owner_co_signed_at", "owner_id", "co_signed_at")` to `__table_args__` (import `Index` from sqlalchemy if absent). Update the docstring's create_all caveat note to include this index.
- `ballast/backend/db/repository.py` -- `ScopedRepository`. ADD a fail-closed `list_page(*, order_by, descending=False, limit, offset=0, filters=())` that applies the owner filter in SQL under a user scope, appends `filters`, orders, and applies `.limit()/.offset()`. Keep it generic; do not weaken the missing-scope guard.
- `ballast/backend/coach/decision_record.py` -- `list_cosigned_decisions` (~line 223): add `limit`/`offset` params, replace the load-all + Python filter/sort with a `list_page` call (`filters=(DecisionRecord.status == "cosigned",)`, `order_by=DecisionRecord.co_signed_at`, `descending=True`), fetching `limit + 1` to compute `has_more`; return the page rows plus a `has_more` flag (e.g. a small result tuple/dataclass). ADD `prune_stale_proposed_decisions(*, session, older_than_days, now=None)` issuing a `delete(DecisionRecord).where(status == "proposed", created_at < cutoff)` under an explicit SYSTEM path, committing, returning the deleted count. Guarded so it can only ever match `proposed`.
- `ballast/backend/coach/prune_job.py` -- NEW. Thin `python -m coach.prune_job` CLI mirroring `digest/job.py`: open a session, call `prune_stale_proposed_decisions` with `DECISION_PROPOSED_RETENTION_DAYS`, log + print the count, exit 0. "A scheduler/cron would invoke this."
- `ballast/backend/api/coach.py` -- `DecisionListResponse` (~line 206): add `has_more: bool`, `limit: int`, `offset: int` (additive). `list_decisions` (~line 516): add `limit: int = Query(DECISION_PAGE_SIZE, ge=1, le=DECISION_MAX_PAGE_SIZE)` and `offset: int = Query(0, ge=0)`, pass through, build the paginated envelope. Stays read-only, no broker/live-session dependency.
- `ballast/backend/api/config.py` -- `Settings`: add `DECISION_PAGE_SIZE: int = 50`, `DECISION_MAX_PAGE_SIZE: int = 100`, `DECISION_PROPOSED_RETENTION_DAYS: int = 30`.
- `ballast/backend/tests/test_coach_api.py` -- `ensure_tables` fixture (~line 87): add `CREATE INDEX IF NOT EXISTS ix_decision_record_owner_co_signed_at ON decision_record (owner_id, co_signed_at)` for carried-over DBs. Add pagination + prune + isolation + replay-unchanged tests.

## Tasks & Acceptance

**Execution:**
- [x] `db/models.py` -- add composite `(owner_id, co_signed_at)` index to `DecisionRecord.__table_args__`; update caveat docstring.
- [x] `db/repository.py` -- add fail-closed `list_page(...)` to `ScopedRepository` (owner filter in SQL, extra filters, order, limit, offset).
- [x] `coach/decision_record.py` -- paginate `list_cosigned_decisions` via `list_page` (SQL filter/order/window, `limit+1` for `has_more`); add `prune_stale_proposed_decisions` (SYSTEM-scope delete of stale `proposed` only, returns count).
- [x] `coach/prune_job.py` -- new cron-style CLI invoking the prune function with the retention setting.
- [x] `api/coach.py` -- add bounded `limit`/`offset` query params + additive `has_more`/`limit`/`offset` envelope to `list_decisions`/`DecisionListResponse`.
- [x] `api/config.py` -- add page-size, max-page-size, and proposed-retention settings.
- [x] `tests/test_coach_api.py` -- reconcile the new index in `ensure_tables`; add unit + endpoint tests covering the I/O matrix.

**Acceptance Criteria:**
- Given a user with more cosigned decisions than one page, when `GET /api/coach/decisions` is called with no params, then exactly `DECISION_PAGE_SIZE` newest-first rows return with `has_more=true`, and the query executes ordering/filter/window in SQL (verified by fetching a bounded set, not all rows).
- Given `limit`/`offset` params, when they exceed the max cap or go below their floors, then the request is rejected with 422 (bounds enforced), and valid values page correctly with an honest `has_more`.
- Given a second user with their own decisions, when the first user paginates, then no foreign row is ever returned or counted (per-user isolation holds through the paginated SQL path).
- Given `GET /api/coach/decisions/{id}`, when replayed after this change, then output is byte-identical to the pre-change verbatim replay and a foreign/unknown id still 404s.
- Given `proposed` records older than `DECISION_PROPOSED_RETENTION_DAYS` alongside recent `proposed` and `cosigned` records, when the prune job runs, then only the stale `proposed` rows are deleted, the returned count matches, and no `cosigned` or recent `proposed` row is touched.
- Given the full suite, when it runs, then it passes with zero network and zero credentials, existing decisions/replay tests stay green, and no `float` crosses any money path.

## Review Triage Log

### 2026-08-01 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 3: (high 0, medium 1, low 2)
- defer: 2
- reject: 9
- addressed_findings:
  - `[medium]` `[patch]` Offset pagination lacked a tiebreaker — rows sharing an identical `co_signed_at` could be skipped or duplicated across a page boundary. Added a deterministic primary-key tiebreaker to `ScopedRepository.list_page` (stable total order for any caller); added tie-stable and exact-full-final-page pagination tests.
  - `[low]` `[patch]` A negative `DECISION_PROPOSED_RETENTION_DAYS` misconfig would push the cutoff into the future and destructively prune recent `proposed` rows. Added an `older_than_days >= 0` guard in `prune_stale_proposed_decisions` + a test.
  - `[low]` `[patch]` The import-time settings comment in `api/coach.py` cited a non-existent `api/precedent` precedent. Corrected the rationale (Query defaults must be concrete at definition time; these are process-level config).
  - Deferred: (1) the composite index, like `broker_ref`/`idempotency_key` before it, is not created on a pre-existing DB by `create_all` — logged as a go-live schema step. (2) Stale `cosigning` zombie rows (crash between claim and cosign/release) have no reclamation path — pre-existing 6.1-lifecycle gap.

## Design Notes

- **Index-backed SQL window is the fix.** The `(owner_id, co_signed_at)` composite index serves the scoped `WHERE owner_id=? AND status='cosigned' ORDER BY co_signed_at DESC LIMIT ? OFFSET ?` shape. `list_page` keeps the fail-closed owner filter (never bypass `ScopedRepository`), so isolation and scale are fixed in the same seam.
- **`has_more` via `limit+1`, not a COUNT.** Fetch one extra row; if present, there's another page and we drop it. Avoids a second aggregate query and keeps the read index-only. `limit`/`offset` echoed back so the client can page without guessing.
- **Offset, not cursor.** Personal decision history is small-to-moderate; offset+index is bounded and fast enough for v1, and there is no existing cursor pattern to mirror. Keyset is deliberately out of scope.
- **Prune is a cron CLI, not read-path work.** The established cadence pattern here is cron-invoked CLI jobs (`digest/job.py`, `marketdata/ingest.py`), not a scheduler. Pruning writes, so it lives in the sole-writer module under SYSTEM scope and is invoked by `python -m coach.prune_job`. Deleting a never-placed `proposed` row is safe: it was never executed, no order exists, and its `idempotency_key` never reached the broker. Cosigned rows are immutable and never eligible — the delete predicate hard-pins `status == "proposed"`.
- **Additive envelope.** Adding `has_more`/`limit`/`offset` to `DecisionListResponse` leaves the existing `decisions` array intact, so any current consumer keeps working.

## Verification

**Commands:**
- `cd ballast/backend && python -m pytest tests/test_coach_api.py -q` -- expected: all pass, including new pagination/prune/isolation/replay tests.
- `cd ballast/backend && python -m pytest -q` -- expected: full suite green, no regressions.
- `cd ballast/backend && python -m coach.prune_job` -- expected: runs, prints a deleted-count summary, exits 0 (against a configured DB).

## Auto Run Result

Status: done

**Implemented change:** Made the Decisions history read scale-safe. `GET /api/coach/decisions` is now paginated (`limit`/`offset`, bounded by `DECISION_MAX_PAGE_SIZE`, additive `has_more`/`limit`/`offset` envelope); scoping, the `cosigned` filter, `co_signed_at`-desc ordering, and the page window all execute in SQL through the fail-closed `ScopedRepository.list_page`, backed by a new `(owner_id, co_signed_at)` composite index — the whole table is no longer loaded into memory. Never-co-signed `proposed` records get a retention policy via a cron-style `python -m coach.prune_job` CLI that deletes `proposed` rows older than `DECISION_PROPOSED_RETENTION_DAYS` (cosigned rows are immutable and never touched). Verbatim replay and per-user isolation are unchanged.

**Files changed:**
- `ballast/backend/db/models.py` — added the `(owner_id, co_signed_at)` composite index to `DecisionRecord`.
- `ballast/backend/db/repository.py` — added fail-closed `ScopedRepository.list_page(...)` (owner filter + filters + order + limit/offset in SQL, with a deterministic primary-key tiebreaker).
- `ballast/backend/coach/decision_record.py` — paginated `list_cosigned_decisions` (returns a `DecisionPage`), added `prune_stale_proposed_decisions` (system-scope delete of stale `proposed` only, negative-window guard).
- `ballast/backend/coach/prune_job.py` — new cron-style prune CLI.
- `ballast/backend/api/coach.py` — bounded `limit`/`offset` query params + additive paginated envelope.
- `ballast/backend/api/config.py` — `DECISION_PAGE_SIZE` / `DECISION_MAX_PAGE_SIZE` / `DECISION_PROPOSED_RETENTION_DAYS`.
- `ballast/backend/tests/test_coach_api.py` — index reconciliation in `ensure_tables`; pagination, isolation, replay-unchanged, prune, tie-stable, exact-boundary, and negative-window tests.

**Review findings breakdown:** 3 patches applied (1 medium: pagination tiebreaker to prevent skip/duplicate on tied `co_signed_at`; 2 low: negative-retention guard, misleading-comment fix). 2 items deferred (go-live index migration on pre-existing DBs; stale `cosigning` zombie-row reclamation). 9 findings rejected (by-design intent, misconfig-only, or already matching accepted codebase convention). 0 intent gaps, 0 bad-spec loopbacks.

**Verification:** `python -m pytest tests/test_coach_api.py -q` → 65 passed; `python -m pytest -q` (full suite) → 381 passed, zero regressions; `python -c "import coach.prune_job"` → clean. Zero network, zero credentials.

**Residual risks:** The composite index and the prune benefit from indexes that `create_all` will not add to an already-provisioned production DB (tracked as a go-live step in deferred-work, consistent with `idempotency_key`/`broker_ref`). Offset pagination is O(offset) at deep pages, acceptable for per-user personal history at v1 scale.
