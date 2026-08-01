---
stepsCompleted: ["step-01-validate-prerequisites", "step-02-design-epics", "step-03-create-stories", "step-04-final-validation"]
inputDocuments:
  - "_bmad-output/planning-artifacts/prds/prd-ai_practice_project-2026-07-22/prd.md"
  - "_bmad-output/planning-artifacts/prds/prd-ai_practice_project-2026-07-22/addendum.md"
  - "_bmad-output/planning-artifacts/architecture/architecture-ai_practice_project-2026-07-22/ARCHITECTURE-SPINE.md"
  - "_bmad-output/planning-artifacts/ux-designs/ux-ai_practice_project-2026-07-22/DESIGN.md"
  - "_bmad-output/planning-artifacts/ux-designs/ux-ai_practice_project-2026-07-22/EXPERIENCE.md"
---

# Ballast - Epic Breakdown

## Overview

Complete epic and story breakdown for **Ballast v1 (coach-only)** — a web-based (Vite/React SPA + FastAPI) multi-user AI investing coach on top of Schwab. Decomposes the PRD, UX design contract, and architecture spine into implementable stories. Deferred features (guru, curriculum, quiz, progression, belts, "you take the reins", push/SMS, go-to-market) are explicitly out of this breakdown.

## Requirements Inventory

### Functional Requirements

FR1: Email + password sign-up/login; each user's data fully isolated from other users.
FR2: Link the user's own Charles Schwab account via Schwab OAuth.
FR3: Surface Schwab session/token status; prompt gracefully (with explanation) for the ~weekly re-authentication rather than failing silently.
FR4: On first connection, import and display holdings and cash, explained in plain English.
FR5: View the full current portfolio (holdings, balances, cash) with plain-English descriptions.
FR6: Show how the current portfolio maps to the index-core strategy (what's "core," what isn't).
FR7: User initiates a decision ("should I invest $X / buy now?"); receives a concrete recommendation to approve or decline.
FR8: No trade executes without explicit user approval (propose-and-approve).
FR9: On approval, place the order in Schwab and confirm execution.
FR10: v1 order scope = buying/holding a small set of broad index funds/ETFs; selling only as coach-guided rebalancing; no options/shorting/complex orders; the broad core may diversify beyond the S&P at user choice (never avoiding the index).
FR11: Review user-initiated actions and warn before anything self-destructive (explain the risk); never block (coach advises, user decides).
FR12: Every recommendation includes plain-English reasoning — no black-box calls. [INVARIANT]
FR13: Every factual/precedent claim is backed by real historical market data; unbackable claims are not made (never fabricated by the LLM). [INVARIANT]
FR14: Every recommendation explicitly states what is uncertain / not known.
FR15: Recovery-precedent view — for a decision/scary moment, show real past instances of similar drops and their recovery (v1: matched by drawdown-magnitude band + optional event category).
FR16: Decision co-sign — on approval, persist a co-signed, immutable decision record (reasoning + precedent + uncertainties).
FR17: Decision replay — replay the original co-signed reasoning + precedent after a decline / on demand.
FR18: Just-in-time teaching — at the moment of an action, explain the principle and mechanics behind it.
FR19: Missed-growth meter — data-backed running total of growth forgone by holding uninvested cash. [SHOULD]
FR20: Headline contextualizer — when the user raises a scary headline, compare it to similar past events' actual market outcomes (drawdown-keyed; on-demand, never a push). [SHOULD]
FR21: Opt-in calm weekly email digest (plan status, on-track reinforcement); no push/SMS; never alarmist.
FR22: Handle and clearly report order rejection, partial fills, and timeouts; the user always sees the true reconciled state; no phantom or duplicate orders.
FR23: Approval→placement integrity — if the session/token expires between approval and placement, do not place a stale/partial order; re-establish a live session and re-confirm intent first.

### NonFunctional Requirements

