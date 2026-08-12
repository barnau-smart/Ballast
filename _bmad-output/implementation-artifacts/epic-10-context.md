# Epic 10 Context: Allocation Coach — Deploy My Cash (prescriptive portfolio analysis)

<!-- Generated from planning artifacts. Regenerate with compile-epic-context if planning docs change. -->

## Goal

Turn Ballast from a discipline tool into a prescriptive value-engine that answers the beginner's real freeze — "I have $2,000 sitting there, what do I actually do with it?" The epic x-rays the account against a user-chosen target model portfolio and hands back concrete, pre-filled moves (buys, trims, fund switches) that a human co-signs. Deterministic code computes every number and populates the order controls; the AI only narrates the situation and settled principles. It matters because it converts idle-cash paralysis into a concrete, honest, trustworthy next action without ever forecasting the market or manufacturing a trade.

## Stories

- Story 10.1: Target-allocation model — pick a model portfolio
- Story 10.2: Gap-to-target deploy-my-cash engine → action items (populate, don't submit)
- Story 10.3: Fiduciary-advisor narration + never-invent-a-fact + the 5 good-lesson tests
- Story 10.4: Additional analysis buckets — concentration/single-stock + cost/fees

## Requirements & Constraints

Load-bearing guardrails (locked, non-negotiable acceptance criteria for every story):

- **Opinion, not forecast.** The AI may opine on the user's situation and settled investing principles; it must NEVER forecast or predict the market.
- **Never invent a fact.** Deterministic code computes every number; the AI narrates only. A validator rejects any figure the AI states that was not in the engine-provided set (reuse the Precedent-Engine / evidence-record / validation-gate pattern).
- **"Nothing to do right now" is a valid, honest output.** When already at target or there is no investable cash, do not manufacture a trade.
- **Rebalance toward target, never chase performance.** Diversification keys on genuinely different asset classes (US equity vs international equity vs bonds), NOT two flavors of large-US (e.g. SCHB ≈ an S&P-500 fund is the same class, not diversification).
- **Populate, don't submit.** All action items pre-fill order controls; the human always co-signs via the existing approve spine. Never auto-submit.
- **Calm / no-FOMO voice; suite stays green.** Default target selection is *undecided* → a calm, non-nagging prompt to pick (mirror the Epic 9 set-or-decline pattern). Each narrated move must pass the 5 good-lesson tests: principle-not-pick, why-generalizes, recognized best practice, teaches the tradeoff, facts-not-forecast.
- **Money handling:** all money values are `Decimal` / `WireMoney`; per-user scoped; no exponent notation on the wire.
- **Deferred (explicitly NOT this epic):** aware-but-don't-act, praise-the-healthy, tax-awareness (tax consequence of a fund switch is noted honestly but not computed), and the entire teaching layer (graduated autonomy, backtests, micro-lessons, strategy personas).

## Technical Decisions

- **Model portfolios as code reference data** (like the existing `index_core`): a small set of named mixes (e.g. Conservative / Balanced / Growth), each fixed target weights across asset classes (US equity / international equity / bonds) mapped to concrete index-core funds. Not user-editable weights in v1.
- **Per-user target selection** persists in a new owned config reached only through the fail-closed `ScopedRepository` (AD-10). Editable; default undecided. Expose `GET`/`PUT` for the selection and the resolved target weights for downstream analysis.
- **Deterministic engine** computes current allocation (holdings grouped by asset class) vs. selected target, and investable cash. Investable cash = Epic 9 `ready_to_trade` − reserve, excluding parked money-market. The engine generates action items (deploy cash to close largest gaps; trim over-concentration; switch high-fee holdings) as concrete fund+amount buys/sells toward target.
- **Recommendation contract (AD-2):** recommendations are the validated structured object `{action_label, order_intent?, reasoning, evidence[], uncertainties[]}` produced by `retrieve → compose → validate → surface`; the validation gate rejects any object missing reasoning, ≥1 real evidence record, or uncertainties. `order_intent` is the typed executable payload `{symbol, side, amount}`.
- **Precedent / evidence pattern (AD-3, AD-12):** numbers are code-retrieved and LLM-cited-only; every evidence record has the fixed shape `{id, kind, statement, stats, source, as_of}`. This is the reuse target for the never-invent-a-fact validator.
- **One owner per concern (AD-6) + single execution path (AD-7):** the Coach Engine is the sole path from proposal to the Broker Port; new suggestion logic injects at retrieve/compose, never at surface or execution.
- **Fake-LLM fallback is deterministic templated copy** (never a dead-end) so the narration path degrades safely.
- **Expense-ratio reference table** (small, for the known funds) is required for the 10.4 cost/fees bucket.

## UX & Interaction Patterns

- **Set-or-decline prompt** for the target selection: calm, non-nagging, mirrors the Epic 9 pattern; undecided is a first-class state, not an error.
- **Coach-card sequence** holds: recommendation → why → precedent/data → uncertainty callout → co-sign zone; reasoning and uncertainty are never collapsed by default.
- **Advisor persona narration** prioritizes what matters (the why, the tradeoff, prioritization) as situational opinion; no market forecasts.
- Market up/down uses green ▲ / sky-blue ▼ with signs, never red; no unprompted / FOMO output (pull-not-push); `prefers-reduced-motion` respected.

## Cross-Story Dependencies

- **Reuses Epic 9** cash states/reserve (investable cash = ready-to-trade − reserve, excl. parked) and the 9-3 liquidation machinery (trim / de-speculate → redeploy), reused in 10.4.
- **Reuses the propose → approve → co-sign spine** (populate controls, human submits) across all action-item stories.
- **Reuses the Precedent-Engine + evidence-record + validation-gate architecture** for the never-invent-a-fact enforcement in 10.3 (and applied by 10.4's narrated items).
- **Within the epic:** 10.2 depends on 10.1 (needs the resolved target to compute gaps); 10.3 narrates the deterministic action items produced by 10.2; 10.4 adds analysis buckets that must pass the 10.3 safeguards. Story 10.1 is already implemented (commit `2af3d0a`).
