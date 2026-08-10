# Epic 9 Context: Cash Intelligence — Honest Idle-Cash

<!-- Generated from planning artifacts. Regenerate with compile-epic-context if planning docs change. -->

## Goal

Ballast today treats "cash" as one undifferentiated pool, which makes the Missed-Growth meter dishonest: it under-counts by ignoring money parked in money-market funds, yet naively folding those in would over-count by ignoring the yield they already earn. Money-market money also cannot be spent directly — it must be sold and settled first, a step the app neither models nor surfaces. This epic teaches Ballast to distinguish three kinds of cash — **ready-to-trade** (settlement cash, instantly spendable, ~0% yield), **parked** (user-specified money-market funds, one liquidation+settlement step from spendable, earning yield), and **reserved** (a user-declared amount they never want to invest) — so missed-growth is computed only on genuinely investable money, yield-aware, and the buy flow honestly respects that money-market funds aren't directly tradeable. It matters because the feature that could scare a nervous beginner ("you're missing growth") must instead visibly protect them.

## Stories

- Story 9-1: Cash-state model + reserve/parked-funds declaration (foundation)
- Story 9-2: Honest, yield-aware missed-growth
- Story 9-3: Just-in-time liquidation + deferred/resumed buy

## Requirements & Constraints

- **Reserve is user-owned and honest-by-construction.** A first-run step requires the user to explicitly set OR decline a reserve — never silently assumed. After that, an unset reserve is legitimately 0. Reserve is optional, zero-allowed, and editable later. The user owns buffers Ballast can't see (e.g. bank savings); the app can never fully know the reserve — it can only be declared, never inferred.
- **Reserve is drawn from money-market first,** keeping ~0%-yield settlement cash liquid for trading (money-market still earns yield and is liquid enough for emergencies).
- **Missed-growth base = settlement cash + user-specified money-market funds − reserve** (only genuinely investable money). Yield-aware: settlement cash misses the full benchmark return; money-market money misses benchmark return minus its own yield.
- **Nudges are capped and reserve-framed.** Never nudge on the full amount; always show the protected reserve alongside so the figure can never read as "invest everything." No unprompted FOMO/"you're missing out" alerts — ever (pull, not push: features respond to the user, never nag).
- **Data freshness must be surfaced** (as-of dates) so stale prices can't make a calm-looking number silently wrong.
- **Liquidation is just-in-time only** — surfaced solely at the buy step when settlement cash is insufficient for a decided purchase; never a proactive "go liquidate" nudge.
- **Never place a buy without settled funds.** The app pre-populates the money-market liquidation (sell) order for the exact needed amount and stops, waiting for the user to review and submit. A human always submits; no fully automated liquidation.
- **Deferred/resumed buy.** Because a sell may not settle instantly, on settlement a durable "pending buy" card resumes the original intended buy, pre-filled, for the user to submit. It must persist so a missed notification can't lose it (web-only notification OK for v1).
- **Beginner-investor honesty/calm framing is a hard constraint.** Coach voice is patient, warm, plain-spoken, never alarmist, never hype — a reviewable acceptance criterion. Losses/negatives shown in sky-blue, never red; screens stay calmest when the user is most anxious.

## Technical Decisions

- **Extend the existing precedent/missed-growth engine, don't replace it.** Missed-growth already lives in the `precedent` package computed over `market_daily` daily data. Story 9-2 refines that same calculation to be cash-state-aware and yield-aware.
- **The Precedent Engine is the sole source of market statistics;** the Coach Engine is the sole writer of decision records; the Broker Port is the sole path to brokerage state. No module bypasses an owner. The LLM never computes numbers — it narrates and cites only IDs it was handed.
- **Evidence records carry an `as_of`** field in their fixed shape `{id, kind, statement, stats{}, source, as_of}` — reuse this to satisfy the data-freshness requirement rather than inventing a new mechanism.
- **Liquidation reuses the propose-and-approve DNA of the 8.4 "suggest & populate the order" button:** backend deterministically computes and pre-fills the sell order, LLM narrates only, the human executes via the /approve path. No new execution paradigm.
- **9-1 is already built** (cash_config table, `/api/cash/config`, additive `cash_states` on the portfolio read) and is the foundation 9-2 and 9-3 build on.

## UX & Interaction Patterns

- **Missed-growth meter is quiet and always-available,** framed as information not pressure — stated once, calm ("your idle cash has sat out ~$X of growth"), with the protected reserve shown alongside.
- **The liquidation/deferred-buy flow mirrors the coach card:** a pre-filled order the user reviews and submits (Approve/Co-sign style), routing to a calm confirmation. Never auto-executes.
- Follow the existing calm-terminal visual language: no urgency, no red for the user's own money, reasoning always visible as real text.

## Cross-Story Dependencies

- 9-2 and 9-3 both depend on 9-1's three-state cash model, reserve/parked declaration, and the `cash_states` portfolio field (9-1 done).
- 9-3 depends on the Broker Port sell-order path and reuses the Epic 8.4 suggest-and-populate + `propose → approve` execution pipeline.
- 9-2 depends on the existing `precedent`/missed-growth engine and `market_daily` data.