NFR1: Financial-data security — credentials, OAuth tokens, and financial data encrypted at rest and in transit; strict per-user isolation; brokerage tokens handled as high-sensitivity secrets.
NFR2: Structural enforcement of the trust invariants (FR12–FR14) — an unbacked or black-box recommendation must be physically un-surfaceable, not merely discouraged.
NFR3: Execution reliability — trade execution confirmed and reconciled against Schwab; the true state is always shown (no phantom/duplicate orders).
NFR4: Schwab session reality — tolerate the ~7-day refresh-token expiry with no data loss; graceful re-auth; clear degraded mode (read/coach continue; execution needs a live session); no order on an expired session.
NFR5: Privacy — a user's data is never used to serve another; no sharing without explicit consent.
NFR6: Plain language — all user-facing coach output written for a self-aware beginner; no unexplained jargon.
NFR7: Responsiveness — coach reviews and precedent lookups return quickly enough to feel conversational (target: within a few seconds).
NFR8: Coach voice & tone — patient, warm, honest, plain-spoken; tone is a reviewable acceptance criterion, not merely "no jargon."

### Additional Requirements

(From the architecture spine — technical requirements that shape stories.)

- **Project scaffold (Epic 1, Story 1):** no pre-built starter; hand-rolled — Vite 8 + React 19 SPA (presentation only) + FastAPI 0.136 (Python 3.12+) backend (owns all logic) + PostgreSQL 18. Backend/SPA split IS the NFR2 enforcement boundary (AD-1).
- **Paradigm:** modular monolith, hexagonal — external deps (brokerage, LLM, market data) behind ports with swappable adapters (AD-8).
- **Coach Engine:** recommendation pipeline `retrieve → compose → validate → surface`; Recommendation = validated structured object `{action_label, order_intent?, reasoning, evidence[], uncertainties[]}`; validation gate rejects unbacked/incomplete (AD-2). Sole writer of decision records (AD-6). Two backing types (event-precedent vs strategy); no-confident-call → strategy-backed default plan (AD-4).
- **Precedent Engine:** deterministic service over local `market_daily` (Tiingo EOD → Postgres, daily refresh job); sole source of market stats; Evidence Record Contract fixed shape (AD-3, AD-12).
- **LLM Gateway:** sole caller of Claude (Anthropic SDK; sonnet-4-6 default / opus-4-8 hard reasoning); structured output; deterministic model routing; prompt-assembly + citation-check owned by Coach Engine (AD-3, AD-6).
- **Broker Port + SchwabAdapter (schwab-py 1.5.1):** Broker Port Contract — normalized `OrderOutcome` + idempotency key + `get_order_status` (AD-13); sole path to brokerage; single execution path `propose → approve → Coach Engine → Broker Port → reconcile → persist` (AD-7).
- **Auth & data:** FastAPI-Users 15 (email+pw + JWT); fail-closed scoped-repository layer with explicit SYSTEM scope for jobs (AD-10); app-layer token encryption (AES/Fernet), key outside DB.
- **Portfolio cache:** single-writer projection, broker-authoritative, reconcile-wins (AD-14).
- **Decision record:** immutable, carries `schema_version` for replay durability (AD-5).
- **Deferred (not this build):** deployment/hosting & environment topology; assume single-region/single-instance for v1.

### UX Design Requirements

(From DESIGN.md + EXPERIENCE.md — first-class work items.)

UX-DR1: Token-based theme system — the look is CSS-variable tokens (DESIGN.md); every component reads tokens, never hardcodes; theme is swappable. v1 theme = `ballast-terminal` (green phosphor interface, rare red brand, neon-pink accent, sky-blue market-down, green market-up, violet uncertainty).
UX-DR2: Component set (behavioral + visual): wordmark, terminal-bar, cursor, button-primary, button-ghost, coach-card, data-block, uncertainty-callout, cosign-block, chip, input, market-indicator, reauth-banner.
UX-DR3: Information architecture — 6 surfaces: Auth, Onboarding (Schwab link), Dashboard, Coach, Decisions, Settings.
UX-DR4: Coach-card behavioral pattern — fixed sequence `recommendation → why → precedent data-block → uncertainty callout → co-sign zone`; reasoning and uncertainty never collapsed by default.
UX-DR5: State patterns — coach-thinking (calm), empty/pre-link, no-precedent/no-confident-call → default plan, order outcomes (filled/partial/rejected/timeout/pending), session-expired/degraded mode, dip/loss (calm, losses in sky-blue not red).
UX-DR6: Accessibility floor — WCAG AA contrast on the dark theme; color-independence (▲/▼ + labels, never color alone); `prefers-reduced-motion` disables cursor blink + wordmark flicker (no scanlines); full keyboard path through the coach loop; screen-reader text equivalents for data blocks.
UX-DR7: Voice & microcopy — patient/warm/honest/plain; explicit uncertainty phrasing; no-confident-call default-plan phrasing; terse sourced data voice in mono.
UX-DR8: Responsive web — comfortable one-column on mobile, roomy on desktop; dark-only in v1.

