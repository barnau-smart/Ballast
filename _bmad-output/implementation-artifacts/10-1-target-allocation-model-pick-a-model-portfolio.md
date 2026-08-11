# Story 10.1: Target-allocation model — pick a model portfolio

Status: ready-for-dev

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a beginner investor using Ballast,
I want to pick one simple named target mix (Conservative / Balanced / Growth) once,
so that the app has a clear "where my money should be" to measure against — and I never have to invent an allocation myself.

## Context & scope

This is **Story 1 of new Epic 10 (Allocation Coach — Deploy My Cash)** — the FOUNDATION the rest of the epic builds on. It delivers **the target-allocation model + per-user selection**: named model portfolios as code reference data, a per-user "which model did you pick" config, its API, and the calm set-or-decline UI. It **does not** analyze the portfolio or generate any action items yet — it only establishes and exposes the *target* that Story 10-2's gap-to-target engine will consume.

Source of truth for the epic: `_bmad-output/brainstorming/brainstorm-allocation-coach-deploy-my-cash-2026-08-11/brainstorm-intent.md`. Epic + drafted ACs: `_bmad-output/planning-artifacts/epics.md` (Epic 10 → Story 10.1).

**Locked decision (from the brainstorm):** the target is set by the user **picking a NAMED MODEL PORTFOLIO** — NOT a risk questionnaire, NOT age-based, NOT user-editable weights. Each model = fixed target weights across **asset classes** (US equity / international equity / bonds) mapped to concrete index-core funds. This is the simplest, most transparent, and most "explainable" option — it fits the epic's "never invent, always explainable" ethos.

