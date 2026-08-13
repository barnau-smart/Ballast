# Story 10.9: Backend cash-cover safety — a BUY never places on margin

Status: done
baseline_commit: e21a5a1
independent_review: 2026-08-13 (3-layer, MERGE-READY — no Critical/High; 2 residuals → deferred-work.md)

<!-- HARD GATE (docs/dev-loop-policy.md, per-story-spec-approval): APPROVED by MasterB
     2026-08-13. Money-path → mandatory independent review before merge. Locked design:
     (D1) the coverage gate lives INSIDE execute_approved_order (the sole broker-place
     caller) so no path can bypass it; (D2) coverage base = real SETTLED cash only
     (max(0, view.cash)) — parked money-market does NOT count and the reserve is NOT
     subtracted (APPROVED 2026-08-13); (D3) an uncovered BUY is REFUSED calmly, refuse-only
     for v1 (broker never touched, claim released, retryable), and the user funds it via
     the existing 9-3 liquidation → deferred-buy spine — NO auto-seed of a PendingBuy in
     v1 (APPROVED 2026-08-13). DO NOT enable for real money until this lands + is
     independently reviewed. -->

## Story

As a beginner whose brokerage account can trade on margin,
I want the coach to refuse to place any BUY that exceeds the real settled cash I actually hold,
so that co-signing a deploy or a buy can never quietly borrow money on margin — Ballast only ever spends cash I already have.

## Context & PROVEN problem

This is the **CRITICAL merge-blocker carved out of Story 10-8's independent review
(2026-08-13)** — its own load-bearing execution safeguard (10-8 AC3), promoted to a
standalone backend money-safety story because it is **pre-existing and cross-cutting**,
not specific to the deploy path.

**The gap.** `coach/execution.py:execute_approved_order` is the SOLE caller of
`BrokerPort.place_order` (AD-7). Before placing it enforces: placement-time
session/provider integrity (4.8), v1 order scope (`is_index_core`, widened for SELL only —
9.3/10.5), `amount > 0`, and order-shape (`validate_order_intent`, 8.1). It has **ZERO
cash-coverage check.** The Schwab adapter refuses only a sub-share amount or an unusable
quote (`OrderNotPlaceableError`, `brokers/schwab_adapter/adapter.py:413`) — never a
shortfall. **So a co-signed BUY whose dollar amount exceeds the account's real settled
cash places directly. On a margin account, Schwab fills the shortfall on margin** — the
beginner unintentionally borrows, contrary to Ballast's no-leverage ethos.

