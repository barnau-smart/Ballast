---
title: "Ballast — Product Requirements Document"
status: final
created: 2026-07-22
updated: 2026-07-22
---

# Ballast — Product Requirements Document

## 1. Overview

Ballast is a **web-based, multi-user AI investing coach** that connects to a user's Charles Schwab account. Its job is not to beat the market — it is to make a nervous beginner **feel confident and act consistently**, by being an always-available, plain-English coach that reviews every move, explains its reasoning, backs it with real historical precedent, and stops the user from self-sabotaging.

The product is deliberately **coach-first**. It exists to defeat one specific enemy uncovered in discovery: **regret from a self-caused loss** (and its cousins, omission bias and headline-paralysis). Every v1 feature is a weapon aimed at that enemy — deployed *before* a decision (precedent + context), *at* the decision (a co-signed, reasoned recommendation you approve), and *after* it (replaying the rationale you both agreed on when the market dips).

Built multi-user from day one (isolated per-user accounts and data), but **not** optimized for market/scale yet — going to market is an open decision.

## 2. Goals & Success Metrics

**Primary goal:** reduce the anxiety of investing decisions and help users invest consistently, capturing the market's return (which most retail investors fail to do) rather than chasing outperformance.

**Success signals (behavioral, not returns) — initial targets to calibrate with real usage `[ASSUMPTION]`:**
- **SM1 — Consistency:** ≥90% of a user's intended contributions made over a rolling 3-month window (a maintained contribution streak).
- **SM2 — Stayed the course:** zero panic-sells and no halted contributions during a market drawdown of ≥10% (the behavior gap avoided).
- **SM3 — Confidence:** self-reported confidence ("I understand what I'm doing"), captured on a periodic in-app check-in, trends upward over a 3-month window.
- **SM4 — Decision follow-through:** a majority of reviewed, co-signed recommendations are acted on rather than abandoned.

**Counter-metrics (must stay LOW — the product must not cause them):**
- **CM1 — Overtrading:** trade frequency stays at or below the user's contribution cadence; a rising trend is a red flag that Ballast is inducing activity (the opposite of its purpose).
- **CM2 — Persistent paralysis / cash drag:** large idle-cash balances that never get deployed.
- **CM3 — Anxiety-driven abandonment:** users disabling or abandoning Ballast within a short window after a downturn (signal it *added* stress instead of removing it).

## 3. Users

**Primary persona — "the anxious beginner" (e.g., MasterB, the first user):** knows they *should* invest, has heard "just buy an index fund," but doesn't understand it well enough to feel confident. Freezes on timing ("is now a bad time?"), fears a self-caused loss, is self-aware about being over-cautious, and wants to *understand* — not just outsource. Success for them: less dread, more literacy, a plan they trust and stick to.

Multi-user from the start: each user has an isolated account, their own linked Schwab connection, and their own data. The product does not assume a single operator.

## 4. Key User Journeys

**UJ-1 — The scary-moment decision (the core loop).** It's payday. A frightening headline is in the news (say, a geopolitical shock). Our user wants to invest but is frozen — "what if I buy and it drops?" They open Ballast and ask. Ballast shows **how similar past events actually played out** (recovery precedent), lays out its **reasoning**, is honest about **what's uncertain**, and **co-signs** a concrete recommendation ("invest $X into your index core"). The user approves; Ballast executes to Schwab. A week later the market dips; the user opens Ballast anxious, and it **replays the reasoning they both agreed on** — talking calm-past-self's logic to panicked-present-self. The user holds. Regret defused.

**UJ-2 — Onboarding & connection.** A new user signs up (email + password), links their Schwab account via Schwab's OAuth, and Ballast pulls in their portfolio and explains, in plain English, what they currently hold and how it maps to the index-core strategy.

## 5. Scope

**In (v1 — coach only):**
- Multi-user accounts; per-user Schwab connection; portfolio visibility.
- The Coach: propose-and-approve trade review with transparent reasoning, precedent-backing, honest uncertainty, and just-in-time teaching.
- The regret-defense loop: recovery-precedent view, decision co-sign, decision replay.
- SHOULD (v1 if time): missed-growth meter, headline contextualizer.
- Optional calm weekly email digest.

