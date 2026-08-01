---
title: 'Story 6.1 — Atomic Decision Claim & Idempotency Hardening'
type: 'feature'
created: '2026-08-01'
status: 'done'
baseline_revision: '3ac6780aefa0414f0c4515b6551b4a8624fcbdba'
review_loop_iteration: 0
followup_review_recommended: false
final_revision: '8b6204a81a40a8432fffb83d675db2f3ca090e9f'
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-4-context.md'
warnings: []
---

<intent-contract>

## Intent

**Problem:** Two of the app's most consequential seams are guarded only against *sequential* re-entry, not concurrent in-flight re-entry — the single systemic gap the Epic 4 retrospective flagged. In `api/coach.py::approve`, the guard is `load_decision → check status=='cosigned' → execute_approved_order (awaits, yielding the loop) → cosign → commit`: two overlapping `/approve` calls carrying the same `decision_id` both observe `proposed`, each mints its OWN per-request idempotency key (`mint_idempotency_key()`), and each calls `broker.place_order` — the persisted-key defense only kicks in AFTER the first commit, so it does not cover the in-flight window (against a real broker this double-places real money). The weekly digest job (`digest/job.py::send_weekly_digests`) has the identical shape: it sends THEN advances `last_sent_week`, so two overlapping runs both see the stale marker and both send — a duplicate proactive email violates the core never-nag promise (NFR8). Both are harmless in v1 *only* because the Schwab adapter and the digest cron are single-path stubs today; this story closes them structurally **before** real-broker wiring (6.3) or a parallelized digest can make them bite. (Closes deferred-work: 4.9 concurrent-approve + 5.1 digest double-send — one root cause.)

**Approach:** Give the decision-record and digest seams a real **atomic claim** — a conditional `UPDATE … WHERE <still-unclaimed>` whose `rowcount == 1` is won by exactly one of any number of concurrent transactions (Postgres serializes the row update; the loser gets `rowcount == 0`). For decisions: (1) mint a **stable per-decision idempotency key at proposal time** and persist it on the proposed row (reused verbatim across every placement of that decision, replacing the per-request mint), backed by a **DB unique index** on `idempotency_key` — an in-convention backstop (mirrors `market_daily`/`digest_preference` `UniqueConstraint` and the `unsubscribe_token` `unique=True` index); (2) add an intermediate `cosigning` status and an atomic `proposed → cosigning` claim in the sole-writer module, so only the claim winner ever calls `place_order`; the loser returns the recorded outcome (if the winner already cosigned) or a calm 409 in-progress. A refusal or failure **releases** the claim (`cosigning → proposed`) so the decision stays retryable. For the digest: reorder to **claim-before-send** via a conditional `UPDATE DigestPreference … WHERE last_sent_week IS DISTINCT FROM :week` gated on `rowcount == 1`; only the winner sends, and a send failure rolls the claim back for next run. All Epic-4 execution invariants (explicit-approval-only, entry-gate 409, placement-time integrity, v1-scope 422, single-placement, AD-7 sole-caller, AD-6 sole-writer) and the immutability of the recommendation snapshot are unchanged.

## Boundaries & Constraints

