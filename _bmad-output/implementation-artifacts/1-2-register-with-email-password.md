---
baseline_commit: NO_COMMITS
---

# Story 1.2: Register with email & password

Status: done

## Story

As a new user,
I want to create an account with email and password,
so that I have a private, secure place for my investing coach.

## Acceptance Criteria

1. **Account creation with hashed password.** Given the sign-up screen, when I submit a valid email + password, then an isolated user record is created and my password is stored **hashed (never plaintext)** via FastAPI-Users. [Source: epics.md#Story-1.2 (FR1, NFR1)]
2. **Duplicate email rejected in plain language.** A duplicate email is rejected with a plain-language message (no stack trace, no jargon, no leaking whether internal errors occurred). [Source: epics.md#Story-1.2 (NFR6)]
3. **Isolation-ready user identity.** The user record uses a UUID primary key and is the identity that every later per-user scoped query keys on (this story creates the `user` table + model that AD-10 fail-closed scoping will build on in Story 1.4). [Source: ARCHITECTURE-SPINE.md#AD-10, #Consistency-Conventions]

**Cross-cutting:** user-facing error/validation copy is plain, warm, and jargon-free (NFR6/NFR8). Passwords/secrets are never logged. [Source: EXPERIENCE.md#Voice-and-Tone, ARCHITECTURE-SPINE.md#Consistency-Conventions "Logging"]

## Tasks / Subtasks

- [x] **Task 1: Add auth/persistence dependencies** (AC: 1)
  - [x] Add to `ballast/backend` deps: `fastapi-users[sqlalchemy]==15.*`, `sqlalchemy[asyncio]` (2.x), `asyncpg` (async driver for FastAPI-Users). Keep existing `psycopg[binary]` (sync) for the health check unless you cleanly migrate it. Install into the existing `.venv`; if a pin lacks a Python 3.14 wheel, pick the nearest working version and note it. FastAPI-Users must stay 15.x.
  - [x] Update `pyproject.toml` and `.env.example` (add any new settings, e.g. a separate async `DATABASE_URL` form `postgresql+asyncpg://...` if needed).
- [x] **Task 2: SQLAlchemy async DB layer in `db/`** (AC: 1, 3)
  - [x] Add an async SQLAlchemy engine + session factory in `ballast/backend/db/` (reuse `settings.DATABASE_URL`; derive the `+asyncpg` URL). Do NOT break the existing sync `check_db()` used by `/api/health`.
  - [x] Define a `User` model via FastAPI-Users' SQLAlchemy base with a **UUID primary key** (`SQLAlchemyBaseUserTableUUID`). This is the canonical user identity.
  - [x] Provide table creation for local/dev (either `Base.metadata.create_all` on startup, or a simple Alembic migration — pick one, document it; a lightweight create-all-on-startup is acceptable for v1 per the spine's single-instance assumption).
- [x] **Task 3: FastAPI-Users wiring in `api/`** (AC: 1, 2)
  - [x] Configure the FastAPI-Users `UserManager`, the SQLAlchemy user database adapter, and password hashing (FastAPI-Users default Argon2/bcrypt — do NOT hand-roll hashing).
  - [x] Mount the **register** route (`POST /api/auth/register`) returning the created user (id + email; NEVER the password/hash).
  - [x] Do NOT wire login/JWT here — that is Story 1.3. Set up only what registration needs (the auth backend/JWT strategy can be stubbed/prepared but login routes belong to 1.3). If FastAPI-Users requires an auth backend object to construct the router, create it but only expose the register router in this story.
  - [x] Ensure duplicate-email registration returns a clean plain-language error (FastAPI-Users returns `REGISTER_USER_ALREADY_EXISTS`; map it to a warm, plain message and an appropriate 4xx via the existing error envelope). Password validation failures likewise return plain messages.
- [x] **Task 4: Auth screen wiring (frontend, minimal)** (AC: 1, 2)
  - [x] Turn the `/auth` placeholder into a minimal sign-up form (email + password) that POSTs to `${VITE_API_BASE_URL}/api/auth/register` and shows success or the plain-language error. Presentation-only (AD-1): no validation logic beyond basic required fields; the backend is the source of truth. Reads tokens only (stylelint must still pass).
  - [x] Keep it minimal — full auth UX (login, session, styled states) matures in 1.3+. A working register form that surfaces backend success/error is the bar here.
- [x] **Task 5: Tests** (AC: 1, 2, 3)
  - [x] Backend (pytest, real DB): registering a new email returns success and persists a user row whose stored password is a **hash, not the plaintext** (assert the stored value != submitted password and verifies via the hasher). UUID PK present.
  - [x] Backend: registering a duplicate email returns a 4xx with a plain-language message and does NOT create a second row.
  - [x] Backend: the response body never contains the password or its hash.
  - [x] Frontend: the sign-up form renders, submits, and displays success + the duplicate/error state (mock the fetch).
  - [x] Ensure existing 1.1 tests (health, routes, reduced-motion, lint) still pass — no regressions.
- [x] **Task 6: Verify end-to-end**
  - [x] With Postgres up + backend running: `curl -X POST /api/auth/register` with a new email → 201/200 + user; repeat same email → clean 4xx plain message. Confirm no plaintext password anywhere in DB or logs.
  - [x] Run full backend + frontend suites + `lint:css` + `npm run build`.

## Dev Notes

### Builds directly on Story 1.1 (done) — reuse, don't reinvent
Story 1.1 established these patterns — extend them, do not duplicate:
- **Backend app factory** `create_app()` in `ballast/backend/api/app.py`; entrypoint `api.main:app` on port 8000. Add the FastAPI-Users routers here.
- **Config** via `pydantic-settings` `BaseSettings` (`api/config.py`) reading `DATABASE_URL` + CORS origins from env. Add new settings here (e.g. `SECRET`/JWT settings can wait for 1.3; registration needs the user-manager secret for token generation — a `USER_MANAGER_SECRET` env with a dev default is fine, documented in `.env.example`, never a real secret committed).
- **Error envelope**: unhandled errors already return `{"error": {"type", "message"}}` — reuse it for the plain-language mapping.
- **Structured logging** exists (`api/logging_config.py`) — never log passwords/tokens.
- **DB module** `db/connection.py` has sync `check_db()` for health — keep it working; add the async engine/session alongside it.
- **Tests**: pytest with FastAPI TestClient (`tests/`), real Postgres (docker `db` service). Frontend vitest + RTL in `src/test/`. Follow these conventions.
- **Frontend**: 6 routes exist; `/auth` is a placeholder (`src/routes/Auth.jsx`). Tokens in `src/theme/tokens.css`; stylelint bans hardcoded values — the new form must use `var(--ballast-*)` only.

### Stack (pinned) [Source: ARCHITECTURE-SPINE.md#Stack]
- FastAPI-Users **15.x** (email+pw + JWT). Python 3.12+. PostgreSQL 18. FastAPI 0.136.
- Currently installed (from 1.1): fastapi 0.136.3, uvicorn 0.51.0, psycopg[binary] 3.3.4, pydantic-settings 2.14.2, pytest 9.1.1, httpx 0.28.1; frontend vite 8.1.5, react 19.2.8, react-router-dom 7.18.1.

### Architecture constraints [Source: ARCHITECTURE-SPINE.md]
- **AD-10 (fail-closed per-user isolation):** this story creates the user identity (UUID) that Story 1.4's scoped-repository layer keys on. Model the User cleanly now. Brokerage-token encryption is a later story — do not add it here.
- **AD-1:** the SPA is presentation-only. The sign-up form does fetch + render only; all validation/hashing/uniqueness is server-side.
- **Conventions:** UUID primary keys; timestamps ISO-8601 UTC; consistent error envelope; never log secrets. [Source: #Consistency-Conventions]
- **Owner:** auth lives in `backend/api/` (auth, FastAPI-Users, sessions) per the source tree. [Source: #Structural-Seed]

### Scope guardrails
- **In scope:** user table + model, password hashing, register endpoint, duplicate rejection, minimal sign-up form, tests.
- **Out of scope (later stories):** login/JWT/logout (1.3), fail-closed scoped-repository enforcement (1.4), Schwab (Epic 2), any coach/precedent/LLM logic. Do not touch those packages.
- **Do not hand-roll** password hashing or auth — use FastAPI-Users' built-ins.

### Voice for user-facing copy (NFR6/NFR8) [Source: EXPERIENCE.md#Voice-and-Tone]
Plain, warm, no jargon. E.g. duplicate email → "An account with that email already exists. Try logging in instead." Not "IntegrityError" or "REGISTER_USER_ALREADY_EXISTS".

### Testing standards
Real DB integration for the persistence/hashing assertions (do not mock the DB for AC1). Assert the negative: stored password ≠ plaintext, and response never leaks the hash. Keep tests green for 1.1 (no regressions).

### Project Structure Notes
- New/changed backend files land in `ballast/backend/db/` (async engine + User model) and `ballast/backend/api/` (FastAPI-Users wiring, register router, config additions).
- Frontend change is limited to `src/routes/Auth.jsx` (+ its styles if any, token-based).

### References
- [Source: _bmad-output/planning-artifacts/epics.md#Story-1.2]
- [Source: ARCHITECTURE-SPINE.md#AD-10, #AD-1, #Stack, #Consistency-Conventions, #Structural-Seed]
- [Source: EXPERIENCE.md#Voice-and-Tone, #Information-Architecture]
- [Source: implementation-artifacts/1-1-project-scaffold-theme-foundation.md — established patterns]

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (orchestrator) + a general-purpose subagent for implementation; adversarial security review by a second fresh-context subagent.

### Debug Log References

- Live: `POST /api/auth/register` new email → 201 `{id,email,is_active,...}` (no password/hash); duplicate → 400 `{"error":{"type":"auth_error","message":"An account with that email already exists. Try logging in instead."}}`; short password → 400 `"Password must be at least 8 characters."`; 8-char password → 201.
- DB proof: stored `hashed_password` is Argon2id (`$argon2id$v=19$...`), never plaintext; verified by `PasswordHelper().verify_and_update`.
- Backend pytest: 9 passed (real Postgres). Frontend vitest: 18 passed. `lint:css` clean; `npm run build` clean. No 1.1 regressions.

### Completion Notes List

- **AC1:** FastAPI-Users 15.0.5 + SQLAlchemy 2.0.51 async + asyncpg 0.31.0. `User` on `SQLAlchemyBaseUserTableUUID` (UUID PK, unique email); passwords hashed with Argon2id via FastAPI-Users' pwdlib (not hand-rolled). Register response schema (`UserRead`) exposes id+email+flags only — never password/hash (test-asserted).
- **AC2:** Duplicate email → clean 4xx via the canonical error envelope with warm plain copy; internal codes (`REGISTER_USER_ALREADY_EXISTS`) never leaked.
- **AC3:** UUID primary key + unique/indexed email — the identity Story 1.4's fail-closed scoping (AD-10) will key on. `user` table created via create-all-on-startup (lifespan); Alembic deferred.
- **Password validation (added in review):** `UserManager.validate_password` enforces an 8-char minimum; the handler surfaces the plain reason to the user. Empty/short passwords are rejected and create no row (test-covered).
- **AD-1 respected:** the `/auth` sign-up form is presentation-only (fetch+render, tokens only); all validation/hashing/uniqueness is server-side.
- **Scope discipline:** only `POST /api/auth/register` exposed — no login/JWT/logout (Story 1.3), no Schwab/coach/LLM. The JWT strategy/auth backend object exists solely to construct `FastAPIUsers`.
- **Secrets:** `USER_MANAGER_SECRET` from env (insecure dev default, documented in `.env.example`, never a real committed secret); `on_after_register` logs `user_id` only; no password/secret logged.
- **Notes:** async engine uses `NullPool` (asyncpg connections are event-loop-bound); sync `check_db()`/`/api/health` untouched and still green.

### File List

**Created — backend**
- `ballast/backend/db/models.py` (Base + User model, UUID PK)
- `ballast/backend/db/session.py` (async engine/session, create_db_and_tables, get_user_db)
- `ballast/backend/api/schemas.py` (UserRead, UserCreate)
- `ballast/backend/api/users.py` (UserManager incl. validate_password, auth backend, FastAPIUsers)
- `ballast/backend/tests/test_register.py` (register/hashing/duplicate/password-validation tests)

**Created — frontend**
- `ballast/frontend/src/test/auth.test.jsx`

**Modified — backend**
- `ballast/backend/pyproject.toml` (fastapi-users[sqlalchemy]==15.*, sqlalchemy[asyncio]>=2.0, asyncpg, pytest-asyncio)
- `ballast/backend/api/config.py` (USER_MANAGER_SECRET, async_database_url)
- `ballast/backend/api/app.py` (register router, lifespan create-all, auth error-envelope mapping incl. password reason)
- `ballast/backend/.env.example`

**Modified — frontend**
- `ballast/frontend/src/routes/Auth.jsx` (minimal sign-up form)
- `ballast/frontend/src/routes/screen.css` (token-based form styles)

## Senior Developer Review (AI)

- **Date:** 2026-07-23 · **Outcome:** CHANGES-REQUIRED → resolved → done
- **BLOCKER (fixed):** passwords were entirely unvalidated (empty/1-char accepted) — the `REGISTER_INVALID_PASSWORD` warm-copy mapping was dead code. Added `validate_password` (min 8) + tests; handler now surfaces the plain reason. Re-verified live + 9/9 backend tests.
- **Confirmed solid:** Argon2id hashing (not plaintext), no hash/password leak, UUID identity + unique email, duplicate handling, secret hygiene (env-sourced, not logged), scope discipline (register-only), AD-1 presentation-only frontend. Tests are real-DB and non-vacuous.
- **Deferred (not this story):** no JS/ESLint rule to catch hardcoded values in JSX inline styles (1.1 scaffold gap — convention is no-inline-styles); revisit reuse of a single secret across token purposes when login lands (1.3).

## Change Log

- 2026-07-23 — Story 1.2 implemented: FastAPI-Users 15 registration with Argon2id hashing, UUID user identity, plain-language duplicate + password-validation errors, minimal token-based sign-up form. Security review found + fixed an unvalidated-password BLOCKER. All suites green (backend 9/9, frontend 18/18, lint + build clean). Status → done.
