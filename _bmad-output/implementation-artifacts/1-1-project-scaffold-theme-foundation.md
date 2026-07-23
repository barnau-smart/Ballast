---
baseline_commit: NO_COMMITS
---

# Story 1.1: Project scaffold & theme foundation

Status: done

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a developer,
I want the app scaffolded with its token-based theme and shell,
so that every later feature is built on a consistent, themeable foundation.

## Acceptance Criteria

1. **Runtime scaffold + connectivity.** A Vite 8 + React 19.2 SPA (presentation-only), a FastAPI 0.136 backend (Python 3.12+), and PostgreSQL 18 all run locally, and the SPA successfully reaches the backend (a real request/response round-trip, not a mock). [Source: epics.md#Story-1.1; ARCHITECTURE-SPINE.md#Stack]
2. **Token-based theme, no hardcoded values.** All colors, spacing, and type come from CSS-variable tokens implementing the `ballast-terminal` theme (green phosphor `#5dff8a`, rare brand-red `#ff2b3a`, neon-pink accent `#ff5cae`, sky-blue market-down `#6ad0ff`, violet uncertainty `#c77dff`). No component hardcodes a color/spacing/type value — every value reads a token. Theme is swappable (all tokens live in one place). [Source: epics.md#UX-DR1; DESIGN.md#Colors]
3. **6-surface route skeleton + reduced-motion.** The 6-surface route skeleton renders — Auth, Onboarding, Dashboard, Coach, Decisions, Settings — and `prefers-reduced-motion` disables the terminal cursor blink and the wordmark flicker (no scanlines anywhere). [Source: epics.md#UX-DR6, EXPERIENCE.md#Information-Architecture, DESIGN.md#Components]

**Cross-cutting acceptance criteria** (this is a scaffold story — the coach-facing invariants FR12/FR13/FR14 do not apply to rendered content yet, but these UX invariants are established structurally here):
- Market up/down must use green `▲` / sky-blue `▼` with signs — **never red** for loss (establish the token discipline now: `market-down` is sky-blue, `brand-red` is brand-only). [Source: DESIGN.md#Hard-color-rules]
- `prefers-reduced-motion` respected for all ambient motion. [Source: EXPERIENCE.md#Accessibility-Floor]

## Tasks / Subtasks

- [x] **Task 1: Repository layout & tooling** (AC: 1)
  - [x] Create the source tree exactly as the architecture spine defines it (see Dev Notes → Source Tree). Root container is `ballast/` with `frontend/` and `backend/`.
  - [x] Create `backend/` module skeleton directories now (empty `__init__.py` packages): `api/`, `coach/`, `precedent/`, `llm/`, `brokers/`, `marketdata/`, `digest/`, `db/`. Do NOT implement their logic — later stories own that. This story only establishes the package boundaries.
  - [x] Add a `README.md` at repo root documenting how to run backend, frontend, and Postgres locally.
- [x] **Task 2: PostgreSQL 18 local** (AC: 1)
  - [x] Provide a `docker-compose.yml` (or documented equivalent) that runs PostgreSQL 18 locally with a `ballast` database.
  - [x] Backend reads DB connection settings from environment variables (never hardcoded); provide a `.env.example`. Secrets/keys come from env, never committed. [Source: ARCHITECTURE-SPINE.md#Consistency-Conventions "Auth & secrets"]
  - [x] Backend establishes a real connection to Postgres at startup and fails loudly if it can't (this proves the DB is wired, satisfying the "Postgres runs locally" half of AC1). No schema/migrations required in this story beyond what the connection check needs.
- [x] **Task 3: FastAPI backend + health endpoint** (AC: 1)
  - [x] Stand up a FastAPI 0.136 app (Python 3.12+) with a `GET /api/health` endpoint returning JSON `{status, db: "ok"|"down"}` where `db` reflects a live Postgres check.
  - [x] Use a consistent JSON error envelope (establish the shape now; later API stories reuse it). [Source: ARCHITECTURE-SPINE.md#Consistency-Conventions "API contract"]
  - [x] Configure CORS so the Vite dev server origin can reach the API in local dev.
  - [x] Add structured logging config; never log secrets/tokens. [Source: ARCHITECTURE-SPINE.md#Consistency-Conventions "Logging"]
- [x] **Task 4: Design tokens as CSS variables** (AC: 2)
  - [x] Create a single tokens stylesheet (e.g., `frontend/src/theme/tokens.css`) that declares CSS custom properties for EVERY value in DESIGN.md frontmatter: all colors, the type scale (xs 11 / sm 13 / base 15 / lg 19 / xl 26 / hero 42), the 3 font families (display serif, terminal mono, body sans), spacing scale `[4,8,12,16,20,24,32,44]` on a 4px unit, and radii (base 6, pill 999). Scope them under a theme selector (e.g., `[data-theme="ballast-terminal"]` on `:root`/`html`) so the theme is swappable per UX-DR1.
  - [x] Establish a lint/enforcement discipline so components can't hardcode values (see Dev Notes → Token Enforcement). At minimum, a stylelint rule or documented convention + a token-only usage pattern.
- [x] **Task 5: 6-surface route skeleton** (AC: 3)
  - [x] Install a router (React Router) and register exactly 6 routes → placeholder screens: `/auth` (Auth), `/onboarding` (Onboarding), `/` or `/dashboard` (Dashboard, the calm home), `/coach` (Coach), `/decisions` (Decisions), `/settings` (Settings). [Source: EXPERIENCE.md#Information-Architecture]
  - [x] Each screen is a minimal placeholder reading only tokens; no business logic (AD-1: SPA is presentation-only).
  - [x] Wire one placeholder screen (Dashboard) to call `GET /api/health` and render the result — this is the concrete proof of AC1 SPA↔backend connectivity.
- [x] **Task 6: Signature components + reduced-motion** (AC: 2, 3)
  - [x] Build the two motion-bearing signature elements this story needs: **wordmark** (red serif `BALLAST`, gentle flicker) and **cursor** (blinking green block). Both read tokens only.
  - [x] Implement `@media (prefers-reduced-motion: reduce)` to disable the wordmark flicker AND the cursor blink (render static). Verify no scanlines exist anywhere. [Source: DESIGN.md#Components, EXPERIENCE.md#Accessibility-Floor]
  - [x] (Scope note) Do NOT build the full component library (coach-card, data-block, cosign-block, etc.) — those belong to later stories. Only wordmark + cursor are required here to satisfy the reduced-motion AC. A `market-indicator` (green ▲ / sky-blue ▼) is optional but recommended to lock in the color-independence discipline early.
- [x] **Task 7: Verify all three ACs end-to-end**
  - [x] `docker compose up` (Postgres) + backend + `npm run dev` all start clean; Dashboard shows a live health result. (AC1)
  - [x] Grep the frontend for hardcoded hex colors / px font sizes outside `tokens.css` → none. (AC2)
  - [x] All 6 routes render; toggle OS reduced-motion (or DevTools emulation) → cursor and wordmark stop animating. (AC3)

## Dev Notes

### Critical context (read before coding)
- **This is the foundation everything else is built on.** Get the token system and module boundaries right — every later story (25 stories across 5 epics) inherits these decisions. Getting the theme swappable and hardcode-free now is the whole point of the story; a "works but hardcoded" scaffold fails AC2.
- **Greenfield.** The repo currently contains only tooling (`.claude/`, `_bmad/`, `_bmad-output/`, etc.). No `ballast/` app code exists yet. There is no previous story and no relevant git history — you are creating the app from scratch.
- **AD-1 is load-bearing from line one:** the React SPA is **presentation-only** and holds NO business logic. Placeholder screens render data the backend returns; they never originate logic. This split IS the NFR2 trust-enforcement boundary — respect it even in the scaffold. [Source: ARCHITECTURE-SPINE.md#AD-1]

### Stack (pinned — do not substitute) [Source: ARCHITECTURE-SPINE.md#Stack]
| Name | Version |
| --- | --- |
| Python | 3.12+ |
| FastAPI | 0.136 |
| React | 19.2 |
| Vite | 8.x |
| PostgreSQL | 18 |

- Use **Vite 8** (not create-react-app; no pre-built starter — hand-rolled per the epics' "Additional Requirements"). React 19.2 (function components + hooks; note React 19 removed some legacy APIs — use the modern `react-dom/client` `createRoot` entry).
- **Do not add** `schwab-py`, the Anthropic SDK, `FastAPI-Users`, or a market-data client in this story — those are pulled in by their owning stories (2.1, 4.1, 1.2, 3.1 respectively). Keep the dependency surface minimal.

### Source Tree (create exactly this — do not invent a different layout) [Source: ARCHITECTURE-SPINE.md#Structural-Seed]
```text
ballast/
  frontend/            # Vite 8 + React 19 SPA (presentation only)
    src/
      theme/tokens.css # ← all DESIGN.md tokens as CSS variables (single source)
      routes/          # the 6 surface screens
      components/      # wordmark, cursor (this story); more later
  backend/
    api/               # FastAPI app, routes, health endpoint (auth/sessions later)
    coach/             # (empty package skeleton this story)
    precedent/         # (empty package skeleton)
    llm/               # (empty package skeleton)
    brokers/           # (empty package skeleton)
    marketdata/        # (empty package skeleton)
    digest/            # (empty package skeleton)
    db/                # DB connection, models/migrations (this story: connection only)
```
- **Module naming convention:** domain-named packages; interfaces suffix `Port`, adapters suffix `Adapter` (not needed this story, but establish the convention in READMEs). [Source: ARCHITECTURE-SPINE.md#Consistency-Conventions]

### Design tokens — the exact values to encode [Source: DESIGN.md frontmatter + #Colors + #Typography + #Layout]
Colors:
```
bg #05060a · bg-2 #080b10 · surface #0a0d12 · surface-2 #0e121a
line #1c2733 · line-red #4a1119
text #e9edf2 (coach prose, soft white) · muted #7b8a90
phosphor #5dff8a (interface/data/labels/up) · phosphor-dim #2f9f5c
brand-red #ff2b3a (BRAND ONLY, rare) · brand-red-deep #c01522
accent-pink #ff5cae (links/focus/chips/cursor)
market-up #5dff8a (with ▲) · market-down #6ad0ff (sky blue, with ▼ — NEVER red/pink)
uncertainty #c77dff (violet)
```
Type: display `Benguiat, 'Bookman Old Style', Georgia, serif` (red wordmark/hero only) · terminal `'VT323','Space Mono',ui-monospace,monospace` (data/labels/chrome/cursor) · body `Inter, system-ui, sans-serif` (coach prose, ~1.6 line-height). Scale `{xs:11, sm:13, base:15, lg:19, xl:26, hero:42}`. Radii `{base:6, pill:999}`. Spacing unit 4, scale `[4,8,12,16,20,24,32,44]`.

**Hard color rules (must be structurally easy to follow, hard to violate):**
- Red is **never** loss/alert/error — brand only, used sparingly. Losses use `market-down` (sky-blue); gains use `phosphor` (green); both always paired with a sign/icon (▲/▼), never color alone. [Source: DESIGN.md#Hard-color-rules]
- Coach prose is soft-white `body`; green mono is for data/labels/chrome only.
- Depth = glow + darkness + edge vignette. **No scanlines** (accessibility). [Source: DESIGN.md#Elevation]

### Token Enforcement (how to make hardcoding impossible — AC2)
- Put ALL raw values in `tokens.css` as `--ballast-*` custom properties under `[data-theme="ballast-terminal"]`. Set `data-theme` on `<html>`.
- Components reference `var(--ballast-color-phosphor)` etc. — never literals.
- Add **stylelint** with `declaration-property-value-disallowed-list` (or `color-no-hex`) to fail the build on raw hex/px-font-size outside `tokens.css`. Document this in the frontend README. This is the mechanism that keeps the theme swappable and satisfies AC2 durably.

### 6 Surfaces (placeholders only this story) [Source: EXPERIENCE.md#Information-Architecture]
1. Auth — sign up / log in (email+pw) → Epic 1.2/1.3
2. Onboarding — link Schwab, first portfolio reveal → Epic 2
3. Dashboard — plain-English portfolio + ask-the-coach entry; **the calm home** (wire health check here)
4. Coach — conversational decide surface → Epic 4
5. Decisions — co-signed history + replay → Epic 4
6. Settings — Schwab status/re-auth, digest opt-in, theme, account → Epics 2/5

Deferred features (guru, curriculum, quiz, progression) have **no** surface in v1 — do not add routes for them.

### Accessibility floor to establish now [Source: EXPERIENCE.md#Accessibility-Floor]
- `prefers-reduced-motion: reduce` → disable wordmark flicker + cursor blink (REQUIRED by AC3). Nothing essential depends on motion.
- Color independence: any up/down indicator pairs color with ▲/▼ + label.
- WCAG AA contrast on the dark theme; visible focus (red glow) on interactives. `muted` text must hit AA at body sizes.

### Testing standards
- No formal test framework is mandated by the spine yet (test-architecture stories come later). For this scaffold, provide **at minimum**:
  - Backend: a test asserting `GET /api/health` returns 200 and `db: "ok"` when Postgres is up (use pytest — the Python default).
  - Frontend: a smoke test that all 6 routes render without error, and a test/verification that reduced-motion disables animation (can be a documented manual check if automated motion testing is impractical).
- Keep tests minimal and real — do not fake the DB check in the health test (that would defeat AC1's "SPA reaches the backend" intent).

### Project Structure Notes
- The `ballast/` container sits at the repo root (`/Users/blainearnau/repos/ai_practice_project/ballast/`). All BMad tooling (`_bmad/`, `_bmad-output/`, `.claude/`) stays where it is and is unrelated to the app.
- No conflicts detected with existing structure — this is the first app code in the repo.

### References
- [Source: _bmad-output/planning-artifacts/epics.md#Epic-1 / #Story-1.1 / #Additional-Requirements]
- [Source: _bmad-output/planning-artifacts/architecture/architecture-ai_practice_project-2026-07-22/ARCHITECTURE-SPINE.md#AD-1, #Stack, #Structural-Seed, #Consistency-Conventions]
- [Source: _bmad-output/planning-artifacts/ux-designs/ux-ai_practice_project-2026-07-22/DESIGN.md#Colors, #Typography, #Components, #Elevation, #Hard-color-rules]
- [Source: _bmad-output/planning-artifacts/ux-designs/ux-ai_practice_project-2026-07-22/EXPERIENCE.md#Information-Architecture, #Accessibility-Floor, #Foundation]

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (orchestrator) + two general-purpose subagents for the backend and frontend halves.

### Debug Log References

- End-to-end integration verified: `docker compose up -d db` (postgres:18 healthy) → backend on `:8000` → `curl /api/health` returned `{"status":"ok","db":"ok"}` (real live DB probe, not mocked).
- CORS verified for the Vite origin: `Access-Control-Allow-Origin: http://localhost:5173` present on both OPTIONS preflight and GET.
- Backend pytest: `2 passed` (`test_health_db_ok` hits real Postgres; `test_health_db_down` verifies graceful degraded fallback → HTTP 200 `{"status":"degraded","db":"down"}`).
- Frontend vitest: `14 passed` across 3 files (6 routes render; Dashboard health success + unreachable states; reduced-motion hook/component behavior).
- `npm run lint:css`: clean — proves no hardcoded hex/px-font values outside `tokens.css` (AC2 enforcement).
- `npm run build`: Vite production build succeeds (41 modules).

### Completion Notes List

- **AC1 (runtime + connectivity):** Vite 8.1.5 + React 19.2.8 SPA (presentation-only), FastAPI 0.136.3 (Python 3.14) backend, PostgreSQL 18 (docker-compose). SPA↔backend proven by a live health round-trip wired into the Dashboard + CORS for the Vite origin.
- **AC2 (token theme, no hardcoded values):** All DESIGN.md tokens (20 colors, 3 font stacks, 6-step type scale, 4→44 spacing, radii) live in `src/theme/tokens.css` scoped under `[data-theme="ballast-terminal"]` on `<html>`; components reference `var(--ballast-*)` only; stylelint fails the build on raw hex/px-font-sizes outside tokens.css. Theme is swappable (single source).
- **AC3 (6 surfaces + reduced-motion):** Exactly 6 routes — `/auth`, `/onboarding`, `/`(+`/dashboard`), `/coach`, `/decisions`, `/settings` — no deferred-feature routes. `prefers-reduced-motion: reduce` disables both the wordmark flicker and cursor blink (global CSS media query + a `useReducedMotion` matchMedia hook for testability). No scanlines anywhere.
- **AD-1 respected:** SPA holds no business logic; placeholders render only backend data / tokens.
- **Module boundaries established:** empty `coach/precedent/llm/brokers/marketdata/digest` packages with owner docstrings; logic deferred to their owning epics. No auth/Schwab/Anthropic/market-data deps pulled in.
- **Deviations:** (1) docker-compose named volume mounts `/var/lib/postgresql` (not `/var/lib/postgresql/data`) — required by the postgres:18 versioned-data-dir convention; container reaches healthy. (2) `react-router-dom` is v7.18.1 (story said "React Router" unpinned; v7 is current). (3) Root `.gitignore` extended to exclude `__pycache__`/`*.pyc`/`.pytest_cache`/`.venv`/`node_modules`/`dist` and real `.env` files while keeping `.env.example` tracked (scaffold-tooling hygiene, Task 1).
- Actual installed pins: fastapi 0.136.3, uvicorn 0.51.0, psycopg[binary] 3.3.4, pydantic-settings 2.14.2, pytest 9.1.1, httpx 0.28.1; vite 8.1.5, react 19.2.8, react-router-dom 7.18.1, vitest 4.1.10, stylelint 17.14.1.

### File List

**Created — repo root**
- `docker-compose.yml`

**Created — backend (`ballast/backend/`)**
- `pyproject.toml`, `README.md`, `.env.example`
- `api/__init__.py`, `api/config.py`, `api/logging_config.py`, `api/app.py`, `api/main.py`
- `db/__init__.py`, `db/connection.py`
- `tests/__init__.py`, `tests/test_health.py`
- `coach/__init__.py`, `precedent/__init__.py`, `llm/__init__.py`, `brokers/__init__.py`, `marketdata/__init__.py`, `digest/__init__.py`

**Created — app root (`ballast/`)**
- `README.md` (root run guide added in review — M1 fix)

**Created — frontend (`ballast/frontend/`)**
- `package.json`, `vite.config.js`, `index.html`, `.env.example`, `.gitignore`, `.stylelintrc.json`, `README.md`
- `src/theme/tokens.css`, `src/theme/global.css`
- `src/main.jsx`, `src/App.jsx`
- `src/hooks/useReducedMotion.js`
- `src/components/Wordmark.jsx`, `src/components/Wordmark.css`, `src/components/Cursor.jsx`, `src/components/Cursor.css`, `src/components/MarketIndicator.jsx`, `src/components/MarketIndicator.css`, `src/components/Layout.jsx`, `src/components/Layout.css`
- `src/routes/Dashboard.jsx`, `src/routes/Auth.jsx`, `src/routes/Onboarding.jsx`, `src/routes/Coach.jsx`, `src/routes/Decisions.jsx`, `src/routes/Settings.jsx`, `src/routes/screen.css`
- `src/test/setup.js`, `src/test/routes.test.jsx`, `src/test/dashboard.test.jsx`, `src/test/reduced-motion.test.jsx`

**Modified — repo root**
- `.gitignore`

## Change Log

- 2026-07-23 — Story 1.1 implemented: hand-rolled Vite 8 + React 19 SPA, FastAPI 0.136 backend, PostgreSQL 18 via docker-compose; token-based `ballast-terminal` theme with stylelint enforcement; 6-surface route skeleton; wordmark + cursor with `prefers-reduced-motion` support. All 3 ACs verified (backend 2/2, frontend 14/14, lint + build clean). Status → review.
- 2026-07-23 — Adversarial code review (fresh context): verdict APPROVE-WITH-NITS. Reviewer independently proved the DB health probe is real (failed when pointed at a dead port) and that stylelint enforcement has teeth (failed on injected hex/px). No BLOCKER/HIGH. Fixed M1 (added `ballast/README.md` root run guide — task was checked but deliverable was missing) and M2 (`requires-python` `>=3.14` → `>=3.12` to match the 3.12+ stack spec). LOW nits (CORS header breadth, inline-style lint gap) deferred to owning stories. Status → done.

## Senior Developer Review (AI)

- **Date:** 2026-07-23 · **Outcome:** APPROVE-WITH-NITS → resolved
- **Verified live:** backend pytest 2/2 against real Postgres; DB probe proven genuine (fails on dead port); frontend vitest 14/14; stylelint enforcement proven (fails on injected hardcoded values); `vite build` clean; git hygiene clean (no venv/node_modules/.env tracked). AD-1 respected (SPA presentation-only); no scope creep.
- **Action items resolved:** M1 root README added; M2 Python pin corrected to `>=3.12`.
- **Deferred (owned by later stories):** L1 CORS `allow_headers` breadth → auth story; L2 no lint guard against inline-style hardcoding → convention documented, revisit if needed.