**Always:**
- **Stable per-decision idempotency key, minted at proposal.** `record_proposal` mints ONE idempotency key (UUID4 hex, `mint_idempotency_key()`) and persists it on the proposed `DecisionRecord`. Every placement of that decision reuses this SAME key verbatim; `/approve` no longer mints a per-request key. The key is stable for the life of the decision (a retry after a released claim reuses it, so the broker dedupes a re-place).
- **DB unique index on `idempotency_key`.** Add a unique constraint/index on `decision_record.idempotency_key` — the DB-level backstop that makes a duplicate key physically un-insertable. This is IN-CONVENTION (see `UniqueConstraint` on `market_daily`/`digest_preference` and the `unsubscribe_token` `unique=True, index=True` column); it is NOT the DB-level *immutability trigger/CHECK* that Story 4.9 rejected as absent-from-the-codebase (a genuinely different, still-absent pattern). Because the key is now set at propose, the column is populated from birth (no NULL-uniqueness ambiguity).
- **Atomic `proposed → cosigning` claim (sole writer, AD-6).** A new function in `coach/decision_record.py` performs a scoped conditional `UPDATE decision_record SET status='cosigning' WHERE id=:id AND owner_id=:scope_user AND status='proposed'` and returns whether it won (`rowcount == 1`). This is the ONLY transition into `cosigning`. It commits the claim (releasing any row lock) BEFORE the broker is called, so no DB row lock is held across the `place_order` network call.
- **Only the claim winner places.** `/approve` claims first; on a win it calls `execute_approved_order` with the STABLE persisted key, then `cosign` (`cosigning → cosigned`). On a lost claim (`rowcount == 0`) it re-loads and branches: `cosigned` → return the recorded outcome (idempotent, broker untouched); `cosigning` → calm **409 in-progress** (an approve is already running; try again shortly); missing → 404.
- **Release on refusal/failure keeps the decision retryable.** If, after a won claim, `execute_approved_order` raises `SessionIntegrityError` (409), `OrderScopeError` (422), or any error before a successful placement, the claim is RELEASED (`cosigning → proposed`, sole-writer) and committed before the HTTP error is raised — so a refused decision returns to `proposed` and can be approved again; it never gets stuck.
- **`cosign` now guards `status == 'cosigning'`.** Co-sign is reachable only from a claimed record (`cosigning → cosigned`), still exactly once; the recommendation snapshot / `schema_version` / `created_at` are still never mutated. `co_signed_at`, executed `order_intent`, and reconciled outcome are written once; `idempotency_key` is already set (at propose) and is not re-written.
- **Digest: claim-before-send, conditional-UPDATE gated on rowcount.** `send_weekly_digests` claims the week FIRST via `UPDATE DigestPreference SET last_sent_week=:week, updated_at=:now WHERE owner_id=:id AND last_sent_week IS DISTINCT FROM :week`; only `rowcount == 1` proceeds to compose+send (committing the claim), `rowcount == 0` is a skip (another run already claimed this week — no send). A send failure rolls back (releasing the claim) so the user is retried next run; per-user failure isolation and the deactivated-user skip are preserved.
- **Conventions.** All new/changed writes to `decision_record` go through `coach/decision_record.py` (AD-6, grep canary holds). Money stays fixed-point (`format(Decimal,"f")`). tz-aware UTC. Per-user isolation via the fail-closed `ScopedRepository` / explicit `owner_id` predicate in the claim UPDATE. Table shape via the existing `create_all` convention (no Alembic).
- **All ACs pass offline** with `BROKER_ADAPTER=fake`, `LLM_ADAPTER=fake` — zero network, zero credentials. The full suite (264 after 5.1) stays green.

**Block If:**
- A stakeholder wants to hold a `SELECT … FOR UPDATE` row lock across the `place_order` network call (lock held during slow broker I/O) instead of the claim-commit-then-place pattern — HALT (anti-pattern; the intermediate `cosigning` claim exists precisely to avoid this).
- A stakeholder wants to introduce Alembic/migrations for the new index or status — HALT (follow the existing `create_all` convention, as every prior story does).
- Satisfying any AC would require a live Schwab or Anthropic call (real key/network/paid tokens) — HALT.
- A stakeholder wants to keep the per-request key mint (weakening the guarantee back to sequential-only) — HALT (the stable persisted key is the point).

