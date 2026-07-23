# Brief → PRD Reconciliation: Ballast

**Date:** 2026-07-22
**Inputs:** brief.md + addendum.md (2026-07-21) vs. prd.md + addendum.md (2026-07-22)
**Scope note:** The PRD deliberately tightened v1 to **coach-only** and deferred the guru (paper → real-capped) to the next release. This is an explicit, sound scoping decision and is **NOT** treated as a gap below. The reconciliation focuses on material drops, contradictions, and — most importantly — qualitative/voice intent that a requirements structure can silently erode.

**Verdict:** Structurally faithful and in several places sharper than the brief (the "regret from a self-caused loss" enemy, the co-sign/replay loop, and behavioral counter-metrics are genuine upgrades). The real losses are qualitative: the anti-anxiety *mission* and the patient-teacher *voice* are present as scattered mechanics but never named as a governing product principle, and a few concrete brief commitments (self-locked ceiling machinery, broad-diversification-beyond-S&P, the digest cadence/"pull-not-push" framing) thinned out or shifted.

---

## A. Material items in the brief NOT carried into the PRD/addendum

### A1. The self-locked "Ulysses" ceiling is now purely a guru feature — but the brief framed the commitment device more broadly
- **Brief:** the configurable risk dial + optional self-locked ceiling with cooldown-to-raise ("calm-you protects impulsive-you") is a **product invariant** (addendum principle #5) and a headline differentiator. It sits alongside the coach's job of protecting the user from self-sabotage.
- **PRD:** the self-locked ceiling appears only inside the deferred guru (PRD addendum §2). In v1 coach-only, the guardrail becomes FR11 ("warns before anything self-destructive... but never blocks"). That is a *softer* mechanism than the brief's commitment device.
- **Why it matters:** The brief's "commitment device" idea (pre-committing while calm to constrain your future panicked self) is arguably the single most powerful anti-behavior-gap concept in the whole document. In v1 it survives only as a warning the user can freely ignore. Nothing in v1 lets the calm user *pre-commit* (e.g., pre-authorize the payday contribution, lock a "don't let me sell the core" guardrail). This is a philosophy-level thinning, not just a deferred feature — consider whether a lightweight v1 commitment mechanism belongs in the coach.

### A2. "Broad" = can diversify beyond the S&P — the PRD narrows this
- **Brief (explicit and emphasized):** "Broad means the coach reviews your *whole* portfolio and can diversify beyond the S&P if you choose (e.g., total-market / international) — it never means avoiding the index." The addendum records "S&P only vs. broad portfolio → **Broad**" as a deliberate decision.
- **PRD:** FR6 maps holdings to an "index-core strategy"; FR10 limits v1 orders to "a small set of broad index funds/ETFs"; the open assumption names "a Schwab total-market or S&P 500 fund as the default core." The user-facing *ability to choose* diversification (international, total-market) is not a stated capability.
- **Why it matters:** A reasonable dev could implement v1 as "buy one S&P fund," which the brief explicitly said the product must not be reduced to. The "broad, whole-portfolio review, your choice to diversify" intent is at risk. Recommend an explicit FR or design note preserving user-chosen diversification within the core.

### A3. Competitive positioning / the "seam" is dropped from the PRD narrative
- **Brief + addendum:** the sharpest strategic claim is that Ballast occupies a seam almost no one else does — **advice + coaching + execution in your own account** (PortfolioPilot = advice-only no execution; Autopilot/robos = execution without coaching). This is the "what makes this different."
- **PRD:** no competitive framing survives. Understandable for a PRD, but the *design consequence* — that the review-AND-execute-in-one-loop is the differentiating feature, not a nice-to-have — is not flagged. It's implicitly in FR7-FR9, but the "this combination barely exists / don't split advice from execution" intent is unstated.
- **Severity:** Low-moderate. Positioning legitimately lives elsewhere, but the "one loop, don't send them elsewhere to trade" principle is load-bearing for UX and worth a one-line design invariant.

### A4. Digest cadence and "pull, not push" framing thinned
- **Brief:** "a regular plain-English digest/notification" that grows literacy so "confidence compounds," plus the strong **pull-not-push** invariant (guru never nags; no unprompted alerts).
- **PRD:** FR21 keeps the calm weekly email digest and the no-FOMO/no-alarm constraint (good). But (a) the digest's *educational/literacy-building* purpose from the brief is reduced to "summarizing plan status and reinforcing on-track"; the "teaches continuously... confidence compounds" role is softer. (b) The pull-not-push invariant survives only as "no unprompted FOMO alerts" (Design Invariant #4). The broader "the tool never nags you, it speaks when summoned" personality — a core anti-anxiety property — is narrowed to just FOMO alerts.
- **Why it matters:** "Never nags" is a tone/trust property beyond FOMO. A compliant-but-naggy product would violate the brief's spirit while passing every FR.

### A5. "Fee-only fiduciary you can actually afford / for free, and he owns it" — the affordability + ownership thesis
- **Brief:** repeatedly frames Ballast as "a fee-only fiduciary advisor you can actually afford," the "honest moat" being "it does exactly what its owner wants, for free, and he owns it," and the vision of serving "millions priced out of real advice."
- **PRD:** the affordability/ownership motivation is absent (the PRD went multi-user-from-day-one, which is fine). Not a functional gap, but the *why-now / who-this-is-for-economically* rationale that anchors the mission is gone. Low severity; flag only so it isn't lost from vision docs.

---

## B. Contradictions between brief and PRD

### B1. "Audience of one, architected to open later" → "multi-user from day one" (RESOLVED SHIFT, not a true contradiction — but verify intent)
- **Brief:** "Built first for an audience of one — its creator — but architected cleanly so it could open up to others later." Decision table: "**Me, architected to open later**." Scope defers "opening the tool to other users."
- **PRD:** "web-based, **multi-user** AI investing coach," "**multi-user from day one** (isolated per-user accounts and data)," FR1 email/password + per-user isolation.
- **Assessment:** This is a genuine divergence. The brief chose *single-user now, architected for later*; the PRD chose *build multi-user now, defer only the public offering / go-to-market*. The PRD is internally consistent (it repeatedly says multi-user *architecture* is fine, multi-user *public launch* is the RIA line) and this is defensible — but it is a real scope expansion (auth, isolation, per-user Schwab connections are now v1 build cost) that the brief did not authorize. **Confirm this was an intentional decision, not drift.** It adds meaningful v1 engineering (FR1, NFR1, NFR5) that the brief would have deferred.

### B2. Schwab execution friction framed as "the main engineering pain" — PRD under-weights it
- **Brief + addendum:** execution is "the hard half"; the ~7-day refresh-token expiry (weekly manual re-login) is called out as *the* central engineering risk, and read-only is easy while execution is hard.
- **PRD:** captured in FR3 and NFR4 (graceful re-auth, degraded mode) — good — but framed as a routine tolerance requirement, not as the dominant v1 risk. Combined with B1 (multi-user now), v1 has taken on *both* the hard execution half *and* multi-tenancy, which is a heavier v1 than the brief's "keep v1 focused." Not a contradiction in requirements, but a risk-weighting mismatch worth surfacing to planning.

### B3. Guru sequencing consistent (no contradiction — noted for completeness)
- Brief deferred real-money satellite as fast-follow just behind paper; PRD addendum keeps exactly this order (paper first, then real-capped). Aligned.

---

## C. Qualitative / voice / "feel" intent at risk in the FR structure

This is where a requirements document silently loses the most, and it is the heart of this product.

### C1. The mission — "make investing feel less scary" — is never stated as a governing principle
- The brief's emotional thesis is unmistakable and repeated: "built above all to make investing feel *less scary*," "the nagging *am I doing it right?* quietly fades," "makes investing a little less scary, one explained decision at a time."
- The PRD translates this into behavioral **metrics** (SM1-SM3 anxiety/confidence, CM3 anxiety-driven abandonment) and a well-chosen **enemy** (regret from self-caused loss). That is excellent and arguably sharper than the brief.
- **What's lost:** the *mission as a design lens*. Nowhere does the PRD say "every feature and every word of copy must reduce anxiety." A team can hit every FR and every SM while shipping something clinical and cold. The Design Invariants (§8) are all *negative/safety* rules (no black box, no fabrication, no FOMO, don't beat) — none is the *positive* emotional charter. **Recommend adding a top-line product principle: "Reduce anxiety" as invariant #0**, so it governs tone, copy, pacing, and UX — not just success measurement.

