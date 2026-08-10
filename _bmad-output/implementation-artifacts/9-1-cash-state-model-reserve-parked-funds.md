# Story 9.1: Cash-state model & user-declared reserve + parked funds

Status: ready-for-dev

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a beginner investor using Ballast,
I want to tell the app how much cash I never want to touch (a reserve) and which of my holdings are actually money-market "parked" cash, and to see my cash honestly split into ready-to-trade / parked / reserved,
so that the app understands my money the way I actually keep it — and every later nudge is honest, calm, and only ever about money I'd genuinely invest.

## Context & scope

This is **Story 1 of new Epic 9 (Cash Intelligence)** — the FOUNDATION the other two stories build on. It delivers the **data model + user-declared config + honest three-state representation** of cash. It does **not** change any market math yet.

Source of truth for the whole epic: `_bmad-output/brainstorming/brainstorm-cash-readiness-idle-cash-2026-08-10/brainstorm-intent.md`.

**IN SCOPE (9-1):**
1. A per-user "cash configuration": (a) an **optional, editable reserve amount** (Decimal money, ≥ 0, default *unset*); (b) a **user-specified set of holding symbols to treat as PARKED cash-equivalents** (money-market funds, e.g. `SWVXX`). Never auto-classify tickers.
2. An explicit **set-or-decline reserve gate** so an empty reserve is an intentional choice, never a silent assumption. After the user has explicitly set OR declined, an unset reserve is legitimately treated as `0`.
3. Represent **three cash states** wherever cash/holdings are surfaced (the `GET /api/portfolio` read + dashboard): **ready-to-trade** (settlement cash), **parked** (user-tagged money-market funds), **reserved** (declared amount). Reserve is conceptually drawn **parked-first** — expose enough for 9-2 to compute, but 9-1 does **not** change the missed-growth math.
4. **Display fix:** user-tagged parked funds must stop rendering as stock-like holdings with "up/down since you bought" — render them as **parked cash**.
5. Config is **editable later** from a Settings surface.

**OUT OF SCOPE (explicitly deferred):**
- The yield-aware missed-growth recalculation and capped/reserve-framed nudge → **Story 9-2**. (9-1 only *exposes* the model; `precedent/missed_growth.py` math is untouched.)
- Any liquidation / deferred-buy / pending-buy / notification flow → **Story 9-3**.
- Auto-classifying tickers as money-market (v1 is user-specified — simpler, honest).
- External/bank cash Ballast can't see (the user owns that).
- Per-fund **yield rate** input — defer to 9-2, where the yield math lives (unless trivial to co-locate; 9-2 owns it).

## Acceptance Criteria