**Never:**
- No live-broker placement, no real reconciliation mapping, no `LLM_ADAPTER=anthropic` (those are Stories 6.2/6.3).
- No change to `OrderOutcome`/`OrderIntent`/`Recommendation`/`EvidenceRecord` field shapes, the coach pipeline, the validation gate, or the entry/scope/integrity gates.
- No change to `/recommend` or `/approve` request/response *shapes* (the `decision_id`/`order_intent` contract is unchanged; only the internal claim flow and a new 409-in-progress arm are added).
- No new Decisions/replay endpoints or UI (shipped in 4.10; untouched here). `list_cosigned_decisions` still filters `status == 'cosigned'`, so a transient `cosigning` row is naturally excluded from history.
- Do NOT hand-edit the append-only `deferred-work.md` — the bmad-loop orchestrator reconciles the ledger; this story's job is to make the closures TRUE in code.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Recommend persists stable key | authed user; any decision | one **proposed** record written with a stable `idempotency_key` (non-null) + snapshot + `schema_version`; response unchanged (`decision_id`) | No error (200) |
| Two concurrent approves, same `decision_id` | two overlapping `/approve`, record `proposed` | exactly ONE wins the `proposed→cosigning` claim and places once; the other gets `rowcount==0` → 409 in-progress (or the recorded outcome if the winner already cosigned). `place_order` called EXACTLY once | winner 200; loser 409 (or 200 recorded) |
| Sequential re-approve (already cosigned) | record `cosigned` | recorded outcome returned; broker never re-invoked (unchanged 4.9 behavior) | No error (200) |
| Claim then refusal (integrity) | won claim; session not live / provider mismatch | `SessionIntegrityError` → claim RELEASED (`cosigning→proposed`) → 409 `RECONNECT_MESSAGE`; broker untouched; record retryable | 409 |
| Claim then refusal (scope) | won claim; out-of-scope intent | `OrderScopeError` → claim RELEASED → 422; broker untouched; record retryable | 422 |
| Approve unknown/foreign `decision_id` | absent or another user's id | 404 before any claim; broker untouched | 404 |
| Unique-key backstop | attempt to persist a duplicate `idempotency_key` | DB rejects the duplicate (unique index) | IntegrityError (unreachable via normal flow; structural backstop) |
| Two concurrent digest runs, same week | two overlapping `send_weekly_digests` for one opted-in user | exactly ONE wins the conditional-UPDATE claim and sends; the other sees `rowcount==0` and skips. `sender.send` called EXACTLY once for that user | No error |
| Digest send fails after claim | claim won; `sender.send` raises | claim rolled back (marker released); user UNMARKED, retried next run; run continues (isolation) | logged warning; run ok-continues |
| Sole-writer canary | grep backend tree | `DecisionRecord(...)` construction + all `decision_record` status writes appear only in `coach/decision_record.py` (model in `db/models.py`) | No error |

</intent-contract>

## Code Map

- `ballast/backend/db/models.py` -- MODIFY `DecisionRecord`: add `__table_args__ = (UniqueConstraint("idempotency_key", name="uq_decision_record_idempotency_key"),)` (or `unique=True, index=True` on the column, matching the `unsubscribe_token` precedent). Keep `idempotency_key` typed `str | None` at the column level (nullable stays for schema simplicity / a NULL never collides in Postgres), but it is populated at propose going forward. Add `"cosigning"` to the documented status set in the docstring (proposed → cosigning → cosigned). Import `UniqueConstraint` (already imported at line 30).
- `ballast/backend/coach/decision_record.py` -- MODIFY (sole writer, AD-6):
  - `record_proposal` — mint a stable key via `mint_idempotency_key()` (import from `coach.execution`) and persist it: `repo.add(..., idempotency_key=<key>)`. (This is the ONE place the decision key is minted now.)
  - NEW `async def claim_for_cosign(decision_id, *, scope, session) -> bool` — scoped conditional `update(DecisionRecord).where(DecisionRecord.id == decision_id, DecisionRecord.owner_id == scope.user_id, DecisionRecord.status == "proposed").values(status="cosigning")`; execute, `await session.commit()`, return `result.rowcount == 1`. (System scope not expected here; assert/require a user scope.)
  - NEW `async def release_claim(decision_id, *, scope, session) -> None` — scoped conditional `UPDATE … SET status='proposed' WHERE id AND owner AND status='cosigning'`; commit. Idempotent (a no-op if not in `cosigning`).
  - `cosign` — change the guard from `status == "proposed"` to `status == "cosigning"` (only a claimed record may be cosigned); no longer sets `idempotency_key` (already persisted at propose) — assert it matches the key used, or simply leave the persisted key. Keep the one-shot semantics + snapshot immutability.
  - Update the module docstring for the three-state lifecycle and the stable-key-at-propose change.
- `ballast/backend/api/coach.py` -- MODIFY:
  - `recommend` — unchanged externally; `record_proposal` now stamps the stable key internally (no handler change beyond that being transparent).
  - `approve` — reorder to: `load_decision` (404 if none) → if `status=='cosigned'` return recorded outcome → if `status=='cosigning'` raise 409 in-progress (a NEW calm arm: "This decision is being approved right now — give it a moment and check your Decisions.") → else `won = await claim_for_cosign(...)`; if not `won`, re-load and branch (cosigned→recorded / cosigning→409 / missing→404); if won, `key = record.idempotency_key`; `try: execute_approved_order(intent, broker=…, broker_session=…, idempotency_key=key)` `except (SessionIntegrityError, OrderScopeError) as exc: await release_claim(...); raise HTTPException(409/422)`; on success `cosign(record, order_intent=intent, outcome=outcome, idempotency_key=key)`; `await session.commit()`; return outcome. Remove the per-request `mint_idempotency_key()` call. Update the module + `approve()` docstrings to state the atomic-claim guarantee HONESTLY (no more "deferred to real-broker wiring" caveat for the in-flight window).
