---
title: 'Story 9.3: Just-in-time liquidation + deferred/resumed buy'
type: 'feature'
created: '2026-08-10'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: false
baseline_commit: d93decd8f217e0b731e8bdca896007c3efc61215
baseline_revision: d93decd8f217e0b731e8bdca896007c3efc61215
final_revision: ba380b161040e80327c560e24dffa7ea6bab43fc
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-9-context.md'
warnings:
  - oversized
---

<intent-contract>

## Intent

**Problem:** When a beginner decides to buy an index-core ETF but their instantly-spendable (ready-to-trade) cash is short, Ballast has no honest path: money-market "parked" funds must be sold and settled first, a step the app neither surfaces nor resumes — so the buy either silently fails or the user loses the intent. This is the final honesty gap in Epic 9's three-state cash model.

**Approach:** At the buy step only (never a proactive nudge), when ready-to-trade cash is insufficient, deterministically compute and pre-fill a money-market **SELL** for the shortfall (LLM narrates only; human submits via the existing `/approve` co-sign path), and durably persist a **pending buy** that resumes the original buy — pre-filled — once ready-to-trade cash actually covers it. Reuses the 8.4 suggest-and-populate + propose→approve DNA and the 9-1 cash-state model; adds one durable table.

## Boundaries & Constraints

**Always:**
- Liquidation is **just-in-time only** — surfaced solely when a decided buy's amount exceeds `ready_to_trade` cash. Never a proactive "go liquidate" nudge; never push (pull-only, web surfaces on visit).
- **Never place a buy without settled funds.** The app pre-fills the sell and stops; a human always submits both the sell and the resumed buy via `/approve`. No fully automated liquidation, no auto-buy.
- Liquidation draws from **parked money-market funds only, above the declared reserve** — the protected reserve is never liquidated. The sell reuses `whole_share_quantity` sizing and amount-based `OrderIntent` exactly as buys do.
- The pending buy is **durable** (own table, scoped per owner) so a missed/unseen notification can't lose the intent; it survives restarts and persists until the user resumes or cancels it.
- Resumption keys off **actually-observed settled cash** (`ready_to_trade` from `portfolio_balance`), never a fabricated T+2 timer. `funds_ready = ready_to_trade >= pending buy amount`.
- The execution scope gate permits a **SELL** when the symbol is index-core **OR** a symbol the user has declared as `parked_symbols`; **BUY** stays index-core-only. Untradeable funds surface the existing calm 422, never a dead-end (the pending buy persists).
- Beginner-honest calm framing: no red for the user's own money (sky-blue for negatives), no FOMO/hype, reasoning shown as real text, **data freshness (`as_of`) surfaced** on every liquidation figure. Money crosses the wire as fixed-point strings (`WireMoney`).

**Block If:**
- The epic requires the liquidation sell to execute via `/approve`, which is impossible without widening the SELL scope gate — this is directly implied by the epic and is implemented (not blocked). Block only if a NEW contradiction with the hardened money-path (`coach/execution.py`) surfaces that can't be resolved by the parked-symbol widening above.

