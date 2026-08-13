# Story 10.10: Margin-account detect & gently warn

Status: done
baseline_commit: 4c1bb0a

<!-- HARD GATE (docs/dev-loop-policy.md, per-story-spec-approval): APPROVED by MasterB
     2026-08-13. Product knobs locked: (1) the calm margin note shows on the DEPLOY COACH
     CARD only (highest context, least noise); (2) DISMISSIBLE one-time (client-persisted,
     like the set-or-decline prompts) — calm/non-nagging. Read-only/informational (no order
     placement, no deploy-math change) but touches the Schwab read path + a DB column +
     user-facing safety copy → money-path-adjacent: mandatory independent review before merge. -->

## Story

As a conservative investor whose Schwab account happens to be a margin account,
I want Ballast to notice that and gently tell me,
so that I understand Ballast only ever deploys my settled cash — never my margin buying power — and I'm never surprised by borrowing.

## Context

`docs/real-money-readiness.md` (Gate 1, resolved 2026-08-13): MasterB's real account is a
**margin** account. Ballast is *already* margin-safe on the money math — the engine anchors
deployable cash to `securitiesAccount.currentBalances.cashBalance` (never the
`availableFunds`/`availableFundsNonMarginableTrade` buying-power figures), clamps negative
cash to 0, and Story 10-9 refuses at execution any BUY beyond real settled cash. So this
story changes **no money behavior** — it's the honest, calming *disclosure* the readiness
doc calls for: "detect + gently warn about a margin account rather than silently treat its
cash as deployable." Ballast is designed for cash-account investors; a margin account is a
foot-gun we should name, not hide.

## Design — RECOMMENDED (needs MasterB approval)

### Detect (backend, read-only)
- The Schwab account body already read in `brokers/schwab_adapter/adapter.py:fetch_portfolio`
  carries `securitiesAccount.type` (`"MARGIN"` / `"CASH"`). Read it into a normalized
  `account_type: str | None` (upper-cased; `None` when absent/unknown — fail-open to no warning,
  never a false alarm).
- Thread it through `PortfolioSnapshot` (new `account_type: str | None = None` field,
  `brokers/port.py`) → the single writer `brokers/portfolio.py:reconcile_portfolio` persists it
  on the one `portfolio_balance` row → `PortfolioView` carries `account_type` (read-only) →
  exposed on the plan / a small read endpoint for the frontend.
- **Fake adapter** returns `account_type="CASH"` (or `None`) so dev/demo/tests never warn — the
  warning is real-broker-only by construction. No `BROKER_ADAPTER=schwab` needed to test the
  wiring (inject the snapshot, as the existing reconcile tests do).

