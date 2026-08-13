# Story 10.5: Cost-switch — linked SELL + BUY pair

Status: done
baseline_revision: 9bb6c75
final_revision: 2c0a0c7
review_loop_iteration: 1
followup_review_recommended: true

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

**Decided — the SELL leg of a cost-switch is NOT index-core, so it hits the v1 SELL scope gate (`coach/execution.py:execute_approved_order`); the gate MUST be widened for it, but ONLY for a server-verified genuine cost-switch (added 2026-08-12 after review — see Spec Change Log).** The `switch_to` value that rides the decision snapshot is **client input and MUST NOT be trusted to widen scope**. Before threading `switch_to` onto the snapshot at propose time, the server MUST **re-derive and verify the cost-switch relationship from the user's own cached holdings** — i.e. confirm the SELL `symbol` is actually a high-fee held fund the deterministic cost bucket (`allocation/review.py:find_cost_findings`) flags, and that the supplied `switch_to` equals the canonical cheaper same-class fund that bucket/`strategy/target_allocation.py:CANONICAL_FUND` computes for it. If the pair is not a genuine, server-derived cost-switch (including `switch_to == symbol`, an unheld/mistyped symbol, or a `switch_to` that is not the canonical target), the server drops `switch_to` to `None`: **no snapshot key, no scope widening, no linked buy** — the SELL then behaves exactly as an ordinary order and is refused by the unchanged index-core gate if out of scope. The scope-gate widening in `execute_approved_order` may treat `switch_to`-present as in-scope ONLY because it is now guaranteed server-verified; a BUY never carries `switch_to` and is never widened (beginners still cannot buy arbitrary securities).

**Decided — the linked-buy dedupe key must distinguish DISTINCT switches and NOT conflate with the Story 9.3 deploy/liquidation producer.** Two genuinely different cost-switches into the same canonical fund for the same dollar amount (e.g. two $2,000 high-fee holdings both switching to VTI) must each queue their OWN linked buy; and a cost-switch buy must never be swallowed by (or swallow) a pre-existing 9.3 deploy-shortfall buy that happens to share `(symbol, amount)`. Key idempotency on the **originating `sell_decision_id`** (a replay of the SAME co-signed switch SELL is the only thing that must dedupe to a no-op), not on `(symbol, amount)` alone. Re-approving the same cosigned SELL still creates no duplicate.

**Decided — the "linked buy was queued" signal shown to the user must be SERVER-TRUTH, not a frontend guess.** The `/approve` response MUST carry an explicit field indicating whether a linked cost-switch BUY was durably queued (true only when `execute_approved_order` actually created the `PendingBuy` on a genuinely-placed SELL — the `_is_placed` gate: `filled`/`partial`, or `pending`/`timeout` WITH a `broker_ref`). The frontend surfaces the "step 2 of 2 is queued" reassurance ONLY from that field — never inferred from `status != 'rejected'` — so a `pending`/`timeout`-without-`broker_ref` outcome (which seeds nothing) never tells a beginner their follow-up buy is safely queued when it is not.

**Decided — the linkage's failure handling must not corrupt the caller's transaction, and the "no persistence" contract change must be explicit.** Creating the linked buy inside `execute_approved_order` introduces a DB write into a function historically documented as "no persistence (Story 4.9)". Any failure to seed the linked buy must NEVER fail the already-placed SELL AND must NEVER roll back the caller's other staged/committed decision-record writes — scope the failure handling (e.g. a nested transaction / savepoint) so a blanket `session.rollback()` cannot discard the approve handler's co-sign state. Update the stale `execute_approved_order` docstring to state it now conditionally persists the linked cost-switch buy.

**Rejected:** (a) a fake atomic co-sign that pretends the BUY places instantly — impossible given settlement; (b) a dedicated switch endpoint or a switch-aware `/approve` payload fork — both duplicate the co-sign spine instead of reusing it; (c) **trusting the client-supplied `switch_to` to widen the SELL scope gate** — a beginner-guardrail bypass that lets any authenticated user SELL an arbitrary held non-index-core security and queue a dead pending buy of an arbitrary symbol (surfaced by the 2026-08-12 independent review; see Spec Change Log).

## Acceptance Criteria