**Never:**
- Never auto-submit any order (sell or buy). Never liquidate the reserve. Never chain multiple parked funds in one plan (v1 sells the single largest-value parked holding; partial coverage is surfaced honestly).
- Never add a background/scheduled settlement poller (v1 is pull-only). Never introduce a live-quote dependency into the *plan* step (price parked holdings off cached `market_value/quantity` with `as_of`); live pricing happens only at `/approve` placement.
- Never emit an unprompted FOMO alert. Never present the pre-reserve amount as spendable.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Sufficient cash | buy amount ≤ ready_to_trade | `needs_liquidation:false`; buy proceeds normally, no sell, no pending buy | none |
| Shortfall, parked covers it | ready_to_trade < amount; available parked ≥ shortfall | `needs_liquidation:true, coverable:true`; pre-filled SELL (largest parked, amount≈shortfall) + durable pending buy + proposed sell decision returned | none |
| Shortfall, partial parked | available parked (above reserve) < shortfall | `coverable:false`; SELL sized to the whole parked holding, honest "covers $Y of $X"; pending buy still created | none |
| Shortfall, no parked / all reserved | available parked = 0 | `needs_liquidation:true, coverable:false`; calm "nothing to liquidate — this will resume when cash settles"; pending buy created, no sell | none |
| Pending buy, funds ready | ready_to_trade ≥ pending amount | `GET pending-buys` → `funds_ready:true`; resume returns pre-filled buy intent + proposed buy decision | none |
| Resume before ready | funds_ready:false | resume refused | calm 409 "funds haven't settled yet" |
| SELL of parked at /approve | side=SELL, symbol in parked_symbols | scope gate permits; placed via existing path | untradeable fund → existing calm 422; pending buy persists |
| SELL of non-core non-parked | side=SELL, symbol neither | scope gate refuses | calm 422 (OrderScopeError) |
| Unauthenticated | no session | all endpoints refuse | 401 |

</intent-contract>

## Code Map

- `ballast/backend/db/models.py` -- add `PendingBuy` table (OwnedEntityMixin): `id`, `owner_id`, `buy_intent` JSON (original `OrderIntent` snapshot), `amount` Numeric(20,2) (hoisted for `funds_ready` compare), `status` String ('awaiting_funds'|'resumed'|'cancelled'), `sell_decision_id` UUID|None, `created_at`, `resumed_at`|None, `cancelled_at`|None. Index `(owner_id, status)`.
- `ballast/backend/db/migrations.py` -- add idempotent `CREATE TABLE IF NOT EXISTS pending_buy (...)` + `CREATE INDEX IF NOT EXISTS` for carried-over DBs (fresh DBs get it from `create_all`); follow the existing three-phase pattern.
- `ballast/backend/cash/liquidation.py` -- NEW deterministic planner: `plan_liquidation(scope, session, *, buy_symbol, buy_amount, buy_intent, as_of) -> LiquidationPlan`. Reads `ready_to_trade` (portfolio balance), holdings, `CashConfig`; computes shortfall, available parked (`parked_market_value − resolved reserve`, floored at 0), selects largest-value parked holding, sizes the SELL amount + est shares (`whole_share_quantity`), sets `coverable`, carries `as_of`. Pure/deterministic (no LLM, no live quote). Plus `narrate_liquidation(gateway, facts) -> str` (LLM narrates only, degrades to templated fallback — mirror `coach/suggest.narrate_suggestion`).
- `ballast/backend/api/cash.py` -- add endpoints: `POST /liquidation-plan`, `GET /pending-buys`, `POST /pending-buys/{id}/resume`, `POST /pending-buys/{id}/cancel`. Reuse `record_proposal` to mint the proposed sell/buy decisions; money as fixed-point strings; 401 gate via `get_scope`.
- `ballast/backend/coach/execution.py` -- widen the order scope gate in `execute_approved_order`: a SELL is in-scope when `is_index_core(symbol)` OR the symbol is in the scope user's `parked_symbols` (read-only load). BUY unchanged (index-core only). Keep all existing session-integrity / validation / claim-release behavior.
- `ballast/backend/coach/decision_record.py` -- reuse `record_proposal` for the sell (and the resumed buy). No lifecycle change; `sell_decision_id` links back from `PendingBuy`.
- `ballast/frontend/src/components/LiquidationCard.jsx` (+ `.css`) -- calm pre-filled SELL card (symbol, ~amount, est shares, `as_of`, protected-reserve line, honest partial/untradeable copy); submits via the existing `/approve` flow. No red, no FOMO.
- `ballast/frontend/src/components/PendingBuyCard.jsx` (+ `.css`) -- durable pending-buy surface; when `funds_ready`, a calm "resume your buy" that calls resume then `/approve`. Persists across sessions; pull-only.
- `ballast/frontend/src/components/CoachConsult.jsx` -- at the buy approve step, when `ready_to_trade < amount`, fetch `/liquidation-plan` and render `LiquidationCard` instead of a hard failure.
- `ballast/frontend/src/App.jsx` (or the dashboard route that renders `PortfolioPanel`) -- mount `PendingBuyCard` so pending buys surface on visit.

