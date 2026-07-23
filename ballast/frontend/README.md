# Ballast — Frontend

Presentation-only Vite + React SPA for Ballast, the AI investing coach. No
business logic lives here; screens are placeholders wired to the design spine.

## Stack

- Vite 8 + React 19.2
- React Router (`react-router-dom`) — 6 surface routes
- Vitest + React Testing Library (jsdom) for tests
- Stylelint for token discipline enforcement

## Install

```bash
npm install
```

## Run the dev server

```bash
npm run dev
```

Runs on http://localhost:5173 (Vite default).

## Environment

The Dashboard calls `${VITE_API_BASE_URL}/api/health` on mount to prove
SPA ↔ backend connectivity. Configure the backend base URL via a Vite env var:

| Variable            | Default                 | Purpose                     |
| ------------------- | ----------------------- | --------------------------- |
| `VITE_API_BASE_URL` | `http://localhost:8000` | Base URL of the FastAPI API |

Copy `.env.example` to `.env` to override. If the backend is unreachable, the
Dashboard renders a "backend unreachable" state and does not crash.

## Tests

```bash
npm test
```

Runs `vitest run` (non-watch). Covers:

- Smoke test: all 6 routes render without error.
- Dashboard health check (fetch is mocked; success + unreachable states).
- Reduced-motion behavior (see below).

## Auth & session (Story 1.3)

The `/auth` screen supports both **log in** and **sign up** (toggle at the top;
log in is the default). Presentation-only (AD-1): the SPA holds no auth logic
beyond storing, sending, and clearing the token and rendering signed-in state.
The server is the single source of truth for credential checks and token
validity.

- **Login** POSTs the OAuth2 password form (`username`=email, `password`,
  `Content-Type: application/x-www-form-urlencoded`) to
  `/api/auth/jwt/login`. On success the returned JWT is stored and the app
  routes to the Dashboard, where the header shows a "signed in" indicator and a
  **log out** action.
- **Authed requests** use `apiFetch` (`src/lib/session.js`), which attaches
  `Authorization: Bearer <token>` when a token is held. The token, password, and
  Authorization header are never logged.

### Token storage — and the XSS tradeoff

The JWT is kept **in memory** for the session and **mirrored to
`localStorage`** so a page reload stays signed in (`src/lib/session.js`).
`localStorage` is readable by any script running on the page, so this carries an
**XSS tradeoff**: a script injected into the page could read the token. This is
an accepted simplification for the Ballast v1 SPA. The documented hardening path
is **httpOnly-cookie** auth (the token would then be unreadable by JS), deferred
to a later story.

### Logout is client-side (JWT is stateless)

FastAPI-Users' default JWT strategy is **stateless** — there is no server-side
token denylist. **Log out ends the session from the client's perspective**: the
token is discarded (cleared from memory + `localStorage`) and no longer sent,
and a best-effort `POST /api/auth/jwt/logout` is made. An already-issued token
is *not* server-revoked before its 1-hour expiry; true revocation (a denylist)
is a deferred hardening enhancement, out of scope for v1.

## CSS lint (token discipline)

```bash
npm run lint:css
```

## Token-only discipline

All raw values — hex colors, px font sizes, spacing, radii — live **only** in
`src/theme/tokens.css`, declared as CSS custom properties under
`[data-theme="ballast-terminal"]` (set on `<html>` in `index.html`). Every
component references `var(--ballast-*)` and never a literal.

Stylelint enforces this: `color-no-hex` and a
`declaration-property-value-disallowed-list` rule fail the build on raw hex
colors or px font-sizes used outside `tokens.css` (which is exempt via an
override). If a component hardcodes a value, `npm run lint:css` fails.

## Reduced motion

Two signature animations exist: the **Wordmark** flicker (red serif `BALLAST`)
and the **Cursor** blink (green phosphor block).

Reduced motion is handled two ways:

1. **Hook-driven (tested):** `useReducedMotion` reads
   `window.matchMedia('(prefers-reduced-motion: reduce)')`. When it returns
   true, `Wordmark` and `Cursor` add a `--static` class that sets
   `animation: none`, rendering the static variant. Tests override
   `window.matchMedia` in jsdom and assert the static class + a
   `data-reduced-motion` attribute (see `src/test/reduced-motion.test.jsx`).
2. **Global CSS fallback:** a global
   `@media (prefers-reduced-motion: reduce)` rule in `src/theme/global.css`
   also disables both animations, independent of JS.

There are **no scanlines** anywhere in the app.