**IN SCOPE (10-1):**
1. **Model-portfolio reference data** — a new `strategy/` module (mirrors `strategy/index_core.py`): the asset-class taxonomy, a symbol→asset-class map for the index-core funds, the named model portfolios with their target weights, and the canonical buy-fund per asset class. Fixed reference data, deterministic/pure.
2. **Per-user target selection** — a new owned config table (mirrors Epic 9 `CashConfig`) reached ONLY through the fail-closed `ScopedRepository` (AD-10), one row per user, storing the chosen model key (or none). Editable. A brand-new user reads a calm default (undecided).
3. **`GET`/`PUT /api/target-allocation`** — read the choices + current selection + resolved target weights; set the selection (invalid key → calm 422). Mirrors `api/cash.py`.
4. **Frontend** — a Settings "Target mix" card to pick a model (each shown with a plain-English description + its mix as simple percentages), and a calm, non-nagging, dismissible Dashboard prompt when undecided (mirrors Epic 9's Cash-setup card + set-or-decline prompt).
5. **Resolved target exposed** for Story 10-2 to consume (weights by asset class + canonical funds), `null`/absent when undecided.

**OUT OF SCOPE (explicitly deferred):**
- Any portfolio analysis, gap-to-target computation, or action items → **Story 10-2**.
- The AI fiduciary-advisor narration + never-invent safeguard → **Story 10-3**.
- Concentration / cost-fee buckets → **Story 10-4**.
- User-editable custom weights, a risk questionnaire, or age-based defaults (deliberately NOT this — the locked decision is named presets).
- Classifying *every possible* held symbol into an asset class beyond the curated index-core set (10-2's concern; here we only need the index-core map + the target definition).

## Acceptance Criteria

**AC1 — Model-portfolio reference data is deterministic, pure, and honest (new `strategy` module).**
**Given** a new module `ballast/backend/strategy/target_allocation.py` (mirroring `strategy/index_core.py`),
**When** it is loaded,
**Then** it defines: (a) an **asset-class taxonomy** of exactly three classes — US equity, international equity, bonds; (b) a **symbol→asset-class map** covering the curated `INDEX_CORE_SYMBOLS` (so a held index-core fund resolves to its class); (c) a small set of **named model portfolios** — `conservative`, `balanced`, `growth` — each with **target weights across the three classes that sum to exactly `Decimal("1.00")`** (money/weights are `Decimal`, never float), a plain-English description, and a **canonical buy-fund per asset class**; and pure helper functions (`get_model(key)`, `list_models()`, `resolve_weights(key)`) that are deterministic (same input → equal output, no I/O). The weights are fixed reference data (extend deliberately, like `index_core`) — NOT user-editable.

**AC2 — Per-user target selection persists, fail-closed (AD-10).**
**Given** an authenticated user,
**When** they read or write their target-allocation selection,
**Then** it is stored in a NEW per-user owned table reached ONLY through the fail-closed `ScopedRepository` (exactly one row per user, `UniqueConstraint(owner_id)`), the chosen model is stored as its **string key** (validated against the known model keys) or NULL when undecided, `created_at`/`updated_at` are tz-aware UTC, and a brand-new user with no row reads a calm default (**undecided / no model chosen**).

**AC3 — Set-or-decline is honest (undecided is a real state, never silently a default model).**
**Given** a user who has never chosen a model,
**When** the app needs the target for any display or (later) calculation,
**Then** the config distinguishes **undecided** (no row / model = NULL — this drives the calm prompt) from **chosen** (a valid model key); the app **never silently assumes a model** for an undecided user; and picking a model is idempotent and takes effect on the next read. (There is no "decline" state — without a target the coach simply can't prescribe; the prompt is non-blocking and dismissible, never forced.)

**AC4 — The API exposes choices, selection, and the resolved target.**
**Given** the authenticated user's `GET /api/target-allocation`,
**When** the response is built,
**Then** it returns `{ model: <key|null>, choices: [{ key, name, description, weights }...], resolved: <{weights_by_class, funds_by_class}|null> }` (weights render as fixed-point strings via `WireMoney`/`format_money`); and `PUT /api/target-allocation` accepting `{ model: <key> }` validates the key against the known models (an unknown key → a calm **422**, mirroring `/api/cash/config`), persists idempotently, and returns the updated shape. Auth via `get_scope`; session via `get_async_session`; per-user scoped.

**AC5 — The resolved target is available for downstream analysis (10-2 seam).**
**Given** a user who has chosen a model,
**When** downstream code needs the target,
**Then** a scoped helper resolves the user's selection to concrete **target weights by asset class + the canonical buy-fund per class** (e.g. via `strategy/target_allocation.py` + the config); an undecided user resolves to `None`/absent (never a fabricated target). This is the ONLY contract Story 10-2 depends on.

**AC6 — Frontend: a calm Settings "Target mix" card + a set-or-decline prompt.**
**Given** the app,
**When** the user visits Settings,
**Then** a **"Your target mix"** card lets them pick one of the models — each shown with its plain-English description and its mix as simple percentages (e.g. "60% US stocks · 30% international · 10% bonds") — wired to `GET`/`PUT /api/target-allocation` (optimistic + fail-quiet, mirroring the Epic 9 Cash-setup card); **and** a calm, non-blocking, **dismissible** Dashboard prompt appears when no model is chosen, inviting them to pick one and linking to Settings, with dismissal persisted in `localStorage` (mirroring Epic 9's set-or-decline prompt). It disappears once a model is chosen.

**AC7 — Calm/honest voice + overlap honesty (NFR8).**
**Given** any new copy this story adds,
**When** it is shown,
**Then** it uses the beginner-friendly, non-alarmist voice with **no** FOMO/urgency/"red" language (same testable tone bar as the digest — the `FORBIDDEN` word list in `tests/test_digest_compose.py`); model descriptions explain the mix in plain English (e.g. "mostly bonds for a steadier ride"); and diversification is always framed by **asset class** (US vs. international vs. bonds) — the copy/model design never implies that two flavors of large-US (e.g. SCHB and an S&P-500 fund) diversify each other.

**AC8 — The full suite stays green; the new table provisions cleanly.**
**Given** the change set,
**When** the backend `pytest` and frontend `vitest` suites run,
**Then** all tests pass; the new table is created by `create_all` on any DB where it does not yet exist (a brand-new table needs no `ALTER` migration); and new tests cover the reference data (weights sum to exactly 1.00 for every model; symbol→class map; pure-helper determinism), the config model + set/undecided semantics + **scoped isolation** (user A cannot read/modify user B), the API (default undecided, set, invalid key → 422, money as fixed-point strings), and the frontend card + prompt (calm copy; prompt only when undecided; dismissal persists).

## Tasks / Subtasks

- [ ] **Task 1 — Model-portfolio reference data (AC: 1, 7)**
  - [ ] New `ballast/backend/strategy/target_allocation.py` mirroring `strategy/index_core.py`: an `AssetClass` enum/consts (`US_EQUITY`, `INTL_EQUITY`, `BONDS`); `SYMBOL_ASSET_CLASS: dict[str, AssetClass]` covering `INDEX_CORE_SYMBOLS` (US: VTI/ITOT/SCHB/VOO/IVV/SPY/SWPPX; Intl: VXUS/IXUS/VEU; Bonds: BND/AGG/BNDX/SCHZ; **note VT is whole-world — document it as a spans-classes special case, out of the pure map for v1, resolved in 10-2**); `CANONICAL_FUND: dict[AssetClass, str]` = {US: `VTI`, Intl: `VXUS`, Bonds: `BND`}; and `MODEL_PORTFOLIOS` with the LOCKED v1 weights (each summing to `Decimal("1.00")`):
    - `conservative` — US `0.30` / Intl `0.10` / Bonds `0.60` — "Mostly bonds for a steadier ride — smaller ups and downs."
    - `balanced` — US `0.45` / Intl `0.20` / Bonds `0.35` — "A middle path: a solid stock base with a real bond cushion."
    - `growth` — US `0.60` / Intl `0.30` / Bonds `0.10` — "Mostly stocks for long-term growth — expect bigger swings."
  - [ ] Pure helpers: `list_models()`, `get_model(key)` (unknown key → `None` or a caught error), `resolve_weights(key)` → weights-by-class + funds-by-class. Deterministic, no I/O (like `is_index_core`).
- [ ] **Task 2 — Per-user `TargetAllocationConfig` model (AC: 2, 3)**
  - [ ] Add `TargetAllocationConfig(OwnedEntityMixin, Base)` in `ballast/backend/db/models.py` (table `target_allocation_config`) mirroring `CashConfig`: `id` UUID pk; `model_key: Mapped[str | None] = mapped_column(String(32), nullable=True)`; `created_at`/`updated_at` tz-aware UTC; `__table_args__ = (UniqueConstraint("owner_id", name="uq_target_allocation_config_owner"),)`. No `db/migrations.py` entry needed (new table → `create_all` builds it in full).
- [ ] **Task 3 — Config helpers, scoped + fail-closed (AC: 2, 3, 5)**
  - [ ] New `ballast/backend/strategy/target_config.py` (or `allocation/config.py` — a new package with `__init__.py`) mirroring `cash/config.py`: `get_or_create_config` (create-on-first-read with `IntegrityError` lost-race handling, commit on create); `get_config` (READ-ONLY, no create — for the read path); `set_model(scope, session, key)` (validate `key` ∈ known model keys, else raise a caught `ValueError` → 422); `resolve_target(config)` (→ weights/funds by class, or `None` when undecided). All via `ScopedRepository(TargetAllocationConfig, scope, session)`.
- [ ] **Task 4 — Target-allocation API (AC: 4, 5, 7)**
  - [ ] New `ballast/backend/api/target_allocation.py` router (`prefix="/api/target-allocation"`) mirroring `api/cash.py`: `GET` → `{ model, choices[], resolved }`; `PUT` accepting `{ model }` (invalid → calm 422). Weights out as fixed-point strings via `WireMoney`. Auth via `get_scope`; session via `get_async_session`.
  - [ ] Register the router in `ballast/backend/api/app.py` (`app.include_router(target_allocation_router)`), alongside `cash_router`.
- [ ] **Task 5 — Frontend: Settings "Target mix" card (AC: 6, 7)**
  - [ ] `ballast/frontend/src/routes/Settings.jsx`: add a "Your target mix" card (mirror the Epic 9 `CashSetupCard`): a radio/select of the models, each with its plain-English description + mix percentages; wired to `GET`/`PUT /api/target-allocation` via `apiFetch`; optimistic + fail-quiet.
- [ ] **Task 6 — Frontend: Dashboard set-or-decline prompt (AC: 3, 6, 7)**
  - [ ] `ballast/frontend/src/routes/Dashboard.jsx`: a calm, non-blocking, dismissible prompt when `model === null`, inviting the user to pick a target mix and linking to Settings; dismissal persisted in `localStorage` (mirror the Epic 9 reserve prompt). Disappears once a model is chosen.
- [ ] **Task 7 — Tests (AC: 8, 7) — full suite must stay green**
  - [ ] Backend `ballast/backend/tests/test_target_allocation.py`: reference data (every model's weights sum to exactly `Decimal("1.00")`; symbol→class map correct; helper determinism); config default-undecided; set model; invalid key → 422; **scoped isolation** (A cannot read/modify B); GET/PUT round-trip with weights as fixed-point strings; new-user reads undecided.
  - [ ] Frontend `ballast/frontend/src/test/`: a `target-allocation` settings test (get/put, pick a model) and a Dashboard test (prompt appears only when undecided, is dismissible, disappears once chosen). Assert calm copy (reuse the digest FORBIDDEN-word discipline).

## Dev Notes

### The pattern to mirror: Epic 9's `CashConfig` (this story is its twin)
Story 10-1 is structurally **the same shape as Story 9-1** (a per-user owned config + reference data + API + a Settings card + a set-or-decline prompt). Mirror it almost 1:1 — the dev should read the Epic 9 files first and copy their structure:
- Owned config model: `CashConfig` in `ballast/backend/db/models.py` (`OwnedEntityMixin`, `UniqueConstraint("owner_id")`, tz-aware timestamps) [Source: ballast/backend/db/models.py#CashConfig].
- Config helpers: `ballast/backend/cash/config.py` — `get_or_create_config` (IntegrityError lost-race + commit), `get_config` (read-only, no create — use this on the read/prompt path so a GET never writes), `set_*` with validation → `ValueError` → 422, and a `resolve_*` helper [Source: ballast/backend/cash/config.py].
- API: `ballast/backend/api/cash.py` — authed `GET`/`PUT` funneling through `get_scope`, calm 422 on a config fault, `WireMoney` on the wire [Source: ballast/backend/api/cash.py]. Register the router in `api/app.py` next to `cash_router` [Source: ballast/backend/api/app.py (include_router calls)].
- Frontend card: the `CashSetupCard` inside `ballast/frontend/src/routes/Settings.jsx` (fetch on mount, optimistic PUT, fail-quiet) [Source: ballast/frontend/src/routes/Settings.jsx].
- Set-or-decline prompt: the reserve prompt in `ballast/frontend/src/routes/Dashboard.jsx` (shown when undecided, dismissal persisted in `localStorage`) [Source: ballast/frontend/src/routes/Dashboard.jsx].

### The reference-data pattern to mirror: `strategy/index_core.py`
The model-portfolio module is the twin of `index_core.py`: a curated, fixed, upper-cased, case-insensitive symbol set + pure classifier, living in `strategy/` because multiple consumers use it. Follow its shape (a frozen data structure + pure functions + a plain-English rationale string) [Source: ballast/backend/strategy/index_core.py]. Reuse `INDEX_CORE_SYMBOLS` as the universe the symbol→asset-class map must cover.

### Money & determinism conventions
Weights are `Decimal` (never float); each model's class weights sum to exactly `Decimal("1.00")` — assert this in a test for every model. On the wire, render weights as fixed-point strings via `WireMoney`/`format_money` [Source: ballast/backend/money.py]. The reference module and all helpers are pure/deterministic (no I/O, no wall-clock) so tests are trivial and the "resolved target" is reproducible.

### Fail-closed persistence (AD-10)
The new config is a per-user OWNED table routed through `ScopedRepository` (never a global) — a user can only ever read/write their own selection. Mirror the `CashConfig` scoped-isolation test exactly [Source: ballast/backend/db/repository.py#ScopedRepository, ballast/backend/tests/test_cash_config.py].

### Migrations
A brand-new table needs **no** `db/migrations.py` entry — `create_all` (`db.session.create_db_and_tables`) builds any missing table in full (including its `UniqueConstraint`); `create_all` runs before `run_startup_migrations` in the lifespan [Source: ballast/backend/api/app.py (lifespan), ballast/backend/db/migrations.py].

### Testing conventions & the live-link safety gotcha
- Backend: `pytest` with the function-scoped `client` fixture; conftest forces `LLM_ADAPTER=fake` + disables schedulers; a session-autouse guard refuses to run against a DB holding a live `brokerage_token` [Source: ballast/backend/tests/conftest.py]. Reuse the `_assert_calm`/FORBIDDEN-word tone check for AC7 [Source: ballast/backend/tests/test_digest_compose.py].
- ⚠️ **Do NOT run the suite against the dev DB `ballast`** — it holds MasterB's LIVE Schwab link and the suite DELETEs `brokerage_token`. Run against the disposable `ballast_test` DB: `DATABASE_URL=postgresql://ballast:ballast@localhost:5432/ballast_test BALLAST_ALLOW_DIRTY_BROKERAGE_DB=1 uv run pytest -q` (from `ballast/backend`). `ballast_test` already exists.
- Frontend: `vitest` + `@testing-library/react`; mirror the Epic 9 `cash-config.test.jsx` + `dashboard.test.jsx` (mock `apiFetch`/`fetch`, route by URL substring; clear `localStorage` in `afterEach` because the prompt persists dismissal). Run `npm test` in `ballast/frontend`.

### Project Structure Notes
- New: `ballast/backend/strategy/target_allocation.py` (reference data), the target-config helpers module + `__init__.py`, `ballast/backend/api/target_allocation.py`, `ballast/backend/tests/test_target_allocation.py`; new frontend test(s) under `ballast/frontend/src/test/`.
- Modified: `ballast/backend/db/models.py` (add `TargetAllocationConfig`), `ballast/backend/api/app.py` (register router), `ballast/frontend/src/routes/Settings.jsx` (Target-mix card), `ballast/frontend/src/routes/Dashboard.jsx` (prompt).
- Naming/placement mirrors the Epic 9 cash feature and `strategy/index_core.py` — no structural variance. The config is a per-user OWNED table routed through `ScopedRepository` (AD-10); the model portfolios are GLOBAL reference data (like `index_core`), not per-user.

### References
- [Source: _bmad-output/brainstorming/brainstorm-allocation-coach-deploy-my-cash-2026-08-11/brainstorm-intent.md] — locked decisions, MVP scope, the model-portfolio decision, guardrails.
- [Source: _bmad-output/planning-artifacts/epics.md#Epic 10 → Story 10.1] — the drafted epic ACs.
- [Source: ballast/backend/strategy/index_core.py] — the reference-data pattern (curated set + pure classifier + rationale).
- [Source: ballast/backend/db/models.py#CashConfig] — the per-user owned-config model to mirror.
- [Source: ballast/backend/cash/config.py] — get_or_create / get (read-only) / set-with-validation / resolve helpers to mirror.
- [Source: ballast/backend/api/cash.py] — the authed GET/PUT + calm-422 endpoint to mirror; register in `api/app.py` next to `cash_router`.
- [Source: ballast/backend/money.py] — `WireMoney`/`format_money` money-on-the-wire contract.
- [Source: ballast/backend/db/repository.py#ScopedRepository] — fail-closed per-user persistence funnel (AD-10).
- [Source: ballast/frontend/src/routes/Settings.jsx#CashSetupCard, src/routes/Dashboard.jsx] — the Settings card + set-or-decline prompt to mirror.
- [Source: ballast/backend/tests/test_cash_config.py, ballast/frontend/src/test/cash-config.test.jsx, dashboard.test.jsx] — the test patterns (scoped isolation, calm copy, prompt) to mirror.
- [Source: ballast/backend/tests/conftest.py, tests/test_digest_compose.py] — test harness + calm-copy tone check + the live-link DB guard.

## Dev Agent Record

### Agent Model Used

### Debug Log References

### Completion Notes List

### File List