## Tasks & Acceptance

**Execution:**
- [x] `ballast/backend/db/models.py` -- Add the `PendingBuy` model per Code Map (OwnedEntityMixin, tz-aware UTC timestamps, `amount` as `Mapped[Decimal]` Numeric(20,2), index `(owner_id, status)`).
- [x] `ballast/backend/db/migrations.py` -- Add idempotent `CREATE TABLE IF NOT EXISTS pending_buy` + its index to the startup migration statements (carried-over DBs); verify `create_all` covers fresh DBs.
- [x] `ballast/backend/cash/liquidation.py` -- Implement `plan_liquidation` (deterministic, reserve-aware, parked-only, single-largest-holding, `whole_share_quantity` sizing, `as_of` freshness, fixed-point-safe result) and `narrate_liquidation` (LLM narrate-only with deterministic fallback). Return a frozen `LiquidationPlan` dataclass carrying: `needs_liquidation`, `coverable`, `ready_to_trade`, `shortfall`, `sell_symbol|None`, `sell_amount|None`, `est_shares|None`, `sell_order_intent|None`, `reserved|None`, `reserve_decided`, `as_of`, `reasoning`.
- [x] `ballast/backend/coach/execution.py` -- Widen the SELL scope gate to permit declared `parked_symbols` (BUY unchanged); preserve session-integrity, `validate_order_intent`, atomic-claim, and claim-release paths.
- [x] `ballast/backend/api/cash.py` -- Add the four endpoints. `POST /liquidation-plan` computes the plan; when `needs_liquidation`, create (idempotently — dedupe an existing `awaiting_funds` PendingBuy for the same `(owner, buy_symbol, amount)`) a durable PendingBuy, and when `coverable` also a proposed sell decision (`record_proposal`), returning `sell_decision_id`/`pending_buy_id`. `GET /pending-buys` lists `awaiting_funds` with computed `funds_ready` + `as_of`. `POST /pending-buys/{id}/resume` guards `funds_ready` (else calm 409), mints a proposed buy decision from `buy_intent`, transitions `awaiting_funds→resumed`, returns `decision_id` + pre-filled buy intent. `POST /pending-buys/{id}/cancel` transitions `→cancelled`. All scoped (401 unauth), money as fixed-point strings.
- [x] `ballast/frontend/src/components/LiquidationCard.jsx` (+ `.css`) -- Render the plan calmly (pre-filled SELL, est shares, `as_of` freshness, protected-reserve line, honest partial/untradeable/`coverable:false` copy) and submit the sell via the existing `/approve` handler. Preserve no-red / no-FOMO.
- [x] `ballast/frontend/src/components/PendingBuyCard.jsx` (+ `.css`) -- Render durable pending buys; show a calm "resume your buy" action only when `funds_ready`, otherwise a calm "waiting for funds to settle" state; wire resume→`/approve`; offer cancel. No red, no nudge.
- [x] `ballast/frontend/src/components/CoachConsult.jsx` -- At the buy approve step, when `ready_to_trade < amount`, fetch `/api/cash/liquidation-plan` and render `LiquidationCard` in place of a hard failure; keep the sufficient-cash path unchanged.
- [x] `ballast/frontend/src/App.jsx` (dashboard route) -- Mount `PendingBuyCard` so pending buys surface on visit (pull-only).
- [x] `ballast/backend/tests/test_liquidation.py` -- Unit-test the planner I/O-matrix rows: sufficient cash, coverable shortfall, partial (parked < shortfall), no-parked/all-reserved, reserve-aware exclusion, determinism, fixed-point JSON, `as_of` carried.
- [x] `ballast/backend/tests/test_cash_liquidation_endpoint.py` -- Endpoint tests: plan creates PendingBuy (+ proposed sell when coverable) and dedupes on repeat; `pending-buys` computes `funds_ready`; resume guarded on `funds_ready` (409 when not) and transitions to `resumed`; cancel transitions to `cancelled`; 401 unauth.
- [x] `ballast/backend/tests/test_execution.py` -- Add: SELL of a declared parked symbol is permitted; SELL of a non-core non-parked symbol is refused (OrderScopeError→422); BUY still index-core-only.
- [x] `ballast/frontend/src/test/liquidation.test.jsx`, `ballast/frontend/src/test/pending-buy.test.jsx` -- Assert calm voice, no `brand-red|accent-pink|line-red`, `as_of` shown, protected-reserve rendered, resume only when `funds_ready`, no FOMO copy.