- `ballast/backend/coach/execution.py` -- REFERENCE: `mint_idempotency_key` (now called from `record_proposal`), `execute_approved_order` (still accepts and uses the supplied `idempotency_key` verbatim), `OrderScopeError`, `SessionIntegrityError`. Unchanged.
- `ballast/backend/digest/job.py` -- MODIFY `send_weekly_digests`: replace the send-then-mark flow with claim-then-send. Per user: `claim = await session.execute(update(DigestPreference).where(DigestPreference.owner_id == owner_id, DigestPreference.last_sent_week.is_distinct_from(week_key)).values(last_sent_week=week_key, updated_at=<now>))`; if `claim.rowcount == 0` → `result.skipped.append(label)`, `await session.rollback()` (nothing to keep), `continue`; else resolve user/email/portfolio, `sender.send(message)`, `await session.commit()` (claim persists), `result.sent.append(...)`. Keep the existing per-user `try/except` + guarded `rollback()` (a send failure releases the claim). Preserve the deactivated-user skip (skip BEFORE claiming, or release on skip) and the no-matching-user anomaly path. Update the module docstring's idempotency note to describe the atomic claim.
- `ballast/backend/db/repository.py` -- REFERENCE: `ScopedRepository` (`add`/`get`/`list`, per-user). The atomic claim UPDATE is expressed directly in `coach/decision_record.py` (an owner-predicated `update()` statement, mirroring how `digest/job.py` already issues a scoped `update(DigestPreference)`), not via a new repo method — unless a scoped-conditional-update helper is preferred; either way per-user isolation is enforced by the explicit `owner_id` predicate. Unchanged.
- `ballast/backend/tests/test_coach_api.py` -- MODIFY: existing `/approve` tests still pass a real `decision_id`; assert a proposed record now carries a non-null stable `idempotency_key`, and that a cosigned record's key equals the proposed key (stability). Add: (a) an interleaved in-flight approve test — drive two `/approve` calls whose placement is paused (inject a broker/`place_order` that blocks on an `asyncio.Event`, or interleave two independent `AsyncSession` claims) and assert `place_order` is called EXACTLY once, one caller 200s and the other gets 409-in-progress (or the recorded outcome), and the record ends `cosigned`; (b) a two-session claim unit test — two `claim_for_cosign` calls on the same proposed id return `True` then `False` (only one wins); (c) refusal releases the claim — an integrity-409 / scope-422 leaves the record back at `proposed` (not stuck at `cosigning`), broker untouched; (d) the unique-index backstop rejects a duplicate `idempotency_key`. Keep the sole-writer canary green (now also covering the claim/release writes).
- `ballast/backend/tests/test_digest_job.py` -- MODIFY: add an overlapping-run test — two `send_weekly_digests` invocations racing the same opted-in user for one week (two `AsyncSession`s against the test DB, or an interleave) result in `sender.send` called EXACTLY once and one run reporting the user `skipped`; assert a send failure after the claim rolls back so the user is unmarked and re-sent on a subsequent run. Keep the existing idempotent-sequential and failure-isolation tests green.

## Tasks & Acceptance

**Execution:**
- [x] `ballast/backend/db/models.py` -- Add the unique index/constraint on `DecisionRecord.idempotency_key`; document the `proposed → cosigning → cosigned` lifecycle. -- The DB-level backstop making a duplicate placement key un-insertable (in-convention with existing unique indexes).
- [x] `ballast/backend/coach/decision_record.py` -- Mint+persist the stable key in `record_proposal`; add `claim_for_cosign` (atomic proposed→cosigning, rowcount-gated) and `release_claim` (cosigning→proposed); change `cosign`'s guard to `cosigning`. -- Makes the sole writer the owner of an atomic decision claim; the key is stable per decision.
- [x] `ballast/backend/api/coach.py` -- Reorder `approve` to claim-before-place using the stable key; add the 409-in-progress arm; release the claim on integrity/scope refusal; drop the per-request mint. Honest docstrings. -- Closes the in-flight double-approve window at the money seam.
- [x] `ballast/backend/digest/job.py` -- Reorder to claim-before-send via a conditional `UPDATE … WHERE last_sent_week IS DISTINCT FROM :week` gated on `rowcount==1`; release on send failure. -- Closes the digest double-send window at the never-nag seam.
- [x] `ballast/backend/tests/test_coach_api.py` -- Add the interleaved-approve (place_order once, loser 409/recorded, record cosigned), two-session claim (one True one False), refusal-releases-claim, and unique-key-backstop tests; assert stable-key persistence. -- Locks the concurrency guarantee structurally, offline, against real Postgres.
- [x] `ballast/backend/tests/test_digest_job.py` -- Add the overlapping-run (send once, other skipped) and failure-releases-claim tests. -- Locks NFR8 never-double-send under concurrency.

