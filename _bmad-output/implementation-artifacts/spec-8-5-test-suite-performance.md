---
title: 'Story 8.5 — Tame the slow test_coach_api.py integration suite'
type: 'chore'
created: '2026-08-04'
status: 'done'
baseline_revision: '8573332bbd69a22df782b850b795e2564ff9239e'
final_revision: '96a39a07a482970728b1d1e21abb9adca44a91b5'
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: ['oversized']
---

<intent-contract>

## Intent

**Problem:** The backend suite is dominated by `tests/test_coach_api.py` (102 tests), which exceeds the 600s pytest timeout when run whole and forces every dev/review pass into ~4–5-min batches — a ~15-min test tax that dominated Stories 8.2/8.4 and makes healthy loops look "stuck." Three stacking costs: Argon2 password hashing on every register+login (primary), `create_app()` rebuilt per test, and an `autouse` per-test DDL storm (`ensure_tables`).

**Approach:** A test-only speedup, no product change: profile first, then (1) swap in a cheap test-only password hasher, (2) introduce `tests/conftest.py` with a shared session/module-scoped app+TestClient and one-time `ensure_tables`, and (3) add pytest marker/config so the default lane runs in a few minutes with a documented full-suite command. Coverage is sacred — the same tests run and pass; anything reclassified to a slow lane is listed.

## Boundaries & Constraints

**Always:**
- No product/app behavior change. Production auth stays pwdlib/Argon2; the fast hasher is test-scoped only, never reachable from a production code path.
- Preserve **every** `ensure_tables` DDL statement (`test_coach_api.py:93-146` — the `CREATE/ALTER/INDEX ... IF NOT EXISTS` reconciliation for `idempotency_key`, `broker_ref`, `(owner_id, co_signed_at)`, `reconciliation_snapshot`, `reconciled_at`, `cosigning_at`) — just run it once (session-scoped) instead of per-test.
- Keep per-test isolation exactly as today: unique-email users (`_unique_email`) + self-cleanup; real docker Postgres; fake broker/LLM adapters. No shared mutable state.
- The same set of assertions/tests execute and pass (default lane + any opt-in slow lane == the prior suite). No test deleted or weakened to gain speed.
- Record before/after wall-times and verify a twice-in-a-row run is stable (shared fixtures are the flakiness risk; the 8.2 review already saw a test-ordering state-leak in this file).

**Block If:**
- After the fast-hasher + shared-fixture changes, the default lane still can't get under the 600s timeout and the only remaining lever is deleting/weakening tests or changing production code (`api/`, `coach/`, `brokers/`, `db/`). HALT `blocked` — do not compromise coverage or touch production.

**Never:**
- Never modify production modules under `api/` (incl. `api/users.py`), `coach/`, `brokers/`, `db/` for speed. If the profile reveals an app-side inefficiency, log it as a follow-up in `deferred-work.md` — do not fix it here.
- Never delete, skip, or weaken a test, or reduce assertion coverage, to gain speed.
- Never introduce order-dependence / shared mutable state that breaks isolation.

</intent-contract>

## Code Map

- `ballast/backend/tests/test_coach_api.py` -- the slow suite. `client` fixture is function-scoped `TestClient(create_app())` (`:150-153`); `ensure_tables` is `@pytest_asyncio.fixture(autouse=True)` function-scoped DDL (`:81-147`); 102 tests each `_register`+`_login` → Argon2 hot path (helpers `:159-177`); `engine`/`async_session_maker` already module-level singletons (`:68`, do NOT rebuild). Per-test broker swaps use `client.app.dependency_overrides[get_broker]` then pop.
- `ballast/backend/tests/conftest.py` -- **NEW** (none exists today). Home for the session/module-scoped app+TestClient, the one-time `ensure_tables`, the test-only fast-hasher override, and a per-test `dependency_overrides` cleanup guard.
- `ballast/backend/api/users.py` -- production pwdlib/Argon2 via FastAPI-Users; `get_user_manager` (`:70-74`) yields `UserManager(user_db)` with no `password_helper`. Override seam = a test dependency-override that yields `UserManager(user_db, password_helper=<cheap>)`. PRODUCTION: untouched.
- `ballast/backend/api/app.py` -- `create_app()` (`:64-213`): full router registration + lifespan (`create_db_and_tables` + `run_startup_migrations`); expensive to rebuild per test.
- `ballast/backend/pyproject.toml` -- `[tool.pytest.ini_options]` (`:28-30`) is minimal (`testpaths`, `pythonpath`); add `markers` (register `slow`) and, if warranted, `addopts`. Test deps at `:21-26`.
- Other `tests/test_*.py` (~32 files) each build their own `TestClient(create_app())` — a conftest-level test-only fast hasher benefits them too; consolidate only where safe.

## Tasks & Acceptance