### Persist (additive, idempotent)
- New **nullable** `account_type VARCHAR` column on `portfolio_balance` via
  `db/migrations.py` (`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, additive — the carried-over-DB
  convention). A user who never re-imports simply has `NULL` → no warning (fail-open).

### Warn (frontend, calm, informational)
- When `account_type == "MARGIN"`, show a **calm, non-alarmist** note (no red, no FOMO/warning
  words — respects the Epic-10 calm-copy bar): e.g. *"This looks like a margin account. Ballast
  only ever deploys the settled cash in your account and never buys on margin — your buying power
  isn't treated as spendable cash."* No behavior change, no blocking, purely informational.
- Placement + dismissibility are the two product knobs below.

### Rejected
- **Blocking or refusing to serve a margin account** — overreach; Ballast is already margin-safe
  and the user may knowingly hold a margin account. Inform, don't gate.
- **Inferring margin from the buying-power figures** (`availableFunds` > `cashBalance`) — brittle;
  use the explicit `securitiesAccount.type` instead.
- **Warning on `CASH`/unknown** — no false alarms; only an explicit `MARGIN` warns.

## Acceptance Criteria (finalize at approval)

1. The Schwab adapter reads `securitiesAccount.type` into a normalized `account_type` on
   `PortfolioSnapshot` (`"MARGIN"`/`"CASH"`/`None`); a missing/blank/non-string type → `None`
   (never a false "margin"). No money-math field changes (`cashBalance` anchor unchanged).
2. The single writer persists `account_type` on the `portfolio_balance` row (additive nullable
   column, idempotent migration); `PortfolioView` exposes it read-only; a user with no balance row
   or `NULL` type surfaces no warning (fail-open).
3. The frontend shows the calm margin note ONLY when `account_type == "MARGIN"` — never on
   `CASH`/`NULL`/unknown; the note is calm-copy-clean (no red / FOMO / alarm words) and changes no
   behavior (no block, nothing auto-submitted).
4. The fake broker reports a non-margin account by default, so dev/demo/tests never warn; the
   detect→persist→surface path is testable behind the fake broker (injected snapshot).
5. Per-user scoped (AD-10); the account-type read is read-only; no cross-user leakage.

## Tasks / Subtasks

- [x] Task 1 — Detect: `fetch_portfolio` reads `securitiesAccount.type` → `PortfolioSnapshot.account_type` (normalized upper; missing/blank/non-str → `None`); fake adapter default `"CASH"`.
- [x] Task 2 — Persist: additive nullable `portfolio_balance.account_type` migration (`ADD COLUMN IF NOT EXISTS`); `reconcile_portfolio` writes it on both the UPDATE and first-INSERT paths; `PortfolioView` exposes it; the `Plan`/`PlanOut` (`/api/allocation/plan`) carries it on every status.
- [x] Task 3 — Warn: calm, one-time-**dismissible** `MarginAccountNote` on the DEPLOY COACH CARD (both the deploy + no-action result blocks), gated on `account_type === "MARGIN"`; localStorage-persisted dismissal (mirrors the Dashboard set-or-decline prompts); calm CSS (no red/alarm).
- [x] Task 4 — Tests: adapter type-parse (margin/cash/missing→None/blank→None); reconcile persists + view exposes (+None stays None, +update, never-imported None, fake=CASH); `/plan` exposes MARGIN + null; frontend shows on MARGIN + dismiss persists, hidden on CASH. Backend 877 + frontend 201 green.

## Dev Notes

- `brokers/schwab_adapter/adapter.py:fetch_portfolio` (~:262-280) — `acct = body["securitiesAccount"]`; add `account_type = _normalize(acct.get("type"))` beside the existing `cashBalance` read.
- `brokers/port.py:PortfolioSnapshot` — add `account_type: str | None = None`.
- `brokers/portfolio.py:reconcile_portfolio` + `PortfolioView` — carry/persist the field (single writer, AD-14).
- `db/migrations.py` — additive `ADD COLUMN IF NOT EXISTS account_type` on `portfolio_balance`.
- `brokers/fake_adapter.py:fetch_portfolio` — default a non-margin type.
- Frontend: the deploy card / dashboard (per approval), reusing the existing calm-note styling.
- Testing: `ballast_test` DB; inject a `PortfolioSnapshot(account_type="MARGIN")` (mirror `test_recoverable_placement` reconcile tests) — no real broker needed.

### References
- [Source: docs/real-money-readiness.md#Gate 1 — deployable cash] — the margin finding + the detect-and-warn follow-up.
- [Source: ballast/backend/brokers/schwab_adapter/adapter.py#fetch_portfolio] · [Source: ballast/backend/db/models.py#PortfolioBalance] · [Source: ballast/backend/brokers/portfolio.py#reconcile_portfolio]
- [Source: docs/dev-loop-policy.md] — per-story spec-approval + independent review before merge.

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (bmad-dev-story, in-chat)

### Completion Notes List

- **Detect:** `brokers/schwab_adapter/adapter.py:fetch_portfolio` reads `securitiesAccount.type`, normalized upper; missing/blank/non-string → `None` (no false margin). Cash anchor (`cashBalance`) unchanged.
- **Snapshot/view/persist:** `PortfolioSnapshot.account_type` + `PortfolioView.account_type` (additive, defaulted — fixed shape preserved); `db/models.py` nullable `portfolio_balance.account_type`; `db/migrations.py` additive `ADD COLUMN IF NOT EXISTS`; `reconcile_portfolio` writes it on both the conditional-UPDATE and first-INSERT paths (single writer, AD-14).
- **Expose:** `Plan.account_type` (populated from `view.account_type` on all 5 statuses) → `PlanOut.account_type` on `/api/allocation/plan`.
- **Fake broker:** returns `account_type="CASH"` so dev/demo/tests never warn (real-broker-only by construction).
- **Frontend:** `MarginAccountNote` on the deploy coach card — calm, informational, one-time-dismissible (localStorage `ballast.marginNoteDismissed`); gated on `account_type === "MARGIN"`.
- **No money-math change:** the deploy engine still anchors to `cashBalance`; account_type is informational only. `followup_review_recommended: true` — money-path-adjacent; mandatory independent review before merge.

### File List

- `ballast/backend/brokers/port.py` — `PortfolioSnapshot.account_type`.
- `ballast/backend/brokers/schwab_adapter/adapter.py` — read + normalize `securitiesAccount.type`.
- `ballast/backend/brokers/fake_adapter.py` — default `account_type="CASH"`.
- `ballast/backend/brokers/portfolio.py` — `PortfolioView.account_type`; `_to_view`; reconcile UPDATE + INSERT write it.
- `ballast/backend/db/models.py` — nullable `portfolio_balance.account_type`.
- `ballast/backend/db/migrations.py` — additive `add_portfolio_balance_account_type`.
- `ballast/backend/allocation/engine.py` — `Plan.account_type` + populated on every return.
- `ballast/backend/api/allocation.py` — `PlanOut.account_type` + mapping.
- `ballast/frontend/src/components/CoachConsult.jsx` — `MarginAccountNote` (dismissible) rendered on the deploy card.
- `ballast/frontend/src/components/CoachConsult.css` — calm margin-note style.
- Tests: `tests/test_schwab_adapter.py` (+4), `tests/test_portfolio.py` (+5), `tests/test_allocation_engine.py` (+2), `frontend/src/test/deploy-cash.test.jsx` (+2).

## Change Log

- 2026-08-13 — Story 10.10 implemented: detect a MARGIN Schwab account (`securitiesAccount.type`) and gently, one-time-dismissibly warn on the deploy coach card that Ballast deploys only settled cash and never buys on margin. Read-only/informational — no money-math change (engine still anchors to `cashBalance`). Additive nullable `portfolio_balance.account_type` column + idempotent migration; threaded snapshot→view→plan→API→frontend. Backend 877 + frontend 201 green. Money-path-adjacent → awaiting mandatory independent review before merge.

## Independent Review (2026-08-13) — MERGE-READY

Two fresh-context reviewers (Blind Hunter + Edge-Case/Acceptance). Both: **MERGE-READY, no
Critical/High.** The cardinal safety property is verified — `account_type` NEVER feeds any
number (cash still anchors to `cashBalance`; no deploy/investable math touched); the Schwab
normalization guard (`isinstance(str) and .strip()`) is airtight so a good read can never
become a cache-wiping FAILED read; written on both reconcile write paths; per-user scoped;
frontend gated strictly on `=== "MARGIN"` with fail-safe localStorage and no XSS; all 5 ACs
MET with code+test evidence; Plan carries account_type on all 5 statuses.

- [x] [Review][Low] cosmetic: `engine.py` final deploy return had `as_of` over-indented after
  the scripted `account_type` insert — **FIXED** (re-aligned to 8 spaces).
- [Review][Low][defer] a strictly-newer reconcile with `account_type=None` nulls a prior
  "MARGIN" → fail-open (no warning), benign by design; the real Schwab always reports `type`.
  Noted, not changed.
- [Review][Low][defer] `allocation/review.py`'s internal `PortfolioView` aggregation helper
  omits `account_type` (defaults None) — not the plan→PlanOut path, no margin note lost, no
  money math affected. Noted, not changed.