### C2. The "patient teacher" / honest plain-English coaching *voice* is reduced to "no jargon"
- **Brief:** "explaining everything in plain English," "a patient teacher," "explains its reasoning," "honest." The *personality* is warm, calm, patient, teacherly, non-condescending.
- **PRD:** NFR6 ("no unexplained jargon") + FR12 ("reasoning in plain English") + FR18 (just-in-time teaching). These preserve *clarity and transparency* but not *warmth, patience, calm*. "No jargon" is a floor; "patient teacher who makes you feel safe" is the intent. A response can be jargon-free, accurate, precedent-backed — and still feel curt or anxiety-inducing.
- **Risk:** Voice is exactly the kind of thing an FR list drops because it isn't easily testable. **Recommend a named "Coach Voice" spec** (calm, patient, honest, non-condescending, never alarmist, teaches without lecturing) as an NFR or design invariant, with example do/don't copy — otherwise the single most differentiating quality of the product is left to chance.

### C3. "Capture, not beat" — well preserved, one nuance thinned
- **Strongly preserved:** Design Invariant #5, the primary goal, and the addendum's rejected-timing section all carry "capture the return most investors fail to capture, don't beat the market" faithfully. This intent is safe.
- **Thinned nuance:** the brief's explicit *judgment rubric* — "Judge it on anxiety reduced, mistakes avoided, and literacy gained — **not on returns vs. the S&P**" — is honored in the success metrics but the *honest caveat framing* ("Ballast makes no promise of outperformance; ~92% of pros can't either; you win by not losing / closing the behavior gap") is only partially present. The *behavior-gap number* (users lose ~1-8%/yr; "you win by helping you not lose") is the persuasive heart of the pitch and appears only glancingly. Worth keeping the "you win by not losing" framing explicit for coach copy, since that *is* the reassurance the anxious user needs.