**Why 10-8 makes it dangerous + reachable.** Story 10-8 (money-market-aware deploy) now
counts declared parked money-market as *deployable in analysis* — so the deploy plan can
say **"deploy $65,949"** while only **$12,182.82 settlement cash** exists
(MasterB's real account: $12,182.82 cash + $93,766.26 SWVXX, reserve $40k). 10-8's
Phase-2 claim — "the existing 9-3 liquidation already funds any buy beyond `ready_to_trade`,
no new code needed" — is **WRONG**: 9-3 (`cash/liquidation.py:plan_liquidation`) is a
READ-ONLY, **frontend-initiated** flow (`POST /api/cash/liquidation-plan`). NOTHING at
`/approve` forces an over-budget buy through it; a deploy buy can be co-signed and placed
directly, bypassing liquidation entirely. The review's three layers all converged: **the
real-cash / no-margin safeguard is not enforced on the backend.**

**Confirmed by the live account** (`docs/real-money-readiness.md`, 2026-08-13): the real
Schwab account IS a margin account; `availableFunds`/`availableFundsNonMarginableTrade` =
$45,302.54 (includes margin buying power, far exceeding cash). The engine already anchors
cash to `cashBalance` (not the margin figure) — but that discipline lives only in the
*analysis read*; **execution has no equivalent guard.** This story adds it.

## Design — APPROVED by MasterB 2026-08-13

_Both load-bearing knobs signed off: D2 coverage base = **settled cash only** (parked MM
excluded, reserve not subtracted); D3 uncovered BUY = **refuse only** for v1 (no auto-seed).
The sub-questions below are resolved accordingly._


### D1 — The gate lives INSIDE `execute_approved_order` (the sole broker-place caller).
Add a cash-coverage gate as a new step **before** `broker.place_order`, symmetric with the
existing session/scope/amount/shape gates. Placing it at the single chokepoint — not in the
`/approve` handler — means **no placement path can bypass it** (future callers, the
reclaimer's forward-recovery path notwithstanding, since that path never re-places). On an
uncovered BUY it raises a new typed error (working name `InsufficientSettledCashError(ValueError)`,
alongside `OrderScopeError`/`OrderNotSupportedError` in `coach/execution.py`) **before the
broker is ever touched** — no order, no phantom idempotency key.

### D2 — Coverage base = real SETTLED cash: `available = max(0, view.cash)`.
A BUY is coverable iff `order_intent.amount <= max(0, view.cash)`, where `view.cash` is the
account's settlement cash (`portfolio_balance.cash`, the same `ready_to_trade` the 9-3
liquidator anchors to, `cash/liquidation.py:314`).
- **Parked money-market does NOT count.** It is deployable *on paper* (10-8 analysis) but is
  NOT spendable until sold and settled — counting it here would re-open the exact margin hole.
- **The reserve is NOT subtracted here.** The reserve is an *analysis-time* cushion (already
  honored where deploy amounts are computed, `allocation/engine.py:390`, and protected by the
  liquidation path, `available_parked = parked − reserve`). The execution invariant is
  strictly *no-margin* — don't spend cash you don't hold. Subtracting the reserve here would
  wrongly refuse a legitimate buy the user explicitly sized. (Open sub-question below.)
- **SELLs are NEVER coverage-gated** — a SELL raises cash; only a BUY can overdraw. The gate
  applies solely to `side == BUY`.
- **Conservative by construction, no live quote needed.** The adapter floors
  `floor(amount / live_ask)` shares, so actual spend ≤ `amount` ≤ settled cash. Checking the
  dollar `amount` against settled cash is a correct upper bound; the gate needs no quote.
- **Known-cash enforcement (refined during dev 2026-08-13, see Change Log).** The gate fires
  only against a KNOWN settled-cash figure — a present `portfolio_balance` row (detected via
  `view.as_of is not None`, since `get_portfolio` reports `cash == 0` for both "absent" and
  "genuinely $0"). A present-but-non-finite (`NaN`/`Inf`) cash is treated as `0` → **fail-closed
  refuse** (a corrupt balance can never certify coverage). But a **scope-less/system call** (no
  user cash to read) and a **never-imported account** (no balance row) are NOT blocked — the gate
  never fabricates a $0 refusal from absent data (never-invent-a-fact), and the production
  `/approve` path always supplies a user scope, so the proven margin hole (10-8: a real balance
  row with cash `$12,182.82` < a `$65,949` buy) is fully closed. This keeps blast radius at zero
  for the existing suite (mechanics tests that don't seed a balance are unaffected) and never
  blocks a legitimate user whose balance simply isn't cached yet.

### D3 — An uncovered BUY is REFUSED calmly; funding routes through the existing 9-3 spine.
`/approve` maps `InsufficientSettledCashError` to a **calm 422** and **releases the atomic
claim** (`cosigning → proposed`, retryable) — exactly like the `OrderScopeError` /
`OrderNotPlaceableError` arms (`api/coach.py:990-1001`) — so the decision is never stranded
and the broker was never touched. The refusal message routes the user to free up cash by
selling parked money-market first: the **existing** just-in-time liquidation flow
(`POST /api/cash/liquidation-plan` → records a durable `PendingBuy` + proposes the SELL →
the buy resumes via `resume_pending_buy` once `funds_ready`). This makes 10-8's "reuse 9-3"
claim actually TRUE — by *forcing* the over-budget buy through it instead of assuming it.

**RECOMMENDED scope for v1: refusal only** (no new writes at `/approve`; `execute_approved_order`
keeps its no-persistence contract). Whether `/approve` should ALSO auto-seed a `PendingBuy`
for the refused buy (so a beginner isn't dropped and needn't re-enter it) is a knob — see
the approval question. The minimal safe deliverable is the **refusal**; auto-seeding is an
additive convenience that can follow.

### Rejected
- **Trusting `availableFunds` / margin buying power as spendable cash** — the entire bug.
- **Gating in the `/approve` handler instead of the execution owner** — leaves the sole
  broker-place caller unguarded for any other caller; violates the one-owner chokepoint ethos.
- **A live-quote pre-check in the gate** — unnecessary; the dollar-amount upper bound is
  already conservative and quote-free (and the adapter still floors + refuses sub-share).
- **Silently downsizing an over-budget buy to fit settled cash** — dishonest (changes the
  order the user co-signed); refuse + route to liquidation instead.

## Acceptance Criteria

1. **No BUY places beyond real settled cash (the load-bearing invariant).** In
   `execute_approved_order`, a `BUY` `order_intent` places only when
   `amount <= max(0, view.cash)` for the caller's scope. **Given/When/Then:** Given a user with
   `$12,182.82` settlement cash (and any amount of parked money-market), When a co-signed BUY
   for `$65,949.08` reaches `/approve`, Then the placement is REFUSED **before** `place_order`
   is called (broker never touched — assert zero broker interactions), the atomic claim is
   released (`cosigning → proposed`, retryable), and `/approve` returns a calm 422. Never margin.
2. **Coverage base is settlement cash only.** Parked money-market value is NOT added to the
   coverable amount, and the reserve is NOT subtracted from it. **Given/When/Then:** Given a
   user whose declared parked money-market would make them "deployable" for `$65,949` in
   analysis but who holds `$12,182.82` settlement cash, When a `$20,000` BUY is co-signed,
   Then it is REFUSED (parked MM is not spendable cash), not placed.
3. **A covered BUY is unaffected (pure additive gate).** **Given/When/Then:** Given a user with
   `$5,000` settlement cash, When a co-signed BUY for `$1,000` (≤ settled cash) reaches
   `/approve`, Then it places exactly as today (whole-share floor + reconcile + co-sign all
   unchanged) and the outcome is returned truthfully.
4. **SELLs are never coverage-gated.** **Given/When/Then:** Given any settled-cash balance
   (including `$0`), When a co-signed SELL (index-core, a declared parked money-market symbol
   per 9.3, or a server-verified cost-switch per 10.5) is approved, Then the cash-coverage gate
   does not apply and the SELL proceeds through its existing scope path (a SELL raises cash).
5. **Known-cash enforcement, fail-closed on corrupt data (refined during dev — see Change Log).**
   **Given/When/Then:** Given a present balance whose cash is non-finite (`NaN`/`Inf`), When a BUY
   is evaluated, Then `available` is treated as `0` and the BUY is REFUSED (a corrupt balance can
   never certify coverage). Given a scope-less/system call OR a never-imported account (no balance
   row), When a BUY is evaluated, Then the gate does NOT fire (no settled-cash truth to assert an
   overdraw against — never fabricate a $0 refusal; the production `/approve` path always supplies
   a user scope, so this never weakens the real protection).
6. **Idempotent re-approve is untouched.** An already-`cosigned` decision still returns its
   RECORDED outcome without re-invoking the broker and WITHOUT re-running the coverage gate
   (a genuinely-placed buy is never retroactively refused) — the existing `record.status ==
   "cosigned"` short-circuit (`api/coach.py:859`) is preserved.
7. **Calm, honest refusal that routes to funding.** The 422 message is calm and specific —
   it explains there isn't enough settled cash yet and that selling some money-market fund will
   free it up (mirrors the 9-3 liquidation voice), never alarmist, never a raw 500. The 10-3
   guardrails still apply to any narration (no forecast, no invented numbers).
8. **Per-user scoped (AD-10), Decimal money, read-only cash read.** The coverage read uses the
   fail-closed scoped `get_portfolio(scope, session)` (a user only ever sees their OWN cash);
   money is `Decimal`; the gate places/writes nothing itself (`execute_approved_order` keeps its
   no-persistence contract). No cross-user cash leakage.

## Tasks / Subtasks

- [x] Task 1 — Add the cash-coverage gate to the sole broker-place caller (AC: 1, 2, 4, 5, 8)
  - [x] In `coach/execution.py`, defined `InsufficientSettledCashError(ValueError)` with a calm
        docstring (mirrors `OrderScopeError`): raised BEFORE `place_order` for a BUY that exceeds
        real settled cash; the broker is never touched.
  - [x] In `execute_approved_order`, AFTER the scope/amount/shape gates and BEFORE `place_order`:
        for `canonical_intent.side == OrderSide.BUY`, calls `_assert_buy_covered_by_settled_cash`
        — reads `available = max(0, view.cash)` via the scoped `get_portfolio(scope, session)`
        (read-only, lazy import). Refuses when `amount > available`. **Refinement:** enforces only
        against a KNOWN balance (`view.as_of is not None`); scope-less/system calls and
        never-imported accounts are not blocked; a present-but-non-finite cash fails closed
        (available 0). SELLs skip the gate entirely. See Change Log.
  - [x] Updated the `execute_approved_order` module docstring "What this owner guarantees" list
        with the cash-cover / no-margin bullet (settled-cash base, no parked-MM, SELLs exempt).
- [x] Task 2 — Map the refusal at `/approve` calmly + retryably (AC: 1, 6, 7)
  - [x] In `api/coach.py:approve`, added an `except InsufficientSettledCashError` arm above
        `OrderScopeError` (and above the trailing `except Exception`): `release_claim`
        (`cosigning → proposed`) then a calm `HTTPException(422, ...)` with the route-to-liquidation
        message. Broker never touched.
  - [x] Confirmed the `record.status == "cosigned"` idempotent-replay short-circuit is untouched
        (the gate lives in `execute_approved_order`, which the cosigned branch never calls; existing
        re-approve tests stay green).
- [x] Task 3 — Tests (AC: all) — `ballast_test` DB, fake broker + fake LLM
  - [x] Backend (`tests/test_cash_cover_safety.py`, 11 tests): a BUY > settled cash is REFUSED
        (spy broker `place_calls == 0`); parked-MM does not count; a covered BUY (and the exact
        boundary) places unchanged; an index-core SELL and a parked SELL at `$0` cash are NOT gated;
        no-balance-row + scope-less BUYs are not blocked; non-finite cash fails closed; per-user
        scope isolation (A's tiny cash gates A's buy despite a rich B).
  - [x] Regression: the flipped 10-8 repro ($12,182.82 settlement + $93,766.26 SWVXX, reserve
        $40k) — the `$65,949.08` primary deploy BUY is REFUSED at execution (analysis deployable ≠
        placeable-without-liquidation).
  - [x] Existing execution/approve/liquidation/cost-switch/coach-api tests stay green — full suite
        **852 passed** (836 baseline + new), zero regressions.

## Dev Notes (reuse / touch points)

### Read these files completely before editing (UPDATE, not NEW)
- **`coach/execution.py:execute_approved_order` (~:299-396)** — the sole `place_order` caller and
  the gate site. The new coverage check goes after `validate_order_intent`/amount gate, before
  `key = ...; place_order`. It ALREADY accepts `scope`/`session` (used for the 9.3 parked-symbol
  SELL widening via `_scope_user_parked_symbols`) — reuse the same lazy-import + fail-closed pattern
  to read cash. Keep the no-persistence contract (Story 4.9): this gate READS, never writes.
- **`api/coach.py:approve` (~:797-1074)** — the call site. Add the `except` arm mirroring
  `OrderScopeError` (`:990`) / `OrderNotPlaceableError` (`:995`): `release_claim` then calm 422.
  The handler already has `scope`/`session`/`broker`; it passes `scope=scope, session=session`
  into `execute_approved_order` (`:965`), so the gate has what it needs.
- **`brokers/portfolio.py:get_portfolio` / `PortfolioView`** — read-only cached view; `view.cash`
  is settlement cash (`portfolio_balance.cash`, Story 6.5 / AD-14), the SAME source the 9-3
  liquidator anchors to. Public shape `(holdings, cash, as_of, is_empty)` is FIXED.

### Reuse map (do NOT reinvent)
- **`cash/liquidation.py:plan_liquidation` + `api/cash.py` (`liquidation_plan`, `list_pending_buys`,
  `resume_pending_buy`, `PendingBuy`)** — the EXISTING funding spine an uncovered buy routes to
  (sell parked MM → settle → resume as a co-signed buy). This story does NOT change it; it makes it
  reachable-by-necessity. `ready_to_trade = view.cash` is the coverage base both share (`:314`).
- **`coach/execution.py` error pattern** — `OrderScopeError` / `OrderNotSupportedError` /
  `SessionIntegrityError` are the template for the new typed refusal (raised pre-broker,
  mapped to a calm HTTP status, claim released).
- **`allocation/engine.py:build_plan` (~:370-405)** — where investable = `ready_to_trade + parked −
  reserve` is computed (10-8, analysis). This story is the EXECUTION counterpart; keep the
  distinction explicit in comments (analysis counts parked MM; execution never does).

### What must be preserved (regressions to avoid)
- The whole-share floor / reconcile / atomic co-sign claim / recoverable-`broker_ref` ordering
  (Stories 6.1/7.2) — all UNCHANGED; the gate sits strictly before `place_order`.
- Populate-don't-submit and the single execution path (AD-7): no new placement path, no auto-submit.
- SELL scope widening (9.3 parked, 10.5 cost-switch) — a SELL must remain ungated by coverage.
- All Epic 10 Group-C money-safety contracts verified clean must stay clean.

### Honest limitations / open sub-questions (for MasterB approval, not blocking dev)
- **`view.cash` may include UNSETTLED funds.** Per `docs/real-money-readiness.md`, Schwab's
  `cashBalance` can include unsettled cash and there is currently no clean settled-only field; on a
  MARGIN account this is second-order (margin covers unsettled, no good-faith violation). This gate
  is a strict improvement (it blocks the gross margin-buy) but is NOT a perfect settled-vs-unsettled
  guarantee. Tracked by the OPEN cross-epic action item (Epic 9 #2a / Epic 10 "confirm
  `portfolio_balance.cash` is SETTLED-only before real-money deploy"). If/when a cash (non-margin)
  account is onboarded, revisit reading a live settled-cash field at `/approve`.
- **Reserve at execution.** APPROVED 2026-08-13: v1 base = settled cash, **no reserve subtraction**
  (reserve stays an analysis-time concern + is protected by the liquidation path). Revisit only if a
  future need to protect the cushion at execution appears.
- **Auto-seed on refusal.** APPROVED 2026-08-13: v1 = **refuse only** (no `PendingBuy` written at
  `/approve`; `execute_approved_order` keeps its no-persistence contract). Auto-seeding the refused
  buy as an `awaiting_funds` `PendingBuy` (reusing the 9-3 producer) is a possible fast follow-up,
  not this story.

### Testing standards
- Backend: `cd ballast/backend && DATABASE_URL=postgresql://ballast:ballast@localhost:5432/ballast_test BALLAST_ALLOW_DIRTY_BROKERAGE_DB=1 uv run pytest -q`
  (⚠️ `ballast_test` DB ONLY — never `ballast`, which holds the live Schwab link the suite deletes).
  Fake broker + fake LLM by conftest. To assert "broker never touched," use a fake/spy broker and
  assert `place_order` interaction count is 0 on a refused buy.
- Frontend: out of scope for this backend story (the deploy/coach UI already surfaces the 9-3
  liquidation flow; a follow-up may improve how a coverage-refusal routes there).

### Project Structure Notes
- Backend Python under `ballast/backend/` (FastAPI + SQLAlchemy async + Postgres). No new DB
  objects, no migration (a pure read-gate). Behind the fake broker in tests; the safeguard matters
  most on the real (schwab) margin path but is broker-agnostic (enforced before `place_order`).

### References
- [Source: _bmad-output/implementation-artifacts/10-8-money-market-aware-deploy.md#Independent Review Findings (2026-08-13) — REWORK REQUIRED] — the CRITICAL finding this story delivers (10-8 AC3 execution safety).
- [Source: docs/real-money-readiness.md] — the live-account margin findings (`availableFunds` vs `cashBalance`, no clean settled-cash field).
- [Source: ballast/backend/coach/execution.py#execute_approved_order] · [Source: ballast/backend/api/coach.py#approve] · [Source: ballast/backend/cash/liquidation.py#plan_liquidation] · [Source: ballast/backend/brokers/portfolio.py#PortfolioView]
- [Source: docs/dev-loop-policy.md] — per-story spec-approval hard gate + mandatory independent review for money-path work.

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (bmad-dev-story, in-chat)

### Debug Log References

- RED: `tests/test_cash_cover_safety.py` failed collection on the missing
  `InsufficientSettledCashError` import (gate did not exist).
- GREEN: after adding the error + gate + `/approve` arm, the 11 new tests pass; full
  backend suite `852 passed` in ~58s (`ballast_test` DB, fake broker + fake LLM).
- Key design finding: `get_portfolio` collapses "no balance row" and "genuinely $0"
  into `cash == 0`, so `view.as_of is None` is the honest "no imported balance" signal —
  used to enforce only against a KNOWN balance (zero blast radius on the existing suite;
  `test_coach_api` seeds no balance, `test_recoverable_placement` BUYs are scope-less).

### Completion Notes List

- **Gate (`coach/execution.py`):** new `InsufficientSettledCashError`; new
  `_assert_buy_covered_by_settled_cash` helper; called in `execute_approved_order` for a
  BUY only, AFTER `validate_order_intent`, BEFORE key mint / `place_order` — so the broker
  is never touched on a shortfall. Coverage base = `max(0, view.cash)` (settlement cash);
  parked MM excluded; reserve not subtracted. Read-only, scoped, lazy import (no cycle);
  the owner keeps its no-persistence contract.
- **Refusal (`api/coach.py:approve`):** `except InsufficientSettledCashError` arm above
  `OrderScopeError` — releases the atomic claim (`cosigning → proposed`, retryable) and
  raises a calm 422 routing the user to the 9-3 liquidation. Idempotent re-approve
  (`cosigned` short-circuit) untouched.
- **Refinement vs the drafted AC5 (documented in Change Log + approved-design fail-closed
  bullet):** the gate enforces only against a KNOWN balance (`view.as_of is not None`);
  scope-less/system calls and never-imported accounts are not blocked; a non-finite cached
  cash fails closed. This preserves the MasterB-approved "settled cash only" + "refuse only"
  decisions while (a) never fabricating a $0 refusal from absent data (never-invent-a-fact),
  (b) not blocking a legitimate not-yet-imported user, and (c) keeping the existing suite at
  zero regressions. The proven margin hole (10-8: a real balance row, cash `$12,182.82` < a
  `$65,949` buy) is fully closed.
- **Money-safety preserved:** whole-share floor / reconcile / atomic co-sign claim /
  recoverable-`broker_ref` ordering all unchanged (gate is strictly pre-placement); SELL
  scope widening (9.3/10.5) unaffected; per-user scoping intact. Behind the fake broker;
  broker-agnostic (enforced before `place_order`).
- **No DB change / no migration** (pure read-gate). `followup_review_recommended: true` —
  money-path story; mandatory independent review before merge (per docs/dev-loop-policy.md).

### File List

- `ballast/backend/coach/execution.py` — `InsufficientSettledCashError`;
  `_assert_buy_covered_by_settled_cash`; BUY cash-cover gate in `execute_approved_order`;
  module-docstring guarantees bullet.
- `ballast/backend/api/coach.py` — import `InsufficientSettledCashError`; `except` arm in
  `approve` (release claim + calm 422).
- `ballast/backend/tests/test_cash_cover_safety.py` — NEW: 11 tests (refusal + broker-untouched,
  parked-MM-excluded, covered/boundary places, SELLs not gated, no-balance/scope-less not blocked,
  non-finite fail-closed, per-user isolation, flipped 10-8 repro).

## Change Log

- 2026-08-13 — Story 10.9 implemented: backend cash-cover safety. `execute_approved_order`
  (the sole `place_order` caller) now refuses a BUY that exceeds the account's known real
  settled cash (`max(0, view.cash)`) BEFORE touching the broker — closing the pre-existing,
  10-8-reachable margin hole (a co-signed deploy buy > settlement cash could fill on margin).
  Parked money-market is excluded and the reserve is not subtracted (strict no-margin);
  SELLs are never gated; `/approve` maps the refusal to a calm, retryable 422 that routes to
  the 9-3 liquidation. +11 tests, full backend suite 852 passed. Money-path → awaiting
  mandatory independent review before merge.
- 2026-08-13 — Spec refinement during dev (AC5 + approved-design fail-closed bullet): changed
  "no scope/session OR missing cash → available 0 → refuse" to **known-cash enforcement** —
  enforce only against a present balance (`view.as_of is not None`); scope-less/system calls
  and never-imported accounts are not blocked (never fabricate a $0 refusal); a present-but-
  non-finite cash fails closed. Preserves the two MasterB-approved knobs (settled-cash-only,
  refuse-only); rationale: honesty (never-invent-a-fact), don't block not-yet-imported users,
  and zero blast radius on the existing suite. The real margin hole (known balance, cash <
  buy) remains fully closed.

## Independent Review Findings (2026-08-13) — MERGE-READY

_Mandatory money-path independent review: 3-layer parallel adversarial pass in fresh,
independent contexts (Blind Hunter + Edge Case Hunter + Acceptance Auditor). All three
converged: **MERGE-READY, no Critical or High findings.** The no-margin invariant holds at
the sole `place_order` chokepoint with correct Decimal/scope/ordering/boundary semantics;
all 8 ACs verified MET and genuinely tested (broker-untouched asserted via a spy; the flipped
10-8 repro locked)._

Verified by the reviewers:
- No bypass to `place_order`: `execute_approved_order` is the sole placement caller; the
  reconcile/reclaimer paths only re-read (never place); `resume_pending_buy` and the 10.5
  linked cost-switch BUY place nothing — they re-enter `/approve` → the gate.
- `view.as_of is None` is a sound "no balance" discriminator (in `_to_view`, `as_of` is None
  **iff** the balance row is absent, and cash is forced to 0 there — no state has `as_of None`
  with real cash). The deploy + suggest BUY paths cannot produce a co-signable BUY for a
  no-balance user, so the refinement does not reopen the proven hole.
- Decimal/NaN/Inf/negative-cash handling, `>` boundary, per-user scope isolation, the
  release-claim + calm-422 except arm (no shadowing), and idempotent re-approve (gate never
  re-runs on a `cosigned` decision) all correct.

Residuals — logged to `deferred-work.md`, NOT merge-blockers (out of this story's scoped
deploy hole):
- [Defer][Medium] **Human-coach hand-entered BUY on a linked account with no cached balance
  row is not gated** (the one reachable margin path the known-cash refinement leaves). Deploy/
  suggest are fully covered. Fix before real-money reliance on the human-coach BUY path: force
  a balance refresh before that `/approve`, or fail-closed a snapshot-intent-less BUY when
  `as_of is None`.
- [Defer][Medium] **Gate reads the CACHED balance, not a live broker balance** — a BUY can pass
  against last-reconciled cash that overstates current settled cash (narrowed, not eliminated,
  margin window). Spec-approved quote-free design (D2); merges with open Epic 9 #2a
  (settled-only confirmation). Reconcile-before-gate or rely on Schwab buying-power backstop.
- [Defer][Low] Cosmetic: for a tiny BUY that is both over-cash and sub-share, the cash message
  now precedes the adapter's sub-share message (both calm 422s, broker untouched).
