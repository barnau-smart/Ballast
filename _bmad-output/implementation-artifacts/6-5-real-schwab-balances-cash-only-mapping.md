---
title: 'Story 6.5 — Real Schwab Balances & Cash-Only Mapping (AD-14)'
type: 'feature'
created: '2026-08-01'
status: 'done'
baseline_revision: '6c5b50a7e4f447ac2e2014122e481ab862342e65'
final_revision: '8bd4c8f5ae43dfb754f8138fb552371c8df2a89d'
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-6-context.md'
warnings: [oversized]
---

<intent-contract>

## Intent

**Problem:** Idle cash is denormalized onto `portfolio_cache` HOLDING rows, so an all-cash / cash-heavy account (few or zero holdings, zero rows) surfaces `cash = Decimal("0")` — the users with the MOST idle cash see a confidently false "you have no idle cash" from the missed-growth meter, and the FR11 oversized-lump warning has no real portfolio value to measure against (the AD-14 cash-only gap). Compounding it, real Schwab cash never actually flows: `SchwabAdapter.fetch_portfolio()` still raises "not wired," and the read/refresh path builds a tokenless Schwab adapter, so even wired it could not authenticate a read.

**Approach:** Introduce a dedicated per-user balances source — a new `portfolio_balance` table (one row per user: `cash`, `as_of`) written by the same single-writer projection — and read idle cash from it (never from a holdings row), so cash survives when holdings is empty. Then wire `SchwabAdapter.fetch_portfolio()` to map Schwab's `get_account` positions + `currentBalances.cashBalance` into the `PortfolioSnapshot`, and bind the user's decrypted token on the read/refresh + link-import path so real balances actually reconcile. The `PortfolioView` contract (`holdings`, `cash`, `as_of`) is UNCHANGED, so every downstream consumer (dashboard, missed-growth, oversized-lump) needs no change.

## Boundaries & Constraints

**Always:**
- Idle cash and `as_of` are READ from the dedicated `portfolio_balance` row — never derived from a `portfolio_cache` holdings row. A cash-only account (cash > 0, zero holdings) reports its true cash.
- `reconcile_portfolio` stays THE single writer (AD-14). It writes the balance row and the holdings rows in ONE commit. Reconcile-wins now keys on the persisted `portfolio_balance.as_of` (present even for cash-only accounts, so a stale re-fetch never clobbers newer cash truth); a strictly-newer snapshot supersedes, an equal/older one leaves the cache untouched.
- All money stays `Decimal`, never binary float; `as_of` is tz-aware UTC. All access is via the fail-closed `ScopedRepository` (AD-10) — a reconcile only ever touches this user's own balance + cache.
- `PortfolioView` keeps its exact shape/fields (`holdings`, `cash`, `as_of`, `is_empty`) so no consumer changes. `PortfolioOut.cash` stays fixed-point `WireMoney` on the wire.
- `SchwabAdapter.fetch_portfolio()` sources cash from a DEDICATED balances field (`securitiesAccount.currentBalances.cashBalance`), not inferred from positions. Parsing is defensive (missing → 0 / skip). On any transport/parse failure it RAISES a clear typed error — it NEVER returns an empty/partial snapshot (which would reconcile the cache to nothing). Never logs token/secret material; schwab-py stays lazily imported (AD-8).
- The Schwab read/refresh + link-import path binds THIS user's decrypted-in-memory token (the `get_execution_broker` decrypt pattern). The fake adapter passes through untouched — the whole default/tested path stays credential-free and offline.

**Block If:**
- The story's intent cannot be met without changing the `PortfolioView` public shape or a downstream consumer's contract (it should not — sourcing cash from the new table is internal to `brokers.portfolio`).

