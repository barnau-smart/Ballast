# Story 8.2 (BRIEF): Resting-Order Lifecycle + GTC + Cancel (Order Interface Expansion — Story B1 of 3)

Status: backlog — planning brief only (scope LOCKED by MasterB 2026-08-04)

> **This is a scoping brief, NOT a ready-for-dev story.** Generate the ultimate-context story via `/bmad-create-story 8-2` **after Story 8.1 is `done`** (ideally code-reviewed first), so it incorporates 8.1's real learnings (final enum shapes, adapter branch structure, test patterns). This brief locks scope + decisions so that pass doesn't have to re-derive them.

## Feature / Goal

Story A (8.1) added marketable LIMIT orders that fill immediately. **Story B1 delivers MasterB's actual goal: place a resting order that does NOT fill immediately — "buy near the low and wait" — and cancel it if you change your mind.** It removes 8.1's fill-immediate constraint for limits, adds **GTC** so a resting order can wait across days (a DAY order expires at the close), and adds **cancel** (there is none today). This is the capability the whole order-interface expansion exists for, and it unblocks the zero-money live-broker seam proof.

**This story also resolves the 6.7 partial-fill terminality decision** (Epic 6 open action item): a resting order that partially fills then completes is exactly the deferred case.

## Scope split (decided 2026-08-04)

- **B1 (THIS story, 8.2):** resting-limit lifecycle + **GTC** + **cancel** + zero-money seam proof + partial-fill terminality decision.
- **B2 (DEFERRED — likely cut for a beginner app):** STOP / STOP_LIMIT and extended sessions (AM/PM). See "Deferred to B2" below. These stay REJECTED by `validate_order_intent` exactly as in 8.1 — do NOT enable them here.

## Grounded SDK / code findings (verified 2026-08-04, installed schwab-py)

- **GTC is a one-line override, NOT a hand-roll.** `equity_buy_limit(sym, qty, price).set_duration(Duration.GOOD_TILL_CANCEL).build()` verified → `orderType=LIMIT, duration=GOOD_TILL_CANCEL, session=NORMAL`. So B1 reuses the 8.1 convenience builders and just overrides duration for GTC. (The 8.1 builders hardcode DAY/NORMAL; overriding duration on the returned `OrderBuilder` works.)
- **Cancel is feasible:** `client.cancel_order` + `client.get_order` exist. `BrokerPort` has NO cancel today (verified: authorize/exchange/fetch/place/get_status/get_status_by_ref only).
- **`set_session`/`set_duration` exist** on `OrderBuilder` (needed for GTC now; AM/PM later).
- **STOP is the reason B2 is bigger:** there are **NO `equity_*_stop` convenience builders** in the installed SDK (only market/limit). STOP/STOP_LIMIT would need a hand-rolled generic `OrderBuilder` (`set_order_type(STOP)`, `set_stop_price`, single equity leg) — extra work + risk, and a beginner footgun. This correction is why B2 is deferred/cut.

## Locked design decisions (honor; from [[order-interface-expansion-plan]])

- Still human-entered overrides on `/approve`; the LLM coach never proposes non-market orders (`RECOMMENDATION_OUTPUT_SCHEMA` stays market-only).
- Keep `is_index_core(symbol)` for all order types.
- Backward compatible — 8.1's defaults (MARKET/REGULAR/DAY) and the whole existing market + marketable-limit flow stay unchanged.
- Reuse Story 6.7's durable reconcile machinery — do NOT rebuild it.

## B1 scope (detail)

- **Drop the marketable guard for resting limits.** In BOTH adapters, 8.1's `OrderNotPlaceableError` "not marketable — coming later" refusal becomes: a non-marketable limit is a legitimate **resting** order that co-signs with a `pending`/working outcome and is reconciled later. The **sub-share sizing refusal STAYS** (`floor(amount/limit_price) < 1` is still `OrderNotPlaceableError`). The fake adapter must deterministically return a `pending` (not filled) outcome for a non-marketable resting limit, carrying a stable `broker_ref` so the reconcile-by-ref path can resolve it.
- **Working/pending co-sign + async re-reconcile.** A non-immediately-filled order co-signs with a `pending` outcome (never a phantom fill) and is resolved through the EXISTING durable path: `reconcile_pending_decision` → `get_order_status_by_ref(broker_ref)` (`coach/execution.py:221`; both adapters implement it) → `record_reconciliation` (write-latest, monotonic-toward-settlement, `decision_record.py:618`). B1 mostly *enables* orders to land in that path; verify `pending → filled/partial/rejected` flows through it end-to-end.
- **GTC:** extend `validate_order_intent` to ACCEPT `Duration.GTC` (currently rejected in 8.1). Adapter: override duration on the limit builder (`.set_duration(GOOD_TILL_CANCEL)` per the verified finding). Snapshot serialization already emits `duration` omit-when-default (8.1), so a GTC order additively carries `duration: "gtc"`. `Session.AM/PM` and `OrderType.STOP/STOP_LIMIT` STAY rejected.
- **Cancel** (the only genuinely new port surface — treat with AD-6/AD-7 rigor):
  - Add `cancel_order(broker_ref)` to `BrokerPort` + both adapters (schwab-py `client.cancel_order`; fake cancels deterministically).
  - New Coach-Engine cancel owner (mirror the AD-7 sole-caller discipline — no API handler calls the broker directly).
  - New `POST /api/coach/decisions/{id}/cancel` endpoint (mirror the `/reconcile` pattern, `api/coach.py:676`), per-user scoped, live-session gated.
  - Cancel must be **idempotent + scoped + refuse a terminal order calmly** (never cancel a filled/rejected order; a 422/calm envelope, never a 500). A cancelled order maps to `rejected`/cancelled and is not re-placeable.
