# Story 10.12: Keep settled cash fresh across sequential buys

Status: review
baseline_commit: 6f37e66

<!-- HARD GATE (docs/dev-loop-policy.md, per-story-spec-approval): APPROVED by MasterB 2026-08-13 —
     approval of the design (Option 1 recommended). MONEY-PATH: the Story-10.9 cover gate reads
     a CACHED balance that isn't refreshed after a fill, so sequential in-session buys can
     overdraw on stale cash. Mandatory independent review before merge. -->

## Story

As an investor placing more than one buy in a session,
I want each buy checked against my cash *after* the previous buy spent it,
so that a second co-signed buy can't quietly overdraw (and dip into margin) because Ballast was still looking at my pre-buy cash.

## Context — the reachable staleness gap (from the 10-9 review)

Story 10-9's cover gate (`coach/execution.py:_assert_buy_covered_by_settled_cash`) refuses a BUY
beyond `max(0, view.cash)` — but `view.cash` is the CACHED `portfolio_balance.cash`, written only
by `reconcile_portfolio` (at link/import and manual refresh). **`/approve` does NOT refresh the
balance after a fill** (it persists `broker_ref` + cosigns, nothing more). So:

- **Reachable in-Ballast case (the real risk):** cash $750, buy #1 $500 fills → real cash is now
  $250, but the cached balance still says $750. Buy #2 for $500 PASSES the 10-9 gate (500 ≤ 750)
  and, on a margin account, fills the $250 shortfall on margin. Two normal co-signed buys in one
  session silently overdraw.
- **Lower-risk cases (separate/deferred):** cash spent OUTSIDE Ballast since the last refresh; and
  `cashBalance` possibly including UNSETTLED funds (Epic 9 #2a — second-order on a margin account
  since margin covers unsettled; needs a live cash-account to confirm — see "Out of scope" below).

The 10-9 Edge Case Hunter flagged this as a Medium residual. This story closes the reachable
in-Ballast case cheaply and conservatively.

## Design — RECOMMENDED: decrement cached settled cash after a filled BUY (Option 1)

After a co-signed BUY genuinely places with an executed cost (a `filled`/`partial` outcome carrying
`filled_qty` × `avg_price`), **debit the cached `portfolio_balance.cash` by that executed cost**, so
the next buy's 10-9 cover gate sees the reduced cash. Details:

- **BUY only, executed cost only.** Debit `filled_qty * avg_price` (the real amount spent), on a
  `filled` or `partial` fill. A `rejected`/`pending`/`timeout` outcome spends nothing → no debit.
- **Never increment on a SELL.** SELL proceeds do NOT settle immediately (T+1); crediting them to
  cached settled cash would let a later buy spend UNSETTLED money — the exact margin hole. So this
  is a one-way, conservative DEBIT: it can only make the next buy MORE conservative, never less.
- **No network read, no holdings replace.** Unlike a full `reconcile_portfolio`, this touches only
  the cash figure — cheap, and it can't clobber holdings or race the reconcile-wins `as_of` gate.
- **No-op when there is no balance row** (`as_of is None`) → the whole existing approve-BUY test
  harness (which never seeds a balance) is UNAFFECTED (near-zero blast radius, unlike a
  reconcile-before-gate approach which would populate FAKE_CASH and refuse the `$5,000` test buys).
- **Single-writer respected (AD-14).** The write goes through a NEW sanctioned helper in the
  balance-owning module (`brokers/portfolio.py`, e.g. `debit_cash(scope, session, amount)`) — the
  portfolio module stays the sole writer of `portfolio_balance`; `/approve` calls it, never writes
  the row directly. Placed AFTER the cosign commit (best-effort, wrapped) so a debit failure can
  never fail the placed order nor roll back the co-sign (mirrors the 10-5 linked-buy seeding).
- **A manual/link refresh still fully reconciles** (authoritative), correcting any drift from the
  running debit — the debit is a between-refreshes conservative approximation, not a new source of
  truth.

### Rejected / deferred alternatives
- **Reconcile-before-gate at `/approve`** — a full broker read + holdings replace on every approve:
  adds latency to every co-sign, replaces holdings mid-approve, and has real blast radius (populates
  FAKE_CASH → refuses the `$5,000` approve tests + any asserting portfolio state). Heavier for no
  extra safety over the debit for the in-Ballast case; the outside-spend case it would catch is
  lower-risk. Rejected for v1.
- **Reconcile-AFTER-fill (full)** — same holdings-replace/latency cost; the debit gets the same
  cash-safety for the reachable case without it.

## Acceptance Criteria (finalize at approval)

1. After a co-signed BUY fills (`filled`/`partial`), the cached `portfolio_balance.cash` is debited
   by the executed cost (`filled_qty * avg_price`), so a subsequent buy's 10-9 cover gate sees the
   reduced cash. **G/W/T:** Given cash $750 and a filled $500 buy, When a second $500 buy is
   co-signed, Then it is refused by the 10-9 gate (only ~$250 remains) — no margin.
2. A `rejected`/`pending`/`timeout` BUY debits nothing (nothing was spent). A SELL never changes the
   cached cash (proceeds are unsettled — never credited).
