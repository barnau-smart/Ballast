---
name: Ballast
status: final
created: 2026-07-22
updated: 2026-07-22
sources:
  - '_bmad-output/planning-artifacts/prds/prd-ai_practice_project-2026-07-22/prd.md'
  - '_bmad-output/planning-artifacts/architecture/architecture-ai_practice_project-2026-07-22/ARCHITECTURE-SPINE.md'
  - '_bmad-output/planning-artifacts/briefs/brief-ai_practice_project-2026-07-21/brief.md'
---

# EXPERIENCE.md — Ballast

Visual identity lives in `DESIGN.md`; this spine owns *how it works*. Tokens are referenced as `{colors.brand-red}` etc. Both spines win over any mock on conflict.

## Foundation

- **Form factor:** responsive web app (React SPA); comfortable one-column on mobile, roomy on desktop.
- **Theme:** dark-only in v1 (the `ballast-terminal` theme — green phosphor interface + rare red brand + neon-pink accent). Theming is token-based (`DESIGN.md`), so a calmer "market" theme is a future swap, not a rebuild.
- **UI system:** hand-rolled tokenized components (no heavy component library); every component reads DESIGN.md tokens, never hardcoded values.

## Emotional Design Principles (product-specific, load-bearing)

These override ordinary UX instincts wherever they conflict:
1. **Calmest when it matters most.** The scarier the user's moment (a dip, a headline), the more legible and serene the screen gets. No red, no motion, no urgency.
2. **Always the why + the unknown.** No screen ever shows a bare verdict. Every recommendation shows its reasoning and an explicit "what I can't know."
3. **Pull, not push.** Ballast never sends an unprompted alert, badge, or "you're missing out" nudge. It speaks when asked. The only proactive contact is an opt-in calm weekly email.
4. **Never a dead-end.** "No confident call" always resolves to the reassuring default plan, never silence or "do nothing."
5. **Capture, not beat.** Progress is framed as consistency and staying the course — never as beating the market.

## Information Architecture

Surfaces:
1. **Auth** — sign up / log in (email + password).
2. **Onboarding** — link Schwab (OAuth), first plain-English portfolio reveal.
3. **Dashboard** — portfolio in plain English + entry point to ask the coach; the calm home.
4. **Coach** — the conversational decision surface (propose → approve/decline). The emotional centerpiece.
5. **Decisions** — history of co-signed decisions; the source for replay.
6. **Settings** — Schwab connection status & re-auth, weekly-digest opt-in, theme, account.

Closure: every v1 need maps to a surface, and every surface has a journey that lands there (see Key Flows). Deferred features (guru, curriculum, quiz, progression) have **no** surface in v1.

## Voice and Tone

The coach is a **patient, warm, honest teacher** (DESIGN.md carries the brand voice; this is the microcopy contract):
- **Plain, not clever.** Short sentences, no unexplained jargon. If a term is unavoidable, it's explained in-line.
- **Honest about limits.** "I can't know whether next week is up or down — nobody can." Uncertainty is stated plainly, never buried.
- **Warm, never hype or condescending.** No "🚀", no "smart money," no "you should have…". Never scolds.
- **Reassuring at dips.** "This is the part that feels bad. Here's what the record shows, and here's the plan we agreed on."
- **Default-plan phrasing (no special call):** "There's no strong signal today — and that's normal. The proven move is the steady one: your regular contribution. Here's why waiting usually costs more than it saves."
- **Data voice (mono blocks):** terse, factual, sourced — "S&P ~7% below peak · recovered to even in a median ~2 months (12 similar drops since 1950)."

## Component Patterns (behavioral)