**Acceptance Criteria:**
- Given a decided buy whose amount exceeds ready-to-trade cash and enough parked money-market value above the reserve, when the user reaches the buy step, then a calm pre-filled money-market SELL for the shortfall is shown (with `as_of` freshness and the protected reserve alongside), a durable pending buy is recorded, and nothing is placed until the human submits the sell via `/approve`.
- Given a pending buy exists and ready-to-trade cash has since risen to cover it, when the user next opens the app, then the pending buy surfaces as `funds_ready` with the original buy pre-filled, and resuming routes through the unchanged `/approve` co-sign flow; resuming before funds are ready is calmly refused.
- Given a parked money-market holding, when the human approves its SELL, then the widened scope gate permits it while a BUY remains index-core-only, and an untradeable fund surfaces the existing calm 422 without losing the pending buy.
- Given the change set, when backend `pytest` and frontend `vitest` run, then all pass (existing money-path/execution tests unchanged except the intended SELL widening), the new table is created idempotently on both fresh and carried-over DBs, and all new copy passes the calm/no-red/no-FOMO bars with money as fixed-point strings.

## Review Triage Log

### 2026-08-10 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 4: (high 0, medium 2, low 2)
- defer: 3: (high 0, medium 2, low 1)
- reject: 12
- addressed_findings:
  - `[medium]` `[patch]` **Honesty bug:** `coverable` was computed off the AGGREGATE parked value (`available_parked >= shortfall`) while v1 only sells the SINGLE largest parked holding — so with several parked funds whose sum covers the shortfall but no single one does, the plan returned `coverable=True` alongside a `sell_amount < shortfall`, producing dishonest "fully covered" copy (violates the epic's hard honesty constraint). Fixed in `cash/liquidation.py`: `coverable = sellable >= shortfall` where `sellable = min(largest_holding.market_value, available_parked)` — the reserve-aware capacity of the ONE holding actually sold. Regression test `test_coverable_reflects_single_holding_not_aggregate` locks it (two funds $500+$400, $600 shortfall → `coverable=False`, sells $500).
  - `[medium]` `[patch]` **Fail-not-closed money path:** `/liquidation-plan` accepted `amount` raw (unlike the 9-1 reserve, which is guarded by `_validate_reserve`) — a finite over-range amount (`1e19`) 500s at commit, an over-precision amount (3+ dp) is silently rounded (dishonest), and a non-positive amount is silently treated as "covered". Added `_validate_buy_amount` (finite, `>0`, `< 10**18`, ≤2dp → calm 422; quantizes to 2dp so the pending-buy dedupe key is stable) in `api/cash.py`, mirroring the 9-1 guard. Regression test `test_plan_rejects_invalid_buy_amount` covers 0/negative/3dp/NaN/Infinity/over-range and asserts no durable pending buy is minted.
  - `[low]` `[patch]` `LiquidationCard.jsx` rendered "(about 0 shares)" when `est_shares==0` (contract-permitted when the cached unit price exceeds the sell amount). Guarded the shares clause on `est_shares > 0`.
  - `[low]` `[patch]` The carried-over-DB `CREATE TABLE pending_buy` omitted the ORM `status` default, diverging from the `create_all` DDL. Added `DEFAULT 'awaiting_funds'` for parity (`db/migrations.py`).
- rejected/verified-safe (grounded against source):
  - Orphaned proposed SELL on a partial write: NOT reachable — `record_proposal` does NOT commit (caller does); the sell proposal + pending-buy insert share ONE `session.commit()`, so a failure rolls back both atomically (verified in `coach/decision_record.py` + `api/cash.py`).
  - `parked_market_value` `TypeError`/negative on `None`/negative `market_value`: the `portfolio_cache.market_value` column is `Mapped[Decimal]` (non-nullable) and index/money-market holdings aren't negative — same pre-existing assumption 9-2 already verified-safe; not confidently reachable → reject (not re-deferred).
  - Concurrent two-tab resume minting two proposed BUY decisions: consistent with the pre-existing `/recommend` propose semantics (a user can always mint multiple proposals); the `/approve` atomic claim + per-decision idempotency key still gate placement. Low reachability → reject.
  - `coverable=false` pending buy "stuck forever": BY DESIGN — the durable intent persists until the user funds it (deposit/manual sell) or cancels; that persistence is the story's point, and cancel is the explicit exit.
  - `as_of` "Invalid Date" on the card: already guarded (`{plan.as_of ? … : null}`); backend sends an ISO date or null, never an unparseable string.
  - SELL of a declared-but-unheld parked symbol passing the widened gate; `funds_ready:false` with `as_of:null`; `side` casing compare; evidence-id keyed on `today()` when `as_of` is null; a post-plan cash DROP not re-checked on the frontend; a sub-cent shortfall (impossible with 2dp-validated inputs): all either broker-backstopped, cosmetic, or not confidently reachable → reject.
- deferred: 3 (see `deferred-work.md`) — (1) aggregate over-draw across multiple concurrent `awaiting_funds` pending buys (each reads `funds_ready` against total cash in isolation; broker-backstopped at placement but the UI can optimistically show two-as-ready); (2) on dedupe, the freshly-computed `sell_order_intent` is returned with the STALE linked `sell_decision_id`, so a cash change between visits makes the card's sell amount drift from its decision snapshot (Decisions-history/replay cosmetic; placement matches what the user saw); (3) `/approve` trusts `order_intent` verbatim (no reconciliation against the decision snapshot) — a pre-existing property that the SELL scope-widening ENLARGES (a user can repurpose their own proposed decision_id to place an arbitrary-amount SELL of their own parked fund; bounded to the user's own money, not privilege escalation).

