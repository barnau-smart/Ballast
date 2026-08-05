# Sprint Change Proposal — Reopen Story 8.5 (Test-Suite Performance)

**Date:** 2026-08-05
**Author:** correct-course (with MasterB)
**Trigger story:** `8-5-test-suite-performance` (marked `done`, commit `c76181e`)
**Change scope:** Moderate (localized test-infra defect + a verification-process gate; no product/app code involved)

---

## Section 1 — Issue Summary

Story 8.5 was marked **`done`** with a self-reported result of *"`test_coach_api.py` ~15s, full suite ~35s, 108 passed on 3 consecutive runs."* Independent verification contradicts that report:

- **Run 1:** `uv run pytest tests/test_coach_api.py` ran **12+ minutes** and never completed — killed manually.
- **Run 2 (clean, verbose):** hung again — **3.5 min, ~3s CPU** — same signature.
- **Signature:** postgres shows one connection `idle in transaction / ClientRead` on a `SELECT market_daily…`; the pytest process sits at **~0% CPU**. The async event loop is parked awaiting a DB result that never returns.

**Root cause (high confidence):** a **cross-event-loop asyncpg hang** introduced by the new `tests/conftest.py`:
- `ensure_tables` and `client` are **`scope="session"`** async fixtures that use the **module-global asyncpg `engine`** (`db.session.engine`).
- Individual tests run on pytest-asyncio's **per-function event loops**.
- asyncpg connections are **not safe across event loops** — a pooled connection created on the session-setup loop, then reused by a test on a different loop, awaits a result future on the wrong loop → the await never resolves → hang.

The story's completion notes explicitly **flagged this exact risk** ("async session-scoped `ensure_tables` vs event-loop ScopeMismatch") and claimed to have "empirically disproven" it. **That self-verification was false** — the loop's in-run test execution masked the hang (batched/interrupted invocation), so a real defect passed the gate.

**Contained blast radius:** the **fast test-only SHA-256 password hasher** (conftest #1) is *function-scoped and safe* — it is the dominant speedup (Argon2 was the primary cost) and is not implicated. The hang comes **only** from the session-scoping of the app + `ensure_tables` (#2/#3), which currently only `test_coach_api.py` consumes.

## Section 2 — Impact Analysis

- **Epic impact:** None to Epic 8's product scope. 8.1–8.4 are functionally complete and unaffected; this is test infrastructure only.
- **Story impact:** `8-5-test-suite-performance` must be **reopened** (`done` → `in-progress`); it did not meet its own AC #1 (full suite completes under timeout in one invocation) or AC #6 (stable, no hang). No other story changes.
- **Artifact conflicts:** PRD, epics, architecture, UX — **no impact** (no product behavior involved).
- **Technical impact:** localized to `ballast/backend/tests/conftest.py` (fixture scoping) and `sprint-status.yaml`. No `api/`, `coach/`, `brokers/`, `db/` production code is touched — 8.5 never changed production code, so **no rollback of product code is required**.
- **Process impact (important):** the loop's self-verification cannot be trusted for async-fixture / test-performance changes. A hardened, independent verification gate is required going forward.

## Section 3 — Recommended Approach

**Direct Adjustment** (not rollback, not MVP review): reopen 8.5 and fix the fixture scoping, keeping the safe fast-hasher win. Two candidate fixes, in preference order:

- **(A) Preferred — revert the session-scoping of app + `ensure_tables` to function scope; keep the fast SHA-256 hasher.** Lowest risk. The fast hasher was the dominant win (Argon2 ~22ms × hundreds of ops), so most of the speedup is retained *without* the cross-loop hazard. Measure the result — if it's already "good enough" (well under the timeout), stop here.
- **(B) If (A) leaves it too slow — pin a single session-scoped event loop** (pytest-asyncio `loop_scope="session"` / a session-scoped `event_loop`) so the shared session engine + connections stay on one loop, making the session-scoped app safe. Higher payoff, higher care needed (all async fixtures/tests must share the loop).

**Effort:** small (one fixture file). **Risk:** low for (A), medium for (B). **Timeline:** one dev pass + one independent verification.

## Section 4 — Detailed Change Proposals

**Change 1 — Reopen the story (sprint-status.yaml)**
```
OLD:  8-5-test-suite-performance: done
NEW:  8-5-test-suite-performance: in-progress   # reopened 2026-08-05 — see sprint-change-proposal-2026-08-05.md
```
Rationale: the story did not meet AC #1/#6; a hang is not "done."

**Change 2 — Harden Story 8.5 acceptance (add AC #8, verification gate)**
```
NEW AC #8 — Independent verification (non-negotiable):
  Given the fix, when `uv run pytest tests/test_coach_api.py` is run as a SINGLE,
  UN-BATCHED invocation to completion, then it finishes with all tests passing in
  under 120s with NO hang (no idle-in-transaction stall, process stays CPU-active).
  This must be confirmed by a run INDEPENDENT of the dev agent's own self-report
  (the reopen was caused precisely by a false self-verification). Record the actual
  wall-time and a twice-in-a-row stability check.
```

**Change 3 — Fix `tests/conftest.py` (dev work; handed off)**
- Apply approach (A): make the `client` + `ensure_tables` fixtures **function-scoped** (or otherwise ensure every async DB operation runs on the same loop as its connection), keeping the `_fast_password_hasher` autouse fixture unchanged.
- If (A) is insufficient, apply approach (B): pin `loop_scope="session"` and keep the shared app.
- Preserve every `ensure_tables` DDL statement (already verbatim) and the fast-hasher behavior.

**Change 4 — Record the lesson (deferred-work / retro action item)**
- Note: "The autonomous loop's in-run test execution can mask a hang and report a false pass; test-performance / async-fixture stories require an independent full-suite un-batched run before `done`." Capture for the Epic 8 retro and as a guardrail.

## Section 5 — Implementation Handoff

- **Scope classification:** Moderate (reopen + targeted fix + verification gate).
- **Route to:** Developer agent (`bmad-dev-story` on the reopened 8.5), then an **independent** verification run (outside the dev agent's self-report).
- **Success criteria:** AC #8 met — `test_coach_api.py` completes un-batched under 120s, no hang, stable twice-in-a-row, coverage unchanged, fast-hasher retained.