**Never:**
- Do not change the `missed_growth` engine, the coach FR11 oversized-lump calculation, `PortfolioOut`/`HoldingOut` schemas, or any wire field name/type — they consume `PortfolioView.cash` and stay untouched.
- Do not make a real Schwab network call in any test (credential-gated, mocked-client only, zero network — mirror `test_schwab_adapter.py`). Do not commit real credentials or fixtures with secrets.
- Do not introduce `float` anywhere; do not add a second cash source or read cash from holdings rows anywhere.
- Do not add graceful degraded-mode UX for a Schwab READ failure on `/refresh` (out of scope; note it) — just ensure `fetch_portfolio` fails loud rather than silently emptying the cache.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| All-cash reconcile | snapshot `cash=Decimal("500.00")`, `holdings=[]`, `as_of=T` | `portfolio_balance` row upserted (`cash=500.00`, `as_of=T`); zero cache rows; `get_portfolio().cash == 500.00`, `holdings == []`, `as_of == T` | No error expected |
| Cash-heavy + holdings | `cash=1000`, 2 holdings, `as_of=T` | balance row `cash=1000`; 2 cache rows; view cash=1000, holdings len 2 | No error expected |
| Stale re-fetch (cash-only) | cached balance `as_of=T`; incoming `as_of=T-1h`, different cash | cache untouched; view cash unchanged (staleness now protects cash-only too) | No error expected (skip logged) |
| Never imported | no balance row, no cache rows | `get_portfolio().cash == Decimal("0")`, `as_of is None`, `holdings == []` | No error expected |
| Schwab all-cash fetch | `get_account` JSON: `positions` absent, `currentBalances.cashBalance=750.25` | `PortfolioSnapshot(cash=Decimal("750.25"), holdings=[])`, `as_of` = now(UTC) | No error expected |
| Schwab mixed fetch | positions=[{instrument.symbol:"VTI", longQuantity:10, marketValue:2500}], cashBalance=100 | snapshot: 1 Holding(VTI, qty 10, mv 2500), cash 100 | No error expected |
| Schwab malformed body | `get_account` returns non-dict / transport error | `fetch_portfolio` RAISES a clear typed error (no empty snapshot) | Raise; reconcile propagates (callback logs+survives) |
| Schwab tokenless read | Schwab adapter with no bound token on refresh | refuses loudly (`SchwabNotConfiguredError`) — never a silent empty import | Raise |

</intent-contract>

## Code Map