### 2026-08-10 — Review pass (follow-up)
- intent_gap: 0
- bad_spec: 0
- patch: 2: (high 1, medium 0, low 1)
- defer: 2: (high 0, medium 2, low 0)
- reject: 9
- addressed_findings:
  - `[high]` `[patch]` **Partial-coverage SELL was a UI dead-end with self-contradictory copy.** `POST /liquidation-plan` minted the proposed SELL decision only when fully `coverable`, so a PARTIAL shortfall (single largest parked fund covers only part) returned a populated `sell_order_intent` but `sell_decision_id=null`. `LiquidationCard`'s `hasSell` requires the decision id → it rendered the "nothing to sell" branch while `plan.reasoning` simultaneously said "covers $Y of $X" — a dead-end that contradicts the intent-contract's "pre-fills the sell… human always submits via /approve, never a dead-end" and the partial I/O-matrix row. Fixed in `api/cash.py`: mint the proposed SELL whenever `sell_order_intent is not None` (coverable OR partial), so the partial sell is submittable (free up what you can now; the rest resumes). Updated both docstrings. Added endpoint regression `test_plan_partial_coverage_mints_submittable_sell_decision` (parked $300 vs $900 short → `coverable=False`, `sell_amount="300.00"`, non-null `sell_decision_id` status `proposed`). Root cause of the miss: the frontend PARTIAL fixture spread `sell_decision_id:'dec-1'` from COVERABLE, masking the real `null`; fixed the fixture to carry its own id and strengthened `liquidation.test.jsx` to assert the sell block + `/approve` control render and the "nothing" branch does NOT.
  - `[low]` `[patch]` `LiquidationCard.jsx` rendered "Your $0.00 reserve stays protected" for a decided-and-declined reserve (`resolve_reserve → 0.00`, non-null). The card guarded only on `reserved != null` while the backend `_fallback_reasoning` guards on `reserved > 0`. Aligned the card to `reserved != null && Number(reserved) > 0` so it never promises to protect a zero reserve.