### FR Coverage Map

FR1: Epic 1 — email+password accounts, per-user isolation
FR2: Epic 2 — link Schwab via OAuth
FR3: Epic 2 — session status + graceful ~weekly re-auth
FR4: Epic 2 — import + plain-English portfolio on connect
FR5: Epic 2 — full portfolio view, plain English
FR6: Epic 2 — map portfolio to index-core
FR7: Epic 4 — initiate decision → recommendation
FR8: Epic 4 — no trade without explicit approval
FR9: Epic 4 — place order + confirm on approval
FR10: Epic 4 — v1 order scope (index funds/ETFs, rebalance-only sell)
FR11: Epic 4 — warn before self-destructive moves, never block
FR12: Epic 4 — reasoning on every recommendation (no black box)
FR13: Epic 3 (data backing) + Epic 4 (validator enforcement)
FR14: Epic 4 — explicit uncertainties on every recommendation
FR15: Epic 3 — recovery-precedent view (drawdown matching)
FR16: Epic 4 — co-signed immutable decision record at approval
FR17: Epic 4 — decision replay after a dip / on demand
FR18: Epic 4 — just-in-time teaching at the moment of action
FR19: Epic 3 — missed-growth meter
FR20: Epic 3 — headline contextualizer
FR21: Epic 5 — opt-in calm weekly email digest
FR22: Epic 4 — order outcomes (rejection/partial/timeout), reconciled
FR23: Epic 4 — approval→placement integrity (session expiry)

## Epic List

### Epic 1: Foundation & Account Access
Scaffold the app (Vite/React SPA + FastAPI + Postgres + the token-based theme system) and deliver secure email+password accounts with strict per-user data isolation. Users can create an account and log into the app shell.
**FRs covered:** FR1 · NFR1, NFR5, NFR6 · UX-DR1, UX-DR2, UX-DR3, UX-DR6, UX-DR8 · AD-1

### Epic 2: Connect Schwab & See Your Portfolio
Link the user's own Schwab account via OAuth (with graceful ~weekly re-auth), import their holdings, and show the full portfolio in plain English mapped to the index-core strategy.
**FRs covered:** FR2, FR3, FR4, FR5, FR6 · NFR1, NFR4 · AD-8, AD-10, AD-11, AD-14

### Epic 3: See the Record (Precedent Engine + Calming Views)
Build the deterministic Precedent Engine (Tiingo → `market_daily`, drawdown matching, evidence records) and the user-facing calming tools it powers: recovery-precedent view, missed-growth meter, and headline contextualizer.
**FRs covered:** FR13 (data backing), FR15, FR19, FR20 · NFR7 · AD-3, AD-12

### Epic 4: The Coach — Propose, Approve, Execute, Co-sign & Replay
The core: the Coach Engine pipeline (retrieve→compose→validate→surface) enforcing the trust invariants, the LLM Gateway, propose-and-approve execution with honest order outcomes + integrity, self-destructive-move warnings, just-in-time teaching, the strategy-backed default-plan fallback, and the co-signed immutable decision record + replay.
**FRs covered:** FR7, FR8, FR9, FR10, FR11, FR12, FR14, FR16, FR17, FR18, FR22, FR23 · NFR2, NFR3, NFR8 · AD-2, AD-4, AD-5, AD-6, AD-7, AD-13

### Epic 5: Weekly Digest
An opt-in calm weekly email — plan status and on-track reinforcement, never alarmist, never a push notification.
**FRs covered:** FR21 · (pull-not-push invariant)

---

