# Story 10.11: Close the human-coach no-balance cash-cover gap

Status: draft-awaiting-approval
baseline_commit: 29983a2

<!-- HARD GATE (docs/dev-loop-policy.md, per-story-spec-approval): DRAFT — AWAITING MasterB
     approval of the load-bearing approach (A vs B below). MONEY-PATH: this closes the one
     reachable margin path the 10-9 known-cash refinement left open. Mandatory independent
     review before merge. -->

## Story

As an investor whose linked account hasn't refreshed its balance yet,
I want a hand-entered coach BUY to be refused (or to force a fresh balance) rather than placed uncovered,
so that the "never buy on margin" guarantee holds even on the human-coach path — not just the deploy/suggest paths.

## Context — the residual from the 10-9 independent review

Story 10-9's cash-cover gate (`coach/execution.py:_assert_buy_covered_by_settled_cash`) enforces
only against a KNOWN balance (`view.as_of is not None`) — a deliberate refinement so it never
fabricates a $0 refusal from absent data and doesn't break the suite. The 10-9 review confirmed
the **deploy** and **suggest** BUY paths are fully covered: neither can produce a co-signable BUY
for a user with no balance row (`build_plan` returns `no_cash`; `suggest` refuses at `<1` share).

**The one reachable gap (logged in `deferred-work.md`):** the **human-coach** path — an `/approve`
where the proposed decision carried NO server-derived `order_intent` (`_snap_intent is None`,
`api/coach.py`), so the BUY `amount` is an ARBITRARY hand-entered value. On a linked-but-never-
refreshed account (`portfolio_balance.as_of is None`), the coverage gate does NOT fire, so such a
BUY could place on margin. Narrow (requires a linked account with no cached balance + a hand-
entered BUY), strictly out of 10-9's scoped deploy hole, and backstopped in normal use because a
linked account is refreshed before trading — but it IS a real money-safety residual and the whole
point of 10-9 is "no margin, ever." This story closes it.

## Design — TWO approaches (MasterB picks; this is the approval gate)

The gate lives in `execute_approved_order`, which sees only `order_intent`/`switch_to` — NOT the
decision snapshot. The "arbitrary hand-entered amount" signal (`_snap_intent is None`) lives in the
`/approve` handler. So the fix is scoped to the human-coach path to avoid the 10-9 blast radius (a
blanket "no balance → refuse" would wrongly block every mechanics test + a legitimate not-yet-
imported user's server-derived buys).

### Approach A — Fail-closed the hand-entered BUY when cash is unknown (RECOMMENDED)
In the `/approve` handler, when the proposed decision carries NO `order_intent` snapshot
(`_snap_intent is None`) AND `body.order_intent.side == BUY`, require a KNOWN balance: if
`get_portfolio(scope, session).as_of is None`, refuse with a calm 422 ("We need an up-to-date
balance before this buy — refresh your portfolio and try again") and release the claim (retryable),
BEFORE calling `execute_approved_order`. Server-derived BUYs (deploy/suggest — which always carry a
snapshot intent) are unaffected; SELLs are never gated. Minimal, no new import flow.

### Approach B — Force a balance refresh before a human-coach BUY
Before a hand-entered BUY `/approve`, trigger a `reconcile_portfolio` (a fresh broker read) so
`as_of` is populated and the existing 10-9 gate then enforces coverage on real, fresh cash. More
robust (also fixes cache-staleness for this path) but heavier: adds a broker read on the approve
path, needs a live reading broker + failure handling, and overlaps Story 10-12 (cache-staleness).

**Rejected:** a blanket "scope present + `as_of is None` → refuse" in `_assert_buy_covered_by_settled_cash`
— reintroduces the 10-9 blast radius (breaks every mechanics test that seeds no balance + blocks a
legitimate user's server-derived buys). The distinction must be the hand-entered/no-snapshot signal.

## Acceptance Criteria (finalize at approval)

1. A hand-entered coach BUY (`_snap_intent is None`, side BUY) on an account with no cached balance
   (`as_of is None`) is REFUSED calmly (422, claim released/retryable, broker never touched) —
   never placed on margin.
2. A server-derived BUY (deploy/suggest — carries a snapshot `order_intent`) is UNAFFECTED (the
   existing 10-9 gate governs it; a no-balance user can't produce one anyway).
3. SELLs are never affected. Idempotent re-approve (`cosigned`) is unaffected.
4. Coverage on a KNOWN balance is unchanged (10-9 behavior intact); the existing suite stays green
   (the mechanics tests that seed no balance but DO carry a snapshot intent are not refused).
5. Per-user scoped, read-only cash read; calm copy (no red/alarm); Decimal money.

## Dev Notes

- `api/coach.py:approve` (~:920-970) — `_snap_intent = _snapshot.get("order_intent")`; the
  hand-entered path is `_snap_intent is None`. Add the fail-closed check here (Approach A), symmetric
  with the existing `_placed_order_matches_proposal` refusal arm (release claim → calm 422).
- `coach/execution.py:_assert_buy_covered_by_settled_cash` / `InsufficientSettledCashError` (Story 10-9) — the existing gate; do NOT widen its no-balance behavior (keep known-cash-only).
- `brokers/portfolio.py:get_portfolio` — `view.as_of is None` ⇔ no imported balance row.
- Tests: a linked user, no balance row, hand-entered BUY `/approve` → 422 + broker untouched; a
  seeded-balance hand-entered BUY still governed by 10-9; a server-derived (snapshot-intent) BUY with
  no balance is unaffected; SELL unaffected; existing `test_coach_api` green.

### References
- [Source: _bmad-output/implementation-artifacts/deferred-work.md] — the 10-9 human-coach residual entry.
- [Source: _bmad-output/implementation-artifacts/10-9-backend-cash-cover-safety.md] — the known-cash refinement this completes.
- [Source: ballast/backend/api/coach.py#approve] · [Source: ballast/backend/coach/execution.py#_assert_buy_covered_by_settled_cash]

## Dev Agent Record

### Agent Model Used

_(to be filled by dev)_

### Completion Notes List

### File List