**Acceptance Criteria:**
- Given two simultaneous in-flight `/approve` calls carrying the same `decision_id` on a proposed record, when they execute against the real DB, then exactly one wins the atomic `proposed→cosigning` claim and calls `place_order` EXACTLY once, the other receives a calm 409 in-progress (or the recorded outcome once the winner cosigns), and the record ends `cosigned` — no double-placement is possible (closes deferred-work 4.9; NFR2, AD-5, AD-7).
- Given a decision approved on a live, in-scope path, when it is placed, then the idempotency key used is the STABLE key persisted at `/recommend` time (identical on the proposed and cosigned rows), and a `decision_record.idempotency_key` duplicate is un-insertable (DB unique index) — a retry after a released claim reuses the same key so the broker dedupes rather than double-places.
- Given a won claim that then hits a session-integrity (409) or out-of-scope (422) refusal, when the error surfaces, then the claim is released (`cosigning → proposed`) and the record is approvable again — it is never stuck in `cosigning`, and the broker was never touched.
- Given two overlapping `send_weekly_digests` runs for the same opted-in user in one ISO week, when they race, then exactly one wins the conditional-UPDATE claim and `sender.send` is called EXACTLY once; a send failure after the claim rolls it back so the user is retried next run (closes deferred-work 5.1; NFR8, pull-not-push).
- Given the default offline configuration (`BROKER_ADAPTER=fake`, `LLM_ADAPTER=fake`), when the whole suite runs, then all new concurrency guarantees hold with zero credentials and zero network, the Coach Engine remains the sole writer of decision records (AD-6 canary green) and sole placer (AD-7), the recommendation snapshot immutability and all 4.6/4.7/4.8/4.9/4.10 behaviors still hold, and the previously-green suite stays green.

## Spec Change Log

## Review Triage Log

### 2026-08-01 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 2: (high 1, medium 0, low 1)
- defer: 2
- reject: 7
- addressed_findings:
  - `[high]` `[patch]` `approve` released the claim only on `SessionIntegrityError`/`OrderScopeError`; any OTHER pre-placement error (broker timeout/network/bug) left the record stuck in `cosigning` forever — violating the spec's "any error before a successful placement, the claim is RELEASED" mandate. Added a generic `except Exception:` arm in `api/coach.py::approve` that releases the claim (cosigning→proposed) and re-raises, plus a new test `test_generic_placement_error_releases_claim` asserting a `RuntimeError` in `place_order` leaves the record retryable at `proposed` (place_order attempted once). Both reviewers flagged this as their #1 finding.
  - `[low]` `[patch]` The winner claim re-load dereferenced `record.idempotency_key` with no `None` guard (asymmetric with the loser branch); a concurrent delete between claim-commit and re-load would raise `AttributeError`→500. Added an `if record is None: raise 404` guard for symmetry (calm 404 instead of a 500).

## Design Notes