> **Cross-cutting acceptance criteria** (apply to every coach-facing story): NFR8 voice is patient/warm/honest/plain (no hype/jargon/condescension); no recommendation surfaces without reasoning (FR12) + explicit uncertainties (FR14) + data-backed precedent or nothing (FR13); no unprompted/FOMO output (pull-not-push); market up/down uses green▲/sky-blue▼ with signs, never red (color-independence); `prefers-reduced-motion` respected.

## Epic 1: Foundation & Account Access

Scaffold the app and deliver secure, isolated accounts so a user can create an account and log into the themed app shell.

### Story 1.1: Project scaffold & theme foundation

As a developer,
I want the app scaffolded with its token-based theme and shell,
So that every later feature is built on a consistent, themeable foundation.

**Acceptance Criteria:**

**Given** a fresh repo,
**When** the project is set up,
**Then** a Vite 8 + React 19 SPA (presentation-only), a FastAPI backend (Python 3.12+), and PostgreSQL 18 all run locally and the SPA reaches the backend.
**And** all colors/spacing/type come from CSS-variable tokens implementing the `ballast-terminal` theme (green phosphor, rare red, neon-pink accent, sky-blue down) — no hardcoded values (UX-DR1).
**And** the 6-surface route skeleton (Auth, Onboarding, Dashboard, Coach, Decisions, Settings) renders, and `prefers-reduced-motion` disables the cursor blink + wordmark flicker (UX-DR6).

### Story 1.2: Register with email & password

As a new user,
I want to create an account with email and password,
So that I have a private, secure place for my investing coach.

**Acceptance Criteria:**

**Given** the sign-up screen,
**When** I submit a valid email + password,
**Then** an isolated user record is created and my password is stored hashed (never plaintext) via FastAPI-Users (FR1, NFR1).
**And** a duplicate email is rejected with a plain-language message (NFR6).

### Story 1.3: Log in & session

As a returning user,
I want to log in and stay signed in,
So that I can securely reach my own data.

**Acceptance Criteria:**

**Given** valid credentials,
**When** I log in,
**Then** I receive a JWT session and can reach authed routes; wrong credentials are rejected; logout ends the session (FR1).

### Story 1.4: Fail-closed per-user data isolation

As a user,
I want my data reachable only by me,
So that no other user can ever see my finances.

**Acceptance Criteria:**

**Given** the scoped-repository layer,
**When** any data access runs,
**Then** it is scoped to the authenticated user; a query issued without an explicit scope raises an error (fail-closed); non-user jobs run under an explicit SYSTEM scope (AD-10, NFR5).
**And** user A can never read user B's records (verified by test).

## Epic 2: Connect Schwab & See Your Portfolio

Link the user's Schwab account and show their real holdings in plain English.

### Story 2.1: Broker Port + Schwab OAuth link

As a user,
I want to securely connect my Schwab account,
So that Ballast can see my portfolio and (later) place trades I approve.

**Acceptance Criteria:**

**Given** a `BrokerPort` interface with a `SchwabAdapter` implementation (schwab-py) (AD-8),
**When** I complete Schwab's OAuth flow,
**Then** my tokens are stored encrypted at the app layer with the key held outside the DB (FR2, NFR1, AD-10).

### Story 2.2: Session status, graceful re-auth & degraded mode

As a user,
I want to be told plainly when I need to reconnect Schwab,
So that a weekly re-login never feels like something broke.

**Acceptance Criteria:**

**Given** the ~7-day refresh-token expiry,
**When** the session expires,
**Then** a calm, neutral (never red) banner explains why and how to re-authenticate, read/coach features keep working in degraded mode, and after re-auth I resume in place (FR3, NFR4, AD-11).

### Story 2.3: Import & cache portfolio (single-writer projection)

As a user,
I want my current holdings pulled in on connect,
So that Ballast reflects my real account.

**Acceptance Criteria:**

**Given** a linked account,
**When** the portfolio is fetched,
**Then** `portfolio_cache` is written by a single owner, treats the broker as authoritative, and reconciles-wins on conflict (AD-14, FR4).

### Story 2.4: Plain-English portfolio dashboard

As a beginner,
I want my portfolio explained in plain language,
So that I actually understand what I hold.

**Acceptance Criteria:**

**Given** imported holdings,
**When** I open the dashboard,
**Then** holdings, balances, and cash are shown with plain-English descriptions and no unexplained jargon (FR4, FR5, NFR6).