1. **Linked deferred BUY on SELL co-sign.** Co-signing a cost-switch SELL (a `ReviewFinding` with `kind == "cost"` and a non-null `switch_to`) durably creates one linked `PendingBuy` for `switch_to` in `awaiting_funds`, with `sell_decision_id` = the SELL's `decision_record` id and `buy_intent` a BUY/MARKET `OrderIntent` for `switch_to`.
2. **Server-verified cost-switch only — `switch_to` is never trusted from the client.** `switch_to` is threaded onto the decision snapshot (and the SELL scope gate widened / a linked buy queued) **only** when the server has re-derived the cost-switch from the user's own cached holdings: the SELL `symbol` is a high-fee held fund the cost bucket flags AND the supplied `switch_to` equals the canonical cheaper same-class fund for it (`find_cost_findings`/`CANONICAL_FUND`). **Given/When/Then:** Given an authenticated user, When `/recommend` receives a SELL whose `(symbol, switch_to)` is NOT a genuine server-derived cost-switch (arbitrary/unheld symbol, `switch_to == symbol`, or a non-canonical `switch_to`), Then the server drops `switch_to` to `None` — no snapshot key, no scope widening, no linked buy — and the SELL is refused by the unchanged index-core scope gate if out of scope (a beginner can NOT sell an arbitrary non-index-core security by supplying a `switch_to`).
3. **Reuse the existing deferred-buy surface — no new spine.** The linked `PendingBuy` appears in the existing pending-buy list (`api/cash.py:list_pending_buys`) with a live `funds_ready`, and resumes via the existing `resume_pending_buy` (mints a `proposed` BUY decision the human co-signs) once `ready_to_trade` covers it.
4. **Populate-don't-submit preserved.** Co-signing the SELL never auto-places the BUY; the BUY still requires an explicit human co-sign at resume. No read/detector/endpoint places an order or writes a decision outside the co-sign spine.
5. **Idempotent on the originating SELL, without conflating distinct switches or the 9.3 producer.** **Given/When/Then:** Given a co-signed cost-switch SELL, When it is re-co-signed / replayed, Then no duplicate `PendingBuy` is created (dedupe on the originating `sell_decision_id`). Given two DISTINCT cost-switch SELLs into the same `switch_to` for the same amount, When both are co-signed, Then TWO linked `PendingBuy` rows exist (one per SELL). Given a pre-existing Story 9.3 deploy/liquidation `PendingBuy` sharing `(symbol, amount)`, When a cost-switch SELL is co-signed, Then the cost-switch buy is created independently and neither producer swallows the other.
6. **Honest two-step framing driven by server truth.** The switch narration + review UI make clear it is a two-step switch (SELL now → BUY when cash settles) and that stranded cash is avoided because the BUY is queued & linked. The `/approve` response carries an explicit "linked buy queued" flag set true ONLY when the `PendingBuy` was actually created (a genuinely-placed SELL per `_is_placed`: `filled`/`partial`, or `pending`/`timeout` WITH a `broker_ref`); the frontend surfaces the "step 2 queued" reassurance ONLY from that flag, never inferred from `status != 'rejected'`. The 10-3 guardrails still apply verbatim (no forecast, no invented numbers, degrade-to-template).
7. **Honest amount + whole-share at resume, with a calm sub-share degrade.** The `PendingBuy.amount` is an honest estimate from the SELL's cached market value (mirrors the deploy/switch "estimate now, floor at approve" convention); whole-share flooring happens at resume/approve. **Given/When/Then:** Given settled proceeds fall short of one whole share of `switch_to`, When the user reaches the linked buy at resume, Then it degrades calmly — no un-co-signable (0-share) order is minted; a plain calm note explains the shortfall (verified by test, shared with the 9.3 resume path).
8. **Failure handling is transaction-safe.** **Given/When/Then:** Given the linked-buy creation raises after the SELL is placed, When the failure is handled, Then the already-placed SELL still succeeds AND the approve handler's co-sign state is NOT rolled back (the failure handling is scoped so it cannot discard the caller's other writes); the `execute_approved_order` docstring reflects that it now conditionally persists the linked buy.
9. **Per-user scoped (AD-10), Decimal money, tz-aware UTC** end-to-end; no cross-user linkage; the `PendingBuy` is reachable only through the fail-closed `ScopedRepository`.
10. **Read-only endpoints unchanged.** `GET /api/allocation/review` (and `/plan`, `/narration`) still place nothing and write nothing (existing tests stay green).