**AC1 — Per-user cash config persists, fail-closed (AD-10).**
**Given** an authenticated user,
**When** they read or write their cash configuration,
**Then** it is stored in a new per-user owned table reached ONLY through the fail-closed `ScopedRepository` (a user can never read or write another user's config — exactly one config row per user, enforced by a `UniqueConstraint(owner_id)`), money is stored as `Decimal`/`Numeric` (never float), and a brand-new user with no row reads a calm default (reserve unset / undecided, no parked symbols).

**AC2 — Reserve is optional, zero-allowed, and honest-by-construction (set-or-decline).**
**Given** a user who has never made a reserve decision,
**When** the app needs the reserve for any calculation or display,
**Then** the config distinguishes three states — **never-decided**, **declined** (explicitly "I don't keep one"), and **set** (an explicit amount ≥ 0, including exactly `0`) — the "never-decided" state is never silently treated as `0`; only after an explicit set-or-decline is an unset/declined reserve treated as `0`; a negative reserve is rejected with a calm 422.

**AC3 — Reserve + parked symbols are editable.**
**Given** a user with an existing config,
**When** they change the reserve amount, decline, or edit the set of parked symbols,
**Then** the change persists idempotently and takes effect on the next read; symbols are normalized (trimmed, upper-cased, de-duplicated) and only affect display/classification for symbols the user actually holds (an unknown/unheld symbol is stored but simply matches nothing).

**AC4 — The portfolio read exposes the three cash states without breaking the fixed shape.**
**Given** the authenticated user's `GET /api/portfolio`,
**When** the response is built,
**Then** the existing fields (`holdings`, `cash`, `as_of`) are **unchanged** (reconcile + missed-growth depend on them), and NEW additive fields expose: per-holding `is_parked` (derived at read time from the user's parked-symbol set, like `is_core` is derived from `strategy.index_core`), and a cash-state summary — `ready_to_trade` (= settlement `cash`), `parked` (sum of parked holdings' market value), `reserved` (the resolved reserve: the amount if set, `0` if declined, `null`/absent if never-decided), and `reserve_decided` (bool). All money renders as fixed-point strings via `WireMoney`/`format_money`.

**AC5 — Parked funds render as parked cash, not stock-like movers (display fix).**
**Given** a holding the user has tagged as parked (e.g. `SWVXX`),
**When** the dashboard renders,
**Then** it appears in a distinct **"Parked cash (money market)"** group described as cash-equivalent, and it does **NOT** show an "up / down since you bought" indicator (it is cash, not a bet); the three states (ready-to-trade / parked / reserved) are shown as calm, plain-English figures; the index-core / "the rest" grouping for genuine holdings is preserved.

**AC6 — A calm set-or-decline prompt surfaces once, then lives in Settings.**
**Given** a user who has never decided their reserve,
**When** they view the app,
**Then** a calm, non-blocking prompt invites them to set a reserve OR say they don't keep one (never nagging, never alarmist), and after deciding, the prompt disappears and the reserve + parked-fund config is editable from the Settings surface (mirroring the weekly-digest card pattern).

**AC7 — Calm/honest voice is preserved (hard constraint, NFR8).**
**Given** any new copy this story adds,
**When** it is shown,
**Then** it uses the beginner-friendly, non-alarmist voice and contains **no** FOMO / urgency / "red" language (same testable tone bar as the digest — see the `FORBIDDEN` word list in `tests/test_digest_compose.py`), and a down/parked value is never rendered red/pink.

**AC8 — The full suite stays green; the new table provisions cleanly.**
**Given** the change set,
**When** the backend `pytest` suite and frontend `vitest` suite run,
**Then** all tests pass; the new table is created by `create_all` on any DB where it does not yet exist (a brand-new table needs no `ALTER` migration), and new backend + frontend tests cover the config model, the set-or-decline semantics, scoped isolation, the augmented portfolio read, and the parked-display fix.

## Tasks / Subtasks

- [ ] **Task 1 — New per-user `CashConfig` model (AC: 1, 2)**
  - [ ] Add `CashConfig(OwnedEntityMixin, Base)` in `ballast/backend/db/models.py` (table `cash_config`) mirroring `DigestPreference`: `id` UUID pk; `reserve_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)`; `reserve_decided: Mapped[bool] = mapped_column(nullable=False, default=False)`; `parked_symbols: Mapped[list] = mapped_column(JSON, nullable=False, default=list)`; `created_at` / `updated_at` tz-aware UTC; `__table_args__ = (UniqueConstraint("owner_id", name="uq_cash_config_owner"),)`.
  - [ ] Confirm no `db/migrations.py` entry is required (new table → `create_all` builds it in full). OPTIONAL parity add: a `CREATE UNIQUE INDEX IF NOT EXISTS uq_cash_config_owner ON cash_config (owner_id)` step (low priority; only for carried-over-DB belt-and-suspenders, matching `uq_portfolio_balance_owner`).
- [ ] **Task 2 — Config helpers, scoped + fail-closed (AC: 1, 2, 3)**
  - [ ] New module `ballast/backend/cash/config.py` (new `cash/` package with `__init__.py`) mirroring `digest/preferences.py`: `get_or_create_config(scope, session)` (create-on-first-read with `IntegrityError` lost-race handling, commit on create); `set_reserve(scope, session, *, amount: Decimal | None, decided: bool)` (validates `amount is None or amount >= 0`, else raise a caught ValueError → 422; sets `reserve_decided=True` when the user acts); `set_parked_symbols(scope, session, symbols: list[str])` (normalize upper/trim/dedupe). All via `ScopedRepository(CashConfig, scope, session)`.
- [ ] **Task 3 — Config API (AC: 1, 2, 3, 7)**
  - [ ] New `ballast/backend/api/cash.py` router (`prefix="/api/cash"`) mirroring `api/digest.py`: `GET /api/cash/config` → `{ reserve_amount: WireMoney | None, reserve_decided: bool, parked_symbols: list[str] }`; `PUT /api/cash/config` accepting `{ reserve_amount: Decimal | None, reserve_decided: bool, parked_symbols: list[str] }` (money in as Decimal, out as fixed-point string via `WireMoney`). Auth via `get_scope`; session via `get_async_session`. Reject `reserve_amount < 0` with a calm 422.
  - [ ] Register the router in `ballast/backend/api/app.py` (`app.include_router(cash_router)`), alongside `digest_router`.
- [ ] **Task 4 — Augment the portfolio read with the three states (AC: 4, 5)**
  - [ ] In `ballast/backend/api/portfolio.py`: load the caller's `CashConfig` (scoped), add `is_parked: bool` to `HoldingOut` (derived from the parked-symbol set, case-insensitive — mirror how `is_core=is_index_core(...)` is derived), and add a `CashStatesOut` block to `PortfolioOut`: `ready_to_trade` (= `view.cash`), `parked` (Σ parked holdings' `market_value`), `reserved` (amount if set, `Decimal("0")` if declined, `None` if never-decided), `reserve_decided`. Keep `holdings` / `cash` / `as_of` **byte-identical** (do NOT touch `PortfolioView` or `brokers/portfolio.py`). All money via `WireMoney`.
  - [ ] Do NOT modify `precedent/missed_growth.py` or `api/precedent.py` (9-2 owns the calc).