### Story 2.5: Index-core mapping

As a user,
I want to see what counts as my stable "core,"
So that I understand my strategy at a glance.

**Acceptance Criteria:**

**Given** my portfolio,
**When** I view it,
**Then** Ballast shows which holdings are the index core vs. not, mapped to the index-core strategy (FR6).

## Epic 3: See the Record (Precedent Engine + Calming Views)

Build the deterministic precedent backbone and the user-facing tools that calm the scary moments.

### Story 3.1: Market-data ingestion → `market_daily`

As the system,
I want a local store of decades of daily market data,
So that all precedent is computed from real history I control.

**Acceptance Criteria:**

**Given** a scheduled SYSTEM-scope job,
**When** it runs daily,
**Then** it ingests Tiingo EOD data into `market_daily` (derived analytics, not redistributed raw data) and tolerates source hiccups (AD-3).

### Story 3.2: Drawdown matching & Evidence Record Contract

As the system,
I want to find historically similar drops deterministically,
So that the coach can cite real precedent it can never fabricate.

**Acceptance Criteria:**

**Given** a current drawdown (magnitude + velocity),
**When** the Precedent Engine runs,
**Then** it returns matched historical windows with recovery/forward-return stats as **evidence records** of the fixed shape `{id, kind, statement, stats, source, as_of}` (AD-3, AD-12, FR13, FR15).
**And** the computation is deterministic and involves no LLM.

### Story 3.3: Recovery-precedent view

As an anxious user,
I want to see that drops like this one have recovered,
So that a scary moment feels survivable.

**Acceptance Criteria:**

**Given** a scary moment or decision,
**When** I open the recovery-precedent view,
**Then** it shows real matched drops + recoveries in a calm green data-block (down in sky-blue, up in green, with ▲/▼), citing source + as-of (FR15, UX-DR5).
**And** if no precedent qualifies, it shows the strategy-default rationale instead of an empty state.

### Story 3.4: Missed-growth meter

As a user prone to sitting in cash,
I want to see what staying out has cost,
So that inaction stops feeling free.

**Acceptance Criteria:**

**Given** idle cash over time,
**When** I view the meter,
**Then** it shows a data-backed estimate of forgone growth, framed calmly as information — never as pressure or a nudge (FR19).

### Story 3.5: Headline contextualizer

As a user rattled by the news,
I want to compare a scary headline to history,
So that I can react to data instead of fear.

**Acceptance Criteria:**

**Given** a headline I submit on demand,
**When** Ballast responds,
**Then** it reframes to drawdown-keyed precedent (what the market did in comparable drops) and never classifies the event itself; it is never sent unprompted (FR20, pull-not-push).

## Epic 4: The Coach — Propose, Approve, Execute, Co-sign & Replay

The core loop: trustworthy recommendations, safe execution, and an on-the-record memory.

### Story 4.1: LLM Gateway

As the system,
I want a single controlled path to Claude,
So that model use is consistent, structured, and swappable.

**Acceptance Criteria:**

**Given** the LLM Gateway,
**When** the coach needs language,
**Then** it is the sole caller of the Anthropic API, enforces structured output, and applies deterministic model routing (Opus 4.8 for flagged hard-reasoning, Sonnet 4.6 otherwise) (AD-6).

### Story 4.2: Recommendation object & validation gate

As a user,
I want every recommendation to be reasoned, backed, and honest,
So that I can trust it — structurally, not on faith.

**Acceptance Criteria:**

**Given** a composed Recommendation `{action_label, order_intent?, reasoning, evidence[], uncertainties[]}`,
**When** it passes through the validation gate,
**Then** any object missing reasoning, missing uncertainties, or citing evidence not in the retrieved set is **rejected and cannot be surfaced** (FR12, FR13, FR14, NFR2, AD-2, AD-3).

### Story 4.3: Coach pipeline & default-plan fallback

As a user,
I want to ask the coach and always get an honest, actionable answer,
So that I'm never left paralyzed.

**Acceptance Criteria:**

**Given** I initiate a decision,
**When** the coach runs `retrieve → compose → validate → surface`,
**Then** I get a blessed recommendation; and when there's no confident special call, it returns the strategy-backed **default plan** ("stick to your plan") plus a plain reason — never a dead-end (FR7, AD-4).