- deferred: 2 (see `deferred-work.md`) — (1) the idempotent-dedupe read in `/liquidation-plan` is a non-atomic SELECT with no unique constraint, so concurrent same-`(owner,symbol,amount)` plan requests can each mint a duplicate PendingBuy + proposed SELL (broker-backstopped; distinct from the existing aggregate-over-draw entry); (2) `/liquidation-plan` is a state-mutating POST fired automatically from a presentation `useEffect` in `CoachConsult.jsx`, so viewing the buy step alone writes durable rows (dedupe prevents dup rows but write-on-render is fragile and pairs with (1)).
- rejected/verified-safe (grounded against source):
  - Client-side `Number(amount) >= Number(ready_to_trade)` gate in `CoachConsult.jsx`: a WireMoney-on-client smell, but it only decides whether to FETCH the plan — the backend recomputes authoritatively and short-circuits `needs_liquidation:false`, so no wrong outcome (backstopped) → reject.
  - `date.today()` in the evidence id / `as_of` when `as_of` is null (now also on the resume path): the prior pass already rejected this as cosmetic; deterministic-replay smell only, no user/money impact → reject.
  - `_order_intent_from_snapshot` enum coercion unguarded (`OrderSide/OrderType/Session/Duration` `ValueError` → 500 on the pending-buys list): only reachable with a corrupt/hand-edited `buy_intent` row; the endpoint only ever WRITES valid enums → not confidently reachable → reject.
  - Negative `ready_to_trade` overstating `shortfall`: Ballast is cash-only (no margin), so `view.cash` isn't negative in practice → not reachable → reject.
  - Resume amount source mismatch (`pending.amount` gate vs `buy_intent["amount"]` pre-fill): both are written together in one insert and never diverge; invariant holds by construction → reject.
  - Post-409 stale Resume button in `PendingBuyCard.jsx` (genuine not-settled 409 leaves the button visible): re-clicking just re-shows the same calm 409 message; cosmetic, no harm → reject.
  - `pending_buy.sell_decision_id` has no FK/ON DELETE (a pruned proposed decision dangles the link): a facet of the already-deferred dedupe/stale-`sell_decision_id` entry (the card re-fetches and re-surfaces), not a new issue → reject.
  - `funds_ready` computed against an un-synced portfolio placeholder (cash 0, `as_of` null): yields a correct "waiting" state; benign → reject.
  - Degenerate `coverable=True` with no sell (sub-cent headroom rounds `sell_amount` to 0): unreachable with 2dp-validated inputs — when `sell_amount` rounds to 0, `sellable < shortfall` so `coverable` is necessarily `False`; prior pass rejected the same class → reject.

### 2026-08-10 — Review pass (follow-up 2)
- intent_gap: 0
- bad_spec: 0
- patch: 0
- defer: 1: (high 0, medium 0, low 1)
- reject: 18
- addressed_findings:
  - none
