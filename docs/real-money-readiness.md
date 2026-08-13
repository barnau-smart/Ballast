# Real-money readiness — Allocation Coach (Epic 10)

The money PATH is proven (Epic 7 placed a real SCHB buy end-to-end). But the Epic
9/10 deploy / cost-switch / liquidation logic has only ever run against the FAKE
broker. Close the gates below before a real-money deploy exercise.

## Gate 1 — deployable cash ✅ RESOLVED (keep `cashBalance`)

**RESOLVED 2026-08-13 — keep `cashBalance`.** A live diagnostic against the real
account (a **margin** account) returned:

- `cashBalance` = **$12,182.82** (the field the deploy engine uses)
- `cashAvailableForTrading` = **absent** — not a field Schwab returns for this account
- `availableFunds` / `availableFundsNonMarginableTrade` = **$45,302.54** — these include
  **margin buying power**, far exceeding cash.

**Conclusion: `cashBalance` is correct — no adapter change.** It is the conservative
*cash* anchor. Every other candidate Schwab exposes is a *buying-power* figure
(margin / SMA) — sourcing cash from one would let the deploy coach suggest buying on
**borrowed money**, contrary to Ballast's no-leverage ethos. There is no settled-cash
field to switch to, and the larger ones are the wrong direction. The earlier worry
("`cashBalance` overstates via unsettled") is second-order on a margin account
(margin covers unsettled — no good-faith violation). The one-time diagnostic log has
been removed; the adapter comment records the rationale.

**Two residual notes (not blocking):**
- **The real account is a MARGIN account.** The engine is already margin-safe (anchors
  on `cashBalance`, never buying power; clamps negative cash to 0). But Ballast is
  designed for conservative cash-account investors — a product decision worth making:
  *detect + gently warn* about a margin account rather than silently treat its cash as
  deployable. (New Epic 10 follow-up.)
- **Cash-account generalization:** for a future *cash*-account user, `cashBalance` could
  include unsettled funds and Schwab may expose no cleaner field — revisit the
  settled-cash question if/when a cash-account user is onboarded.

## Gate 2 — tune the disclosed placeholder

`DEFAULT_MONEY_MARKET_APY = 0.04` (yield-aware missed-growth, Story 9-2) is a
disclosed placeholder. Set it to your money-market fund's real APY, or wire a
source, before relying on the missed-growth framing. (Epic 9 action item #4.)

## Gate 3 — attended real-money deploy exercise

With gates 1–2 closed, do ONE small real deploy end-to-end
(propose → approve → place → reconcile), you at the keyboard — mirroring Epic 7's
first real SCHB buy. Then exercise the cost-switch linked SELL+BUY once. Keep the
dollar amount tiny.

## Already safe (no action)

Real trading is opt-in ONLY (`BALLAST_REAL_BROKER=1` + `BROKER_ADAPTER=schwab` +
creds; `scripts/dev.sh` forces the fake broker otherwise). The co-sign gate,
whole-share sizing, and durable reconciliation are all live-proven.
