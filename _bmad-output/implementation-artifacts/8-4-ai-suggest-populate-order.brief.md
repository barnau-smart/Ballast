# Story 8.4 (BRIEF): AI "Suggest & Populate the Order" Button (Order Interface Expansion — Story D, MasterB's core vision)

Status: backlog — planning brief only (compute-owner decision LOCKED by MasterB 2026-08-04)

> **This is a scoping brief, NOT a ready-for-dev story.** Generate the ultimate-context story via `/bmad-create-story 8-4` (the bmad-loop route/plan step will also compile it). Stories 8.1/8.2/8.3 are `done`, so the full order backend + UI contract this builds on is frozen. This brief locks scope + the one product decision so that pass doesn't re-derive them.

## Feature / Goal

Stories 8.1–8.3 built the full order backend (marketable + resting limits, GTC, cancel) and the human-facing order-entry form (`CoachConsult.jsx`, 8.3). **Story 8.4 delivers MasterB's actual north-star: an optional "AI: suggest this order" button that reads the account's available cash + the symbol's recent highs/lows, computes a sensible "buy near the low and wait" resting-limit order, POPULATES all the 8.3 order controls, and shows its reasoning in plain English — the human still does the final execution.** It is the intelligence layer on top of the 8.1–8.3 mechanics.

## Locked design decisions (honor; from [[order-interface-expansion-plan]])

- **COMPUTE-OWNER LOCKED (MasterB, 2026-08-04): the BACKEND computes the suggested price deterministically; the LLM ONLY narrates the reasoning in plain English. The model never does money-math and never sets the number.** (Chosen over "simple fixed % below current" and "LLM proposes + backend clamps.")
- **Populate, not execute.** The button fills the order controls (side, order type=LIMIT, limit price, duration, amount); the human reviews and runs the existing `/approve` → co-sign → place path. Nothing auto-executes. This is the sanctioned softening of the "LLM never sets a limit price" lock — human-in-the-loop is preserved because the human executes.
- Keep `is_index_core(symbol)` for all suggestions.
- The LLM coach's `RECOMMENDATION_OUTPUT_SCHEMA` stays MARKET-only — the *suggest-order* path is a SEPARATE surface, not a change to what the coach proposes on `/recommend`.
- Backward compatible: the button is optional/additive; the manual 8.3 form and all existing flows are unchanged.

## Scope