- **Coach card** — renders the fixed sequence: `action_label` → **Why** → **precedent data-block** → **uncertainty callout** → **co-sign zone**. Reasoning and uncertainty are never collapsed by default (the "why" is not hidden behind a click).
- **Precedent data-block** — expandable to show the underlying instances; always cites source + as-of date. If no precedent qualifies, the block is replaced by the strategy-default rationale (never empty).
- **Approve & Co-sign** — a single deliberate action that both executes (propose-and-approve) and persists the co-signed decision record. Declining ("Not now") is always equally easy and never penalized.
- **Replay** — from a decision in **Decisions** (or surfaced gently when the user opens the app during a dip), replays the original co-signed reasoning + precedent verbatim.
- **Missed-growth meter** (SHOULD) — a quiet, always-available figure; framed as information, never as pressure ("your idle cash has sat out ~$X of growth" — stated once, calm).
- **Headline contextualizer** (SHOULD) — user pastes/asks about a scary headline; response is drawdown-keyed precedent, never event-classification.
- **Re-auth banner** — calm, neutral/muted (never red); explains *why* the weekly Schwab re-login is needed; read/coach continue in degraded mode; only execution is gated.

## State Patterns

- **Coach thinking** — a calm, non-frantic indicator (no spinner urgency); "looking at the record…".
- **Empty (pre-link)** — dashboard invites linking Schwab, explains what will happen, reassures about read scope.
- **No precedent / no confident call** — resolves to the default-plan recommendation + reason (Principle 4).
- **Order outcomes** — explicit, honest states for **filled / partial / rejected / timeout / pending**; the user always sees the true resulting state (no phantom success).
- **Schwab session expired** — degraded mode: portfolio (cached) and coaching remain; execution shows the calm re-auth prompt; no order attempted on a dead session.
- **Dip / loss state** — screens stay calm (Principle 1); replay is one tap away; losses in sky-blue, not red.

## Interaction Primitives

- **Propose → Approve/Decline** — every trade is user-approved; no silent execution.
- **Approve & Co-sign** — primary action; on success, routes to a calm confirmation + the persisted decision.
- **Explain more** — inline teaching expander on any term or reasoning step (FR18); pulls, never interrupts.
- **Ask the coach** — the primary entry; free-text or guided ("Should I invest my paycheck?", "This headline scares me").
- **Re-auth** — resume-in-place after the OAuth redirect; the user returns to exactly where they were.

## Accessibility Floor

- **Contrast:** all text meets WCAG AA on the dark theme; muted neon is used for decoration/large text, not small body copy. Verify `muted` text sizes hit AA.
- **Color independence:** up/down and all status never rely on color alone — always an icon/sign/label (critical given colorblind users + the green/sky-blue scheme).
- **Motion:** `prefers-reduced-motion` disables the wordmark flicker and the terminal cursor blink; nothing essential depends on motion. (No scanlines.)
- **Keyboard:** full keyboard path through the coach loop (ask → review → approve/decline); visible focus (red glow) on all interactives.
- **Screen readers:** precedent data-blocks have readable text equivalents (not image-only); the reasoning and uncertainty are real text, always in the DOM (never hidden from AT).

## Key Flows

**UJ-1 — The scary-moment decision (climax: the co-sign).**
MasterB, mid-week, just paid, sees a frightening geopolitical headline and freezes. He opens Ballast and asks, "Should I invest my $500? This feels like a bad time." The coach responds in one calm card: a clear recommendation, the plain *why*, a green-terminal precedent block ("drops this size recovered in ~2 months, 11 of 12 higher a year later"), and an honest "what I can't know." **Climax:** the dashed-red co-sign zone — *"I'll put my name on this with you"* — and MasterB taps **Approve & Co-sign**. The dread is gone; it's a shared, reasoned decision on the record.

**UJ-2 — The dip, a week later (climax: the replay).**
The market drops. MasterB opens Ballast anxious, half-regretting the buy. The screen is calm — losses in sky-blue, no red, no alarm. A gentle chip: *"Want to see the reasoning we agreed on?"* He taps it and **replay** shows his own co-signed rationale and the precedent, verbatim. **Climax:** calm-past-self talks down panicked-present-self; he closes the app without selling. The behavior gap, avoided.

**UJ-3 — Onboarding (climax: the first plain-English portfolio).**
New user signs up, links Schwab through the OAuth redirect (told plainly what Ballast can see and that a weekly re-login is normal). **Climax:** the first dashboard — their real holdings, explained in plain English, mapped to the index-core idea — the moment "I don't understand my money" starts to lift.