## Tasks / Subtasks

- [ ] Task 1 — Server-verify the cost-switch, then thread it finding → co-sign (AC: 1, 2)
  - [ ] At `/recommend` (`api/coach.py`), when a SELL request carries `switch_to`, **re-derive and verify the cost-switch from the user's own cached holdings** (reuse `allocation/review.py:find_cost_findings` / `strategy/target_allocation.py:CANONICAL_FUND`): accept `switch_to` ONLY if `symbol` is a flagged high-fee held fund and `switch_to` is its canonical cheaper same-class fund. Otherwise drop `switch_to` to `None` (no snapshot key, no widening, no linked buy). NEVER trust the client `switch_to`.
  - [ ] Carry the VERIFIED `switch_to` through the propose→approve decision snapshot (additive key, `schema_version` unchanged) — ride the existing OrderIntent JSON snapshot, not a new endpoint/payload fork. The bound SELL `order_intent` + the existing `_placed_order_matches_proposal` guard keep propose==approve; server verification (not that guard) is what makes the scope-gate widening safe.
  - [ ] Widen the `coach/execution.py:execute_approved_order` SELL scope gate for a `switch_to`-present SELL (now guaranteed server-verified). A BUY is never widened.
- [ ] Task 2 — Create the linked BUY on a genuinely-placed SELL (AC: 1, 5, 6, 8)
  - [ ] On a genuinely-placed cost-switch SELL (`_is_placed`: `filled`/`partial`, or `pending`/`timeout` WITH `broker_ref`), create the linked `awaiting_funds` `PendingBuy` for `switch_to` via a `api/cash.py` creation path, with `sell_decision_id`. Place the creation wherever it is most transaction-safe — either inside `execute_approved_order` (the sole broker-place caller) or in the `/approve` handler immediately after `execute_approved_order` returns a placed outcome; the "sole broker-place caller" intent is satisfied by gating on `_is_placed(outcome)`. Prefer the location that keeps `execute_approved_order`'s persistence contract honest and lets the approve handler own the commit.
  - [ ] **Dedupe on the originating `sell_decision_id`** — a replay of the SAME co-signed switch SELL is a no-op; two DISTINCT switches (even same `symbol`+`amount`) each queue their own buy; a pre-existing 9.3 deploy buy sharing `(symbol, amount)` is never swallowed/conflated.
  - [ ] Return an explicit "linked buy queued" flag on the `/approve` response, true ONLY when the `PendingBuy` was actually created.
  - [ ] Build the `buy_intent` BUY/MARKET `OrderIntent` for `switch_to`; set `amount` from the SELL finding's cached market value (honest estimate). Whole-share flooring deferred to resume/approve (`coach/execution.py:whole_share_quantity`).
  - [ ] **Transaction-safe failure handling**: a failure to seed the linked buy must never fail the placed SELL AND must never roll back the caller's other staged/committed writes (scope it — savepoint/nested tx — not a blanket `session.rollback()`). Update the stale `execute_approved_order` "no persistence" docstring.
- [ ] Task 3 — Calm sub-share degrade at resume (AC: 7)
  - [ ] Ensure the shared resume path (`api/cash.py:resume_pending_buy` + `whole_share_quantity`) degrades calmly when settled proceeds fall short of one whole share of `switch_to` — no 0-share un-co-signable order; a plain calm note. Cover with a test (shared with the 9.3 path).
- [ ] Task 4 — Honest two-step framing driven by server truth (AC: 6)
  - [ ] Update the cost-switch narration/fallback copy (`allocation/review.py` cost fallback) + the review UI (`CoachConsult.jsx`) so a cost finding reads as a linked two-step switch. Surface the "step 2 queued" note ONLY from the `/approve` response flag (never from `status != 'rejected'`). Clear stashed switch state on decline/re-ask/edit and on a failed placement. Keep the 10-3 gates (`check_no_invented_numbers`/`check_no_forecast`) governing any LLM text.
