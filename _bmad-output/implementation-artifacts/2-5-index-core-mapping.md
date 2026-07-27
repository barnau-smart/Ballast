---
baseline_commit: 2a1a25e35ca7dd0d208a672a828c00ad875c548e
---

# Story 2.5: Index-core mapping

Status: done

## Story

As a user,
I want to see what counts as my stable "core,"
so that I understand my strategy at a glance.

## Acceptance Criteria

1. **Holdings mapped to index-core vs. not.** Given my portfolio, when I view it, then Ballast shows which holdings are the index core and which are not, mapped to the index-core strategy. [Source: epics.md#Story-2.5 (FR6)]
2. **Plain explanation of "core".** The mapping is explained in plain English — what "your index core" means (broad, low-cost index funds/ETFs — the stable base) and what "the rest" is — with no unexplained jargon. [Source: EXPERIENCE.md#Voice-and-Tone (NFR6), UJ-3 ("mapped to the index-core idea")]
3. **Core vs. non-core at a glance.** The user can see, at a glance, how much of their portfolio is core vs. not (grouping and/or a value split), so the strategy is legible without reading every row. [Source: epics.md#Story-2.5, FR6]

**Cross-cutting:** classification is domain logic and lives on the backend (AD-1 keeps the frontend presentation-only); the "core" definition = broad index funds/ETFs per FR10 (the broad core may diversify beyond the S&P at user choice, never *avoiding* the index); calm/plain voice; green = interface, NEVER red/pink for "non-core" (non-core is not "bad"); money split summed without float drift; per-user via the existing scoped read. [Source: ARCHITECTURE-SPINE.md#AD-1, epics.md#FR10, DESIGN.md#Hard-color-rules]

## Tasks / Subtasks

- [x] **Task 1: Index-core classifier (backend domain logic)** (AC: 1)
  - [x] Added `strategy/` package (`__init__.py` + `index_core.py`): curated `INDEX_CORE_SYMBOLS` (VTI/VOO/VXUS/VT/BND/BNDX + well-known broad funds), `is_index_core(symbol)`, and a plain `INDEX_CORE_RATIONALE`. Docstring states it's reused by the Coach Engine (Epic 4) — why it lives in `strategy`, not `coach`/`brokers`.
  - [x] Case/whitespace-insensitive; unknown/blank → non-core (conservative).
- [x] **Task 2: Expose the mapping on the portfolio read** (AC: 1, 3)
  - [x] `HoldingOut.is_core` added, populated via `is_index_core` at read time — NOT stored on `portfolio_cache` (stays a pure projection, AD-14). Documented in the schema docstring.
  - [x] No new endpoint; extended the existing pure read.
- [x] **Task 3: Frontend — show core vs. the rest** (AC: 1, 2, 3)
  - [x] `PortfolioPanel` groups into "Your index core" / "The rest" (a `HoldingGroup` component) driven by `is_core`, each with a plain explainer; non-core framed as "not bad, just not the steady base".
  - [x] Per-group value subtotal via `holdingsValue` (cents-based, money-safe); cash stays in its own stat (neither core nor non-core). Neutral styling — no red/pink.
  - [x] Tokens-only; "the rest" group absent when all holdings are core (no empty scaffolding).
- [x] **Task 4: Tests** (AC: 1, 2, 3)
  - [x] Backend: `test_index_core.py` (case-insensitive core hits; AAPL/sector/unknown → non-core; core set upper-case). `test_portfolio.py` asserts `is_core` in the read — all-core fake set + a mixed VTI/AAPL seed → `{VTI:True, AAPL:False}`.
  - [x] Frontend: mixed portfolio renders both groups, VTI in core / AAPL in the rest, the explainer, and correct group subtotals; non-core carries no `brand-red`/`accent-pink`/`line-red` token.
  - [x] All-core → no "the rest" group. No regressions (backend 105, frontend 40; 2.3/2.4 tests updated for `is_core`).
- [x] **Task 5: Verify** — backend 105 passed; frontend 40 passed + CSS lint clean + build succeeds. Mixed, all-core, and empty portfolios reasoned through (adversarial review confirmed edge cases).

## Dev Notes

### What "index core" means [Source: epics.md#FR6, #FR10]
- **FR6:** show how the current portfolio maps to the index-core strategy (what's "core," what isn't).
- **FR10:** v1 order scope is a small set of broad index funds/ETFs; the broad core may diversify beyond the S&P *at user choice* (never *avoiding* the index). So "core" = broad, low-cost index funds/ETFs (total-market / large-cap-index / broad bond funds); "the rest" = anything else the user happens to hold (individual stocks, sector/thematic funds, crypto, etc.). Non-core is NOT framed as bad — just outside the stable base.
- This is UJ-3's payoff: the first dashboard shows real holdings "mapped to the index-core idea." [Source: EXPERIENCE.md UJ-3]

### Builds on 2.3 + 2.4 (done) — reuse
- **`GET /api/portfolio`** (2.3) + `HoldingOut` (2.4 render): extend with `is_core`; the frontend already renders holdings + plain descriptions + a cents-based total helper (`lib/holdings.js`) — reuse `totalValue` for the group subtotals. [Source: api/portfolio.py, frontend/src/lib/holdings.js, components/PortfolioPanel.jsx]
- **`portfolio_cache`** stays a pure broker projection (AD-14) — `is_core` is derived at read time, never persisted. [Source: db/models.py, brokers/portfolio.py]
- **`describeHolding`** already gives every symbol a plain description; the core/non-core grouping sits alongside it.

### Architecture placement
- New `strategy/` package (domain-named, alongside `coach`/`precedent`/`brokers`/…). The index-core reference is strategy knowledge the Coach Engine will also consume in Epic 4 (the coach recommends investing "into your index core"), so it must not live inside the Epic-4 `coach/` pipeline nor in broker-specific code. Classification is domain logic → backend (AD-1: the frontend stays presentation-only and just renders `is_core`). [Source: ARCHITECTURE-SPINE.md#AD-1, #Consistency-Conventions (module naming)]

### Scope guardrails
- **In scope:** the index-core classifier (backend), exposing `is_core` on the portfolio read, and the frontend core/"the rest" grouping + plain explainer + value split, with tests.
- **Out of scope:** the coach/recommendation pipeline that *acts* on the core (Epic 4), any order/rebalancing logic (Epic 4), storing classification on the cache, a per-user editable core universe (v1 uses a fixed curated set; user-choice diversification within the core is an Epic-4/settings concern — do NOT build a core editor here), precedent/market-data (Epic 3).
- **Color rule:** non-core is neutral, never red/pink (it is not a loss or error).

### Testing standards
Backend real-DB read test (reconcile the fake set, GET, assert `is_core` per holding) + a pure classifier unit test (case-insensitivity, unknown → non-core). Frontend: mixed vs all-core portfolios; assert the explainer + split render and that non-core carries no brand-red/pink token (color rule is acceptance-level). Update the 2.3/2.4 tests for the added `is_core` field.

### Project Structure Notes
- New: `strategy/__init__.py`, `strategy/index_core.py`, `tests/test_index_core.py`.
- Touched: `api/portfolio.py` (`HoldingOut.is_core`), `tests/test_portfolio.py` (assert `is_core`), frontend `components/PortfolioPanel.jsx` (+ maybe `lib/holdings.js` split helper), `src/test/dashboard.test.jsx` (grouping/explainer + `is_core` in mocks).

### References
- [Source: _bmad-output/planning-artifacts/epics.md#Story-2.5, #FR6, #FR10]
- [Source: ARCHITECTURE-SPINE.md#AD-1 (presentation-only frontend), #AD-14 (cache is a pure projection), #Consistency-Conventions (module naming, money)]
- [Source: EXPERIENCE.md#Voice-and-Tone (plain, no jargon), UJ-3 (mapped to the index-core idea); DESIGN.md#Hard-color-rules]
- [Source: implementation-artifacts/2-3-… (GET /api/portfolio), 2-4-… (PortfolioPanel, lib/holdings.js)]

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (Claude Code, autonomous story loop)

### Debug Log References

- Backend 105 passed; frontend 40 passed; CSS lint clean; build succeeds. (Requires docker Postgres for the read test.)
- Fresh-context adversarial review: no defects; ACs, AD-1, AD-14, and scope all satisfied. Tightened the frontend color-rule guard to also match the `line-red` token (nit).

### Completion Notes List

- **Classification is backend domain logic (AD-1).** `strategy/index_core.py` is the single definition of the index-core strategy (FR6/FR10) — placed in a new `strategy/` package because both the portfolio view (Epic 2) and the Coach Engine (Epic 4, which recommends investing "into your index core") consume it, and it is neither broker-specific nor part of the Epic-4 coach pipeline. Conservative + case-insensitive; unknown → non-core.
- **Derived, not persisted (AD-14).** `is_core` is computed at read time in `GET /api/portfolio`; `portfolio_cache` stays a pure broker projection, so the core set can evolve without a re-import. No schema change, no new endpoint.
- **Plain + calm (AC2, color rule).** The dashboard groups "Your index core" vs "The rest" with jargon-free explainers; non-core is explicitly framed as "not bad, just not the steady base" and styled neutrally — never red/pink (a test asserts no `brand-red`/`accent-pink`/`line-red`).
- **At a glance (AC3).** Per-group value subtotals summed money-safe in cents; cash shown separately (neither core nor non-core). All-core hides "The rest"; empty shows the calm invite.
- **Scope:** no per-user core editor (v1 fixed curated set), no coach/execution logic — those are Epic 4.
- Note: a few core symbols (SPY/IVV/ITOT/AGG…) don't yet have a bespoke plain description and fall to the Story 2.4 generic fallback — acceptable by design (always understandable), extend the description map opportunistically later.

### File List

- ballast/backend/strategy/__init__.py (new)
- ballast/backend/strategy/index_core.py (new — classifier + curated core set + rationale)
- ballast/backend/api/portfolio.py (HoldingOut.is_core, derived at read time)
- ballast/backend/tests/test_index_core.py (new)
- ballast/backend/tests/test_portfolio.py (is_core assertions + mixed-seed test)
- ballast/frontend/src/lib/holdings.js (partitionByCore, holdingsValue, explainers)
- ballast/frontend/src/components/PortfolioPanel.jsx (core / the-rest grouping)
- ballast/frontend/src/components/PortfolioPanel.css (group header styles)
- ballast/frontend/src/test/dashboard.test.jsx (grouping tests + is_core in mocks)

## Change Log

- 2026-07-26: Implemented index-core mapping — a backend classifier (`strategy/index_core`) exposed as `is_core` on the portfolio read, and a dashboard that groups holdings into "Your index core" vs "The rest" with plain explainers and a value split. Backend 105 / frontend 40 passed; adversarial review clean. Status → done. Completes Epic 2.