### C4. The counter-evidence / "honest, shows its own bear case" honesty principle is currently guru-only
- **Brief invariant #3:** "Counter-evidence mandatory — every guru idea ships with its honest bear case + odds." Broader brief tone: radical honesty, "always shows its own counter-argument," "honest about what's uncertain."
- **PRD:** for the *coach*, this becomes FR14 ("explicitly states what is uncertain / what it does not know") — which is good and correctly kept. The full counter-evidence/bear-case mechanism correctly defers with the guru.
- **No gap**, but note: FR14's "state what's uncertain" is the coach-side heir of the honesty invariant; ensure it's treated as a *hard invariant* like FR12/FR13 (currently FR12/FR13 are marked hard invariants and echoed in §8, but FR14 is not elevated to §8). The "honest about uncertainty" property is core to the anti-anxiety trust bargain and deserves invariant status alongside its siblings.

---

## D. Summary table

| # | Type | Item | Severity |
|---|------|------|----------|
| A1 | Dropped/thinned | Self-locked "Ulysses" commitment device is guru-only; no v1 pre-commitment mechanism | High |
| A2 | Narrowed | "Broad = user can diversify beyond S&P" narrowed toward single-index-core | Med-High |
| A3 | Dropped | Advice+coaching+execution "seam" positioning / one-loop design intent | Low-Med |
| A4 | Thinned | Digest's literacy purpose + "never nags / pull-not-push" reduced to no-FOMO | Med |
| A5 | Dropped | Affordability + ownership ("fiduciary you can afford, for free") thesis | Low |
| B1 | Shift | Single-user-now → multi-user-from-day-one (verify it was intentional) | High (verify) |
| B2 | Risk mismatch | Execution "hard half" under-weighted; v1 now carries execution + multi-tenancy | Med |
| C1 | Voice/mission | "Reduce anxiety" not stated as a governing design principle/invariant | High |
| C2 | Voice | Patient-teacher warmth reduced to "no jargon"; no Coach Voice spec | High |
| C3 | Voice nuance | "You win by not losing / behavior gap" reassurance framing thinned | Low-Med |
| C4 | Invariant status | Coach-side honesty (FR14 "state uncertainty") not elevated to §8 invariants | Low-Med |

## E. Recommended actions (highest leverage first)
1. **Add a positive product invariant #0: "Reduce anxiety."** Make the anti-anxiety mission a governing lens over tone/copy/UX, not just a success metric (fixes C1).
2. **Write a "Coach Voice" NFR/spec** — calm, patient, honest, non-condescending, never alarmist — with do/don't examples (fixes C2).
3. **Confirm the multi-user-from-day-one decision** and re-check v1 scope weight against the brief's "keep v1 focused" (B1/B2).
4. **Add an FR/design note preserving user-chosen diversification** within the core ("broad ≠ single S&P fund") (A2).
5. **Reconsider a lightweight v1 commitment device** (pre-authorize contributions / "don't let me sell the core" lock) rather than deferring all pre-commitment to the guru (A1).
6. **Broaden Design Invariant #4** from "no FOMO alerts" to "pull-not-push / never nags," and **elevate FR14** to invariant status (A4, C4).
