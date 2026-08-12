# Story 10.5: Cost-switch — linked SELL + BUY pair

Status: ready-for-dev

<!-- HARD GATE (docs/dev-loop-policy.md, adopted 2026-08-12): governing spec APPROVED by
     MasterB 2026-08-12. Linkage design settled: thread switch_to through the decision
     snapshot so execute_approved_order creates the linked PendingBuy on SELL co-sign. -->

## Story

As a beginner following a cost/fees "switch" recommendation,
I want the follow-up BUY into the cheaper fund to be automatically queued and linked to the SELL I co-sign,
so that co-signing the switch never leaves me accidentally stranded in cash if I forget or can't do the second leg.

## Context & problem

Story 10-4's cost/fees bucket (`allocation/review.py:find_cost_findings`) SELLs the **whole** high-fee position and only **narrates** the follow-up BUY into the cheaper same-class canonical index fund (`switch_to`); it is never linked or populated. A beginner who co-signs the SELL and skips (or forgets) the BUY is stranded in cash — a de-facto market-timing exit that contradicts the epic's "deploy your cash" premise. Surfaced by the Epic 10 Group-B independent review (recorded in `spec-10-4`), promoted to this story.

## Design decisions (APPROVED 2026-08-12)

A real BUY **cannot execute until the SELL's proceeds settle** (typically T+1). A truly "atomic" pair co-signed in one action is therefore broker-impossible and would either strand cash or misrepresent settlement.

**Decided — reuse, not new machinery:** when the user co-signs the cost-switch SELL, durably register a **linked deferred BUY** using the existing Epic 9 Story 9-3 `PendingBuy` mechanism (`db/models.py:PendingBuy`, table `pending_buy`). The BUY for `switch_to` sits in `awaiting_funds`, linked to the SELL via `sell_decision_id`, and resumes through the **existing** deferred-buy surface (`api/cash.py:resume_pending_buy`) once `ready_to_trade` covers it — the same "buy resumes when cash settles" UX Epic 9 already ships. Honest about settlement, no new co-sign spine, a durable linked reminder instead of orphaned cash.

**Decided — how the co-sign path learns a SELL is a cost-switch:** **thread `switch_to` through the decision snapshot at propose time** so `coach/execution.py:execute_approved_order` creates the linked `PendingBuy` on a successful cost-switch SELL co-sign. This is the load-bearing design choice; implement it as the primary mechanism (no dedicated switch endpoint, no switch-aware approve fork). The `switch_to` value rides the same JSON decision snapshot the OrderIntent already uses (money as fixed-point strings), so it survives propose→approve exactly like the order does.

**Rejected:** (a) a fake atomic co-sign that pretends the BUY places instantly — impossible given settlement; (b) a dedicated switch endpoint or a switch-aware `/approve` payload fork — both duplicate the co-sign spine instead of reusing it.

## Acceptance Criteria

1. **Linked deferred BUY on SELL co-sign.** Co-signing a cost-switch SELL (a `ReviewFinding` with `kind == "cost"` and a non-null `switch_to`) durably creates one linked `PendingBuy` for `switch_to` in `awaiting_funds`, with `sell_decision_id` = the SELL's `decision_record` id and `buy_intent` a BUY/MARKET `OrderIntent` for `switch_to`.
2. **Reuse the existing deferred-buy surface — no new spine.** The linked `PendingBuy` appears in the existing pending-buy list (`api/cash.py:list_pending_buys`) with a live `funds_ready`, and resumes via the existing `resume_pending_buy` (mints a `proposed` BUY decision the human co-signs) once `ready_to_trade` covers it.
3. **Populate-don't-submit preserved.** Co-signing the SELL never auto-places the BUY; the BUY still requires an explicit human co-sign at resume. No read/detector/endpoint places an order or writes a decision outside the co-sign spine.
4. **Idempotent.** Re-co-signing / replaying the same switch SELL does not create duplicate pending buys — reuse the existing `(symbol, amount)` dedupe (`_find_awaiting_pending_buy`).
5. **Honest two-step framing.** The switch narration + review UI make clear it is a two-step switch (SELL now → BUY when cash settles) and that stranded cash is avoided because the BUY is queued & linked. The 10-3 guardrails still apply verbatim (no forecast, no invented numbers, degrade-to-template).
6. **Honest amount + whole-share at resume.** The `PendingBuy.amount` is an honest estimate from the SELL's cached market value (mirrors the deploy/switch "estimate now, floor at approve" convention); whole-share flooring happens at resume/approve. If settled proceeds fall short of one whole share of `switch_to`, degrade calmly (no un-co-signable order; a plain calm note).
7. **Per-user scoped (AD-10), Decimal money, tz-aware UTC** end-to-end; no cross-user linkage; the `PendingBuy` is reachable only through the fail-closed `ScopedRepository`.
8. **Read-only endpoints unchanged.** `GET /api/allocation/review` (and `/plan`, `/narration`) still place nothing and write nothing (existing tests stay green).

## Tasks / Subtasks

- [ ] Task 1 — Thread the switch linkage from finding → co-sign (AC: 1, 4)
  - [ ] Carry `switch_to` from the cost `ReviewFinding` through the propose→approve decision snapshot (approved design) so the co-sign path can identify a cost-switch SELL — ride the existing OrderIntent JSON snapshot, not a new endpoint/payload fork.
  - [ ] In `coach/execution.py:execute_approved_order` (the sole broker-place caller), on a successful cost-switch SELL co-sign, create the linked `awaiting_funds` `PendingBuy` for `switch_to` via the existing `api/cash.py` creation path, with `sell_decision_id`. Reuse the `(symbol, amount)` idempotent dedupe.