### Story 4.4: Just-in-time teaching

As a beginner,
I want to learn why as I go,
So that my confidence grows with my balance.

**Acceptance Criteria:**

**Given** a recommendation,
**When** I read it,
**Then** the reasoning explains the principle + mechanics in plain English, and an "explain more" expander is available on demand without interrupting (FR18).

### Story 4.5: Self-destructive-move warnings

As a user about to do something rash,
I want the coach to flag it honestly,
So that I don't hurt myself — but I stay in control.

**Acceptance Criteria:**

**Given** a user-initiated action (e.g., panic sell, over-concentration, oversized lump),
**When** the coach reviews it,
**Then** it warns and explains the risk but never blocks — the user decides (FR11).

### Story 4.6: Propose-and-approve execution

As a user,
I want to approve every trade before it happens,
So that I'm never surprised by an order.

**Acceptance Criteria:**

**Given** a recommendation with an `order_intent`,
**When** I approve it,
**Then** the Coach Engine sends the order to the Broker Port and nothing executes without my explicit approval; v1 scope is index funds/ETFs (rebalance-only selling; no options/shorting/complex orders) (FR8, FR9, FR10, AD-7).

### Story 4.7: Order outcomes & reconciliation

As a user,
I want to always see what really happened to my order,
So that I never doubt the true state of my money.

**Acceptance Criteria:**

**Given** a placed order,
**When** it resolves,
**Then** the Broker Port returns a normalized `OrderOutcome {status: filled|partial|rejected|timeout|pending, filled_qty, avg_price, broker_ref}`, uses an idempotency key so a timeout never double-places, and reconciles via `get_order_status`; the true state is shown with no phantom/duplicate orders (FR22, NFR3, AD-13).

### Story 4.8: Approval→placement integrity

As a user,
I want a lapsed session to never cause a bad order,
So that trust in execution is absolute.

**Acceptance Criteria:**

**Given** the session/token expires between my approval and placement,
**When** the system goes to place the order,
**Then** it does not place a stale or partial order — it re-establishes a live session and re-confirms my intent first (FR23, AD-11).

### Story 4.9: Co-signed immutable decision record

As a user,
I want my approved decision recorded with its reasoning,
So that it's a shared, on-the-record call.

**Acceptance Criteria:**

**Given** I approve a recommendation,
**When** it's persisted,
**Then** the blessed Recommendation (with its evidence + uncertainties snapshot and a `schema_version`) is saved as an immutable co-signed decision record that no feature mutates (FR16, AD-5).

### Story 4.10: Decisions history & replay

As a user in a dip,
I want to revisit the reasoning we agreed on,
So that calm-past-me can talk down panicked-present-me.

**Acceptance Criteria:**

**Given** past co-signed decisions,
**When** I open Decisions (or the app during a dip),
**Then** I can replay the original reasoning + precedent verbatim; the dip screen stays calm (losses in sky-blue, no red) and offers replay gently, never as an alarm (FR17, UX-DR5).

## Epic 5: Weekly Digest

### Story 5.1: Opt-in calm weekly email digest

As a user,
I want an optional gentle weekly summary,
So that I feel on-track without ever being nagged.

**Acceptance Criteria:**

**Given** I opt in from Settings,
**When** the weekly digest sends,
**Then** it's an email (no push/SMS) with plan status + on-track reinforcement in the calm coach voice, never alarmist or FOMO-inducing, with an easy unsubscribe (FR21, NFR8, pull-not-push).

## Epic 6: Go Live — Real Broker & LLM Integration

Retire the deferred integration risk from Epics 4–5: harden the money/email seams against concurrency, then wire the real Anthropic and Schwab adapters behind the existing gates so the Coach works against live services — not just fakes. **No real credential may reach a placement or send path until Story 6.1 lands.** Scope is v1 index funds/ETFs; the trust invariants (structural teeth, sole-writer, per-user isolation, never-a-dead-end, calm/honest/never-red) are unchanged and must still hold.

### Story 6.1: Atomic decision claim & idempotency hardening (GATING)

As a user whose money can actually move,
I want a double-approval or overlapping job to be structurally impossible,
So that no race can place two orders or send two emails.

