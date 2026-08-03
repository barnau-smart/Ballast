# Story 2.6: Complete the fake-broker Schwab link from the browser

Status: done

<!-- Small dev/demo-experience fix on top of Epic 2 onboarding. Real (schwab) flow unchanged. -->

## Story

As **someone running Ballast locally on fake adapters (dev/demo)**,
I want **"Connect Schwab" to actually complete the link in the browser**,
so that **I can walk the whole product end-to-end (link → portfolio → coach → approve → decisions) without a dead-end or a manual API call.**

## Problem

The fake broker adapter's `authorization_url` is a non-navigable stub (`https://fake-broker.ballast.local/...`). The onboarding page always did `window.location.assign(authorization_url)`, so in fake mode "Connect Schwab" dead-ended at `DNS_PROBE_FINISHED_NXDOMAIN`. Real Schwab uses schwab-py's own local callback loop (`:7080`), so fake mode was only ever linkable via the API `/callback` (how the tests do it) — never from the browser. Surfaced during the Story 4.11 live demo run.

## Approach

Tell the client which broker minted the URL and let it complete a credential-free link in-app:
- Backend: `/api/brokerage/authorize` returns an additive `provider` field.
- Frontend: when `provider === 'fake'`, onboarding POSTs the signed `state` straight to `/api/brokerage/callback` (the fake adapter auto-approves any `code`), then refreshes status → connected. When `provider` is anything else (real `schwab`), the external redirect is unchanged.

## Acceptance Criteria

- Given the fake adapter, when the user clicks Connect Schwab, then the link completes in-app (a `/callback` POST with the authorize `state`), status flips to connected, and **no external navigation** occurs.
- Given a non-fake provider, when the user clicks Connect Schwab, then the external `authorization_url` redirect happens exactly as before (real flow untouched).
- Given `/api/brokerage/authorize`, then the response includes `provider` (`"fake"` under the fake adapter); the field is additive and older clients ignore it.

## Files

- `ballast/backend/api/brokerage.py` — `AuthorizeResponse.provider` + populated from `broker.provider` in the authorize handler.
- `ballast/frontend/src/routes/Onboarding.jsx` — `handleConnect` completes in-app when `provider === 'fake'`, else redirects.
- `ballast/backend/tests/test_brokerage.py` — assert authorize returns `provider == "fake"`.
- `ballast/frontend/src/test/onboarding.test.jsx` — fake provider completes the link in-app, no redirect, callback carries the state.

## Verification

- `cd ballast/backend && .venv/bin/pytest tests/test_brokerage.py -q` → 29 passed.
- `cd ballast/frontend && npm test` → 86 passed; `npm run lint:css` clean.
- Live: `/authorize` → `provider: "fake"`; register → authorize → callback → status `live` → `/recommend` (live Anthropic) → `/approve` → `filled`.

## Dev Agent Record

- Additive backend field; real-mode redirect path unchanged. Presentation-only frontend chooses the handoff by provider. Verified end-to-end on the running fake-broker + live-LLM stack.