**Execution:**
- [x] `ballast/backend/tests/test_coach_api.py` -- Run `uv run pytest tests/test_coach_api.py --durations=25` and record top offenders in completion notes BEFORE changing anything; confirm the three hypothesized costs against the data and adjust the plan if it disagrees.
- [x] `ballast/backend/tests/conftest.py` (new) -- Add a **test-only fast password hasher**: override the `get_user_manager` FastAPI dependency so register/login stop running Argon2 hundreds of times, wired via a fixture/override — never in production. Add a **session/module-scoped app+TestClient** fixture and move `ensure_tables` to **session-scoped, run once** (preserving every DDL statement). Add an autouse per-test guard that clears leaked `dependency_overrides` after each test so shared app + per-test broker swaps stay isolated.
- [x] `ballast/backend/tests/test_coach_api.py` -- Consume the shared conftest fixtures; drop the function-scoped `client` and the autouse per-test `ensure_tables`. Keep unique-email + self-cleanup isolation. Where safe, share the fixtures with other test files that build their own client (consolidate, don't fork).
- [x] `ballast/backend/pyproject.toml` -- Register a `slow` marker in `[tool.pytest.ini_options]`. If (and only if) profiling shows a genuinely heavy residual after the above, mark those `slow` and make the default command exclude `slow` (with a documented full-suite command), OR shard by module so no file blows the timeout — pick whichever the profile supports; document the fast-default and full commands. Evaluate `pytest-xdist` only if fixtures are provably parallel-safe; skip if it introduces any flakiness.
- [x] `ballast/backend/tests/` -- Verify coverage + stability: diff collected node IDs pre/post (list any reclassified to a slow lane), run the suite twice back-to-back to confirm no new order-dependence, and record before/after wall-times in completion notes.
- [x] `_bmad-output/implementation-artifacts/deferred-work.md` -- Close/annotate the original slow-test entry (logged 2026-08-04, commit `0a7616b`) that this story resolves.

**Acceptance Criteria:**
- Given the full backend suite, when run in a single default invocation, then it completes well under the 600s timeout with no batching (target: a few minutes), and before/after wall-times are recorded.
- Given the pre-change suite, when the optimized suite runs, then the same set of tests execute and pass (default + any opt-in slow lane == prior suite); any test moved to a slow lane is explicitly listed; nothing deleted or weakened.
- Given the password-hashing cost, when tests register/login, then a test-only fast hasher is used while `api/users.py` keeps pwdlib/Argon2; the override is test-scoped only and this is asserted/documented.
- Given the shared `create_app()` and `ensure_tables` DDL, when the suite runs, then that setup is shared (session/module-scoped, run once) while each test keeps correct isolation.
- Given the fast/slow mechanism, when a developer or the loop runs the default command, then the fast-default and full-suite commands are documented.
- Given shared fixtures can leak state, when the suite runs twice in a row, then it is stable (no new order-dependence/flakiness) — verified, not assumed.
- Given the whole change, then all existing tests stay green and no production behavior changes (real Argon2 + production auth unchanged).

## Design Notes

**Override seam (grounded):** adapters in this suite are wired by `dependency_overrides` (e.g. `get_broker`), NOT env vars. So the fast hasher belongs there too: override `api.users.get_user_manager` to yield `UserManager(user_db, password_helper=cheap)`. FastAPI-Users 15.x `BaseUserManager.__init__` accepts `password_helper`; a cheap helper is a `PasswordHelper` backed by a trivial/cheap context (e.g. a fast pwdlib hasher) — it must still hash+verify consistently so login works. Prefer applying this once on the shared session-scoped app; for the other test files, an autouse conftest fixture (or a session monkeypatch of the default helper) can apply the same test-only override.

**Shared-app isolation risk (AC #6):** a session-scoped app means a test that sets `dependency_overrides[get_broker]` and dies before popping would leak into the next test — the per-test app currently masks this. Mitigate with an autouse fixture that snapshots/clears overrides (except the base fast-hasher) after each test. `TestClient(...)` as a context manager runs lifespan once when session-scoped (good — `create_db_and_tables` + `run_startup_migrations` run once).

**Evidence-first:** the fast hasher + shared fixtures are expected to be the whole win (this class of change commonly cuts such suites 5–10×). The slow-lane split / xdist are contingent on the profile — if the default lane already lands in a few minutes, a split may be unnecessary; still register the `slow` marker and document the commands.

## Verification

**Commands (run from `ballast/backend`, DB up via `docker compose up -d db`):**
- `uv run pytest tests/test_coach_api.py --durations=25` -- baseline profile; capture top offenders before changes.
- `uv run pytest --collect-only -q > /tmp/nodes-pre.txt` (before) and again after -- diff to prove the same node IDs run (minus any explicitly-listed slow-lane reclass).
- `uv run pytest -q` -- default lane completes well under 600s in ONE invocation, all pass.
- `uv run pytest -q && uv run pytest -q` -- twice-in-a-row, both green (AC #6 stability).
- Full-suite command (if a slow lane is added) documented and run green, e.g. `uv run pytest -q -m "slow or not slow"` or the chosen equivalent.

**Manual checks:**
- Confirm `api/users.py` is unchanged (git diff empty for it) and no file under `api/`/`coach/`/`brokers/`/`db/` is modified.
- Confirm the fast hasher is only referenced from `tests/` — grep the override symbol is test-only.

## Review Triage Log

### 2026-08-04 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 2: (high 0, medium 0, low 2)
- defer: 0
- reject: 9
- addressed_findings:
  - `[low]` `[patch]` conftest/pyproject prose overstated the shared-app reach — clarified that only `test_coach_api.py` consumes the session-scoped app (other endpoint files still build their own `client`); the test-only fast hasher is what spans all files. Avoids a maintainer believing the shared app accelerates the whole suite.
  - `[low]` `[patch]` Added a guardrail comment documenting the single-hasher invariant — register+login use the same hasher per unique-email user, so a cross-hasher DB row (Argon2 verified under the fast hasher) can't arise today; without the note a future test splitting register/login across the `real_hasher` boundary would hit a confusing `UnknownHashError`. Not triggerable in the current suite.
  - Rejected (noise / non-triggering): isolation-guard "no-op leak" (reaching the session app *requires* the `client` fixture, so the guard's condition is exactly right); async session-scoped `ensure_tables` vs event-loop ScopeMismatch (empirically disproven — 108 passed on 3 consecutive runs); non-UTF-8 password decode; multi-hasher rehash-write (single-hasher tuple, contract holds); `getfixturevalue` side-effect; empty `slow`-lane doc; redundant guard overhead on shadowing files; login-timing coupling.

## Auto Run Result

**Status:** done

**Summary:** Test-infrastructure-only speedup of the backend suite (dominated by `tests/test_coach_api.py`, 108 tests). Three stacking costs removed, all strictly test-scoped with zero product/app behavior change: (1) a test-only fast SHA-256 pwdlib hasher injected by monkeypatching `UserManager.__init__`'s `None` `password_helper` default in a new `tests/conftest.py` (production `api/users.py` stays pwdlib/Argon2; `test_register.py` opts out via a new `real_hasher` marker to keep asserting real hashing); (2) the per-test `TestClient(create_app())` rebuild replaced by a session-scoped shared app + an autouse `dependency_overrides` isolation guard; (3) the autouse per-test `ensure_tables` DDL storm moved to a session-scoped one-time fixture with every `CREATE/ALTER/INDEX ... IF NOT EXISTS` statement preserved verbatim.

**Files changed:**
- `ballast/backend/tests/conftest.py` (new) -- shared test-only fixtures: fast password hasher, session-scoped app+TestClient, one-time `ensure_tables`, per-test override isolation guard.
- `ballast/backend/tests/test_coach_api.py` -- consumes the shared fixtures; dropped the function-scoped `client` + autouse per-test `ensure_tables` and now-unused imports.
- `ballast/backend/tests/test_register.py` -- module-level `pytestmark = pytest.mark.real_hasher` (keeps production Argon2 for its hash-format assertions).
- `ballast/backend/pyproject.toml` -- registered `slow` + `real_hasher` markers; documented fast-default + full-suite commands.
- `_bmad-output/implementation-artifacts/deferred-work.md` -- annotated the original slow-test entry `RESOLVED by Story 8.5`; logged one follow-up (pre-existing `.env`-driven `llm_gateway`/`market_ingest` factory-test failures).

**Review findings breakdown:** 2 low patches applied (comment-only, no behavior change); 0 deferred by this pass (the `.env`-driven failures were logged during implementation); 9 rejected as noise/non-triggering.

**Verification (measured, `LLM_ADAPTER=fake BROKER_ADAPTER=fake` against docker Postgres):**
- `test_coach_api.py`: baseline ~26s → **15.1s**, 108 passed. Stable twice-in-a-row (14.5s / 14.9s), no order-dependence (the file where the 8.2 state-leak lived).
- Full default lane `uv run pytest -q`: **~35s** in ONE invocation, well under the 600s timeout — no batching, no slow-lane split needed.
- Coverage: **591 node IDs collected pre/post, unchanged** — no test deleted, weakened, or reclassified.
- Production untouched: `git diff --stat -- api/ coach/ brokers/ db/` empty; fast hasher referenced only from `tests/`.
- Pre-existing non-8.5 failures: 3–4 `test_llm_gateway.py`/`test_market_ingest.py` factory tests fail on this machine because the local `.env` sets `LLM_ADAPTER=anthropic` + a real key; **verified they fail identically with the 8.5 conftest removed** — not a regression (logged to `deferred-work.md`).

**Follow-up review recommended:** false — only two localized, low-consequence, comment-only patches; all load-bearing behavior verified green.

**Residual risks:** low. The latent cross-hasher `UnknownHashError` edge is documented and not triggerable under the current unique-email + same-hasher-per-test pattern. The shared-app win is intentionally confined to `test_coach_api.py`; migrating other endpoint files onto the session fixture is a safe future consolidation, out of scope here.