**Acceptance Criteria:**

**Given** two concurrent in-flight `/approve` calls carrying the same `decision_id` (and, separately, an overlapping digest run),
**When** they execute,
**Then** exactly one wins via an atomic proposed→cosigning→cosigned claim (conditional `UPDATE … WHERE status='proposed'` gated on `rowcount==1`, or `SELECT … FOR UPDATE`), a **stable per-decision idempotency key is persisted at proposal time and reused across placements**, a DB **unique index on `idempotency_key`** backs it, and the digest marker advances via a conditional `UPDATE … WHERE last_sent_week IS DISTINCT FROM :week` gated on `rowcount==1` — with tests that exercise the in-flight window, not just sequential re-runs (closes deferred-work 4.9 concurrency + 5.1 double-send; NFR2, NFR8, AD-5, AD-7). **Blocks 6.3.**

### Story 6.2: Live LLM Gateway enablement & hardening

As the system,
I want the real Anthropic adapter proven against the live structured-output path,
So that the coach can emit real, parseable recommendations — not just fall back to the default plan.

**Acceptance Criteria:**

**Given** `anthropic` installed and `LLM_ADAPTER=anthropic` with a valid key,
**When** the coach runs `retrieve → compose → validate → surface` against the live API,
**Then** the real gateway is the sole Anthropic caller, enforces structured output and deterministic model routing, its runtime robustness is hardened (timeouts, malformed/refused responses degrade to the default plan — never a dead-end), and a real LLM-emitted `order_intent` citing a retrieved evidence ID passes the 4.2 gate and surfaces — verified once end-to-end behind the existing gates (closes deferred-work 4.1 real-adapter hardening; FR7, FR12–FR14, NFR2, AD-6).

### Story 6.3: Live Schwab placement & reconciliation mapping

As a user,
I want approved orders to actually reach Schwab and reconcile truthfully,
So that the Coach moves real money safely.

**Acceptance Criteria:**

**Given** Story 6.1 has landed and `BROKER_ADAPTER=schwab` with a live brokerage session,
**When** I approve an in-scope order,
**Then** the real `place_order`/`get_order_status` map Schwab responses to the normalized `OrderOutcome {status, filled_qty, avg_price, broker_ref}`, the same placement-time integrity + provider-match + v1-scope gates fire, indeterminate placements reconcile exactly once via the persisted idempotency key, and no phantom/duplicate order is possible (FR8–FR10, FR22, FR23, NFR3, AD-7, AD-11, AD-13).

### Story 6.4: Fixed-point money serialization pass

As a user,
I want every money value on the wire to read as a plain decimal,
So that amounts are never shown in confusing exponent notation.

**Acceptance Criteria:**

**Given** any endpoint serializing money (`amount`, `filled_qty`, `avg_price`),
**When** the value is large or unusual,
**Then** a shared fixed-point formatter (`format(Decimal, "f")`) is applied everywhere so no `E+` notation can cross the wire, round-tripping cleanly through the documented `Decimal(str(...))` consumer (closes deferred-work 4.6/4.7 money-format item).

### Story 6.5: Real Schwab balances & cash-only mapping (AD-14)

As a user holding mostly cash,
I want the app to see my real idle cash,
So that the missed-growth meter and oversized-lump warning tell me the truth.

**Acceptance Criteria:**

**Given** real Schwab balances available after 6.3,
**When** the portfolio cache is built for an all-cash or cash-heavy account,
**Then** idle cash is mapped from a dedicated balances source (not derived from a holdings row), so the missed-growth meter stops falsely reporting "no idle cash" and the FR11 oversized-lump warning has a real portfolio value to measure against (closes deferred-work AD-14 cash-only gap; FR11).

### Story 6.6: Decisions history scale hardening

As a user with a long history,
I want Decisions to stay fast and bounded as records accumulate,
So that the on-the-record memory scales.

**Acceptance Criteria:**

**Given** many decision records over time,
**When** I open Decisions,
**Then** `GET /decisions` is paginated and backed by a `(owner_id, co_signed_at)` index, and never-co-signed `proposed` records have a retention/pruning policy — with per-user isolation and verbatim replay unchanged (closes deferred-work 4.9/4.10 pagination/index/retention item).