- [ ] **Task 5 — Frontend: parked display + three-state stats (AC: 5, 7)**
  - [ ] `ballast/frontend/src/lib/holdings.js`: add a `partitionByCash(holdings)` (or extend `partitionByCore`) that splits parked (`is_parked`) out first, then core/rest for the remainder; ensure `gainDirection` is never shown for parked holdings.
  - [ ] `ballast/frontend/src/components/PortfolioPanel.jsx`: render a new **"Parked cash (money market)"** group (cash-equivalent copy, NO `MarketIndicator`), and show the three-state summary (ready-to-trade / parked / reserved) using the new API fields; relabel "Cash ready to invest" to reflect *ready-to-trade* settlement cash. Preserve the core/"the rest" groups for real holdings.
- [ ] **Task 6 — Frontend: Settings cash card + set-or-decline prompt (AC: 3, 6, 7)**
  - [ ] `ballast/frontend/src/routes/Settings.jsx`: add a "Cash setup" card (mirror the digest card): a reserve amount input + a "set" / "I don't keep one" (decline) control, and a way to tag which held symbols are parked (a checkbox list of the user's current holdings is the simplest honest UI). Wire to `GET`/`PUT /api/cash/config` via `apiFetch`; optimistic + fail-quiet like the digest toggle.
  - [ ] Surface a calm, non-blocking **set-or-decline prompt** when `reserve_decided === false` (recommended: a dismissible card on the Dashboard near the cash summary, or a banner) that links to / inlines the set-or-decline action; it disappears once decided.
- [ ] **Task 7 — Tests (AC: 8, 7) — full suite must stay green**
  - [ ] Backend `ballast/backend/tests/test_cash_config.py`: default-on-first-read; set reserve (incl. exactly `0`); decline; negative → 422; parked-symbol normalize/dedupe; **scoped isolation** (user A cannot read/modify user B); GET/PUT round-trip with money as fixed-point strings.
  - [ ] Backend `test_portfolio.py` (extend): portfolio read now returns `is_parked` + the cash-state summary; `reserved` is `null` when never-decided, `0` when declined, the amount when set; `holdings`/`cash`/`as_of` unchanged.
  - [ ] Frontend `ballast/frontend/src/test/`: a `cash-config` settings test (get/put, set + decline) and a Dashboard test (parked group renders with no up/down indicator; three-state stats show; set-or-decline prompt appears only when undecided). Assert calm copy (reuse the digest FORBIDDEN-word discipline).

## Dev Notes

### The persistence-model decision (and why a NEW table)
Add a **new owned table `cash_config`**, not columns on `DigestPreference`. Rationale: it is a distinct domain (financial config vs. email opt-in), it follows the established one-owned-table-per-concern pattern (`brokerage_token`, `portfolio_cache`, `portfolio_balance`, `decision_record`, `digest_preference` are all separate owned tables), and it keeps the digest preference untouched. Use `OwnedEntityMixin` (gives the indexed `owner_id` FK) + `UniqueConstraint("owner_id", name="uq_cash_config_owner")` — this is exactly the `DigestPreference` / `PortfolioBalance` shape [Source: ballast/backend/db/models.py:408-455 (DigestPreference), :166-209 (PortfolioBalance), :53-75 (OwnedEntityMixin)].

**Migrations:** a brand-new table needs **no** `db/migrations.py` entry — `create_all` (`db.session.create_db_and_tables`) builds any *missing table in full* (including its `UniqueConstraint`); `db/migrations.py` exists ONLY to `ALTER`/add columns+indexes onto *pre-existing* tables (the Epic 6/7 additions to `decision_record`) [Source: ballast/backend/db/migrations.py:1-35, :145-179]. Note `create_all` runs before `run_startup_migrations` in the lifespan [Source: ballast/backend/api/app.py:76-82]. Optional belt-and-suspenders for carried-over DBs: a `CREATE UNIQUE INDEX IF NOT EXISTS uq_cash_config_owner` step (parity with the `uq_portfolio_balance_owner` entry at migrations.py:137-141) — low priority.

**Set-or-decline representation (the honesty crux, AC2):** two columns disambiguate the three states cleanly:
- **never-decided:** `reserve_decided = False` (default). Do NOT treat as `0`; this is what triggers the prompt (AC6).
- **declined:** `reserve_decided = True`, `reserve_amount = NULL` → resolve to `Decimal("0")`.
- **set:** `reserve_decided = True`, `reserve_amount = X` (X ≥ 0; `0` is a legitimate explicit set).

This mirrors the intent's "explicit set OR decline; after that an unset reserve is legitimately 0" [Source: brainstorm-intent.md#Decisions-locked].

### API surface (mirror the digest preference pattern)
`api/digest.py` is the exact precedent: authed `GET`/`PUT /api/digest/preference` funneling through `get_scope` + a helpers module (`digest/preferences.py`) that uses `ScopedRepository` with a `get_or_create` + `IntegrityError` lost-race guard [Source: ballast/backend/api/digest.py:54-72, ballast/backend/digest/preferences.py:35-79]. Mirror it: `api/cash.py` + `cash/config.py`.
- Money on the wire: type response money fields as `WireMoney` (renders `Decimal` as fixed-point string, passes `None` through) [Source: ballast/backend/money.py:30-54, ballast/backend/api/portfolio.py:31,42-56].
- Money in the request: accept `Decimal | None` (Pydantic coerces the JSON string/number to `Decimal`); validate `>= 0` in the helper and surface a calm **422** the way `/api/portfolio/refresh` raises `HTTPException(status_code=422, ...)` for a config fault [Source: ballast/backend/api/portfolio.py:118-126].
- Register the router in `api/app.py` next to the other `include_router` calls [Source: ballast/backend/api/app.py:207-226].

### The portfolio-read seam (UPDATE — `api/portfolio.py`)
- **Current behavior:** `read_portfolio` calls `get_portfolio(scope, session)` → `PortfolioView(holdings, cash, as_of)`; `_to_out` maps each holding to `HoldingOut` deriving `is_core=is_index_core(h.symbol)` at read time, and sets `cash`/`as_of` [Source: ballast/backend/api/portfolio.py:71-102].
- **What changes:** additionally load the caller's `CashConfig` (scoped), derive `is_parked` per holding from the parked-symbol set (case-insensitive, same read-time-derivation philosophy as `is_core` — NEVER stored on `portfolio_cache`, which stays a pure broker projection, AD-14), and add the `CashStatesOut` summary. `PortfolioOut.holdings`/`cash`/`as_of` stay identical.
- **What must NOT break:** `PortfolioView`'s public shape is FIXED — every consumer (dashboard, missed-growth, coach oversized-lump) depends on `holdings`/`cash`/`as_of`/`is_empty` unchanged [Source: ballast/backend/brokers/portfolio.py:59-81]. Do NOT touch `brokers/portfolio.py` (the AD-14 single writer) or `db/models.py:PortfolioCache` (pure projection). `ready_to_trade` == `view.cash` (the authoritative settlement cash from `portfolio_balance`, Story 6.5) [Source: ballast/backend/brokers/portfolio.py:88-95].
- **Parked value:** sum `market_value` of holdings whose symbol is in the parked set. This is display data for 9-1; 9-2 will consume `parked` + `reserved` for the honest missed-growth base (`cash + parked − reserve`) [Source: brainstorm-intent.md#Decisions-locked].
- **Do NOT modify** `precedent/missed_growth.py` (reads `view.cash` today) or `api/precedent.py:missed_growth` — that recalc is 9-2 [Source: ballast/backend/precedent/missed_growth.py:125-145, ballast/backend/api/precedent.py:163-184].

### Frontend seams (UPDATE)
- `Dashboard.jsx` fetches `GET /api/portfolio` and renders `<PortfolioPanel>` + `<MissedGrowthMeter>` [Source: ballast/frontend/src/routes/Dashboard.jsx:18-52]. The set-or-decline prompt fits here (near the cash summary), fail-quiet like the existing catch.
- `PortfolioPanel.jsx` currently: `totalValue(holdings, cash)`, `partitionByCore(holdings)` → "Your index core" / "The rest" groups; each holding shows a `MarketIndicator` when `gainDirection(holding)` is non-null; "Cash ready to invest" shows `portfolio.cash` [Source: ballast/frontend/src/components/PortfolioPanel.jsx:32-149]. **Change:** pull parked holdings into their own group with NO `MarketIndicator`; add the three-state summary. Hard color rule: down/parked is NEVER red/pink [Source: ballast/frontend/src/components/PortfolioPanel.jsx:29-31,106].
- `holdings.js`: `partitionByCore` is driven by the backend `is_core` flag [Source: ballast/frontend/src/lib/holdings.js:76-83]; add a parked split driven by `is_parked`. `gainDirection` returns null when cost basis is unknown — parked funds should simply never render an indicator regardless [Source: ballast/frontend/src/lib/holdings.js:101-107].
- `Settings.jsx` is the pattern for the new cash card: `apiFetch` GET on mount, optimistic PUT on change, fail-quiet [Source: ballast/frontend/src/routes/Settings.jsx:18-59]. `apiFetch` attaches the bearer to same-origin backend calls [Source: ballast/frontend/src/lib/session.js].
- Note `SWVXX` and `SWPPX` are currently in `INDEX_CORE_SYMBOLS` handling only for `SWPPX` (S&P index MF); `SWVXX` (money-market) is NOT core and today falls into "The rest" as a mover — exactly the bug this story fixes via user tagging [Source: ballast/backend/strategy/index_core.py:24-46].

### Money & determinism conventions
- All money is `Decimal` end-to-end, `Numeric(20, 2)` in the DB, never binary float [Source: ballast/backend/db/models.py:147-158,195]. On the wire use `WireMoney`/`format_money` (fixed-point, no `E+`/`E-`) [Source: ballast/backend/money.py:1-54].

### Testing conventions
- Backend: `pytest` with a **function-scoped** `client` fixture; conftest forces `LLM_ADAPTER=fake`, `DECISION_MAINTENANCE_ENABLED=false`, `MARKETDATA_INGEST_ENABLED=false` at import, and a session-autouse **brokerage-db guard** refuses to run against a DB holding a live `brokerage_token` (override `BALLAST_ALLOW_DIRTY_BROKERAGE_DB=1`) [Source: ballast/backend/tests/conftest.py:66-76,183-237]. Scoped/preference tests: see `test_digest_compose.py` and the `_assert_calm` FORBIDDEN-word tone check to reuse for AC7 [Source: ballast/backend/tests/test_digest_compose.py:1-40]. Run from `ballast/backend` with `uv run pytest -q`.
- Frontend: `vitest` + `@testing-library/react`; mirror `settings.test.jsx` (mock `apiFetch`/`fetch`, `setToken`) and `dashboard.test.jsx`. Run `npm test` in `ballast/frontend`.
- ⚠️ The full suite shares the dev Postgres and DELETEs by symbol in some fixtures — do not run it against a DB holding a live brokerage link or the demo market-data backfill without expecting a wipe (documented go-live gotcha).

### Project Structure Notes
- New: `ballast/backend/cash/__init__.py`, `ballast/backend/cash/config.py`, `ballast/backend/api/cash.py`, `ballast/backend/tests/test_cash_config.py`; new frontend test(s) under `ballast/frontend/src/test/`.
- Modified: `ballast/backend/db/models.py` (add `CashConfig`), `ballast/backend/api/app.py` (register router), `ballast/backend/api/portfolio.py` (augment read), `ballast/frontend/src/lib/holdings.js`, `ballast/frontend/src/components/PortfolioPanel.jsx`, `ballast/frontend/src/routes/Settings.jsx`, and `ballast/frontend/src/routes/Dashboard.jsx` (set-or-decline prompt).
- Naming/placement mirrors the digest feature (`digest/preferences.py` + `api/digest.py`) and the portfolio read (`api/portfolio.py`) — no structural variance. `cash_config` is a per-user OWNED table routed through `ScopedRepository` (AD-10); it is NOT global reference data (unlike `market_daily`).
- Variance/rationale: a NEW owned table (vs. reusing an existing preference table) is the deliberate, convention-aligned choice (one owned table per concern).

### References
- [Source: _bmad-output/brainstorming/brainstorm-cash-readiness-idle-cash-2026-08-10/brainstorm-intent.md] — full concept, locked decisions, out-of-scope, honesty constraint.
- [Source: ballast/backend/db/models.py#DigestPreference (408-455), #PortfolioBalance (166-209), #OwnedEntityMixin (53-75)] — owned-table + one-row-per-user pattern to mirror.
- [Source: ballast/backend/db/repository.py#ScopedRepository (44-177)] — fail-closed per-user persistence funnel (AD-10).
- [Source: ballast/backend/digest/preferences.py (35-79)] — get_or_create + set + IntegrityError lost-race pattern to mirror.
- [Source: ballast/backend/api/digest.py (39-72)] — authed GET/PUT preference endpoint pattern to mirror.
- [Source: ballast/backend/api/portfolio.py (42-102)] — the read seam + read-time `is_core` derivation to extend with `is_parked` + cash states.
- [Source: ballast/backend/brokers/portfolio.py#PortfolioView (59-95)] — FIXED view shape; do not modify; `cash` == authoritative settlement cash.
- [Source: ballast/backend/precedent/missed_growth.py (125-265), ballast/backend/api/precedent.py (139-184)] — the meter that 9-2 (NOT 9-1) will make yield-aware; do not touch here.
- [Source: ballast/backend/money.py (30-54)] — `WireMoney`/`format_money` money-on-the-wire contract.
- [Source: ballast/backend/db/migrations.py (1-179)] — why a new table needs no ALTER migration.
- [Source: ballast/backend/api/app.py (66-118, 207-226)] — lifespan (create_all → migrations) + router registration.
- [Source: ballast/backend/strategy/index_core.py (24-65)] — core classification the parked classification sits beside; SWVXX/SWPPX note.
- [Source: ballast/frontend/src/components/PortfolioPanel.jsx (32-149), src/lib/holdings.js (76-107), src/routes/Settings.jsx (14-96), src/routes/Dashboard.jsx (14-54)] — frontend seams for the display fix + settings card + prompt.
- [Source: ballast/backend/tests/conftest.py (66-76,183-237), tests/test_digest_compose.py (1-40)] — test harness + calm-copy tone check.

## Dev Agent Record

### Agent Model Used

### Debug Log References

### Completion Notes List

### File List
