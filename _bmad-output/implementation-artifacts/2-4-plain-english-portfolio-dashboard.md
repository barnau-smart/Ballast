---
baseline_commit: 2a1a25e35ca7dd0d208a672a828c00ad875c548e
---

# Story 2.4: Plain-English portfolio dashboard

Status: done

## Story

As a beginner,
I want my portfolio explained in plain language,
so that I actually understand what I hold.

## Acceptance Criteria

1. **Holdings, balances & cash shown in plain English.** Given imported holdings (Story 2.3 cache), when I open the dashboard, then my holdings, total balance, and cash are shown with plain-English descriptions and NO unexplained jargon. [Source: epics.md#Story-2.4 (FR4, FR5, NFR6), EXPERIENCE.md#Screens (Dashboard), #Voice-and-Tone]
2. **Empty / pre-link state invites linking (no dead end).** When there is no imported portfolio yet (never linked, or linked-but-not-yet-imported), the dashboard shows a calm, plain invitation to connect Schwab and what will happen — never a blank/error screen. [Source: EXPERIENCE.md#State-Patterns (Empty pre-link), #Screens]
3. **Degraded mode still shows the cached portfolio.** When the brokerage session is expired, the dashboard still renders the cached holdings (read continues in degraded mode); the reauth-banner (Story 2.2) already handles the reconnect prompt. No order/execution affordance appears here. [Source: ARCHITECTURE-SPINE.md#AD-11, EXPERIENCE.md#State-Patterns (Schwab session expired)]

**Cross-cutting:** plain/warm voice, short sentences, any unavoidable term explained in-line; green = interface / data-labels, body prose in soft white; NEVER red or pink for loss/error (loss uses sky-blue ▼ with a sign, gain green ▲); tokens-only styling; honor `prefers-reduced-motion`; money formatted as currency (never raw floats), sourced from the backend `Decimal` values. [Source: DESIGN.md#Hard-color-rules, #Do's-and-Don'ts, EXPERIENCE.md#Voice-and-Tone]

## Tasks / Subtasks

- [x] **Task 1: Fetch the cached portfolio** (AC: 1, 3)
  - [x] `Dashboard.jsx` fetches `GET /api/portfolio` via `apiFetch`; state machine loading → ready with empty vs populated handled in `PortfolioPanel`.
  - [x] Fetch error / non-ok falls back to `{holdings:[], cash:0, as_of:null}` → calm empty invite (fail-quiet, never blank/throw).
- [x] **Task 2: Plain-English descriptions (no unexplained jargon)** (AC: 1)
  - [x] `lib/holdings.js` `describeHolding` — warm, jargon-free descriptions for VTI/VXUS/BND (+ VOO/BNDX/VT) with a generic fallback so an unknown symbol never renders as a bare ticker.
  - [x] Each row: plain description (body voice) + symbol (mono label) + quantity + market value; "bonds" explained in-line.
- [x] **Task 3: Balances & cash** (AC: 1)
  - [x] Total (holdings + cash) summed in integer cents (`totalValue`) — no float drift — and cash, both formatted via `Intl.NumberFormat` currency. Warm labels ("Everything you hold" / "Cash ready to invest").
  - [x] Per-holding gain/loss vs `cost_basis` uses `MarketIndicator` (green ▲ / sky-blue ▼, with sign+icon); break-even shows no indicator (honest — never implies a gain).
- [x] **Task 4: Empty / pre-link state** (AC: 2)
  - [x] Calm invite: what Ballast does (read-only pull), scope reassurance, "Connect Schwab" link to onboarding. No error styling.
- [x] **Task 5: Styling & a11y** (AC: 1, 2, 3)
  - [x] Tokens-only (`PortfolioPanel.css` + reused `screen.css`); body prose soft white, data labels green mono. No red/pink tokens anywhere in the panel (loss = sky-blue ▼).
  - [x] Calm-home tone kept; the old sample health card / stray MarketIndicator removed in favor of the real portfolio.
- [x] **Task 6: Tests** (AC: 1, 2, 3)
  - [x] Renders VTI/VXUS/BND with descriptions + symbols + formatted values; asserts every symbol has an accompanying description (no bare ticker); total ($5,500.25, exact) + cash render.
  - [x] Empty state renders the invite (not error/blank); panel absent.
  - [x] Loss row renders sky-blue ▼ + "down since you bought"; asserts `--down` present, `--up` absent, and NO `brand-red`/`accent-pink` in the markup.
  - [x] Graceful degradation on fetch failure → invite.
  - [x] No regressions (7 files / 38 tests pass; `routes.test.jsx` fetch stub updated to the portfolio shape).
- [x] **Task 7: Verify** — frontend suite 38 passed + CSS lint clean + build succeeds. States reasoned through: populated, empty, and expired (cached still renders — reauth-banner owns the prompt).

## Dev Notes

### Builds on 2.3 + 2.2 + Epic 1 (done) — reuse
- **`GET /api/portfolio`** (Story 2.3): returns `{ holdings: [{symbol, quantity, market_value, cost_basis}], cash, as_of }` — the AD-14 cache read. `as_of === null` ⇒ never imported (empty state). Money values are `Decimal` serialized by Pydantic (JSON numbers preserving scale). [Source: api/portfolio.py]
- **`apiFetch`** (`lib/session.js`): the authed fetch wrapper — attaches the bearer token same-origin only. Use it (NOT raw `fetch`) for `/api/portfolio`. [Source: frontend/src/lib/session.js]
- **reauth-banner** (2.2) already renders app-wide on `expired` and drives reconnect — the dashboard does NOT duplicate that; it just keeps showing the cached portfolio in degraded mode (AD-11). [Source: components/ReauthBanner.jsx]
- **Patterns:** `screen.css` (`ballast-screen`, `ballast-card`), `MarketIndicator` (green ▲ / sky-blue ▼), `useReducedMotion`, `theme/tokens.css`. Follow the existing `Dashboard.jsx` structure (fetch-on-mount + state machine). [Source: routes/Dashboard.jsx, components/MarketIndicator.jsx, theme/tokens.css]

### Voice & color (acceptance-level, not cosmetic) [Source: DESIGN.md, EXPERIENCE.md]
- Plain, not clever: short sentences, no unexplained jargon; explain any unavoidable term in-line (NFR6). This is UJ-3's climax — "the first plain-English portfolio" — so the copy carries real weight.
- Green = interface / data-labels; body prose soft white. **Never red or pink for loss/error** — a down move is sky-blue ▼ with a sign. Pair color with an icon/sign (color-blind safe).
- Pull, not push: calm home, no aggressive motion, no attention-grabbing.

### Scope guardrails
- **In scope:** the dashboard's plain-English portfolio view (holdings + balances + cash), the symbol→description mapping, the empty/pre-link invite, degraded-mode read, styling + a11y, tests.
- **Out of scope:** index-core mapping (Story 2.5 — do NOT build the "core vs the rest" grouping here; a holding's description may hint at it, but the mapping/meter is 2.5), any coach/recommendation UI (Epic 4), order/execution affordances (Epic 4), a new backend endpoint (2.3's `GET /api/portfolio` is sufficient — do NOT add backend routes). Do NOT re-implement the reauth prompt (2.2 owns it).
- **Presentation-only (AD-1):** the frontend holds no business logic — it renders what the backend returns. The symbol→description map is presentation copy, not business logic.

### Testing standards
Vitest + Testing Library (mirror `dashboard.test.jsx` / `reauth-banner.test.jsx`): mock `apiFetch`/`fetch` responses, render under `MemoryRouter`. The no-jargon and never-red-for-loss rules are acceptance criteria — assert them (every symbol has a description; a loss row carries no brand-red/pink token and uses the ▼/sky-blue treatment). Assert the empty state renders the invite, not an error.

### Project Structure Notes
- Touched: `routes/Dashboard.jsx` (add the portfolio section + states). New: a `PortfolioPanel`/holdings component + its stylesheet if the Dashboard grows large (optional — keep it coherent), and `src/test/portfolio-dashboard.test.jsx`. A small `lib/holdings.js` (or inline) for the symbol→description map + currency formatting helper.
- Aligns with the existing `routes/` + `components/` + `theme/tokens.css` structure; tokens-only styling.

### References
- [Source: _bmad-output/planning-artifacts/epics.md#Story-2.4]
- [Source: ARCHITECTURE-SPINE.md#AD-11 (degraded-mode read), #AD-1 (presentation-only frontend)]
- [Source: DESIGN.md#Components (data-block, market-indicator), #Hard-color-rules, #Do's-and-Don'ts]
- [Source: EXPERIENCE.md#Screens (Dashboard), #State-Patterns (Empty pre-link / session expired), #Voice-and-Tone, UJ-3]
- [Source: implementation-artifacts/2-3-import-cache-portfolio-single-writer-projection.md (GET /api/portfolio shape), 2-2 (reauth-banner + degraded mode)]

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (Claude Code, autonomous story loop)

### Debug Log References

- Frontend: 38 passed (7 files); CSS lint clean; production build succeeds.
- Fresh-context adversarial review: no high-severity defects. Fixed the one honesty nit it flagged (F1) — a break-even holding was rendering "▲ + up since you bought"; `gainDirection` now returns null on exact equality so no indicator is shown.

### Completion Notes List

- **Presentation-only (AD-1).** The dashboard renders what `GET /api/portfolio` (Story 2.3) returns; `lib/holdings.js` is presentation copy + formatting only (symbol→description map, currency formatting, cents-based total, gain direction) — no business logic.
- **No unexplained jargon (AC1/NFR6).** Every holding pairs its ticker with a short, warm description; unknown symbols get a generic fallback, so a bare ticker never renders. Tested per-symbol.
- **Money is exact.** Total sums in integer cents (`totalValue`) to avoid float drift; values format via `Intl.NumberFormat` currency and `formatCurrency` guards NaN/Infinity → `$0.00`. Test asserts an exact `$5,500.25` total.
- **Empty/degraded (AC2/AC3).** Empty or never-imported → a calm "Connect Schwab" invite (never blank/error); a fetch failure fails quiet to the same invite. In degraded mode the cached holdings still render — the reauth-banner (Story 2.2) owns the reconnect prompt; this panel adds no execution affordance.
- **Hard color rule (AC3).** A loss uses the sky-blue ▼ market-indicator, never red/pink; the panel stylesheet uses no red/pink tokens, and a test asserts the loss markup contains neither `brand-red` nor `accent-pink`.
- **Reduced-motion:** the panel has no animation/transition, so there is nothing to suppress (vacuously satisfied) — no hollow test added for it.
- **Scope:** no index-core mapping/meter (that's Story 2.5), no coach/execution UI, no new backend endpoint.

### File List

- ballast/frontend/src/lib/holdings.js (new — descriptions, currency, total, gain direction)
- ballast/frontend/src/components/PortfolioPanel.jsx (new)
- ballast/frontend/src/components/PortfolioPanel.css (new)
- ballast/frontend/src/routes/Dashboard.jsx (fetch /api/portfolio + render PortfolioPanel)
- ballast/frontend/src/test/dashboard.test.jsx (rewritten for the portfolio dashboard)
- ballast/frontend/src/test/routes.test.jsx (fetch stub updated to the /api/portfolio shape)

## Change Log

- 2026-07-26: Implemented the plain-English portfolio dashboard on top of Story 2.3's `GET /api/portfolio` — holdings with jargon-free descriptions, exact-money balances/cash, calm empty invite, degraded-mode read, sky-blue-not-red loss treatment. Frontend 38 passed; adversarial review clean. Status → done.
