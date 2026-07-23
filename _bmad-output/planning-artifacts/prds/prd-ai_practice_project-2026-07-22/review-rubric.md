# PRD Quality Review — Ballast

## Overall verdict

This is a genuinely strong mid-rigor PRD: it has a real thesis (defeat regret from self-caused loss), the features demonstrably serve that thesis, the scope cut (coach-first, guru deferred) is honest and reasoned, and the Design Invariants are load-bearing rather than decorative. It holds up well against its stated stakes. What's at risk is **done-ness clarity at the execution boundary** — several of the most consequential FRs (order placement, execution reconciliation, re-auth, precedent matching) lean on adjectives ("gracefully," "quickly," "conversational") or leave the failure/edge behavior unspecified, which is exactly where downstream architecture and story creation will stall. The requirement content is complete for a v1 learning build, but the money-touching path needs tighter acceptance conditions before it's build-ready.

## 1. Decision-readiness — strong

The PRD makes real decisions and owns their trade-offs rather than smoothing to neutral. "Capture, don't beat" (§8.5) is a stated bet that discards outperformance as a goal — a genuine give-up, reinforced by the rejected-direction section in the addendum (§4) that shows the timing premise was killed on evidence, not quietly dropped. The coach-vs-guru priority ("The Guru... the immediate next release," §5) is a stated sequencing decision, not a wish list. The regulatory posture (§9) names the RIA line explicitly and gates it before public launch rather than hand-waving. Open Questions (§10) are actually open — the "similar past event" definition and contribution-scheduling model are real unknowns deferred to design, not rhetorical.

One soft spot: "going to market is an open decision" (§1) is stated but never given an owner or a decision trigger beyond the compliance gate. Acceptable at this stage.

### Findings
- **low** Go-to-market decision left ownerless (§1, §9) — flagged as open but no trigger/owner. *Fix:* add a one-line `[NOTE FOR PM]` naming what evidence would reopen the monetization question.

## 2. Substance over theater — strong

Little furniture here. The single persona (§3, "the anxious beginner") is the right call for a solo-plus-friends learning product — one persona that actually drives features (freezes on timing → FR15 precedent; fears self-caused loss → FR16 co-sign; wants to understand → FR18 teaching). No persona padding. The Vision (§1) is specific to this product — "talking calm-past-self's logic to panicked-present-self" (§4, UJ-1) could not be swapped into a generic fintech PRD. NFRs mostly carry product-specific weight (NFR2 elevates FR12–14 to an architectural constraint; NFR4 names the concrete Schwab 7-day token reality) rather than boilerplate.

The one boilerplate-adjacent item is NFR7 (Responsiveness) — see done-ness.

## 3. Strategic coherence — strong

This PRD has a thesis and everything bends to it. The enemy is named ("regret from a self-caused loss," §1) and the feature set is explicitly organized as before/at/after the decision — precedent (FR15) before, co-signed reasoning (FR7/FR16) at, replay (FR17) after. That is a coherent arc, not a backlog with headings.

Success Metrics are the standout: SM1–SM4 measure *behavior quality* (consistency, staying the course, confidence, follow-through), which is what the thesis is actually about — not vanity activity metrics. And CM1–CM3 are real counter-metrics: CM1 (overtrading) directly guards against the product perverting its own purpose. This is textbook thesis-validating metric design.

MVP scope kind is a clean "experience/problem-solving" cut and the scope logic matches.

### Findings
- **medium** SM1, SM2, SM4 lack defined measurement windows/thresholds (§2) — "a maintained contribution streak," "do not panic-sell," "act on recommendations" have no baseline, target value, or observation period. *Fix:* even rough bounds ("streak = ≥3 consecutive intended contributions met; panic-sell = any sell >X% of core within N days of a drawdown >Y%") make these testable. SM3 already flags its own gap in §10.

## 4. Done-ness clarity — thin

This is the dimension that needs work and it's the one story creation leans on hardest. The reasoning/transparency invariants (FR12–14) and the propose-and-approve gate (FR8) are crisp and testable — a clear pass/fail exists. But the execution path and the non-functional bounds are soft:

- **FR3 / NFR4 — "prompts the user gracefully," "graceful re-auth," "clear degraded-mode behavior."** The *intent* (read/coach continues, execution requires live session) is stated, which is good, but "gracefully" is an adjective, not an acceptance condition. What happens to an *in-flight approved order* when the token expired between approval and placement? Undefined.
- **FR9 / NFR3 — execution reliability.** NFR3 correctly demands "no phantom or duplicate orders" and reconciliation against Schwab — but there is no FR describing what the user sees or the system does when an order is *rejected, partially filled, or times out*. FR9 covers the happy path ("places the order... confirms execution") only. For a money-touching product this is the single biggest requirement gap.
- **NFR7 — "quickly enough to feel conversational."** Pure adjective. No bound.
- **FR20 headline contextualizer / FR15 recovery-precedent — "similar past events."** §10 honestly flags the definition is open, so this is a known deferral rather than a hidden gap — but FR15 is in-scope for v1 and its core matching logic being undefined means "done" for FR15 currently can't be written.

