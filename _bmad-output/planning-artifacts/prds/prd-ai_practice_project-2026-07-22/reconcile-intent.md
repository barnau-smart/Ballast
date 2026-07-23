# Reconcile: Brainstorm Intent vs. PRD (+ Addendum)

**Date:** 2026-07-22
**Inputs:** `brainstorm-intent.md` (source of truth) vs. `prd.md` + `addendum.md` (drafted from it)
**Scope note:** Per the reconciliation brief, the coach-only v1 tightening and the deferred guru are *intended* scoping decisions and are NOT treated as gaps.

---

## Verdict

**Faithful.** All 14 MoSCoW features are accounted for, the design invariants are present (and correctly strengthened), and the root problem is the explicit anchor. No dropped Must/Should feature; no mis-parked deferred item. Findings are minor fidelity/nuance gaps, not missing scope.

---

## 1. MoSCoW Feature Traceability

### MUST — v1 (all present)

| # | Intent feature | PRD landing | Status |
|---|----------------|-------------|--------|
| 1 | Reasoning on every call (no black-box) | FR12 + Invariant 1 + NFR2 | Present |
| 2 | Precedent-backed claims only | FR13 + Invariant 2 + NFR2 | Present |
| 3 | Flag unknowns / acknowledge uncertainty | FR14 | Present |
| 4 | Just-in-time teaching | FR18 | Present |
| 5 | Recovery-precedent view | FR15 | Present |
| 6 | Expert co-signs the decision | FR16 | Present |
| 7 | Decision replay after a dip | FR17 | Present |

All seven Musts are reflected as v1 FRs. FR12/FR13 are additionally hardened at the system level (NFR2) and re-stated as invariants — a faithful amplification of intent #1/#2.

### SHOULD — v1 if time (all present)

| # | Intent feature | PRD landing | Status |
|---|----------------|-------------|--------|
| 8 | Missed-growth meter | FR19 (marked "if time") | Present |
| 9 | Headline contextualizer | FR20 (marked "if time") | Present |

Both correctly labeled Should / "v1 if time" (Scope §5, §6.7). FR20 additionally binds the no-FOMO invariant (responds to user concern, no unprompted alerts) — faithful to intent's "no FOMO alerts."

### COULD / WON'T — deferred (all correctly parked)

| # | Intent feature | PRD landing | Status |
|---|----------------|-------------|--------|
| 10 | Strategy curriculum | Addendum §2 "Later" | Parked correctly |
| 11 | Financial-literacy quiz/assessment | Addendum §2 "Later" | Parked correctly |
| 13 | Agreement-based progression | Addendum §2 "Later" | Parked correctly |
| 14 | Karate-belt tiers | Addendum §2 "Later" | Parked correctly |
| 12 | "You take the reins" (WON'T) | Addendum §2 "Later," flagged most complex / depends on progression | Parked correctly |

Deferral ordering matches intent: Guru is the "immediate next release" (PRD §5 + addendum §2), then the apprenticeship arc #10→#11→#13→#14→#12. The "make itself unnecessary / works itself out of a job" north star is preserved (addendum §2 closing line).

**All 14 features accounted for. Nothing dropped or mis-prioritized.**

---

## 2. Design Invariants

Intent §5 lists four non-negotiables. PRD §8 carries all four and adds a fifth:

| Intent invariant | PRD Invariant | Status |
|------------------|---------------|--------|
| No black-box recommendations | §8.1 (FR12) | Present |
| Precedent-backed or don't claim | §8.2 (FR13) | Present |
| Coach has final word (coach-is-boss) | §8.3 | Present |
| No FOMO alerts | §8.4 | Present |
| — | §8.5 Capture, don't beat (added) | Faithful addition — lifted from intent §1 "capture-not-beat," correctly elevated to invariant |

All four intent invariants present. The added 5th is justified by intent §1 and is not a deviation.

---

## 3. Root Problem Anchor

Intent §2 names the enemy as **regret from a self-caused loss** (regret aversion), compounded by **omission bias** and **headline-paralysis**.

- PRD §1 states it explicitly: "one specific enemy uncovered in discovery: regret from a self-caused loss (and its cousins, omission bias and headline-paralysis)."
- The before/during/after regret-loop framing (intent synthesis §4) is preserved verbatim in PRD §1 and §5.
- Omission bias is operationalized: missed-growth meter (FR19) + counter-metric CM2 (cash drag).
- Headline-paralysis is operationalized: headline contextualizer (FR20) + recovery-precedent (FR15) + UJ-1's geopolitical-shock scenario.

**The root problem is faithfully the anchor.** Success metrics (SM1–SM4) and counter-metrics (CM1–CM3) are all behavior-based (not returns), consistent with intent's "defeat regret, not fix bad timing."

---

## 4. Findings (minor — fidelity/nuance, not missing scope)

**F1 — "Guru dial / leash" undersold in the PRD body (LOW).**
Intent §1 makes the guru-leashing dial a first-class product framing ("any guru behavior is leashed and capped by a dial"). The PRD body (§1, §5) mentions the guru only as deferred and does not surface the dial concept; the full dial + Ulysses ceiling detail lives only in the addendum §2. This is consistent with the intended deferral, but the *coach-is-boss ↔ leashed-guru* relationship (invariant §8.3 gestures at it: "the (future) guru never touches the core") is the load-bearing half. Acceptable given guru is deferred; noted so it isn't lost when the guru release is spec'd.

**F2 — "Teaching = reasoning in another hat" synthesis insight not carried forward (LOW).**
Intent synthesis §4 states #4 and #10 are #1 "wearing a different hat" — trust and education are the same feature. The PRD implements FR18 (teaching) and FR12 (reasoning) as separate FRs without noting they are the same underlying capability. This is a design/architecture note, not a scope gap — worth preserving so teaching and reasoning aren't built as two disconnected subsystems. Consider capturing in architecture.

**F3 — "Audience shift" decision only partially reflected (LOW).**
Intent §5 flags a deliberate ripple: design for ANY user (not the creator's knowledge), meeting each where they are, *assessed via the literacy quiz*. The PRD does adopt multi-user "any user" framing (§1, §3) — faithful. BUT the mechanism the intent tied it to (literacy quiz #11) is deferred to post-v1. Net effect: v1 claims "meet each user where they are" (NFR6, persona §3) without the assessment instrument that intent paired with that promise. Not a contradiction (plain-language default covers the floor), but the "meet them where they are" capability is weaker in v1 than the intent's phrasing implies. Flag for expectation-setting; the deferral of #11 itself is correct.

**F4 — Market/monetization "maybe" preserved correctly (INFO, no action).**
Intent §5 ends "Market is still 'maybe.'" PRD §1, §5, §9 all keep go-to-market as an open decision and add the RIA/compliance gate. Faithful — noted as confirmation, not a gap.

---

## 5. Not Flagged (per instructions / verified intentional)

- Coach-only v1 tightening (guru fully deferred) — intended, not a gap.
- Deferred guru (paper-sim → capped real-money satellite) — intended, correctly ordered as next release.
- Added invariant §8.5 (capture-don't-beat) — justified by intent §1, faithful.
- Rejected market-timing direction (addendum §4) — correctly carried as rejected, matches intent's "explicitly rejects market-timing/FOMO."

---

## Conclusion

The PRD is a faithful projection of the brainstorm intent. Coverage is complete across all 14 MoSCoW items, invariants, and the root-problem anchor. The four findings are low-severity fidelity notes (guru-dial framing, teaching=reasoning insight, audience-shift/quiz coupling) to preserve downstream — none represents dropped or mis-prioritized scope.