- [ ] Task 5 — Tests (AC: all)
  - [ ] Backend: co-signing a server-verified cost-switch SELL creates one linked `awaiting_funds` PendingBuy (`sell_decision_id` set, `switch_to` symbol, BUY/MARKET); **a client-supplied `switch_to` on an arbitrary/unheld/non-canonical/self symbol is REJECTED (no widening, no buy, SELL out-of-scope refused)**; replay is a no-op but two distinct switches both queue; no conflation with a 9.3 deploy buy sharing `(symbol, amount)`; `pending`/`timeout`-without-`broker_ref` seeds nothing and the response flag is false; per-user scoped; a non-switch SELL creates none; read endpoints still write nothing/place nothing; sub-share proceeds degrade calmly at resume; the linkage failure path never fails a placed SELL nor rolls back co-sign state.
  - [ ] Frontend: the review card frames the switch as two-step; the "step 2 queued" note appears ONLY when the `/approve` response flag is true; nothing auto-submits; stashed switch state is cleared on decline/re-ask/edit/failed-placement.

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

## Spec Change Log

### 2026-08-12 — Review loopback 1 (bad_spec): server-verify `switch_to`, harden dedupe/honesty/tx-safety

- **Triggering findings (independent review of the iteration-1 implementation):**
  - `[high]` The scope-gate widening trusted the **client-supplied `switch_to`** — any authenticated user could POST `{symbol:"GME", side:"sell", amount:"50000", switch_to:"X"}` and bypass the index-core SELL scope gate to sell an arbitrary held non-index-core security, queuing a dead pending buy of an arbitrary symbol. The cited `_placed_order_matches_proposal` guard only enforces propose==approve; both were built from the same unvalidated request, so it does NOT constrain what may be authorized. Trust boundary crossed at `/recommend`.
  - `[high]` The frontend showed "step 2 of 2 is queued" for any `status != 'rejected'`, but the backend only seeds the buy for `_is_placed` (`pending`/`timeout` require a `broker_ref`). A `pending`/`timeout`-without-`broker_ref` outcome falsely told a beginner their follow-up buy was safely queued when nothing was created — the exact stranded-cash outcome the story exists to prevent (real-broker path).
  - `[medium]` `(symbol, amount)` dedupe collided two genuinely-distinct cost-switches (same target + amount → second silently swallowed, cash stranded) and could conflate with the Story 9.3 deploy/liquidation `PendingBuy` producer.
  - `[medium]` A blanket `session.rollback()` in the linkage failure arm inside `execute_approved_order` couples to the approve handler's commit ordering and could discard co-sign state; the function's "no persistence (Story 4.9)" docstring became false (it now commits).
  - `[low]` `switch_to == symbol` (self-switch) was allowed via the raw `/recommend` path.
- **What was amended (all OUTSIDE any intent contract):** Design decisions — added a mandatory server-side re-derivation/verification of the cost-switch from the user's own holdings (never trust client `switch_to`), the dedupe-on-`sell_decision_id` rule, the explicit server-truth "linked buy queued" `/approve` flag, and the transaction-safe failure-handling + docstring requirement. Acceptance Criteria — added AC2 (server-verified only, with the arbitrary-symbol rejection G/W/T), AC5 (idempotent-without-conflation G/W/T), AC6 (server-truth honesty flag), AC7 (calm sub-share degrade G/W/T), AC8 (transaction-safe failure); renumbered scoping/read-only ACs to 9/10. Tasks — restructured into 5 tasks leading with server verification and adding dedupe/flag/tx-safety/sub-share/state-clearing subtasks + adversarial tests.
- **Known-bad state avoided:** shipping a beginner-guardrail bypass (client-trusted scope widening) and a dishonest "your buy is queued" reassurance on the real-broker settlement path.
- **KEEP (must survive re-derivation):** the approved load-bearing design — thread the VERIFIED `switch_to` through the immutable decision snapshot (additive key, `schema_version` unchanged) so `execute_approved_order` seeds the linked `PendingBuy`; reuse `PendingBuy` + the `api/cash.py` list/resume/cancel lifecycle (the BUY leg IS an `awaiting_funds` `PendingBuy` linked by `sell_decision_id`); populate-don't-submit (BUY is a second human co-sign at resume, never auto-placed); `_is_placed` gating; best-effort "never fail a placed SELL over the linkage"; the honest two-step narration copy with the 10-3 gates preserved; the real-DB register/login per-user test scaffolding.

## Review Triage Log

