# Real-money readiness — Allocation Coach (Epic 10)

The money PATH is proven (Epic 7 placed a real SCHB buy end-to-end). But the Epic
9/10 deploy / cost-switch / liquidation logic has only ever run against the FAKE
broker. Close the gates below before a real-money deploy exercise.

## Gate 1 (CRITICAL) — settled / deployable cash

**Finding.** The deploy engine's `ready_to_trade` is Schwab
`securitiesAccount.currentBalances.cashBalance` (`ballast/backend/brokers/schwab_adapter/adapter.py:272`),
and the engine treats it as fully spendable. But **`cashBalance` is the TOTAL cash
balance** — on a cash account it can include **unsettled** proceeds (a recent sale
that hasn't cleared, T+1). Deploying against unsettled cash risks a good-faith /
settlement rejection. Epic 9's "genuinely spendable" premise wanted *deployable*
cash. (This is Epic 9 action item #2a.)

**Recommendation (grounded in Schwab Trader API semantics).** For a cash account
the field that reflects what is actually available to place new trades — net of
unsettled funds and holds — is `currentBalances.cashAvailableForTrading`, not
`cashBalance`. Likely fix: source cash from `cashAvailableForTrading` (or
`min(cashBalance, cashAvailableForTrading)` to be conservative). **Caveat:** on a
MARGIN account `cashAvailableForTrading` can include margin buying power (which would
OVER-state settled cash). Ballast targets cash accounts, but we confirm against the
real payload before changing it.

**Verify against YOUR account (needs your live Schwab connection):**
1. Run in real-broker mode: `BALLAST_REAL_BROKER=1 ./scripts/dev.sh`. ⚠️ Real path —
   but a portfolio READ places no orders; connecting + reading is safe.
2. Connect Schwab (OAuth). The app reconciles your portfolio read-only.
3. Capture your account's `currentBalances` fields — specifically the values of:
   `cashBalance`, `cashAvailableForTrading`, `availableFunds`,
   `availableFundsNonMarginableTrade`.
4. Share those 4 numbers. If `cashBalance` exceeds the available/settled figure, I
   switch the adapter to the correct field (a ~1-line change) + a regression test.

*Mechanism for step 3 (NOW IN PLACE):* the adapter logs a
`schwab_currentBalances_diagnostic` line on every real portfolio read (figures only —
no tokens/PII), listing the returned field names + `cashBalance` /
`cashAvailableForTrading` / `availableFunds` / `availableFundsNonMarginableTrade`. So:
connect once in real-broker mode, find that line in the app log, and paste it. (I
remove the diagnostic once we confirm the correct field and switch the adapter.)

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