- **New backend "suggest order" endpoint** (per-user scoped, e.g. `POST /api/coach/suggest-order` — mirror the `/recommend` + `/approve` patterns in `api/coach.py`). Input: symbol (+ optional target amount). Output: a fully-formed *suggestion* — `order_type=LIMIT`, deterministic `limit_price`, `duration` (DAY vs GTC), `amount`, and an LLM-authored `reasoning` string. It does NOT place anything.
- **Deterministic pricing (backend, no LLM).** Compute the resting-limit "buy near the low" price from: the live quote (adapter quote — currently only the adapter sees bid/ask; this path needs it) + recent `MarketDaily` OHLC lows (data already stored). **Default formula to confirm in create-story: a modest discount toward the recent ~20-trading-day low, CLAMPED so the suggested buy price never sits at/above the live ask** (a "buy near the low" must be below market or it isn't resting). Decimal-safe, never float. Precise window/discount params are an implementation-tuning detail for the create-story pass, NOT a fresh product fork.
- **Amount sizing** from available cash / money-market (respect the 8.1/8.2 whole-share + sub-share-refusal rules; reuse, don't reinvent). If cash can't afford ≥1 share at the suggested price, return a calm "not enough idle cash for a whole share" suggestion-declined envelope (never a crash, never a phantom order).
- **LLM narration only.** Feed the *already-computed* numbers to the LLM gateway (reuse the 4.1 gateway / fake-first pattern) and have it produce a calm, beginner-plain `reasoning` ("This buys near XYZ's recent low and waits; it rests until the price is reached, or you can cancel it"). The model receives the numbers as facts to explain — it never computes or alters them. Fake gateway returns a canned narrative deterministically.
- **Frontend: the "Suggest this order" button** on the 8.3 order-entry surface (`CoachConsult.jsx`). On click → calls the endpoint → **populates the 8.3 controls** (order type, limit price, duration, amount) and renders the reasoning inline. The user can edit any field, then approve through the normal path. Reuse the 8.3 form + 4.11 coach-card components; do not build a parallel surface (avoid the Epic 4 "centerpiece missed" pattern).

## Out of scope (Story 8.4)

- Auto-execution / the AI ever placing the order — the human always executes.
- The LLM computing or adjusting the price (backend owns the number, full stop).
- STOP / STOP_LIMIT / extended sessions (AM/PM) — still rejected by `validate_order_intent` (B2, likely cut).
- Any change to what the coach proposes on `/recommend` (stays MARKET-only).
- New broker/port surface (8.2 already added cancel; this needs none).

## Draft acceptance criteria

1. `POST /suggest-order` (per-user, live-session-appropriate) returns a deterministic LIMIT suggestion — a backend-computed `limit_price` at/below the recent-low target and below the live ask, plus `duration`, `amount`, and an LLM `reasoning` — and places NOTHING. Same symbol + same market data → same price (deterministic; assert without the LLM).
2. The price is computed entirely in the backend with NO LLM involvement; a test proves the number is identical whether the real or fake LLM is used (the model only narrates).
3. Insufficient idle cash for ≥1 whole share at the suggested price returns a calm suggestion-declined envelope (422/calm), never a 500, never a phantom order.
4. The frontend button calls the endpoint and populates the 8.3 order controls + shows the reasoning; the user can edit fields and approve through the existing `/approve` path; nothing executes without the human.
5. `is_index_core` still gates suggestions; non-core symbols get a calm refusal.
6. All 8.1/8.2/8.3 + existing tests stay green; `/recommend` stays MARKET-only; STOP/STOP_LIMIT/AM-PM stay rejected.

## Risks / notes for the create-story pass

- **Expose the live quote to this path:** bid/ask currently lives only inside the adapter (verified in 8.1). The suggest-order endpoint needs a read of the live ask to clamp the price — confirm the cleanest seam (adapter quote read used by placement) and reuse it; do NOT leak the adapter into the coach.
- **Determinism is the safety property** — the whole point of "backend computes" is reproducibility and keeping money-math out of the model. Encode a test that pins the price given fixed quote + `MarketDaily` fixtures.
- Reuse the 4.1 LLM gateway (fake-first, zero-network tests) for narration; the fake returns a canned reasoning so tests never hit the network.
- Confirm the exact `MarketDaily` OHLC query for the recent-low window and the available-cash source (portfolio_balance cash from 6.5 — the cash-only mapping — is the honest source).
- Tune the formula (window length, discount) against real data later; ship a sensible default the user can override in the form.

## References

- [Source: ballast/backend/api/coach.py] — `/recommend` + `/approve` endpoint patterns to mirror; `OrderIntentIn/Out` contract (frozen by 8.1/8.2) the suggestion maps onto.
- [Source: ballast/backend/coach/execution.py] — sizing / whole-share + sub-share refusal rules (8.1/8.2) to reuse for `amount`.
- [Source: ballast/backend/llm/] — 4.1 gateway (fake-first) for the reasoning narration only.
- [Source: MarketDaily model] — stored OHLC highs/lows (the recent-low data source; already exists).
- [Source: ballast/backend/brokers/*adapter*] — live bid/ask quote (currently adapter-only; needs a read seam for the clamp).
- [Source: ballast/frontend/src/components/CoachConsult.jsx] — 8.3 order-entry controls the button populates.
- [Memory: [[order-interface-expansion-plan]] (compute-owner decision locked), [[fake-mode-vs-real-coaching]] (demo mode), [[epic6-live-trade-decisions]] (sizing rules)].