### Findings
- **critical** Execution-failure path unspecified (FR9, NFR3) — no requirement covers order rejection, partial fill, timeout, or the approval→placement token-expiry race. *Fix:* add an FR: "If an approved order is rejected, partially filled, or cannot be confirmed, Ballast shows the true state, never silently retries, and never double-submits; the user is told exactly what did/didn't happen." This is the acceptance condition NFR3 is gesturing at.
- **high** Adjective-bound NFRs and re-auth behavior (FR3, NFR4, NFR7) — "gracefully," "conversational," "clear degraded-mode" have no testable condition. *Fix:* give NFR7 a rough p95 latency target; state FR3/NFR4 in terms of what the user can and cannot do while a session is expired, and what happens to work in progress.
- **medium** FR15 matching logic undefined but in-scope (§6.5, §10) — recovery-precedent is v1, yet "similar past event" is deferred to design, so FR15 has no writable "done." *Fix:* acknowledge in the FR that v1 may ship a deliberately simple matcher (e.g., drawdown-magnitude only) so it's buildable, and keep the richer definition as the open item.

## 5. Scope honesty — strong

Omissions are explicit and well-organized. §5 "Out of v1" lists deferrals *in priority order* with the guru named as the immediate next release — this is de-scoping done in the open, not silently. The `[ASSUMPTION]` and `[OPEN]` tags in §10 are used honestly and cover the real inferences (data provider, fund set, event-matching, cadence, SM3 method). The addendum cleanly separates technical-how and deferred roadmap from the requirement narrative, which keeps the PRD itself honest about what is a requirement vs. a direction.

Open-items density is low and appropriate for the stakes. No blocker here.

### Findings
- **low** Assumptions not indexed with IDs (§10) — inline `[ASSUMPTION]`/`[OPEN]` tags exist but aren't numbered/cross-referenced. Fine at this rigor level; note only for downstream traceability. *Fix:* optional — number them if UX/architecture will cite them.

## 6. Downstream usability — adequate

The PRD will feed UX and architecture, and mostly reads cleanly section-by-section. FR IDs are contiguous and unique (FR1–FR21); NFRs NFR1–NFR7; SMs and CMs numbered. Cross-references resolve (FR12–14 ↔ NFR2 ↔ Invariants §8; NFR4 ↔ FR3; addendum maps FR13/15/19/20 to the precedent engine). The two UJs each have an implied protagonist ("our user" / MasterB from §3).

Weaknesses are minor: **there is no Glossary**, so domain nouns ("index-core strategy," "core," "co-sign," "precedent," "the guru," "satellite") are defined by usage and mostly consistent — but "index core," "index-core strategy," and "core" drift in form across FR6/FR10/§8. UJ-2's protagonist is unnamed ("a new user"). For a PRD of this scope these are cosmetic, not structural.

### Findings
- **medium** No Glossary; downstream will re-derive domain terms (whole PRD) — "index core"/"core"/"satellite"/"co-sign"/"precedent" are load-bearing and used across FRs, NFRs, and the addendum. *Fix:* add a short glossary (5–8 terms); it directly de-risks source-extraction for architecture.
- **low** UJ-2 protagonist unnamed (§4) — "a new user" vs. UJ-1's named user. *Fix:* reuse MasterB for consistency.

## 7. Shape fit — strong

The shape matches the product. This sits between "hobby/solo" and "meaningful-UX consumer product," and the PRD calibrates correctly: two UJs (not ten), one persona, invariants elevated to their own section because they genuinely constrain architecture, and a regulatory section that is proportionate (names the RIA line, gates it, doesn't build a compliance matrix a personal tool doesn't need). It is neither over-formalized (no UJ sprawl for what is nearly a single-operator tool today) nor under-formalized (the money/trust-critical invariants get first-class treatment). The multi-user-architecture-vs-public-offering distinction (§9) is exactly the right amount of rigor for the actual risk.

## Mechanical notes

- **Glossary:** absent. Recommended (see §6). Minor drift: "index-core strategy" (FR6) vs. "index core" (UJ-1) vs. "core" (FR10, §8) — same concept, three forms.
- **ID continuity:** clean. FR1–FR21 contiguous, no gaps/dupes. NFR1–NFR7, SM1–SM4, CM1–CM3 all contiguous. Cross-refs (FR↔NFR↔Invariant↔addendum) all resolve.
- **Assumptions Index roundtrip:** inline tags present in §10 but not indexed/numbered; no separate index section. Acceptable at this rigor; the deferred `#10–#14` item numbers in §5/addendum reference the source brief, not this PRD — fine but worth a note so a reader doesn't hunt for them here.
- **UJ protagonists:** UJ-1 carries context inline well; UJ-2 protagonist unnamed.
- **Required sections:** all present and proportionate — Overview, Goals/SM, Users, UJs, Scope, FRs, NFRs, Invariants, Regulatory, Open Questions.
