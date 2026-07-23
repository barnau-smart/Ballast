---
baseline_commit: NO_COMMITS
---

# Story 1.3: Log in & session

Status: done

## Story

As a returning user,
I want to log in and stay signed in,
so that I can securely reach my own data.

## Acceptance Criteria

1. **Login issues a session; authed routes reachable.** Given valid credentials, when I log in, then I receive a JWT session and can reach authenticated routes. [Source: epics.md#Story-1.3 (FR1)]
2. **Wrong credentials rejected.** Invalid email/password is rejected with a plain-language message (no jargon, no leak of which field was wrong beyond generic "invalid credentials"). [Source: epics.md#Story-1.3 (FR1), EXPERIENCE.md#Voice-and-Tone]
3. **Logout ends the session.** Logging out ends the session so the token no longer grants access to authed routes (from the client's perspective the session is over). [Source: epics.md#Story-1.3 (FR1)]

**Cross-cutting:** plain/warm copy (NFR6/NFR8); tokens/secrets never logged; SPA presentation-only (AD-1). [Source: ARCHITECTURE-SPINE.md#AD-1, #Consistency-Conventions]

## Tasks / Subtasks

- [x] **Task 1: Expose JWT login/logout routes** (AC: 1, 3)
  - [x] Mount FastAPI-Users' auth router (`fastapi_users.get_auth_router(auth_backend)`) at `/api/auth/jwt` → gives `POST /api/auth/jwt/login` and `POST /api/auth/jwt/logout`. The `auth_backend` + JWT strategy already exist from Story 1.2 — reuse them, do not recreate.
  - [x] Confirm login accepts credentials (FastAPI-Users login uses OAuth2 form fields `username`=email + `password`) and returns a bearer token.
- [x] **Task 2: Protected route to prove authed access** (AC: 1)
  - [x] Add `GET /api/users/me` protected by `fastapi_users.current_user(active=True)` returning the current user (via `UserRead` — id + email + flags, never password/hash).
  - [x] Verify an unauthenticated request to it returns 401; a request with a valid bearer token returns the user.
- [x] **Task 3: Plain-language auth errors** (AC: 2)
  - [x] Ensure a bad login (`LOGIN_BAD_CREDENTIALS`) maps through the existing error envelope to a warm, generic message (e.g. "That email or password doesn't match. Please try again."). Do NOT reveal whether the email exists vs. the password was wrong (avoid user enumeration on login). Extend the `_AUTH_ERROR_MESSAGES` map in `api/app.py`.
- [x] **Task 4: Frontend login + session handling** (AC: 1, 2, 3)
  - [x] Turn `/auth` into a screen supporting BOTH sign-up (1.2) and log-in (toggle or two actions). Log-in POSTs the OAuth2 form to `/api/auth/jwt/login`, stores the returned JWT (in memory + a reasonable persistence choice — see Dev Notes on token storage), and shows a signed-in state.
  - [x] On success, route to the Dashboard (the calm home). The Dashboard (or Layout) shows a signed-in indicator and a **Log out** action.
  - [x] Log out clears the stored token and returns to a signed-out state; calls `POST /api/auth/jwt/logout` (best-effort). After logout, authed requests fail.
  - [x] Attach the bearer token to authed requests (e.g. a small fetch helper). Presentation-only: no auth logic beyond storing/sending the token and rendering state.
  - [x] All new UI reads tokens only (stylelint must pass).
- [x] **Task 5: Tests** (AC: 1, 2, 3)
  - [x] Backend (real DB): register → login with correct creds returns a token; that token grants access to `GET /api/users/me` (200 + correct user); no token → 401.
  - [x] Backend: login with wrong password and login with unknown email BOTH return the same generic 4xx plain message (no enumeration).
  - [x] Backend: after logout, the login flow works again; (JWT is stateless — assert logout returns success and the client-side contract; do not over-claim server-side revocation, see Dev Notes).
  - [x] Frontend: login form submits, stores token, shows signed-in state; wrong creds show the plain error; logout returns to signed-out state (mock fetch).
  - [x] No regressions in 1.1/1.2 suites.
- [x] **Task 6: Verify end-to-end**
  - [x] Live: register a user, `POST /api/auth/jwt/login` (form) → token; `GET /api/users/me` with `Authorization: Bearer <token>` → user; without → 401; wrong creds → generic plain error.
  - [x] Run full backend + frontend suites + `lint:css` + `build`.

## Dev Notes

### Builds on Stories 1.1 + 1.2 (both done) — reuse
- **`auth_backend` + JWT strategy already exist** in `ballast/backend/api/users.py` (built in 1.2 to construct `FastAPIUsers`). This story simply *exposes* the login/logout router that uses them. Do not duplicate.
- **`fastapi_users` instance**, `UserManager`, `UserRead`/`UserCreate` schemas, async DB session, `User` model (UUID PK) — all from 1.2. Reuse.
- **Error envelope + `_AUTH_ERROR_MESSAGES`** in `api/app.py` — extend the map; the handler already surfaces mapped codes.
- **Frontend `/auth`** has a sign-up form (1.2). Extend it for login; keep tokens-only styling.
- **Config:** `USER_MANAGER_SECRET` (used by the JWT strategy) already in `api/config.py` / `.env.example`.

### JWT session semantics — be honest in tests (don't over-claim)
FastAPI-Users' default JWT strategy is **stateless**: `logout` clears the client token but does not server-side-revoke an already-issued JWT before its expiry (1-hour lifetime set in 1.2). For AC3, the correct contract is: **logout ends the session from the client's perspective** (token discarded, no longer sent). Do NOT write a test asserting a still-held token is server-rejected after logout — that would require a token denylist, which is out of scope for v1. If you want true revocation, note it as a deferred enhancement; do not implement it here. Test the honest contract: logout succeeds, client state is signed-out, and a request with no token is 401.

### Token storage (frontend) — pick and document
Store the JWT in memory for the session; for persistence across reloads use `localStorage` for v1 simplicity (document the XSS tradeoff — acceptable for a single-page v1; httpOnly-cookie auth is a hardening option later). Keep it simple and presentation-only. Note this choice in the frontend README.

### Security / correctness
- **No user enumeration on login:** wrong password and unknown email must return the *same* generic message. FastAPI-Users already returns a single `LOGIN_BAD_CREDENTIALS` for both — just map it to one warm message.
- Never log the token, password, or Authorization header.
- `GET /api/users/me` must return `UserRead` (no hash).
- AD-1: SPA holds no auth logic beyond store/send token + render.

### Scope guardrails
- **In scope:** login/logout routes, one protected route (`/users/me`) to prove authed access, frontend login/session/logout, tests.
- **Out of scope:** fail-closed scoped-repository enforcement (Story 1.4 — that's the real per-user data isolation), password reset/email verification, Schwab, coach/precedent/LLM. Do not add the full FastAPI-Users users-management router (only `/users/me`), and no Schwab/coach code.

### References
- [Source: _bmad-output/planning-artifacts/epics.md#Story-1.3]
- [Source: ARCHITECTURE-SPINE.md#AD-1, #Consistency-Conventions ("Auth & secrets"), #Stack]
- [Source: EXPERIENCE.md#Voice-and-Tone, #Interaction-Primitives]
- [Source: implementation-artifacts/1-2-register-with-email-password.md — auth backend, UserManager, error envelope, frontend Auth form]

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (orchestrator) + implementation subagent + fresh-context security-review subagent.

### Debug Log References

- Live: register → `POST /api/auth/jwt/login` (OAuth2 form) → bearer JWT; `GET /api/users/me` with token → user (no hash); without token → 401; wrong password AND unknown email → byte-identical 400 `"That email or password doesn't match. Please try again."`; logout → 204.
- Backend pytest: 16 passed (real Postgres). Frontend vitest: 25 passed. `lint:css` + `build` clean. No 1.1/1.2 regressions.

### Completion Notes List

- **AC1:** Mounted `get_auth_router(auth_backend)` at `/api/auth/jwt` (reusing the 1.2 auth backend/JWT strategy — nothing recreated). Added protected `GET /api/users/me` via `current_user(active=True)` returning `UserRead` (no password/hash). 401 without token, 200 with — verified.
- **AC2:** `LOGIN_BAD_CREDENTIALS` mapped to one generic warm message; wrong password and unknown email return the identical body+status → no user enumeration (test asserts `==` on the two responses).
- **AC3 (honest):** logout is client-side (token discarded + best-effort `POST /logout`). JWT is stateless — no server-side revocation implemented or claimed; tests assert only the honest contract.
- **Frontend:** `/auth` now supports login (default) + sign-up (toggle); token stored in memory + localStorage (XSS tradeoff documented in README); Layout shows signed-in indicator + Log out; `apiFetch` bearer helper. Presentation-only (AD-1); tokens-only styling.
- **Security hardening (review LOW-1 fix):** `apiFetch` now attaches the bearer token ONLY to same-origin backend requests — never cross-origin — so the token can't leak to a third-party URL.
- **Secrets:** JWT secret env-sourced (dev default only committed); no token/password/Authorization ever logged (verified against live logs). Scope discipline: only login/logout + `/users/me` (no full user-mgmt router, no reset/verify, no Schwab/coach/LLM, no scoped-repo enforcement — that's 1.4).

### File List

**Created — backend**
- `ballast/backend/tests/test_login.py`

**Created — frontend**
- `ballast/frontend/src/lib/session.js` (token store + `apiFetch` bearer helper, same-origin guard)
- `ballast/frontend/src/hooks/useSession.js`
- `ballast/frontend/src/test/session.test.jsx`

**Modified — backend**
- `ballast/backend/api/app.py` (JWT auth router, `/api/users/me`, `LOGIN_BAD_CREDENTIALS` mapping)
- `ballast/backend/api/users.py` (docstring/comments — code unchanged)

**Modified — frontend**
- `ballast/frontend/src/routes/Auth.jsx` (login + signup toggle)
- `ballast/frontend/src/components/Layout.jsx` (signed-in indicator + Log out)
- `ballast/frontend/src/routes/screen.css`, `src/components/Layout.css` (token-based styles)
- `ballast/frontend/src/test/auth.test.jsx` (login/wrong-creds tests + toggle)
- `ballast/frontend/README.md` (token storage / stateless-logout docs)

## Senior Developer Review (AI)

- **Date:** 2026-07-23 · **Outcome:** APPROVE-WITH-NITS → nit fixed → done
- **Verified live:** login token issued; `/users/me` enforces auth (401/200, no hash leak); no user enumeration (identical body for wrong-pw vs unknown-email); logout honest (client-side only, no false revocation claim); no secrets/tokens logged. Tests real-DB and non-vacuous. No BLOCKER/HIGH.
- **Fixed:** LOW-1 — `apiFetch` now guards the bearer token to same-origin only.
- **Deferred:** LOW-2 CORS `allow_headers=["*"]` narrowing (fine now; revisit during hardening).

## Change Log

- 2026-07-23 — Story 1.3 implemented: JWT login/logout (reusing 1.2 auth backend), protected `/api/users/me`, no-enumeration bad-credentials handling, frontend login/session/logout with same-origin-guarded bearer helper. Security review APPROVE-WITH-NITS; fixed the cross-origin token-attach nit. All suites green (backend 16/16, frontend 25/25, lint + build clean). Status → done.