**Out of v1 (deferred, in priority order):**
- **The Guru** (paper-sim mode first, then configurable capped real-money satellite) — the immediate next release.
- Strategy curriculum (#10), literacy quiz (#11), agreement-based progression (#13), karate-belt tiers (#14).
- "You take the reins" supervised-decision mode (#12).
- Push/SMS notifications; options/shorting/complex order types; going-to-market (monetization, marketing, scale).

## 6. Functional Requirements

### 6.1 Accounts & Onboarding
- **FR1.** Users can sign up and log in with email + password; each user's data is fully isolated from other users'.
- **FR2.** Users can link their own Charles Schwab account via Schwab's OAuth flow.
- **FR3.** The system surfaces the Schwab session/token status clearly and, when the ~weekly re-authentication is required, prompts the user gracefully (explaining why) rather than failing silently.
- **FR4.** On first connection, Ballast imports and displays the user's current holdings and cash, explained in plain English.

### 6.2 Portfolio Visibility
- **FR5.** Users can view their full current portfolio (holdings, balances, cash) in one place, with plain-English descriptions of what each holding is.
- **FR6.** Ballast shows how the user's current portfolio maps to the index-core strategy (what's "core," what isn't).

### 6.3 The Coach — Propose-and-Approve
- **FR7.** A user can initiate a decision ("I want to invest $X" / "should I buy now?"); Ballast produces a concrete recommendation the user can approve or decline.
- **FR8.** No trade is ever executed without explicit user approval (propose-and-approve).
- **FR9.** On approval, Ballast places the order in the user's Schwab account and confirms execution.
- **FR10.** v1 order scope is limited to buying/holding a small set of broad index funds/ETFs; selling is offered only as coach-guided rebalancing. No options, shorting, or complex order types. The "broad index core" may include diversification beyond the S&P (e.g., a total-market or international fund) at the user's choice — it never means *avoiding* the index.
- **FR11.** Ballast reviews user-initiated actions and **warns before anything self-destructive** (e.g., a panic sell, over-concentration, an oversized lump relative to plan), explaining the risk — but never blocks the user (coach advises; user decides), consistent with coach-final-word on strategy.
- **FR22. Execution outcomes:** Ballast handles and clearly reports order rejection, partial fills, and timeouts; after any order the user sees the true resulting state, reconciled against Schwab, with no phantom or duplicate orders.
- **FR23. Approval→placement integrity:** if the Schwab session/token expires between approval and placement, Ballast does not silently place a stale or partial order — it re-establishes a live session and re-confirms the user's intent before placing.

### 6.4 The Coach — Reasoning & Transparency *(invariants)*
- **FR12.** Every recommendation includes its **reasoning in plain English** — no black-box calls. *(Hard invariant.)*
- **FR13.** Every factual/precedent claim is **backed by real historical market data**; if a claim cannot be backed, it is not made. Ballast never fabricates precedent from model memory. *(Hard invariant.)*
- **FR14.** Every recommendation **explicitly states what is uncertain / what it does not know.**

### 6.5 The Coach — Regret Defense
- **FR15. Recovery-precedent view:** for a given decision or scary moment, Ballast shows real past instances where the market dropped under similar conditions and how it recovered, drawn from actual market data. *v1 defines "similar" by drawdown-magnitude band (a comparable % decline) and, where available, event category; the precise matching rule is finalized in design.*
- **FR16. Decision co-sign:** when the user approves a recommendation, Ballast records a co-signed decision record capturing the reasoning and precedent it stands behind — making it a shared, on-the-record call, not the user's alone.
- **FR17. Decision replay:** after a decline (or on demand), Ballast can replay the co-signed reasoning and precedent from the original decision, so the user can revisit the rationale they agreed to when calm.

### 6.6 The Coach — Just-in-Time Teaching
- **FR18.** At the moment of a real action, Ballast explains the **principle and mechanics behind it** in plain English (e.g., why consistent index buying, why this isn't market timing), tying the lesson to the live decision.

### 6.7 Should-Have (v1 if time)
- **FR19. Missed-growth meter:** a running, data-backed estimate of growth forgone by holding uninvested cash, making the cost of inaction visible.
- **FR20. Headline contextualizer:** when the user raises a scary news event, Ballast compares it to similar past events and their actual market outcomes. *(Must obey the no-FOMO invariant — it responds to user concern; it does not push unprompted alerts.)*

### 6.8 Weekly Digest (optional)
- **FR21.** Users can opt into a **calm weekly email digest** summarizing their plan status and reinforcing that they're on track. It must never contain alarmist or FOMO-style content. No push or SMS in v1.

## 7. Non-Functional Requirements

- **NFR1 — Financial-data security:** user credentials, OAuth tokens, and financial data must be encrypted at rest and in transit; per-user data isolation is strict. Brokerage tokens are handled as high-sensitivity secrets.
- **NFR2 — Trust & safety of advice:** the reasoning/precedent invariants (FR12–FR14) are enforced at the system level — the app must be architected so an unbacked or black-box recommendation cannot be surfaced.
- **NFR3 — Reliability of execution:** trade execution must be confirmed and reconciled against Schwab; the user always sees the true state of what did/didn't happen (no phantom or duplicate orders).
- **NFR4 — Schwab session reality:** the system tolerates the Schwab ~7-day refresh-token expiry — no data loss on expiry, graceful re-auth prompts, and clear degraded-mode behavior (read/coach may continue; execution requires a live session). An order is never placed on an expired session (see FR23).
- **NFR5 — Privacy:** a user's financial data is never used to serve another user; no sharing without explicit consent.
- **NFR6 — Plain language:** all user-facing coach output is written for a self-aware beginner — no unexplained jargon.
- **NFR7 — Responsiveness:** coach reviews and precedent lookups return quickly enough to feel conversational (target: typically within a few seconds).
- **NFR8 — Coach voice & tone:** all coach output embodies a patient, warm, honest teacher — calm, plain-spoken, never condescending, never hype. Tone is a reviewable acceptance criterion, not merely "no jargon."

## 8. Design Invariants (non-negotiable)

These hold across the entire product, now and later:
1. **No black-box recommendations** — reasoning always shown (FR12).
2. **Precedent-backed or not claimed** — never fabricate history (FR13).
3. **Coach-is-boss** — the coach has the final word on strategy; the (future) guru never touches the core.
4. **No unprompted FOMO / "you're missing out" alerts** — ever.
5. **Capture, don't beat** — the product never promises or optimizes for market-beating returns.
6. **Pull, not push** — the coach and its features respond to the user; they never nag or surface unprompted (a superset of the no-FOMO rule).
7. **Anxiety-reducing by design** — the product must lower the user's decision-anxiety, never raise it; a technically "correct" feature that increases stress is a failure.

## 9. Regulatory & Compliance Posture

- v1 is a **personal / private, educational-framed** tool. As long as it is not marketed and not giving personalized securities advice to the public, regulatory exposure is low.
- **Gate before any public multi-user launch:** giving *other people* personalized securities recommendations and/or executing trades for them likely triggers U.S. **investment-adviser (RIA)** registration and fiduciary obligations. This must be reviewed before going to market. *(Multi-user architecture is fine; multi-user *public offering* is the line.)*

## 10. Open Questions & Assumptions

- **[ASSUMPTION]** The specific market-data provider for precedent/history is an architecture decision (see addendum), not fixed here.
- **[ASSUMPTION]** The "small set of broad index funds/ETFs" for v1 will be defined during design (e.g., a Schwab total-market or S&P 500 fund as the default core).
- **[RESOLVED in v1]** "Similar past event" = drawdown-magnitude band (+ event category where available); precise rule finalized in design (see FR15 / FR20).
- **[OPEN]** Exact contribution-scheduling model (does Ballast track intended cadence, or only react when the user acts?) — to refine in UX/architecture.
- **[OPEN]** Confidence measurement method for SM3 (in-app check-in cadence).
- **[DEFERRED]** A v1 coach-level commitment device (e.g., a pre-committed contribution plan or a cooldown on risky sells) was considered and deferred to the guru release, where the self-locked "Ulysses" ceiling lives. Revisit if v1 users act impulsively despite warnings (FR11).

## 11. Glossary

- **Index core:** the stable, low-cost backbone of the portfolio — a broad index fund (S&P 500 or total-market) bought consistently.
- **Satellite (future):** a small, capped, optional sleeve where the deferred "guru" may operate; never touches the core.
- **Propose-and-approve:** the coach proposes a trade with reasoning; the user must approve before anything executes.
- **Co-sign:** a recorded, shared decision — the coach stands behind the reasoning and precedent alongside the user's approval.
- **Precedent:** a real, data-backed historical instance used to justify or contextualize a claim (never invented).
- **Capture, don't beat:** aim to earn the market's return by staying consistent and invested, not to outperform it.