- **Why the intermediate `cosigning` state instead of `SELECT … FOR UPDATE` around the placement.** `execute_approved_order` awaits a broker network call (`place_order`, and possibly a reconciliation read). Holding a Postgres row lock across that I/O would pin a DB connection for the broker's latency and risk lock pile-ups. The claim pattern instead does a fast, atomic `UPDATE … WHERE status='proposed'` (row-locked only for the microsecond of the update), COMMITS to publish the `cosigning` state, then places outside any lock. Concurrency safety comes from the DB serializing that one conditional UPDATE — exactly one transaction sees a matching `proposed` row and gets `rowcount==1`.
- **The stable key is the real backstop; the claim is the fast path.** Even in the pathological case where a process crashes strictly between the claim commit and cosign/release (leaving an orphaned `cosigning` row), the STABLE persisted idempotency key means any later placement of that decision reuses the same key, so a real broker dedupes rather than double-fills — no double-place is reachable. Surfacing/auto-recovering an orphaned `cosigning` row (e.g. reconciling it via `get_order_status` with the stable key) is a real-broker concern for Story 6.3, not this story; v1's fake broker cannot orphan a claim (no real latency/crash window in-test). This is why the key-at-propose + unique-index pair is not merely belt-and-suspenders.
- **This is in-convention, and here is the precedent** (pre-empting a review that echoes 4.9's "no DB constraints in this codebase" rejection): `market_daily` uses `UniqueConstraint("symbol","day")`; `digest_preference` uses `UniqueConstraint("owner_id")`; `unsubscribe_token` is a `unique=True, index=True` column. Unique indexes ARE an established pattern. What 4.9 correctly rejected was a DB-level *immutability* mechanism (triggers/CHECK constraints) — a different, genuinely-absent pattern this story does NOT introduce. Conditional `UPDATE … WHERE`/`rowcount` gating already appears in `digest/job.py`. So nothing here is a new-to-the-codebase primitive.
- **Backend is Postgres (asyncpg); tests run against it.** `db.session.engine` is Postgres and the test suite creates tables against it via `create_all` in the `ensure_tables` fixture — so a genuine two-session concurrency test (two `AsyncSession`s, one conditional UPDATE serialized by the row lock) is feasible and is the honest way to prove the in-flight window is closed, rather than asserting a sequential proxy.
- **Handoff to 6.3 (live Schwab placement).** Once the real `place_order`/`get_order_status` land, the stable per-decision key is what a real broker uses to dedupe a re-placement, and an orphaned `cosigning` row can be reconciled by reading authoritative state with that key. 6.1 makes those guarantees TRUE in code so 6.3 can flip `BROKER_ADAPTER=schwab` without reopening this gap.

## Verification

**Commands:**
- `cd ballast/backend && .venv/bin/python -m pytest tests/test_coach_api.py tests/test_digest_job.py -q` -- expected: all pass, including the new concurrency tests; zero network, zero credentials (fake adapters).
- `cd ballast/backend && .venv/bin/python -m pytest -q` -- expected: full suite green (was 264 after 5.1; no regressions).
- `cd ballast/backend && .venv/bin/python -c "import coach.decision_record, coach.execution, sys; assert 'anthropic' not in sys.modules"` -- expected: exit 0 (no SDK import on the fake/default path).
- `git -C /Users/blainearnau/repos/ai_practice_project status --short` -- expected: only Code-Map files changed; `ballast/backend/uv.lock` clean (revert any `uv run` anthropic drift).

**Manual checks:**
- Grep canary: all `decision_record` status writes (`add`/`claim_for_cosign`/`release_claim`/`cosign`) live only in `coach/decision_record.py`; `api/coach.py` delegates.

## Auto Run Result

Status: done

### Summary of implemented change
Closed the single systemic in-flight concurrent-re-entry gap flagged by the Epic 4 retrospective, at both consequential seams, structurally — before real-broker (6.3) or parallelized-digest wiring can make it bite:
- **Decision approve seam:** a stable per-decision `idempotency_key` is now minted at `/recommend` time and persisted on the proposed row (reused verbatim on every placement), backed by a DB unique index. An intermediate `cosigning` status plus an atomic `proposed → cosigning` claim (a scoped conditional `UPDATE … WHERE status='proposed'` gated on `rowcount == 1`, committed before the broker call) means only the claim winner ever places; the loser gets the recorded outcome or a calm 409 in-progress. A refusal or any pre-placement failure releases the claim (`cosigning → proposed`) so the decision stays retryable.
- **Digest seam:** `send_weekly_digests` now claims-before-send via a conditional `UPDATE DigestPreference … WHERE last_sent_week IS DISTINCT FROM :week` gated on `rowcount == 1`; only the winner sends, and a send failure rolls the claim back for next run.

Closes deferred-work 4.9 (concurrent-approve double-place) and 5.1 (digest double-send) in code. All Epic-4 execution invariants, AD-6 sole-writer, AD-7 sole-placer, and recommendation-snapshot immutability are unchanged.

### Files changed
- `ballast/backend/db/models.py` — unique index `uq_decision_record_idempotency_key` on `DecisionRecord.idempotency_key`; documented the `proposed → cosigning → cosigned` lifecycle.
- `ballast/backend/coach/decision_record.py` — mint+persist the stable key in `record_proposal`; new `claim_for_cosign` (atomic proposed→cosigning, rowcount-gated, commits before returning) and `release_claim` (cosigning→proposed, idempotent); `cosign` guard changed from `proposed` to `cosigning`.
- `ballast/backend/api/coach.py` — reordered `approve` to claim-before-place with the stable persisted key; new calm 409 in-progress arm; claim released on integrity(409)/scope(422) refusal AND (review patch) on any other pre-placement error; None-guard on the winner re-load; per-request key mint removed; honest docstrings.
- `ballast/backend/digest/job.py` — claim-before-send conditional UPDATE; release on send failure; deactivated/no-user skips before the claim; fixed a latent expired-ORM read (used the captured `label` after rollback) exposed by the genuine two-session race.
- `ballast/backend/tests/test_coach_api.py` — added interleaved in-flight approve (place_order exactly once, winner 200 / loser 409, record cosigned), two-session claim (True then False), refusal-releases-claim (409 + 422), generic-placement-error-releases-claim (review patch), unique-index backstop; stable-key persistence/equality assertions.
- `ballast/backend/tests/test_digest_job.py` — added overlapping-run race (send exactly once, other skipped) and send-failure-releases-claim tests.

### Review findings breakdown
- **Patches applied (2):** (high) `approve` only released the claim on the two typed refusals — any other pre-placement error stranded the record in `cosigning` forever; broadened to release on any pre-placement error + re-raise, with a new locking test (this is exactly the spec's "release on any error before a successful placement" mandate, which the first implementation missed). (low) added a `None` guard on the winner claim re-load for symmetry with the loser branch (calm 404 instead of a 500 on a concurrent delete).
- **Deferred (2):** NULL-`idempotency_key` on a carried-over pre-6.1 proposed row → post-placement `cosign` assertion strands the record (cannot trigger via normal v1 flow; guard/backfill at go-live/6.3); digest send-then-commit → a commit failure after a successful send re-sends next run (rare sequential window; needs an idempotent send at real-email wiring). Both appended to `deferred-work.md`.
- **Rejected (7):** transient 409 when the loser re-loads a released-back-to-`proposed` record (self-correcting on retry); release/claim commit-failure requiring a DB-poisoned session; the committing-helper session-boundary concern (spec-designed); speculative `get_portfolio` write coupling; the hypothetical `SERIALIZABLE` isolation change; the post-placement orphaned-`cosigning` recovery (explicitly scoped to Story 6.3 in Design Notes); NULL `is_active` treated as inactive (acceptable fail-closed skip).

### Verification performed
- `pytest tests/test_coach_api.py tests/test_digest_job.py -q` → 56 passed (targeted concurrency tests, genuine two-AsyncSession races against the Postgres test DB).
- `pytest -q` (full suite) → **294 passed**, 0 failed, 0 skipped (baseline was 264 after 5.1; no regressions).
- Import guard `import coach.decision_record, coach.execution; assert 'anthropic' not in sys.modules` → exit 0 (no SDK import on the fake/default path).
- Sole-writer canary: grep confirms all `decision_record` status writes (`add`/`claim_for_cosign`/`release_claim`/`cosign`) live only in `coach/decision_record.py`; `api/coach.py` occurrences are reads/comparisons. AD-6 canary test green.
- `git status --short` — only Code-Map files changed; `ballast/backend/uv.lock` CLEAN (no anthropic drift).

### Residual risks
- The two deferred items above are the honest residuals; both are documented in `deferred-work.md` and are pre-launch-safe (fake broker, stub sender, single-path cron). Neither is reachable via the normal offline v1 flow.
- The orphaned-`cosigning`-after-successful-placement recovery is a real-broker (6.3) concern by design — the stable persisted key makes a re-placement dedupe rather than double-fill, so no double-place is reachable even in that pathological window.
- `followup_review_recommended: false` — the two patches are localized and test-covered (a 6-line exception arm aligning code to an already-correct spec, plus a defensive None guard); no broad, architectural, or API-shape change that would benefit from an independent follow-up pass.
