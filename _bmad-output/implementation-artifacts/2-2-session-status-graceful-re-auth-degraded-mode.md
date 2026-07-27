---
baseline_commit: 2a1a25e35ca7dd0d208a672a828c00ad875c548e
---

# Story 2.2: Session status, graceful re-auth & degraded mode

Status: done

## Story

As a user,
I want to be told plainly when I need to reconnect Schwab,
so that a weekly re-login never feels like something broke.

## Acceptance Criteria

1. **Plain, calm expiry prompt (never red).** Given the ~7-day refresh-token expiry, when the session expires, then a calm, neutral (never red) banner explains why and how to re-authenticate. [Source: epics.md#Story-2.2 (FR3, NFR4, AD-11), DESIGN.md#Components (reauth-banner)]
2. **Degraded mode keeps read/coach working.** When the brokerage session is expired, read/coach features keep working in degraded mode; only actions that need a live brokerage session (execution) are gated. [Source: epics.md#Story-2.2 (NFR4, AD-11)]
3. **Resume in place after re-auth.** After re-authenticating, the user resumes where they were — no data loss, no dead end. [Source: epics.md#Story-2.2 (FR3, AD-11)]

**Cross-cutting:** re-auth banner is neutral/muted, never red (red is brand-only); plain/warm copy; never log tokens. [Source: DESIGN.md#Hard-color-rules, EXPERIENCE.md#State-Patterns, #Voice-and-Tone]

## Tasks / Subtasks

- [x] **Task 1: Session-status model in the broker layer** (AC: 1, 2)
  - [x] Add a brokerage **session status** concept computed from the stored `BrokerageToken.expires_at` (Story 2.1) for the current user: `linked` (no token), `live` (token present + not expired), `expired` (token present + past expiry). Compute in a small service/helper in `brokers/` or `api/`, read via the scoped repo. No new network call needed for status.
  - [x] Extend `GET /api/brokerage/status` to return the session state (`{state: "unlinked"|"live"|"expired", expires_at?, provider?}`) — plain, no token values.
- [x] **Task 2: Degraded-mode gating primitive** (AC: 2)
  - [x] Add a dependency/guard `require_live_broker_session` that ALLOWS read/coach endpoints regardless of session state but BLOCKS execution-class actions when the session is not `live`, returning a plain-language "reconnect needed" response (not an error dump). No execution endpoints exist yet (Epic 4) — provide the guard + a docstring'd example + a test so Epic 4 uses it. Read/coach dependencies must NOT be gated.
  - [x] Make explicit in code/comments: read + coach continue in degraded mode; only live-session actions are gated (AD-11).
- [x] **Task 3: Re-auth flow (resume in place)** (AC: 1, 3)
  - [x] Re-authentication reuses the Story 2.1 link flow (`authorize` → `callback`) — re-linking overwrites the expired token for the user (already the 2.1 behavior). Confirm re-auth restores `live` state.
  - [x] Support "resume in place": the authorize request accepts an optional opaque `return_to` (e.g., the surface the user was on) that round-trips so the frontend can send the user back. Keep it server-validated/simple (do not open a redirect vulnerability — only allow known in-app paths).
- [x] **Task 4: reauth-banner component (calm, never red)** (AC: 1)
  - [x] Build the `reauth-banner` component (DESIGN.md): neutral/muted styling, **never** red/brand; plain copy explaining the ~weekly Schwab re-login is normal and how to reconnect; a "Reconnect" action that starts the re-auth flow. Tokens-only styling. Respects reduced-motion.
  - [x] Show the banner app-wide (Layout) when session state is `expired`; hide when `live`/`unlinked`. It must read from `/api/brokerage/status`.
- [x] **Task 5: Degraded-mode UX** (AC: 2, 3)
  - [x] In degraded mode, read surfaces (Dashboard/Coach placeholders) stay usable; any execution affordance shows the calm reconnect prompt instead of failing silently. Keep minimal (no real execution UI yet).
  - [x] After reconnect, the user lands back where they were (`return_to`) — verify the resume-in-place path.
- [x] **Task 6: Tests** (AC: 1, 2, 3)
  - [x] Backend (real DB): status returns `unlinked` with no token, `live` with a fresh token, `expired` with a past-`expires_at` token (insert via scoped repo). Per-user (A's status never reflects B).
  - [x] `require_live_broker_session` allows when `live`, blocks with a plain message when `expired`/`unlinked`; a read-style dependency is never blocked.
  - [x] Re-auth (fake adapter): after expiry, running authorize→callback restores `live`; `return_to` only accepts allowed in-app paths (reject an external URL).
  - [x] Frontend: reauth-banner renders on `expired`, is styled neutral (no brand-red token), hidden on `live`; "Reconnect" triggers authorize; reduced-motion respected.
  - [x] No regressions.
- [x] **Task 7: Verify** — full backend + frontend suites + lint + build; live-simulate expiry (insert an expired token) and confirm status=`expired`, the guard blocks a sample execution call, and re-auth restores `live`.

## Dev Notes

### Fake-first continues
Still no real Schwab session. Simulate the ~7-day expiry by writing a `BrokerageToken` with a past `expires_at` (the fake adapter already produces tokens; tests can insert an expired one via the scoped repo). Everything here is testable without creds. This is about the *lifecycle + UX*, not the network.

### Builds on 2.1 + Epic 1 (done) — reuse
- `BrokerageToken` (owner_id, encrypted tokens, `expires_at`) + `ScopedRepository` for per-user reads. [Source: db/models.py, db/repository.py]
- `GET /api/brokerage/authorize|callback|status` + HMAC CSRF state (2.1) — extend `status`; reuse the link flow for re-auth. [Source: api/brokerage.py]
- `current_user` (1.3) + `get_scope` (1.4). Error envelope + config. Never log tokens.
- Frontend: `Layout` (signed-in chrome), `apiFetch`, tokens/`useReducedMotion`, `screen.css`. reauth-banner joins the component set.

### Architecture constraints [Source: ARCHITECTURE-SPINE.md#AD-11]
- **AD-11:** the ~7-day refresh-token expiry is tolerated with NO data loss; on expiry the user is prompted to re-authenticate; **read/coach continue in degraded mode**, but execution requires a live session — an order is NEVER placed on an expired session. (Order placement itself is Epic 4; here we build the gate + the UX so Epic 4 plugs in.)
- Banner is neutral/muted, never red (DESIGN.md hard color rule). [Source: DESIGN.md#Hard-color-rules, #Components]
- Approval→placement expiry integrity (re-auth + re-confirm before placing) is **Story 4.8** — not here. Here: session status, the degraded-mode gate, the banner, and resume-in-place.

### Scope guardrails
- **In scope:** session-status computation + endpoint, degraded-mode gate primitive (+ test, no real execution yet), re-auth via the existing link flow with resume-in-place, the reauth-banner + degraded UX.
- **Out of scope:** portfolio import/dashboard (2.3/2.4), index-core (2.5), order execution + approval→placement integrity (Epic 4). Do NOT add order placement. Do NOT add a token-refresh network call (v1 prompts re-auth rather than silent refresh, per AD-11) unless trivial via the fake — prefer prompt-to-reauth.
- **Security:** never log tokens; `return_to` must reject external/open-redirect targets (allowlist in-app paths only).

### Testing standards
Real DB for status/gate tests; assert the calm-banner has no brand-red token (color rule is an acceptance criterion, not a nicety). Assert the gate lets reads through and blocks live-session actions with a plain message. Assert `return_to` rejects external URLs.

### References
- [Source: _bmad-output/planning-artifacts/epics.md#Story-2.2]
- [Source: ARCHITECTURE-SPINE.md#AD-11, #AD-8]
- [Source: DESIGN.md#Components (reauth-banner), #Hard-color-rules; EXPERIENCE.md#State-Patterns (session expired / degraded), #Voice-and-Tone]
- [Source: implementation-artifacts/2-1-broker-port-schwab-oauth-link.md (link flow, BrokerageToken, status endpoint)]

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (Claude Code, autonomous story loop)

### Debug Log References

- Backend real-DB tests require the docker Postgres (`docker compose up -d db`); started it for the run. Full backend suite: 73 passed.
- Frontend: 35 passed (7 files); CSS lint clean; production build succeeds.
- Fresh-context adversarial review found no high-confidence defects; hardened one low-severity observation (deterministic single-row selection in `get_brokerage_session`).

### Completion Notes List

- Session state (`unlinked`/`live`/`expired`) is a pure computation from `BrokerageToken.expires_at` (`brokers/session.py`) — no network call, read through the fail-closed `ScopedRepository` (AD-10) so a user's status can only reflect their own token.
- `GET /api/brokerage/status` extended to return `state` (keeps `linked` for 2.1 back-compat) + non-secret `provider`/`expires_at`; never any token value (asserted in tests).
- Degraded-mode gate `require_live_broker_session` (`api/deps.py`): allows read/coach, blocks execution-class actions with a calm HTTP 409 + plain-language `RECONNECT_MESSAGE` routed through the app error envelope. No execution endpoint exists yet (Epic 4) — guard ships with a docstring example + tests using a test-only harness app.
- Re-auth reuses the 2.1 link flow; callback overwrites the prior token → restores `live`. Resume-in-place via optional `return_to` on `/authorize`, validated against an in-app allowlist (`sanitize_return_to`) that rejects scheme/scheme-relative/backslash targets — no open redirect. Frontend never redirects to `return_to` itself; only to the broker `authorization_url`.
- `reauth-banner` (`ReauthBanner.jsx/.css`) shown app-wide in `Layout` only when `state === 'expired'`; neutral/muted tokens only (surface-2/line/muted/phosphor), never brand-red; `role="status"` (not alert); reduced-motion handled three ways; passes current path as `return_to`.
- Hardening: `get_brokerage_session` now picks the latest-expiring row defensively so status stays deterministic even if the one-row-per-user invariant is ever violated.

### File List

- ballast/backend/brokers/session.py (new)
- ballast/backend/api/brokerage.py (status endpoint extended, return_to allowlist)
- ballast/backend/api/deps.py (require_live_broker_session gate)
- ballast/backend/tests/test_session_status.py (new)
- ballast/frontend/src/components/ReauthBanner.jsx (new)
- ballast/frontend/src/components/ReauthBanner.css (new)
- ballast/frontend/src/components/Layout.jsx (banner wired in)
- ballast/frontend/src/test/reauth-banner.test.jsx (new)

## Change Log

- 2026-07-26: Implemented session-status model, degraded-mode gate, re-auth resume-in-place, and reauth-banner. All backend (73) + frontend (35) tests, CSS lint, and build pass. Adversarial review clean. Status → done.
