# Story 8.6 (BRIEF): Suggestion Honesty — Fill-Likelihood + Falling-Market Floor + Data Freshness

Status: backlog — planning brief only (captured 2026-08-04 from a MasterB design review of the 8.4 pricing heuristic)

> **This is a scoping brief, NOT a ready-for-dev story.** Per the strict-bmad workflow, generate the actual ready-for-dev story via `/bmad-create-story 8-6` when it's picked up. This brief captures the scope so the idea isn't lost. **These are honesty/robustness refinements to the 8.4 suggest path — NOT correctness bugs (8.4 is safe as shipped: it always buys below market, rounds down, filters bad data, refuses when it can't compute).**

## Feature / Goal

Story 8.4 shipped the "AI: suggest & populate the order" button: the backend deterministically computes a resting BUY LIMIT at `round_down_2dp( min(recent_20d_low, live_ask) * (1 − 1%) )` and the LLM narrates it. A design review surfaced three *honesty/robustness* gaps — none of which risk money, but all of which affect whether the user understands what will happen. Story 8.6 makes the suggestion **honest about its own fill likelihood and data quality**, in the app's calm/never-red voice.

## Locked design principle (unchanged from 8.4)

- The **backend still owns the number**; the LLM only narrates. Nothing here lets the model compute or nudge price. These changes add *backend-computed context* (a fill-likelihood estimate, a floor, a freshness check) that the narration then explains — same architecture.

## Scope (three refinements, ranked)

1. **Fill-likelihood honesty (PRIMARY).** Index funds trend up, so a suggestion anchored to the 20-day low can sit far below market and **may never fill** — the user places it, nothing happens, no explanation. Backend computes **how far below today's price the suggestion sits** (`(ask − limit_price) / ask`) and surfaces a calm, plain-English expectation: e.g. *"This is ~X% below today's price. On a fund like this it may take a while to fill — or it may not. You can cancel anytime."* Tune the copy/bands (near / meaningfully-below / far) with the calm-coach voice; pairs naturally with the existing 8.3 GTC "stays open" warning.
2. **Falling-market floor (SECONDARY).** Today's `min(recent_low, ask)` clamp means that when price is making *new lows*, the anchor becomes the live ask, so the suggestion is only 1% below current — the *thinnest* cushion exactly when the market is dropping fastest. Add a floor so the suggested buy is **never less than a configurable N% below the live ask** (i.e. `limit_price = min( clamp_formula, ask * (1 − FLOOR) )`), so a genuine dip is always required. Confirm the floor interacts sanely with the 1% discount and whole-share sizing.
3. **Data-freshness guard (ROBUSTNESS).** The "20-day low" is only as good as the `MarketDaily` bars. If ingestion is stale (e.g. no bar for the latest expected trading day), the low is stale and nobody's told. Add a freshness check: if the newest bar is older than a threshold, either surface an honest "prices may be delayed" note or a calm refusal (decide in create-story) — never silently suggest off stale data.

## Out of scope

- Volatility-scaled discount (ATR-based) — considered and deferred; likely overkill for a beginner index-fund app. Note it, don't build it.
- Any change to the core determinism / backend-owns-the-number architecture.
- Selling / ask-side suggestions (8.4 is buy-only by design).
- The LLM computing or adjusting price.

## Draft acceptance criteria

1. A suggestion carries a backend-computed "distance below market" and a calm plain-English fill-likelihood expectation; a far-below-market suggestion honestly says it may take a long time or never fill; voice stays calm/never-red.
2. The suggested buy price is never closer than N% below the live ask (floor enforced), verified deterministically across a rising, flat, and falling-market fixture (including the new-lows case that today collapses to 1%).
3. Stale `MarketDaily` data (newest bar older than the freshness threshold) produces an honest delayed-data note or a calm refusal — never a silent suggestion off stale bars.
4. Determinism preserved: same bars + same ask ⇒ identical price + identical fill-likelihood band, independent of the LLM (the model still only narrates).
5. All 8.1–8.4 + existing tests stay green; the core pricing formula is unchanged except for the added floor; no money-path behavior changes.

## Risks / notes for the create-story pass

- Keep all new context **backend-computed**; the LLM narrates the fill-likelihood/freshness facts, never derives them.
- The floor (#2) changes the number in the new-lows case — encode the before/after with a falling-market fixture so the behavior change is explicit and intended.
- Reuse the 8.4 calm-decline envelope pattern for the stale-data refusal; reuse the 4.5 / 8.3 warning voice for the fill-likelihood copy.

## References

- [Source: ballast/backend/coach/suggest.py] — `compute_suggested_price` (`min(recent_low, ask) * (1 − 0.01)`, `SUGGEST_LOOKBACK_DAYS=20`, `SUGGEST_DISCOUNT=0.01`), `_recent_low` (the 20-bar min-low query to extend with freshness).
- [Source: db.models.MarketDaily] — OHLC bars; `day` column for the freshness check.
- [Memory: [[order-interface-expansion-plan]] (8.4 pricing locked), [[fake-mode-vs-real-coaching]], [[masterb-working-style]] (calm/honest coach for a beginner)].
