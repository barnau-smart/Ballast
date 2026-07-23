---
baseline_commit: NO_COMMITS
---

# Story 1.4: Fail-closed per-user data isolation

Status: done

## Story

As a user,
I want my data reachable only by me,
so that no other user can ever see my finances.

## Acceptance Criteria

1. **Scoped access, fail-closed.** Given the scoped-repository layer, when any data access runs, then it is scoped to the authenticated user; **a query issued without an explicit scope raises an error (fail-closed)** — never a silent all-access. [Source: epics.md#Story-1.4 (AD-10, NFR5)]
2. **Explicit SYSTEM scope for non-user work.** Non-user jobs (market-data ingestion, digest, batch) run under an explicit, named SYSTEM/global scope that is deliberately requested — not the accidental default. [Source: epics.md#Story-1.4, ARCHITECTURE-SPINE.md#AD-10]
3. **Cross-user isolation, test-verified.** User A can never read user B's records — proven by an automated test against a real per-user table. [Source: epics.md#Story-1.4 (NFR5)]

**Cross-cutting:** never log data belonging to the wrong scope; the layer is the single funnel later stories (portfolio_cache, decision_record) MUST use. [Source: ARCHITECTURE-SPINE.md#AD-10, #Consistency-Conventions "State mutation"]

## Tasks / Subtasks

- [x] **Task 1: Scope type (fail-closed by construction)** (AC: 1, 2)
  - [x] Add `ballast/backend/db/scope.py` defining a `Scope` value object with exactly two constructors: `Scope.for_user(user_id: UUID)` and `Scope.system()` (a named SYSTEM/global scope for jobs). There is NO default/empty scope. A `Scope` always carries either a concrete user id or the explicit SYSTEM marker.
  - [x] Passing `None`/missing where a `Scope` is required must raise (fail-closed) — never be treated as all-access.
- [x] **Task 2: Owned-entity base** (AC: 1, 3)
  - [x] Add an `OwnedEntityMixin` (in `db/models.py` or `db/scope.py`) providing an indexed `owner_id` UUID column that FKs to `"user".id`. This is the mixin every future per-user table (portfolio_cache, decision_record) will use so the scoped repository can filter by owner.
- [x] **Task 3: ScopedRepository (the single funnel)** (AC: 1, 2, 3)
  - [x] Add `ballast/backend/db/repository.py` with a generic `ScopedRepository` that REQUIRES a `Scope` and an owned model class at construction; if the scope is missing/None it raises immediately (fail-closed).
  - [x] Read/list/get operations automatically filter `owner_id == scope.user_id` for a user scope. Create/add operations stamp `owner_id` from the scope (a user scope cannot create rows owned by someone else).
  - [x] A `Scope.system()` scope is the ONLY way to access across users (for jobs) — and it is explicit. A user scope can never reach another user's rows.
  - [x] Attempting to fetch/modify a row whose `owner_id` != the user scope returns nothing / is not permitted (no leak, no cross-write).
- [x] **Task 4: Wire the authenticated user → Scope** (AC: 1)
  - [x] Provide a FastAPI dependency that builds a `Scope.for_user(current_user.id)` from the authenticated user (reusing 1.3's `current_user`). This is how request handlers obtain their scope. (No user-data endpoints exist yet to consume it — that's later stories — but provide the dependency + a docstring'd usage example so later stories use the funnel, not raw sessions.)
- [x] **Task 5: Tests (the isolation proof)** (AC: 1, 2, 3)
  - [x] Using a real DB and a representative owned test model (define a small `OwnedEntityMixin`-based model in the test module; create/drop its table in the test so production schema stays clean):
    - Insert rows owned by user A and user B.
    - A `ScopedRepository` for user A lists/gets ONLY A's rows; never B's (AC3).
    - User A's repo cannot read or mutate a specific B-owned row (returns none / refuses).
    - Constructing/using a repository WITHOUT a scope raises (fail-closed) (AC1).
    - A `Scope.system()` repo can see across users (AC2) — and this required the explicit system scope.
    - Creating via user A's repo stamps `owner_id == A` (cannot forge another owner).
  - [x] No regressions in 1.1/1.2/1.3 suites.
- [x] **Task 6: Verify**
  - [x] Run full backend suite + frontend suite + `lint:css` + `build`. (This story is backend-only; frontend suites just confirm no accidental breakage.)

## Dev Notes

### This is the security spine — get it right
AD-10 is the invariant that makes the whole product trustworthy: "all persistence goes through a scoped-repository layer that is **fail-closed** — a query without an explicit scope is an error, never all-access." Every later per-user feature (portfolio, decisions) depends on this being correct and unavoidable. The goal is to make cross-user access **structurally impossible by default**, not merely discouraged.

### Builds on 1.1–1.3 (all done) — reuse
- Async SQLAlchemy engine/session + `Base` from `ballast/backend/db/session.py` and `db/models.py` (1.2).
- `User` model (UUID PK) — `owner_id` FKs to `"user".id`.
- `current_user` dependency from `api/users.py` (1.3) — the source of the authenticated user id for `Scope.for_user`.
- Conventions: UUID PKs; never log secrets/other-scope data. [Source: ARCHITECTURE-SPINE.md#Consistency-Conventions]

### Design intent (fail-closed, explicit SYSTEM)
- **No implicit scope.** The repository cannot be constructed into an "all rows" state by omission. The ONLY way to cross users is `Scope.system()`, which a caller must type out — so it's greppable and auditable (jobs in `marketdata/`, `digest/`).
- **A user scope is a cage:** reads filter by owner; writes stamp owner; there is no method that returns another user's row under a user scope.
- Keep the API small and obvious so later stories reach for it instead of raw sessions.

### Scope guardrails
- **In scope:** `Scope`, `OwnedEntityMixin`, `ScopedRepository`, the request→scope dependency, and the isolation tests. This is infrastructure — there is no user-facing feature or endpoint to add yet.
- **Out of scope:** actual per-user domain tables (portfolio_cache → Story 2.3, decision_record → Epic 4), Schwab, coach/precedent/LLM, brokerage-token encryption. Do NOT create real domain tables here; use a test-only model to prove isolation.
- **Do not** weaken 1.2/1.3: the `User` table is managed by FastAPI-Users and is not itself an owned-entity (it IS the owner) — don't route FastAPI-Users through the ScopedRepository.

### Testing standards
Real DB integration (no mocks) for the isolation proofs — this is exactly the property that must never silently break. Assert the negative hard: user A's repo returns zero of B's rows; missing-scope raises; forging an owner is impossible. Create/drop the test model's table within the test (or a fixture) so production schema stays clean.

### Project Structure Notes
- New backend files: `db/scope.py`, `db/repository.py`, additions to `db/models.py` (mixin), a scope dependency (in `api/` or `db/`), and `tests/test_isolation.py`.
- No frontend changes expected.

### References
- [Source: _bmad-output/planning-artifacts/epics.md#Story-1.4]
- [Source: ARCHITECTURE-SPINE.md#AD-10, #AD-6, #Consistency-Conventions]
- [Source: implementation-artifacts/1-2-register-with-email-password.md, 1-3-log-in-session.md — DB session, User model, current_user]

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (orchestrator) + implementation subagent + fresh-context adversarial security-review subagent.

### Debug Log References

- Backend pytest: 25 passed (real Postgres), incl. 9 isolation proofs. Frontend: 25 passed (unchanged). `lint:css` + `build` clean.
- Verified structurally: `Scope(...)` direct construction raises `TypeError` (both SYSTEM and USER forms), even under `python -O`; factories work; equality ignores the guard.
- Verified schema cleanliness: only the `user` table exists in the DB — the test-only owned model never leaks (private MetaData + create/drop fixture).

### Completion Notes List

- **AC1 (fail-closed):** `ScopedRepository` requires a `Scope` at construction; a missing/None scope raises. `Scope` itself is fail-closed *by construction* — after review, the raw dataclass constructor is gated by a private guard token so there is **no back-door all-access scope**; only `Scope.for_user()` / `Scope.system()` produce instances.
- **AC2 (explicit SYSTEM):** `Scope.system()` is the sole cross-user path, greppable and used in zero production paths so far. Under SYSTEM scope `add()` requires an explicit `owner_id` (won't create ownerless/misowned rows).
- **AC3 (isolation, tested):** under a user scope the repo is a cage — `list()`/`get()` filter by owner (in SQL); `get()` of another user's row returns `None` and, after the M1 fix, never loads the foreign row into the session; `add()` force-stamps the scope's owner and refuses to forge another. Real-DB tests assert the hard negatives (A sees exactly its rows, zero of B's).
- **Wiring:** `api/deps.py:get_scope` builds `Scope.for_user(current_user.id)` from 1.3's auth — the funnel later stories must use instead of raw sessions.
- **Scope discipline:** no real domain tables added (portfolio/decision deferred); `User`/FastAPI-Users not routed through the repo; no Schwab/coach/LLM.

### File List

**Created — backend**
- `ballast/backend/db/scope.py` (Scope value object, guard-gated construction)
- `ballast/backend/db/repository.py` (ScopedRepository — the single funnel)
- `ballast/backend/api/deps.py` (get_scope dependency)
- `ballast/backend/tests/test_isolation.py` (9 real-DB isolation/fail-closed proofs)

**Modified — backend**
- `ballast/backend/db/models.py` (OwnedEntityMixin — indexed owner_id FK to user.id)

## Senior Developer Review (AI)

- **Date:** 2026-07-23 · **Outcome:** CHANGES-REQUIRED → resolved → done
- **Reviewer actively tried to break isolation** (cross-user get, shared-session identity-map attack, owner forging) and could not — the runtime cage holds; all 3 ACs verified against a real per-user table.
- **H1 (fixed):** `@dataclass(frozen=True)` auto-generated a public `__init__`, so `Scope(_ScopeKind.SYSTEM, None)` back-doored an all-access scope without the greppable `Scope.system()`, and `user_id` was guarded by a bare `assert` (stripped under `-O`). Fixed with a private construction guard + `__post_init__` validation + real `raise`; added a regression test. Now structurally impossible.
- **M1 (fixed):** `get()` loaded the full foreign row before the Python owner check; rewritten to filter ownership in SQL so a non-owned row is never hydrated.
- **L1 (noted):** `add()` returns flushed-not-committed (intentional; caller controls commit).

## Change Log

- 2026-07-23 — Story 1.4 implemented: the AD-10 fail-closed scoped-repository layer — `Scope` (user/SYSTEM), `OwnedEntityMixin`, `ScopedRepository` (the single per-user persistence funnel), and the request→scope dependency. Adversarial security review found + fixed a back-door constructor (H1) and a foreign-row-load (M1); construction is now structurally fail-closed. All suites green (backend 25/25 incl. 9 isolation proofs, frontend 25/25). Status → done. **Epic 1 complete.**
