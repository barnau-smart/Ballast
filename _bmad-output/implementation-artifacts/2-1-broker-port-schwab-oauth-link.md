---
baseline_commit: 2a1a25e35ca7dd0d208a672a828c00ad875c548e
---

# Story 2.1: Broker Port + Schwab OAuth link

Status: done

## Story

As a user,
I want to securely connect my Schwab account,
so that Ballast can see my portfolio and (later) place trades I approve.

## Acceptance Criteria

1. **Broker behind a port with a swappable adapter.** A `BrokerPort` interface exists with a `SchwabAdapter` implementation (schwab-py) and a `FakeBrokerAdapter` for local/dev/test. The Coach/API layers depend only on the interface — the concrete adapter is selected by config and swappable without changing callers (AD-8). [Source: epics.md#Story-2.1, ARCHITECTURE-SPINE.md#AD-8]
2. **OAuth link flow works end-to-end (against the fake).** A user can complete the OAuth handshake: request an authorization URL, then exchange the returned code for brokerage tokens. With the FakeBrokerAdapter this runs fully locally; the SchwabAdapter path is code-complete and gated on real credentials. [Source: epics.md#Story-2.1 (FR2)]
3. **Tokens encrypted at the app layer, key outside the DB, per-user isolated.** Brokerage OAuth tokens are stored encrypted at the application layer (AES/Fernet) with the encryption key held OUTSIDE the database (env/secret-manager). The stored ciphertext is never the plaintext token. Tokens are per-user and reachable only through the fail-closed scoped-repository (AD-10) — user A can never read user B's tokens. [Source: epics.md#Story-2.1 (NFR1, AD-10), ARCHITECTURE-SPINE.md#AD-10]

**Cross-cutting:** tokens/secrets NEVER logged; plain-language copy for any user-facing link status. [Source: ARCHITECTURE-SPINE.md#Consistency-Conventions "Logging", EXPERIENCE.md#Voice-and-Tone]

## Tasks / Subtasks

- [x] **Task 1: BrokerPort interface + token type** (AC: 1)
  - [x] Add `ballast/backend/brokers/port.py` defining `BrokerPort` (Protocol or ABC) with the auth/link surface needed now: `authorization_url(state: str) -> str` and `exchange_code(code: str) -> BrokerTokens`. Add a `BrokerTokens` dataclass `{access_token, refresh_token, expires_at}`.
  - [x] Do NOT define order/execution methods here yet — the full Broker Port Contract (OrderOutcome, get_order_status, AD-13) arrives with execution in Epic 4. Keep this story's port focused on linking. Leave a docstring noting the contract will extend.
- [x] **Task 2: App-layer token encryption (key outside DB)** (AC: 3)
  - [x] Add `ballast/backend/brokers/crypto.py` (or `db/crypto.py`) with Fernet-based `encrypt_token(str) -> str` / `decrypt_token(str) -> str` using a key read from env `TOKEN_ENCRYPTION_KEY` (documented in `.env.example`; a dev default is acceptable but clearly labeled insecure; NEVER a real key committed). The key lives in the environment, never in the database.
  - [x] Fail loudly if the key is missing/invalid at first use.
- [x] **Task 3: Encrypted, per-user token storage** (AC: 3)
  - [x] Add a `BrokerageToken` model (`db/models.py`) using `OwnedEntityMixin` (owner_id → user), storing `provider` (e.g. "schwab"), the ENCRYPTED access + refresh tokens, and `expires_at`. Add to create-all.
  - [x] Persist/read tokens ONLY through a `ScopedRepository` (Story 1.4) so access is per-user and fail-closed. Encrypt on write, decrypt on read. The DB column value must never equal the plaintext token (test this).
- [x] **Task 4: Adapters** (AC: 1, 2)
  - [x] `ballast/backend/brokers/fake_adapter.py` — `FakeBrokerAdapter` implementing `BrokerPort`: `authorization_url` returns a deterministic fake URL embedding `state`; `exchange_code` returns deterministic fake `BrokerTokens`. This makes the whole flow testable with zero credentials.
  - [x] `ballast/backend/brokers/schwab_adapter/` — `SchwabAdapter` implementing `BrokerPort` via **schwab-py 1.5.1**, reading `SCHWAB_CLIENT_ID`, `SCHWAB_CLIENT_SECRET`, `SCHWAB_CALLBACK_URL` from config. It must be code-complete but gated: if credentials are absent, constructing/using it raises a clear "Schwab not configured" error (do NOT crash at import). Real network calls are only exercised when configured — not in tests.
  - [x] Add a factory/dependency that returns the adapter chosen by config `BROKER_ADAPTER` (default `"fake"` for dev/test; `"schwab"` when creds are set).
- [x] **Task 5: Link API endpoints** (AC: 2, 3)
  - [x] Authenticated endpoints (reuse 1.3 `current_user` + 1.4 `get_scope`):
    - `GET /api/brokerage/authorize` → returns the authorization URL (from the active adapter) + a CSRF `state`.
    - `POST /api/brokerage/callback` (or GET, matching OAuth redirect) → takes the `code` (+ `state`), calls `exchange_code`, encrypts + stores the tokens for the current user via the scoped repo.
    - `GET /api/brokerage/status` → plain-language linked/not-linked for the current user (never leak token values).
  - [x] Validate `state` to prevent CSRF on the callback. Never log the code or tokens.
- [x] **Task 6: Frontend — Onboarding link entry (minimal)** (AC: 2)
  - [x] In `/onboarding` (existing placeholder), add a "Connect Schwab" action that calls `/api/brokerage/authorize` and would redirect the user to the returned URL, and shows link status from `/api/brokerage/status`. With the fake adapter this demonstrates the flow locally. Presentation-only, tokens-only styling. Keep minimal — the full onboarding reveal is Stories 2.2–2.4.
- [x] **Task 7: Tests** (AC: 1, 2, 3)
  - [x] Encryption round-trip: `decrypt_token(encrypt_token(x)) == x`; ciphertext != plaintext; missing key raises.
  - [x] Storage (real DB): after a fake link, the persisted token column is ciphertext (≠ the fake plaintext token); reading back via the scoped repo decrypts correctly.
  - [x] Isolation: user A cannot read user B's brokerage token (scoped repo) — reuse the 1.4 guarantee, assert it for this table.
  - [x] Flow (fake adapter): `authorize` returns a URL containing the state; `callback` with a code stores tokens; `status` then reports linked. Unauthenticated access → 401.
  - [x] SchwabAdapter without creds raises a clear configuration error (not an import crash).
  - [x] Frontend: onboarding "Connect Schwab" calls authorize + shows status (mock fetch).
  - [x] No regressions (backend + frontend suites, lint, build).
- [x] **Task 8: Verify** — full backend + frontend suites + lint + build; live curl the fake link flow (authorize → callback → status) and confirm the DB stores ciphertext.

## Dev Notes

### Fake-first strategy (agreed with the user)
No Schwab credentials yet. Build the ENTIRE broker-port contract, encrypted token storage, and link flow against a `FakeBrokerAdapter` so it's fully built and tested with zero credentials. The `SchwabAdapter` is code-complete but credential-gated; when the user's Schwab developer app is approved, setting `BROKER_ADAPTER=schwab` + the three `SCHWAB_*` env vars switches it on with no caller changes (that's the point of AD-8). Do not block this story on real creds.

### Builds on Epic 1 (done) — reuse
- **Scoped repository + Scope + OwnedEntityMixin** (Story 1.4) — brokerage tokens are a per-user owned entity; store/read ONLY through `ScopedRepository`. This is how AC3's per-user isolation is satisfied for free. [Source: db/repository.py, db/scope.py, db/models.py]
- **`current_user`** (1.3) + **`get_scope`** (1.4) dependencies for the endpoints. [Source: api/users.py, api/deps.py]
- **Async SQLAlchemy session + create-all** (1.2). **Error envelope + config pattern** (pydantic-settings). **Structured logging** — never log secrets/tokens.
- **Frontend**: `/onboarding` placeholder + `apiFetch` bearer helper + tokens/session lib (1.3).

### Architecture constraints [Source: ARCHITECTURE-SPINE.md]
- **AD-8:** external deps behind ports; callers depend on `BrokerPort`, never on schwab-py directly. `SchwabAdapter` is the only place schwab-py is imported.
- **AD-10 / NFR1:** tokens are high-sensitivity secrets — encrypted at the app layer (Fernet), key OUTSIDE the DB; per-user isolation via the scoped repo; never logged.
- **AD-6 (one owner):** the Broker Port is the sole path to brokerage state. Nothing else talks to Schwab.
- **Conventions:** UUID PKs; ISO-8601 UTC (`expires_at`); consistent error envelope. [Source: #Consistency-Conventions]
- **schwab-py 1.5.1**, per Stack.

### Scope guardrails
- **In scope:** BrokerPort (link surface), token encryption, encrypted per-user token storage, Fake + Schwab adapters, link/callback/status endpoints, minimal onboarding link entry, tests.
- **Out of scope:** session-status/degraded-mode + graceful re-auth (Story 2.2), portfolio import/cache (2.3), portfolio dashboard (2.4), index-core mapping (2.5), order execution + OrderOutcome/get_order_status (Epic 4). Do NOT implement order methods or portfolio fetch here.
- **Do not** log tokens/codes; do not put the encryption key in the DB; do not import schwab-py outside the SchwabAdapter.

### Security emphasis
Brokerage tokens are the crown jewels. The DB must hold only ciphertext; the key must come from env; access must go through the fail-closed scoped repo. Tests must assert the negative (stored value ≠ plaintext; A can't read B's token; missing key raises).

### References
- [Source: _bmad-output/planning-artifacts/epics.md#Story-2.1]
- [Source: ARCHITECTURE-SPINE.md#AD-8, #AD-10, #AD-6, #Stack, #Consistency-Conventions]
- [Source: EXPERIENCE.md#Information-Architecture (Onboarding), #Voice-and-Tone]
- [Source: implementation-artifacts/1-4-fail-closed-per-user-data-isolation.md (ScopedRepository), 1-3-log-in-session.md (current_user, apiFetch)]

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (orchestrator) + implementation subagent + fresh-context crown-jewels security-review subagent.

### Debug Log References

- Live fake flow: `status`→`{linked:false}` → `authorize`→url+state → `callback`{code,state}→`{linked:true}` → `status`→`{linked:true,provider:"fake"}`. DB proof: stored `access_token` column is Fernet ciphertext (`gAAAAA…`), `= 'fake-access-token-…'` returned false.
- Backend pytest: 43 passed (real Postgres). Frontend vitest: 29 passed. lint + build clean.

### Completion Notes List

- **AC1 (AD-8):** `BrokerPort` (link-only: `authorization_url`, `exchange_code`) with `FakeBrokerAdapter` (default) + credential-gated `SchwabAdapter` (schwab-py 1.5.1, lazy-imported — the only place the SDK is touched). Factory selects by `BROKER_ADAPTER`.
- **AC2:** full OAuth link flow works against the fake with zero creds; Schwab path code-complete and gated (raises `SchwabNotConfiguredError` without creds, not an import crash). Flip `BROKER_ADAPTER=schwab` + 3 `SCHWAB_*` env vars to enable.
- **AC3 (crown jewels):** tokens Fernet-encrypted at the app layer BEFORE any DB write; key from env `TOKEN_ENCRYPTION_KEY` (outside DB; insecure dev default clearly labeled); stored column is ciphertext (test reads the raw column and asserts ≠ plaintext); per-user via `ScopedRepository` (user A cannot read B's token — tested); missing/invalid/tampered key raises. No endpoint returns token values.
- **CSRF:** stateless HMAC-signed `state` (signed with `USER_MANAGER_SECRET`), bound to the authenticated user (blocks cross-user replay), constant-time compare, 15-min TTL. Reviewer probed 8 attack vectors — all rejected.
- **Security review fixes applied:** H1 — `exchange_code` now takes `state` and the SchwabAdapter recreates the auth context + reconstructs the received URL WITH the state, so authlib's state check passes when real creds are enabled (previously it would have failed, breaking the AD-8 "flip env vars and it works" promise). M1 — added a unit test for `_to_broker_tokens` normalization (absolute `expires_at` + relative `expires_in`), the testable part of the gated path.
- **Scope discipline:** no order/execution/portfolio/session-degraded logic (later stories). Never logs code/tokens/state.

### File List

**Created — backend**
- `ballast/backend/brokers/port.py` (BrokerPort + BrokerTokens)
- `ballast/backend/brokers/crypto.py` (Fernet encrypt/decrypt, TokenEncryptionError)
- `ballast/backend/brokers/fake_adapter.py` (FakeBrokerAdapter)
- `ballast/backend/brokers/schwab_adapter/__init__.py`, `schwab_adapter/adapter.py` (SchwabAdapter, gated)
- `ballast/backend/brokers/factory.py` (get_broker)
- `ballast/backend/api/brokerage.py` (authorize/callback/status + HMAC state)
- `ballast/backend/tests/test_brokerage.py`

**Created — frontend**
- `ballast/frontend/src/test/onboarding.test.jsx`

**Modified — backend**
- `ballast/backend/db/models.py` (BrokerageToken owned entity)
- `ballast/backend/api/config.py` (TOKEN_ENCRYPTION_KEY, BROKER_ADAPTER, SCHWAB_*)
- `ballast/backend/api/app.py` (mount brokerage router)
- `ballast/backend/pyproject.toml` (cryptography, schwab-py), `.env.example`

**Modified — frontend**
- `ballast/frontend/src/routes/Onboarding.jsx` (Connect Schwab action + status)

## Senior Developer Review (AI)

- **Date:** 2026-07-23 · **Outcome:** APPROVE-WITH-NITS → H1+M1 resolved → done
- **Crown-jewel guarantees verified under active attack:** encryption-at-rest (raw column ≠ plaintext), key-outside-DB, per-user isolation, CSRF/replay resistance (8 vectors rejected), no secret logging, AD-8 lazy-import. No BLOCKER/HIGH on the fake-first deliverable.
- **H1 (fixed):** credential-gated `SchwabAdapter.exchange_code` would have failed authlib's state check (state not threaded) — fixed by passing `state` through and reconstructing the received URL/context with it. **Live Schwab handshake remains unverified until real creds land** (cannot be exercised without them) — honest caveat.
- **M1 (fixed):** added a normalization unit test for the gated token-shape transform. L2 (token column sizing) noted for creds-enablement.

## Change Log

- 2026-07-23 — Story 2.1 implemented (fake-first): BrokerPort + Fake/Schwab adapters (AD-8), app-layer Fernet token encryption (key outside DB), encrypted per-user token storage via the scoped repo, HMAC CSRF state, and the OAuth link/callback/status endpoints + onboarding entry. Crown-jewels security review passed; fixed the credential-gated state-threading bug (H1) and added the M1 normalization test. All suites green (backend 43/43, frontend 29/29, lint + build clean). Status → done.