### 2026-08-12 — Review pass 2 (post-re-derivation)
- intent_gap: 0
- bad_spec: 0
- patch: 4: (medium 2, low 2)
- defer: 1
- reject: 3
- addressed_findings:
  - `[medium]` `[patch]` Reverse 9.3 conflation — the deploy dedupe `_find_awaiting_pending_buy` matched any `(symbol, amount)` `awaiting_funds` row, so a pre-existing cost-switch buy could be swallowed by a later deploy. Fixed: tag cost-switch buys with `buy_intent.source = "cost_switch"` and skip them in the deploy dedupe (no migration; legacy deploy rows still match). Tests added.
  - `[medium]` `[patch]` Silent linkage-failure gave no UI cue — a placed switch SELL with `linked_buy_queued == false` showed no note (silent stranded cash). Fixed: calm fallback note in `CoachConsult.jsx` naming `switch_to`. Test added.
  - `[low]` `[patch]` `linked_buy_queued` was `false` on idempotent re-approve though the buy exists. Fixed: recorded-outcome path recomputes the flag from a scoped existence check (`_recorded_linked_buy_queued`). Test added.
  - `[low]` `[patch]` Frontend `edit()` cleared the switch stash only when a recommendation was shown, contradicting its comment. Fixed: unconditional `setPendingSwitchTo(null)` on edit. Test added.
- Deferred (1): partial-fill queues a full-position-estimate linked buy rather than realized proceeds — a cross-cutting "estimate now, floor at approve" honesty convention shared with the 9.3 deploy/liquidation path; fail-safe today (resume gated on `funds_ready`). Recorded in `deferred-work.md`.
- Rejected (3): no DB unique constraint on `sell_decision_id` (double-insert is prevented by the atomic Story 6.1 cosign claim that serializes placement); forward-recovery seeding when the record is concurrently deleted mid-approve (unsupported state, negligible); private cross-module import smell (`_is_placed`/lazy `create_switch_pending_buy` — accepted api↔coach cycle-avoidance already used elsewhere).
- Verified closed from pass 1: all four bad_spec HIGH/MEDIUM findings (client-trusted `switch_to` scope bypass; false "buy queued" reassurance; `(symbol, amount)` dedupe collision; blanket-rollback co-sign corruption) — independently re-confirmed closed by the pass-2 Blind Hunter.

### 2026-08-12 — Review pass 1
- intent_gap: 0
- bad_spec: 5: (high 2, medium 2, low 1)
- patch: 0
- defer: 0
- reject: 0 (lower-severity cosmetic/edge findings — `finding?.order?.symbol` fallback, partial-fill over-queue, stale-stash cues — folded into the bad_spec re-derivation rather than tracked separately; moot since code is re-derived)
- addressed_findings:
  - `[high]` `[bad_spec]` client-trusted `switch_to` scope-gate bypass → spec now mandates server-side re-derivation/verification of the cost-switch; re-derive.
  - `[high]` `[bad_spec]` frontend false "buy queued" reassurance vs backend `_is_placed` → spec now mandates an explicit server-truth `/approve` flag driving the note; re-derive.
  - `[medium]` `[bad_spec]` `(symbol, amount)` dedupe collision + 9.3 producer conflation → spec now dedupes on `sell_decision_id`; re-derive.
  - `[medium]` `[bad_spec]` blanket `session.rollback()`/hidden commit coupling + stale docstring → spec now mandates scoped tx-safe failure handling + docstring fix; re-derive.
  - `[low]` `[bad_spec]` self-switch (`switch_to == symbol`) allowed → subsumed by server verification; re-derive.

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (bmad-dev-auto — re-derived after a bad_spec review loopback, then 4 review patches)

### Debug Log References

- Backend: `831 passed, 2 warnings in ~55s` — `ballast_test` DB, fake broker + fake LLM.
- Frontend: `21 files, 195 passed`.

### Completion Notes List

- **Iteration 1 REJECTED by independent review** (client-trusted `switch_to` scope-gate bypass + 3 other money-path gaps) → bad_spec loopback; spec amended (server-verify the cost-switch, dedupe on `sell_decision_id`, server-truth `linked_buy_queued` flag, tx-safe failure handling) and code re-derived.
- **Re-derivation:** `/recommend` calls `_verify_cost_switch` — re-derives cost findings from the user's OWN holdings (`get_portfolio` + `_aggregate_by_symbol` + `find_cost_findings`) and threads `switch_to` onto the immutable snapshot ONLY for a genuine server-verified cost-switch; a raw client `switch_to` never widens the scope gate. The linked `awaiting_funds` `PendingBuy` is seeded in the `/approve` handler (`_maybe_queue_switch_buy`) AFTER the cosign commit, gated on `_is_placed(outcome)`, deduped on `sell_decision_id`; `execute_approved_order` keeps its no-persistence contract. `ApproveResponse.linked_buy_queued` is server truth and drives the frontend two-step note.
- **Pass-2 patches:** deploy dedupe now skips `buy_intent.source == "cost_switch"` rows (reverse-conflation); calm UI fallback note when a placed switch SELL fails to queue the buy; `linked_buy_queued` recomputed on idempotent re-approve; `edit()` clears the switch stash unconditionally.
- No DB migration (reused `PendingBuy`; cost-switch rows self-tag via the JSON `buy_intent.source`).