- [ ] Task 2 — BUY intent + amount (AC: 1, 6)
  - [ ] Build the `buy_intent` BUY/MARKET `OrderIntent` for `switch_to`; set `amount` from the SELL finding's cached market value (honest estimate). Whole-share flooring deferred to resume/approve (`coach/execution.py:whole_share_quantity`).
- [ ] Task 3 — Honest two-step framing (AC: 5)
  - [ ] Update the cost-switch narration/fallback copy (`allocation/review.py` cost fallback) + the review UI (`CoachConsult.jsx`) so a cost finding reads as a linked two-step switch, and the co-signed SELL surfaces the linked pending BUY. Keep the 10-3 gates (`check_no_invented_numbers`/`check_no_forecast`) governing any LLM text.
- [ ] Task 4 — Tests (AC: all)
  - [ ] Backend: co-signing a cost-switch SELL creates one linked `awaiting_funds` PendingBuy (`sell_decision_id` set, `switch_to` symbol, BUY/MARKET); idempotent on replay; per-user scoped; a non-switch SELL creates none; read endpoints still write nothing/place nothing.
  - [ ] Frontend: the review card frames the switch as two-step; co-signing the SELL surfaces the linked pending buy; nothing auto-submits.

## Dev Notes

### Reuse map (do NOT reinvent)
- **`db/models.py:458 PendingBuy`** (table `pending_buy`, `ix_pending_buy_owner_status`) — durable deferred buy: `buy_intent` (OrderIntent JSON snapshot), `amount` hoisted to `Numeric(20,2)` for a direct `funds_ready` compare, `sell_decision_id` linking the SELL decision, `status` ∈ `awaiting_funds|resumed|cancelled`. **This is the linkage primitive — the BUY leg IS a PendingBuy.**
- **`api/cash.py`** — the full lifecycle already exists and is scoped: create/dedupe an `awaiting_funds` buy (`~430-498`, `_find_awaiting_pending_buy`), `list_pending_buys` (with live `funds_ready`), `resume_pending_buy` (atomic `awaiting_funds → resumed`, mints a `proposed` buy the human co-signs), `cancel_pending_buy`. Story 10-5 should CREATE via this path and let the existing list/resume/cancel + UI carry the BUY leg.
- **`allocation/review.py`** — cost findings: `find_cost_findings` builds the SELL `order_intent` + `switch_to` (canonical cheaper fund) + whole-position `amount`; `find_review` aggregates/ranks. The finding already carries everything needed to seed the BUY.
- **`coach/recommendation.py:82 OrderIntent`** (+ `OrderSide`, `OrderType`, `RECOMMENDATION_OUTPUT_SCHEMA`) — the typed order payload; validation lives in `coach/execution.py:validate_order_intent`.
- **`coach/execution.py`** — `execute_approved_order` is the SOLE broker-place caller and the natural hook to create the linked PendingBuy on a successful cost-switch SELL; `whole_share_quantity` floors shares (price ≤ 0 / non-finite → 0).
- **`api/coach.py`** — the propose/`/approve` co-sign spine (atomic `proposed → cosigning → cosigned` claim, Story 6.1). Do NOT fork it; thread the switch linkage through it.
- **`strategy/target_allocation.py:CANONICAL_FUND`** — maps asset class → canonical cheaper fund (`switch_to`).

### What must be preserved (regressions to avoid)
- **Populate-don't-submit** and the atomic co-sign claim — the BUY leg must remain an explicit second human co-sign (via resume), never auto-placed. All money-safety contracts verified clean in the Epic 10 Group-C review must stay clean.
- The whole-share "estimate now, floor at approve" convention (deploy/switch/liquidation all share it).
- Read endpoints (`/plan`, `/narration`, `/review`) place/write nothing — existing tests assert this.
- Epic 9 story 9-3 deferred-buy behavior for the DEPLOY/liquidation path must be unaffected (this story ADDS a second producer of PendingBuys, the cost-switch; verify the existing producer and the list/resume UI don't conflate them or break).

### Testing standards
- Backend: `cd ballast/backend && DATABASE_URL=postgresql://ballast:ballast@localhost:5432/ballast_test BALLAST_ALLOW_DIRTY_BROKERAGE_DB=1 uv run pytest -q` (⚠️ `ballast_test` DB ONLY — never `ballast`, which holds the live Schwab link the suite deletes). Fake broker + fake LLM by conftest.
- Frontend: `cd ballast/frontend && npm test` (vitest).

### Project Structure Notes
- Backend Python under `ballast/backend/` (FastAPI + SQLAlchemy async + Postgres); frontend Vite/React under `ballast/frontend/`.
- New DB migration only if `PendingBuy` needs a new column to record it was seeded by a cost-switch vs a deploy-shortfall (evaluate during design — a nullable discriminator may aid the two-step framing/UI without conflating producers). Migrations: `ballast/backend/db/migrations.py` (`CREATE ... IF NOT EXISTS`, idempotent, additive).

### References
- [Source: _bmad-output/implementation-artifacts/spec-10-4-analysis-buckets-concentration-cost.md#Review Findings — Independent (2026-08-12)] — the cost-switch decision (Decision→Story 10-5)
- [Source: ballast/backend/db/models.py#PendingBuy] · [Source: ballast/backend/api/cash.py#resume_pending_buy] · [Source: ballast/backend/allocation/review.py#find_cost_findings] · [Source: ballast/backend/coach/execution.py#execute_approved_order] · [Source: ballast/backend/coach/recommendation.py#OrderIntent]
- [Source: docs/dev-loop-policy.md] — per-story spec-approval hard gate governing this spec.

## Dev Agent Record

### Agent Model Used

### Debug Log References

### Completion Notes List

### File List