3. No-op when the user has no balance row (`as_of is None`) — existing behavior/tests unaffected.
4. The debit goes through the balance-owning module (AD-14 single-writer preserved); it is
   best-effort AFTER the cosign commit — a debit failure never fails the placed order nor rolls back
   the co-sign. Per-user scoped; Decimal money; never drives cash below 0 (`max(0, …)`).
5. A subsequent manual/link `reconcile_portfolio` still writes the authoritative balance (the debit
   is corrected by the next real refresh).

## Out of scope (kept honest)
- **Outside-Ballast spend** between refreshes and **settled-vs-unsettled `cashBalance`** (Epic 9
  #2a) — the latter needs a live *cash* account to confirm whether Schwab exposes a settled-only
  field; second-order on MasterB's margin account (margin covers unsettled). Tracked separately;
  this story does not add a network read to `/approve`.

## Dev Notes

- `api/coach.py:approve` — after `cosign(...)` + commit (the same best-effort slot as
  `_maybe_queue_switch_buy`, Story 10-5), when `intent.side == BUY` and `_is_placed(outcome)` with a
  `filled_qty`/`avg_price`, call the new `debit_cash`. Wrap so a failure never disturbs the co-sign.
- `brokers/portfolio.py` — new `debit_cash(scope, session, amount)`: scoped read-modify-write (or a
  conditional `UPDATE portfolio_balance SET cash = GREATEST(cash - :amt, 0)`), the sole balance
  writer; no-op when no row. Keep money `Decimal`/`Numeric`.
- `coach/execution.py:_assert_buy_covered_by_settled_cash` — unchanged; it simply reads the now-fresher cached cash.
- `brokers/port.py:OrderOutcome` — `filled_qty` / `avg_price` are the executed-cost inputs.
- Tests: seed balance $750 → filled $500 buy debits to $250 → second $500 buy refused (10-9); a
  `pending`/`rejected` buy debits nothing; a SELL never credits; no-balance-row is a no-op (existing
  approve tests stay green); debit failure doesn't fail the placed order; per-user scope.

### References
- [Source: _bmad-output/implementation-artifacts/10-9-backend-cash-cover-safety.md#Independent Review Findings] — the cached-balance staleness residual.
- [Source: _bmad-output/implementation-artifacts/deferred-work.md] — the 10-9 cache-staleness entry (+ Epic 9 #2a).
- [Source: ballast/backend/api/coach.py#approve] · [Source: ballast/backend/brokers/portfolio.py#reconcile_portfolio] · [Source: ballast/backend/coach/execution.py#_assert_buy_covered_by_settled_cash]
- [Source: docs/dev-loop-policy.md] — per-story spec-approval + independent review before merge.

## Dev Agent Record

### Agent Model Used

_(to be filled by dev)_

### Completion Notes List

### File List

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (bmad-dev-story, in-chat)

### Completion Notes List

- **`brokers/portfolio.py:debit_cash(scope, session, amount)`** — the sole balance writer's
  new one-way debit: scoped `UPDATE portfolio_balance SET cash = GREATEST(cash - amount, 0)`
  (clamps ≥ 0), commits, returns `True` iff a row was debited. No-op on system scope,
  non-finite/≤0 amount, or no balance row (rowcount 0). AD-14 single-writer preserved
  (`/approve` calls this; never writes the row itself).
- **`api/coach.py`** — new best-effort `_maybe_debit_cash_for_buy` (BUY + `_is_placed` +
  executed `filled_qty`×`avg_price` > 0 → `debit_cash`), called in BOTH `/approve` return
  paths AFTER the cosign commit, wrapped so a debit failure never fails the placed/cosigned
  order (mirrors `_maybe_queue_switch_buy`). SELLs never credit; rejected/pending/timeout
  debit nothing.
- **Money-safety:** the executed cost = the real spend (fake MARKET fills `amount/100 × 100`
  = amount exactly). A second buy now sees the reduced cached cash and the 10-9 gate refuses
  an overdraw. Conservative (one-way), no network read, no holdings replace.
- **Zero blast radius:** no-op without a balance row → the existing approve-BUY harness (which
  seeds no balance) is unaffected. Backend 884 passed (877 + 7 new).
- `followup_review_recommended: true` — money-path; mandatory independent review before merge.

### File List

- `ballast/backend/brokers/portfolio.py` — `func` import; `debit_cash` sole-writer helper.
- `ballast/backend/api/coach.py` — import `debit_cash`; `_maybe_debit_cash_for_buy`; call in both `/approve` return paths.
- `ballast/backend/tests/test_portfolio.py` — +5 `debit_cash` unit tests (reduces / clamps-at-0 / no-row no-op / ignores ≤0 / per-user scope).
- `ballast/backend/tests/test_coach_api.py` — +2 integration tests (sequential-buy overdraw blocked; SELL never credits) + helpers.

## Change Log

- 2026-08-13 — Story 10.12 implemented: after a co-signed BUY fills, debit the cached
  `portfolio_balance.cash` by the executed cost (sole-writer `debit_cash`, best-effort after
  cosign) so a subsequent buy's Story-10.9 cover gate sees the reduced cash — closing the
  sequential-buy overdraw/margin gap. One-way (never credits a SELL — proceeds unsettled),
  clamped ≥ 0, no network read, no-op without a balance row. +7 tests, backend 884 passed.
  Money-path → awaiting mandatory independent review before merge.