- deferred: 1 (see `deferred-work.md`) — both new pending-buy reads (`GET /pending-buys` and the plan-time dedupe lookup) use `repo.list()` (full-table `SELECT *` materializing all resumed/cancelled rows, filtered in Python) instead of `list_page` with a SQL-side `status` filter, so the `(owner_id, status)` index this story added for that read goes unused and the read grows unbounded (low consequence at single-user scale; same class as the logged 6.6/4.9 index/unbounded-table entries).
- rejected/verified-safe (grounded against source):
  - `/approve` trusts `order_intent` verbatim + SELL scope widening enlarges the blast radius; concurrent non-atomic plan-dedupe minting duplicate PendingBuy/SELL; write-on-render mutating POST from a `useEffect`: all THREE already captured as open ledger entries from prior passes → not re-deferred (orchestrator owns them).
  - `parked_market_value` (sums all parked) vs `_largest_parked_holding` (skips non-finite/non-positive) filter divergence: `sellable = min(largest.market_value, available_parked)` already caps coverable honesty (the first pass's regression), and negative/NaN `market_value` is unreachable (`Mapped[Decimal]` non-nullable, no negative money-market rows) — prior pass already rejected this reachability → reject.
  - `sell_amount>0` but `est_shares` floors to 0 (high-NAV parked fund vs small shortfall): prior pass explicitly deemed `est_shares==0` contract-permitted and fixed only the display; a <1-share refusal at `/approve` surfaces the same calm 422 as the untradeable-fund path the contract calls "never a dead-end (pending buy persists)" → reject.
  - Resume→cash-drop→approve TOCTOU (funds move after `funds_ready`): broker-backstopped at placement (index-core BUY, `/approve` whole-share sizing + insufficient-funds rejection is authoritative), same class as the already-deferred aggregate-over-draw entry → reject.
  - Parked symbol de-listed between plan and approve → SELL refused: reading LIVE `parked_symbols` at approve is correct-by-design (never sell what's no longer declared parked); outcome is the contract's calm 422 + persistent pending buy → reject.
  - Corrupted `buy_intent` snapshot → 500 on resume/list (bracket access, unguarded enum coercion): prior pass already rejected — only reachable with a hand-edited row; the endpoint only ever writes valid enums → reject.
  - Negative `ready_to_trade` overstates shortfall: prior pass already rejected — Ballast is cash-only (no margin), `view.cash` isn't negative in practice → reject.
  - Migration `DEFAULT 'awaiting_funds'` server-default vs ORM Python-side default drift; `sell_decision_id` no FK/ON DELETE; `OrderIntentField.amount` typed bare `str` not `WireMoney`; client-side `Number()` money gate deciding whether to fetch: all cosmetic/no functional impact (app always supplies `status`; outbound money always via `format_money`; backend recomputes authoritatively) — prior passes rejected the FK and client-gate variants → reject.
  - Cancel returns 409 for a `resumed` row with no UI affordance to abandon the minted proposed BUY: the proposed BUY is inert until co-signed and is swept by the stale-`proposed` pruner; unusual path, low consequence → reject.
  - Migration FK assumes legacy `user.id` is UUID: the app has always used UUID user ids → not reachable → reject.
  - `_liquidation_blessed` mints decisions with self-authored evidence + a canned uncertainty string to satisfy the validator: by-design (the story reuses `record_proposal` for deterministic proposals, mirroring the 8.4 suggest DNA) → reject.

## Design Notes

**Why pull-only resumption (no timer):** the system has no T+2 settlement tracking and must never fabricate one. `funds_ready` is computed live as `ready_to_trade >= amount` from the authoritative `portfolio_balance`, so the resumed-buy card appears exactly when the money is genuinely spendable — honest by construction and matching the epic's "pull, not push."

**Deterministic sell sizing (plan step, no live quote):** price the parked holding off its cached `unit = market_value / quantity` (money-market NAV is stable), size `est_shares = whole_share_quantity(sell_amount, unit)`, and always surface `as_of` so a stale price can't make the figure silently wrong. Live pricing/whole-share flooring happens for real only at `/approve` placement (adapter sizes off the live ask), exactly as buys already work — the plan is an honest estimate, not a promise.

**Reserve-aware, parked-only:** `available_parked = max(parked_market_value − resolve_reserve, 0)`. The declared reserve is never liquidated; `coverable = available_parked >= shortfall`. When not coverable, the plan is honest ("this covers $Y of $X" or "nothing to liquidate yet") and still records the durable pending buy so the intent survives.

**Scope-gate widening (bounded):** the index-core gate exists to stop beginners *buying* random securities; *selling* one's own declared parked cash-equivalent to fund an in-scope buy is squarely safe. The gate widens for SELL only, and only to the user's own `parked_symbols` — the minimal change the epic's "human executes via the /approve path" requires. All other execution hardening (session integrity, validation, atomic claim, claim release) is untouched.

## Verification

**Commands:**
- `cd ballast/backend && DATABASE_URL=<disposable ballast_test> BALLAST_ALLOW_DIRTY_BROKERAGE_DB=1 uv run pytest -q tests/test_liquidation.py tests/test_cash_liquidation_endpoint.py tests/test_execution.py` -- expected: all pass (planner matrix, endpoints, scope-gate widening).
- `cd ballast/backend && DATABASE_URL=<disposable ballast_test> BALLAST_ALLOW_DIRTY_BROKERAGE_DB=1 uv run pytest -q` -- expected: full suite green (no money-path regressions; the new table migrates idempotently).
- `cd ballast/frontend && npm test` -- expected: `liquidation.test.jsx`, `pending-buy.test.jsx`, and the rest pass; no red/FOMO.

**Manual checks:**
- Confirm `pending_buy` is created on a fresh DB (via `create_all`) and on a carried-over DB (via the `CREATE TABLE IF NOT EXISTS` migration) — no ALTER of existing tables.
- Confirm every liquidation/pending-buy response renders money as fixed-point strings and carries `as_of`; confirm the reserve is never included in liquidatable value.



## Auto Run Result

Status: done

### Follow-up review pass 2 (2026-08-10)

Re-review of a `done` spec (fresh follow-up pass, no re-implementation). Ran Blind Hunter (`bmad-review-adversarial-general`) and Edge Case Hunter (`bmad-review-edge-case-hunter`) in parallel over the full diff since baseline `d93decd8`, both grounded against source.

**Change under review (unchanged this pass):** just-in-time liquidation planner + durable pending-buy resume — `cash/liquidation.py` planner + `narrate_liquidation`, four `api/cash.py` endpoints, widened SELL scope gate in `coach/execution.py`, `PendingBuy` model + idempotent migration, and the `LiquidationCard`/`PendingBuyCard`/`CoachConsult` frontend surfaces.

**Findings breakdown:** intent_gap 0 · bad_spec 0 · patch 0 · defer 1 · reject 18.
- **Deferred (1, NEW):** both new pending-buy reads use `repo.list()` (full-table `SELECT *`, Python-side status filter) instead of `list_page` with a SQL `status` filter, leaving the `(owner_id, status)` index this story added unused and the read unbounded. Low consequence at single-user scale; appended to `deferred-work.md`.
- **Rejected (18):** three of the sharpest findings (verbatim `order_intent` trust enlarged by the SELL widening; non-atomic dedupe minting duplicate PendingBuy/SELL; write-on-render mutating POST) are already OPEN ledger entries from prior passes — not re-deferred (orchestrator owns them). The rest were grounded-out as unreachable (negative/NaN `market_value`, negative `ready_to_trade`, corrupt `buy_intent`, non-UUID legacy `user.id`), correct-by-design (live parked-scope read at approve, `est_shares==0` contract-permitted, deterministic `_liquidation_blessed` proposal), or cosmetic (server-default drift, missing FK, bare-`str` money typing, client-side `Number()` fetch gate, cancel-after-resume 409).

**Verification:** No code changed this pass (defer-only outcome), so the suite was not re-run — the finalized `final_revision` from the prior pass was already green (`test_liquidation.py`, `test_cash_liquidation_endpoint.py`, `test_execution.py`, full backend suite, frontend `vitest`). Re-running against a live-linked DB carries the known brokerage-token-wipe risk for zero benefit here.

**Follow-up review recommendation:** false — this pass made no review-driven code changes; the sole outcome was one low-severity NEW deferred entry.

**Residual risks:** the three highest-consequence properties remain OPEN deferred entries (verbatim-`order_intent` trust bounded to the user's own parked funds; concurrent dedupe TOCTOU; write-on-render POST) — all broker-backstopped at `/approve` (nothing auto-places) with no money error, tracked for a focused later pass.
