# Intent: Cash-Readiness & Honest Idle-Cash Intelligence (Ballast)

**One-liner:** Teach Ballast (a beginner-investor coaching app) to distinguish three kinds of "cash" — ready-to-trade, parked-in-money-market, and reserved — so the Missed-Growth meter is honest and the buy flow respects that money-market money isn't directly tradeable.

## Problem

Ballast currently treats "cash" as one undifferentiated thing. The Missed-Growth meter measures only settlement cash, so it **under-counts** (ignores money parked in money-market funds) — yet naively folding those funds in would **over-count**, because it would ignore the yield they already earn. Separately, money-market money **cannot be used to buy directly**: it must be sold and settled first, a step the app neither models nor surfaces. Reserved money the user never intends to invest is also invisible, so any "you're missing growth" nudge risks scaring people about money that was never in play.

## Core Model — three cash states

1. **Ready-to-trade** — settlement cash. Instantly spendable, ~0% yield.
2. **Parked** — user-specified money-market funds (e.g. SWVXX). One step from spendable (must liquidate + settle), earns yield.
3. **Reserved** — user-declared amount they never want to touch. Optional, personal to each user's risk threshold; lives partly outside what Ballast can see (bank savings), so the app can never fully know it.

Key properties that drive everything: a dollar's **speed-to-deploy** and **yield** are readable from the account; the **reserve** lives in the user and can only be *declared*, never inferred.

## Decisions locked

- **Reserve is user-owned and honest-by-construction:** a first-run step requires the user to **explicitly set OR decline** a reserve — never silently assumed. After that, an unset reserve is legitimately treated as 0. Reserve is **optional, zero-allowed, and editable** later. The user is responsible for buffers Ballast can't see.
- **Reserve is drawn from money-market first,** keeping settlement cash liquid for trading (money-market still earns yield and is liquid enough for emergencies; don't waste 0%-yield settlement cash holding a buffer).
- **Missed-growth base = settlement cash + user-specified money-market funds − reserve** (i.e. computed only on genuinely *investable* money).
- **Yield-aware math:** settlement cash misses the *full* benchmark return; money-market money misses *benchmark return minus its own yield*.
- **Nudges are capped and reserve-framed:** never nudge on the full amount; always show the protected reserve alongside so the number can never read as "invest everything."
- **Liquidation is just-in-time only:** it surfaces solely at the buy step when settlement cash is insufficient for a decided purchase — never a proactive "go liquidate" nudge.
- **Propose-and-approve, same as 8.4:** at the shortfall moment the app **pre-populates the money-market liquidation (sell) order** with the exact needed amount and stops, waiting for the user to review + submit. **Never places a buy without settled funds.**
- **Deferred/resumed buy:** because the sell may not settle instantly, on settlement a **durable "pending buy" card + notification** (web-only OK for v1) resumes the original intended buy, pre-filled, for the user to submit. The pending buy persists so it can't be lost if a notification is missed.

## Failure modes guarded against

- **FOMO — stampeding the reserve into the market.** → Cap the nudge; compute only on post-reserve money; always show the reserve as protected.
- **Stale prices** (e.g. from before a crash) making calm-looking numbers silently wrong. → Surface data freshness / as-of so figures never lie by omission.
- **Settlement-timing gap** stranding the user between the sell and the buy. → Never place a buy without settled funds; durable pending-buy card + settlement notification resumes the pre-filled buy.

## Out of v1 scope

- Auto-classifying tickers as money-market (prefer user-specified — simpler, honest).
- External / bank cash Ballast can't see (user owns that).
- Fully automated liquidation (human always submits).

## Notes for story creation

- This is a **natural extension of existing patterns**, not a new paradigm: the liquidation step reuses the **propose-and-approve DNA of the 8.4 "suggest & populate the order"** button, and the calculation extends the existing **`precedent/missed_growth`** engine.
- Likely a **new small epic** (Epic 8 is closed).
- **Beginner-investor honesty framing is a hard constraint** — calm, non-alarmist, never FOMO; the feature that could scare users must instead visibly protect them.