- **Partial-fill terminality:** DECIDED — allow a `partial` to re-reconcile up to `filled` (partial becomes NON-terminal for reconcile). Touches `_TERMINAL_STATUSES` (`decision_record.py:584`) and `INDETERMINATE` (`execution.py:67`). Encode with a test; close the Epic 6 action item.

## Out of scope (Story B1)

- Any UI / order-entry form / beginner warnings → Story C (8.3).
- **The "AI suggests & populates the order" backend** (compute a resting-limit price from the live quote + `MarketDaily` OHLC highs/lows; AI writes the reasoning) → belongs with **Story C** (the button that consumes it), NOT here. B1 is the execution lifecycle only. Recommended design when scoped: backend computes the price deterministically, LLM only narrates. Not yet finalized — see [[order-interface-expansion-plan]].
- The LLM ever proposing a non-market order → never.
- Scheduler-wiring of the 7.2 reclaimer/pruner → separate go-live item.

## Deferred to B2 (8.2b / later — likely cut for a beginner app)

- **STOP / STOP_LIMIT** execution (needs a hand-rolled generic `OrderBuilder` — no convenience builder exists; `set_order_type(STOP)` + `set_stop_price`).
- **Extended sessions (AM/PM)** via explicit `set_session`.
- These remain rejected by `validate_order_intent` (as in 8.1). Only revisit if MasterB wants them; they're beginner footguns and the most work.

## Draft acceptance criteria (B1)

1. A non-marketable resting BUY LIMIT co-signs `pending` (never a phantom fill), carries a `broker_ref`; a later `/reconcile` resolves it truthfully (fake adapter, deterministic).
2. A GTC limit places with `duration: "gtc"` on the built payload (assert `.build()`; no live call) and its cosign snapshot additively carries `duration: "gtc"`; DAY stays the default (unchanged).
3. That resting order can be **cancelled** via the new cancel endpoint; a cancelled order maps to `rejected`/cancelled and is not re-placeable; cancel is idempotent + scoped + refuses a terminal order calmly (never a 500).
4. Partial-fill terminality is decided + encoded + tested (partial → re-reconcilable to filled); the Epic 6 action item is closed.
5. The zero-money seam proof — place a non-marketable resting limit → confirm `pending` mapping → cancel → never fills → costs nothing — runs against the fake adapter deterministically and is documented as the live-broker rehearsal.
6. All 8.1 + existing tests stay green; the market + marketable-limit flows are unchanged; STOP/STOP_LIMIT/AM/PM stay rejected.

## Risks / notes for the create-story pass

- Cancel is the only new *port surface* — AD-6/AD-7 sole-writer/sole-caller rigor (new cancel owner in `coach/execution.py`, sole caller of `broker.cancel_order`).
- Leans on Story 6.7's durable reconcile-by-ref (already built) — confirm it needs *enabling*, not rebuilding.
- The fake adapter needs a way to return a resting `pending` outcome deterministically (e.g., branch on non-marketable limit → `pending` + `broker_ref`, seed-able for a later reconcile — the `seed_order_status_by_ref` helper already exists).
- Watch the "partial becomes non-terminal" change against the monotonic guard in `record_reconciliation` — a partial must still never regress to `pending`, only advance to `filled`.

## References

- [Source: ballast/backend/brokers/port.py] — no cancel today; place/get_status/get_status_by_ref contract to extend.
- [Source: ballast/backend/brokers/fake_adapter.py] — 8.1 `_limit_fill` marketable guard to relax for resting; `seed_order_status_by_ref` for the reconcile seam.
- [Source: ballast/backend/brokers/schwab_adapter/adapter.py] — 8.1 limit branch; `.set_duration()` override for GTC; `client.cancel_order` for cancel.
- [Source: ballast/backend/coach/execution.py:67, 145, 221-294] — `INDETERMINATE`, `execute_approved_order`, `reconcile_pending_decision` (durable reconcile to reuse); new cancel owner lands here.
- [Source: ballast/backend/coach/decision_record.py:584-668] — `_TERMINAL_STATUSES`, `record_reconciliation` (partial-fill terminality).
- [Source: ballast/backend/api/coach.py:676-763] — `/reconcile` endpoint pattern to mirror for `/cancel`.
- [Memory: [[order-interface-expansion-plan]], [[epic6-live-trade-decisions]], [[atomicity-gap-blocks-live]]].