### File List

- `ballast/backend/api/coach.py` — `RecommendRequest.switch_to`; `_verify_cost_switch` (server re-derivation); thread verified `switch_to` + bind SELL intent; `_maybe_queue_switch_buy` (tx-safe, post-cosign); `ApproveResponse.linked_buy_queued` + `_recorded_linked_buy_queued` (server-truth on replay).
- `ballast/backend/coach/execution.py` — `_is_placed`; SELL scope-gate widened ONLY for a server-verified `switch_to`; docstring keeps the no-persistence contract explicit.
- `ballast/backend/api/cash.py` — `create_switch_pending_buy` (dedupe on `sell_decision_id`, self-tags `source:"cost_switch"`); `_find_awaiting_pending_buy_by_sell_decision`; deploy dedupe `_find_awaiting_pending_buy` skips cost-switch rows.
- `ballast/backend/coach/decision_record.py` — `record_proposal` writes the additive verified `switch_to` snapshot key (`schema_version` unchanged).
- `ballast/backend/allocation/review.py` — cost narration/fallback/prompts reframed as a linked two-step switch (10-3 gates preserved).
- `ballast/frontend/src/components/CoachConsult.jsx` — two-step framing; `switch_to` threaded to `/recommend`; "step 2 queued" note + calm failure fallback driven by `linked_buy_queued`; stash cleared on decline/re-ask/edit/failed placement.
- `ballast/backend/tests/test_cost_switch_linked_buy.py` — NEW adversarial backend tests (incl. arbitrary/non-canonical/self `switch_to` rejection, distinct-switch/9.3 non-conflation both directions, per-user scope, replay-flag, sub-share degrade, linkage-failure safety).
- `ballast/frontend/src/test/portfolio-review.test.jsx` — Story 10.5 frontend tests (two-step framing; server-truth note; failure fallback; stash-clear).

## Auto Run Result

Status: done

**Summary:** Co-signing a **server-verified** cost-switch SELL now durably queues one linked deferred BUY of the cheaper canonical fund (`switch_to`) in `awaiting_funds`, linked via `sell_decision_id`, reusing the Epic 9 Story 9.3 `PendingBuy` machinery — so a beginner who co-signs a fee-switch SELL is never silently stranded in cash. Populate-don't-submit throughout: the BUY resumes as an explicit second human co-sign via the existing deferred-buy surface once cash settles.

**Review:** iteration-1 implementation was REJECTED by independent review for a money-path guardrail bypass (client-trusted `switch_to` widening the SELL scope gate) + 3 other gaps → 1 bad_spec loopback (spec amended: server-verify the cost-switch, dedupe on `sell_decision_id`, server-truth `linked_buy_queued`, tx-safe failure handling). Re-derivation passed pass-2 review with the 4 originals confirmed closed; pass 2 applied 4 patches (2 medium, 2 low — reverse-conflation dedupe fix, silent-failure UI cue, replay flag, unconditional stash clear), deferred 1 (partial-fill estimate vs realized proceeds — cross-cutting with 9.3), rejected 3.

**Verification:** Backend `831 passed`; Frontend `195 passed` (both suites, `ballast_test` DB / fake broker + fake LLM). The arbitrary-`switch_to` bypass is closed and covered by a dedicated adversarial test.

**Residual risk / follow-up:** `followup_review_recommended: true` — the pass-2 patches (notably the deploy-dedupe reverse-conflation fix, which touches the shared 9.3 money-path) were applied without a subsequent independent review pass. One deferred honesty item (linked-buy amount = pre-fill estimate, not realized proceeds on a PARTIAL fill) recorded in `deferred-work.md`.
