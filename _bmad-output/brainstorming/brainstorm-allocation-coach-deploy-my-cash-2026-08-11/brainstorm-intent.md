# Brainstorm Intent: Allocation Coach / Deploy-My-Cash

## Feature & Why

**One-line:** Close the gap between where your money IS and where it SHOULD be, and let a human pull the trigger.

**Goal:** Make Ballast prescriptive enough to answer a beginner's "I have $2k sitting there, what do I do with it?" — the exact freeze that made the operator say "I'd just do this in Schwab." Turn Ballast from a discipline tool into a value-engine that x-rays the account and hands the user concrete moves.

**Proof-of-value:** The target user (a beginner) can't enumerate what a portfolio review should catch — he lacks the domain knowledge, and that IS the point. An AI wealth-manager/fiduciary persona supplies the review criteria; the user doesn't have to know what to look for.

## MVP Scope (LOCKED — MoSCoW result)

**The spine (MUST):**
- Portfolio-analysis engine → concrete **action items** (the prescriptive engine; e.g. "money in a loser → liquidate and redeploy toward target" is one example action item, not a separate feature).
- A **target-allocation model** (everything — what to buy, how much — flows from it).
- **Populate-controls-don't-submit** — human co-signs. Already built via the existing propose → approve spine.
- **AI fiduciary-advisor persona** — situational opinion, no forecasts.
- **Safeguards** — never-invent-a-fact + the 5 good-lesson tests.

**MVP analysis buckets:**
- **Allocation / diversification** (incl. overlap-detection).
- **Cash** — largely built in Epic 9.
- **Cost / fees.**
- **Risk / behavior** — concentration / single-stock.

**Reuses existing work:**
- Epic 9 cash states / reserve (investable cash = ready-to-trade minus reserve, excl. parked).
- Epic 9 story 9-3 liquidation machinery (for "liquidate a loser & redeploy").
- Existing propose / approve / co-sign spine.
- Existing Precedent-Engine + evidence-record + validation-gate architecture (for the never-invent safeguard — not a new safety model).

## Locked Decisions / Guardrails

- **Opinion yes, forecasts no.** The AI opines on the USER'S SITUATION + settled principles (concentration, cash drag, diversification gaps, cost, tax, rebalancing) — prioritize, synthesize, take a point of view. It NEVER makes market forecasts/predictions. This is the credibility line for the whole product.
- **Never invent a fact/number.** Deterministic code computes every number; the AI only narrates it. A validator rejects any figure not handed to it. (Reuses the existing Precedent-Engine / evidence-record / validation-gate.)
- **"Nothing to do right now" is a valid honest output.** The engine must be comfortable prescribing NO trade — churn-safety. A design principle, not a feature.
- **Tool-first, learning-optional.** Using Ballast as a permanent tool without ever "graduating" is an ACCEPTABLE use case. Over-automation is not a failure mode.
- **Trust-spine through-line:** every "no" (no chasing, no forecasts, no invented facts, no manufactured trades) is one principle — the app speaks only to what's KNOWABLE (real numbers + settled principles) and refuses everything UNKNOWABLE. That is its defensibility vs. "AI stock-picker" apps.

## The 5 Good-Lesson Tests

Safeguard against teaching bad habits by example. A demonstrated/prescribed move must pass all five:

1. It's a **principle not a pick** (the user could re-derive it).
2. Its **"why" generalizes** (works every time, not just once — filters out chasing).
3. It's a **recognized best practice / fiduciary consensus**, not the app's private opinion.
4. It **teaches the tradeoff / counter-case** (reuse the existing required "uncertainties").
5. **Market data informs the FACTS, never the FORECAST.**

Underlying line: demonstrate timeless principles applied to real numbers, never market calls.

## Parked for Later (out of MVP)

- **Aware-but-don't-act bucket** — with a target-allocation model, findings are binary (gap → action item; no gap → nothing), so the fuzzy middle is a later refinement. (The principle it protected — "nothing to do right now" — is preserved above as churn-safety.)
- **Praise-the-healthy.**
- **Tax-awareness** — heaviest scope, least core to the "I have money, what do I do" job. Safeguard notes kept for pickup: account-type-first (no CG tax in IRA/Roth/401k; taxable = consequences); wash-sale 30-day check; holding-period check (don't cross short→long for impatience); realized-gain awareness (cleanup sell of a winner = taxable); honest-limit — flags "there may be a tax angle, worth a pro" rather than faking certainty. **Tax-aware, not tax-omniscient.**
- **The entire teaching layer** — graduated autonomy (watch → do-with-me → take-the-reins), 20-yr backtests, per-move micro-lessons + graduation checks, strategy personas side-by-side. This is the operator's larger "lead by example" vision, deliberately deferred to build the value-engine first.

## Key Open Question for the Spec

**How does the target-allocation model get SET?** — risk questionnaire / pick a model portfolio / age-based default. Everything (what to buy, how much) flows from it; this is the first thing the spec must answer.

**Fact worth encoding:** SCHB (total US) and an S&P-500 fund are ~85–90% the same holding — so "diversification / overlap" must key on genuinely different asset classes (US vs. international vs. bonds), not two flavors of large-US.