- `ballast/backend/db/models.py` -- ADD `PortfolioBalance(OwnedEntityMixin, Base)` (table `portfolio_balance`): `cash Numeric(20,2) NOT NULL`, `as_of DateTime(timezone=True) NOT NULL`, `id` UUID PK; one logical row per user (owner_id via mixin). Update `PortfolioCache` docstring: the per-row `cash` is a denormalized snapshot copy retained for schema-additivity; the AUTHORITATIVE idle-cash source is now `portfolio_balance` (AD-14 closed). Do NOT drop/alter the `cash` column (keeps the change additive — `create_all` never ALTERs).
- `ballast/backend/brokers/portfolio.py` -- the single writer/reader. `reconcile_portfolio`: read the existing `PortfolioBalance` row for reconcile-wins (`cached_as_of` from it, not from holdings); on a strictly-newer snapshot, upsert the balance row (`cash`, `as_of`) and atomically replace holdings, one commit. `get_portfolio` + `_to_view`: source `cash`/`as_of` from the `PortfolioBalance` row (via a scoped read), holdings from `PortfolioCache`; keep `PortfolioView` shape identical. Keep writing `cash=snap.cash` on holding rows (harmless, no schema churn).
- `ballast/backend/brokers/schwab_adapter/adapter.py` -- wire `fetch_portfolio()`: build client (`_trading_client`), resolve `_account_hash`, call `client.get_account(account_hash, fields=client.Account.Fields.POSITIONS)`; map `securitiesAccount.currentBalances.cashBalance` → cash and `securitiesAccount.positions[]` → `Holding` (symbol from `instrument.symbol`, `longQuantity`, `marketValue`, cost_basis from `averagePrice`×qty when present else `None`), reusing `_decimal_or_zero`; `as_of = datetime.now(timezone.utc)`. Defensive (missing → 0 / skip position with no symbol). On transport/parse failure RAISE (never return empty). Never log secrets.
- `ballast/backend/brokers/factory.py` -- add `get_reading_broker` dependency (mirror `get_execution_broker`: bind THIS user's decrypted token for a Schwab adapter, pass fakes through) with a neutral message; extract the shared token-binding into a small helper reused by both. Also expose a helper to build a token-bound Schwab adapter from freshly-exchanged `BrokerTokens` (for the callback import).
- `ballast/backend/api/portfolio.py` -- `refresh_portfolio`: depend on `get_reading_broker` (was `get_broker`) so a Schwab refresh authenticates; fake unchanged.
- `ballast/backend/api/brokerage.py` -- link callback import-on-connect: reconcile with a token-bound broker for Schwab (bind the just-exchanged in-memory `tokens`); fake path unchanged; keep the "import failure never breaks the link" guard.
- `ballast/backend/tests/test_portfolio.py` -- add cash-only reconcile + read tests (cash survives zero holdings), stale cash-only re-fetch, never-imported → cash 0; keep existing holdings assertions green (read cash via view/endpoint, not a cache row's column).
- `ballast/backend/tests/test_schwab_adapter.py` -- add `fetch_portfolio` mocked-client tests: all-cash (positions absent, cashBalance>0), mixed positions+cash, malformed body raises. Zero network.
- `ballast/backend/tests/test_missed_growth_endpoint.py` -- add: an all-cash account now yields a real idle-cash figure (meter no longer returns `no_idle_cash`), proving the AD-14 close end-to-end through `get_portfolio`.

## Tasks & Acceptance

**Execution:**
- [x] `ballast/backend/db/models.py` -- add `PortfolioBalance` model; update `PortfolioCache` docstring (cash now sourced from `portfolio_balance`).
- [x] `ballast/backend/brokers/portfolio.py` -- upsert/read the dedicated balance row; reconcile-wins keyed on `portfolio_balance.as_of`; `PortfolioView` shape unchanged.
- [x] `ballast/backend/brokers/schwab_adapter/adapter.py` -- wire `fetch_portfolio()` (positions + `currentBalances.cashBalance`), defensive, raise-not-empty on failure.
- [x] `ballast/backend/brokers/factory.py` -- `get_reading_broker` + token-bound-from-`BrokerTokens` helper (shared decrypt with `get_execution_broker`).
- [x] `ballast/backend/api/portfolio.py` -- `refresh_portfolio` uses `get_reading_broker`.
- [x] `ballast/backend/api/brokerage.py` -- callback import binds the Schwab token; fake unchanged; link survives import failure.
- [x] `ballast/backend/tests/test_portfolio.py` + `tests/test_schwab_adapter.py` + `tests/test_missed_growth_endpoint.py` -- cover the I/O matrix (cash-only survival, Schwab mapping, end-to-end meter truthfulness). Ensure new tables via `PortfolioBalance.__table__.create(checkfirst=True)` in the table-setup fixtures (carried-over-DB pattern).

**Acceptance Criteria:**
- Given a reconcile with `cash > 0` and zero holdings, when `get_portfolio` is read, then `cash` equals the snapshot cash (not `0`) and `holdings` is empty — the cash-only account reports its true cash (AD-14 closed).
- Given the missed-growth endpoint for an all-cash account, when it is called after a reconcile, then it returns a real idle-cash figure and no longer reports "no idle cash".
- Given `BROKER_ADAPTER=schwab` with a mocked `get_account` returning positions + `currentBalances.cashBalance`, when `fetch_portfolio` runs, then it returns a `PortfolioSnapshot` with cash from the balances field and holdings from positions; a malformed/failed response RAISES rather than returning an empty snapshot.
- Given the full suite, when it runs, then it passes with zero network and zero credentials, `PortfolioView`/`PortfolioOut` shapes unchanged, and no `float` on any money path.

## Review Triage Log

### 2026-08-01 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 3: (high 0, medium 1, low 2)
- defer: 5
- reject: 9
- addressed_findings:
  - `[medium]` `[patch]` Both reviewers found `fetch_portfolio` violated its own stated FAILURE CONTRACT: `body.get("securitiesAccount") or {}` (plus no `resp.is_error` check) let a valid-JSON error body / missing-envelope response parse into `cash=0, holdings=[]` — a silent EMPTY snapshot that reconciles a real user's cache to nothing (the exact catastrophe the story exists to prevent), and a raw `httpx`/SDK exception could leak past the port (AD-8). Added `SchwabReadError`; `fetch_portfolio` now raises on `resp.is_error`, a non-dict body, or an absent `securitiesAccount` envelope (missing sub-fields inside a valid envelope are still tolerated → cash 0), and wraps any raw transport/parse error as `SchwabReadError`. Updated the transport/malformed tests to expect the typed error and added error-status + missing-envelope raise tests.
  - `[low]` `[patch]` A short / closed / zero-long Schwab position (`longQuantity` 0 or reported only under `shortQuantity`) was emitted as a junk `Holding` (0 shares, non-zero/negative market value, bogus 0 cost basis). Added `if quantity <= 0: continue` (v1 is long-only broad index funds); added a skip test.
  - `[low]` `[patch]` `PortfolioBalance`'s "exactly one row per user" invariant was enforced only by the writer's upsert logic (a concurrent double-insert could silently duplicate and strand a stale-cash row). Added `UniqueConstraint("owner_id", name="uq_portfolio_balance_owner")` (same discipline as `DigestPreference`) so a duplicate becomes a loud IntegrityError.
- notes: Deferred (5) — all real but not caused by this change / credential-gated go-live hardening, logged to `deferred-work.md`: (1) `fetch_portfolio` is a SYNC blocking network call inside the async handlers (event-loop stall); (2) real-path `as_of = now(UTC)` makes reconcile-wins staleness protection inert for real reads; (3) the two-table reconcile is a lock-free read-modify-write (TOCTOU; ties to the recorded Epic 4 atomicity gap; the new unique index is a partial mitigation); (4) re-link doesn't clear the portfolio projection, so a re-link to a different account can show the prior account's cash under staleness skip; (5) `get_reading_broker` shares the `_bind_user_token` decrypt path, so an undecryptable token surfaces a raw 500 on `/refresh` (read-path twin of the already-logged 6.3 `get_execution_broker` decrypt gap). Rejected (9) — the `fields` enum→string coupling (fixture-invented shape already flagged for the go-live re-confirmation the spec documents); multi-account first-account-only read (locked v1 decision, consistent with `place_order`); `Numeric(20,2)` cash rounding (intentional 2dp money, matches the existing `portfolio_cache.cash` scale); negative/NaN `cashBalance` handling (v1 cash/long-only; `_decimal_or_zero`'s finite-guard is consistent); the transition-DB "holdings rows but no balance row" one-time artifact (no pre-launch data; balance is intentionally the authoritative reconcile key); factory tz-normalization asymmetry (`.timestamp()` is correct for any aware datetime); the redundant `written` list and the untyped `tokens` param (cosmetic); and the create_all/no-Alembic note (consistent with the project's stated posture).

### 2026-08-01 — Review pass (follow-up)
- intent_gap: 0
- bad_spec: 0
- patch: 0
- defer: 1
- reject: 24
- addressed_findings:
  - none
- notes: Independent follow-up review (fresh Blind Hunter + Edge Case Hunter) on the finalized `done` story; NO code changes. Deferred (1) — NEW ledger entry: the now-vestigial `portfolio_cache.cash` column is still written on every holding row but no longer read; the spec's Design Notes explicitly flagged dropping it as "a deferred cleanup (log to deferred-work)" and the prior pass never logged it. Rejected (24) — the bulk resolved to items ALREADY deferred by the first pass (the two-table lock-free read-modify-write / concurrent-`IntegrityError`, the real-path `as_of=now(UTC)` inert-staleness, the `_bind_user_token` decrypt→raw-500 on `/refresh`), or ALREADY rejected there (intentional 2-dp `Numeric(20,2)` money incl. cost-basis; negative/NaN `cashBalance` via `_decimal_or_zero`'s finite guard; the `create_all`/no-Alembic + transition-DB "holdings rows, no balance row" one-time artifact; the guessed Schwab token/positions envelope covered by the documented go-live re-confirmation; long-only v1 short/zero-position skip; cosmetic duplication/dead defensive `max`). Verified NOT reachable: the `expires_at`-is-`None`/mixed-tz token-binding crash paths (`BrokerageToken.expires_at` is `nullable=False` on a tz-aware column, and the pattern is pre-existing from 6.3), `snap.cash is None` (contract-typed `Decimal`, `_decimal_or_zero` never returns `None`). Contradicted by spec: the "tokenless `/refresh` should degrade" finding — the I/O matrix mandates it refuse loudly with `SchwabNotConfiguredError`.

## Design Notes

- **Why a dedicated table, not a sentinel cash row.** A single-table cache physically needs ≥1 row to carry account-level cash; a sentinel holding row would pollute `holdings` (dashboard/oversized-lump) and need filtering everywhere. A one-row-per-user `portfolio_balance` is the literal "dedicated balances source (not inferred from a holdings row)" the architecture calls for, and it lets reconcile-wins protect cash-only accounts (the balance row's `as_of` is always present, unlike zero holdings rows).
- **Schema stays additive.** `create_all` never ALTERs an existing table, so we ADD `portfolio_balance` (picked up on fresh create; `Table.create(checkfirst=True)` reconciles carried-over test DBs) and DO NOT touch `portfolio_cache`'s columns. Keeping (and still writing) the now-vestigial `portfolio_cache.cash` avoids a nullable ALTER; dropping it is a deferred cleanup (log to deferred-work).
- **`PortfolioView` is the seam that stays fixed.** Sourcing cash internally from `portfolio_balance` while keeping `PortfolioView(holdings, cash, as_of)` identical means missed-growth, the coach oversized-lump check, and the dashboard need zero changes — the fix is contained to `brokers.portfolio` + the model + the adapter.
- **Schwab mapping mirrors 6.3's fake-first discipline.** Field names (`cashBalance`, `positions[].instrument.symbol/longQuantity/marketValue/averagePrice`) are documented-shape + fixture-driven and re-confirmed at go-live, exactly like `place_order`'s quote/order parsing. `fetch_portfolio` is a READ (no double-place risk), but it must RAISE on failure — a returned empty snapshot would reconcile the cache to nothing.
- **Go-live manual check:** with real `SCHWAB_*` creds + `BROKER_ADAPTER=schwab`, link an account and hit `POST /api/portfolio/refresh`; confirm real cash + positions import and the missed-growth meter reflects true idle cash. The exact balances/positions JSON is re-confirmed here.

## Verification

**Commands:**
- `cd ballast/backend && uv run python -m pytest tests/test_portfolio.py tests/test_schwab_adapter.py tests/test_missed_growth_endpoint.py -q` -- expected: all pass; cash-only accounts report true cash; Schwab `fetch_portfolio` mapping + raise-on-failure covered.
- `cd ballast/backend && uv run python -m pytest -q` -- expected: full suite green (no regressions; `PortfolioView`/wire shapes unchanged; zero network/credentials).

## Auto Run Result

Status: done (independent follow-up review pass on the finalized `done` story)

**Summary:** Ran a fresh adversarial review (Blind Hunter + Edge Case Hunter, same model capability) against the full since-baseline diff for Story 6.5. This pass made NO code changes — the implementation was already hardened by the first review pass (which added `SchwabReadError` + the fail-loud contract, the long-only skip, and the `uq_portfolio_balance_owner` unique constraint). The follow-up surfaced 27 raw findings that deduplicated to 25 distinct: 1 newly deferred, 24 rejected, 0 patches, 0 spec repairs.

**Files changed this pass:** none (source unchanged). Only workflow artifacts updated:
- `6-5-real-schwab-balances-cash-only-mapping.md` — appended the follow-up triage-log entry; set `followup_review_recommended: false`; refreshed `status`/`final_revision`.
- `deferred-work.md` — appended ONE new entry (vestigial `portfolio_cache.cash` cleanup).

**Review findings breakdown:**
- Patches applied: 0.
- Deferred (1, NEW): the now-vestigial `portfolio_cache.cash` column is still written per holding row but never read — the spec's Design Notes explicitly flagged dropping it as a deferred cleanup to log, which the prior pass omitted. Low consequence (both tables written from the same `snap.cash` in one commit, so no drift at rest), captured for a future schema-cleanup migration.
- Rejected (24): the bulk collapsed onto items ALREADY deferred by the first pass (two-table lock-free read-modify-write / concurrent `IntegrityError`; real-path `as_of=now(UTC)` inert staleness; `_bind_user_token` decrypt→raw-500 on `/refresh`) or ALREADY rejected there (intentional 2-dp `Numeric(20,2)` money incl. cost basis; negative/NaN `cashBalance`; `create_all`/no-Alembic + transition-DB one-time artifact; guessed Schwab envelope under the documented go-live re-confirmation; long-only short/zero-position skip; cosmetic duplication / dead defensive `max`). Verified NOT reachable: the `expires_at`-`None`/mixed-tz token-binding crashes (`BrokerageToken.expires_at` is `nullable=False`, tz-aware, pattern pre-existing from 6.3) and `snap.cash is None` (contract `Decimal`; `_decimal_or_zero` never returns `None`). One finding ("tokenless `/refresh` should degrade") was contradicted by the spec's I/O matrix, which mandates a loud `SchwabNotConfiguredError`.

**Verification performed:**
- `uv run python -m pytest tests/test_portfolio.py tests/test_schwab_adapter.py tests/test_missed_growth_endpoint.py -q` → **56 passed**.
- `uv run python -m pytest -q` → **371 passed** (full suite green; zero network/credentials; `PortfolioView`/wire shapes unchanged).

**Follow-up review recommendation:** `false` — this pass made no review-driven code changes; it only logged one low-consequence deferred cleanup and re-confirmed the suite is green.

**Residual risks:** All go-live hardening items for Story 6.5 remain captured in `deferred-work.md` (blocking-network sync fetch on the event loop; inert real-path staleness; two-table TOCTOU; re-link projection hygiene; decrypt→409 calm-envelope; the new vestigial-column cleanup). None are reachable on the current credential-gated/offline path.

