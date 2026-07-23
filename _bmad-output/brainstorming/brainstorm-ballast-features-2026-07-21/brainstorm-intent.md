# Ballast — Brainstorm Intent

## 1. Product

Ballast is a personal, web-based AI investing coach that sits on top of a user's Schwab account. It operates coach-is-boss: the coach runs the strategy and has the final word, while any "guru" behavior is leashed and capped by a dial. It works propose-and-approve — the coach proposes moves and the user approves them. The strategy is index-core and capture-not-beat: it aims to capture market returns, not beat them, and explicitly rejects market-timing and FOMO-driven behavior. Every recommendation is transparent (no black-box calls), and no FOMO alerts are issued.

## 2. Root Problem (the anchor)

The enemy is **regret from a self-caused loss** (regret aversion): a loss the user caused by acting feels worse than an identical passive loss — acknowledged as not-logical, but real. It is compounded by **omission bias** ("I'm not losing if I don't play," even though that's false, so inaction feels safe/costless) and by **headline-paralysis** (scary news, e.g. war uncertainty, makes investing-now feel too dangerous, amplified by the sense that the current era is uniquely scary). Every feature exists to defeat this regret and the paralysis it produces — not to fix bad timing.

## 3. Prioritized Feature Pool (MoSCoW)

**MUST — v1 (trustworthy-expert transparency set + front half of the regret loop):**
1. Reasoning on every call — no black-box recommendations.
2. Precedent-backed claims only — cite historical evidence or don't make the claim.
3. Flag unknowns / acknowledge uncertainty alongside advice.
4. Just-in-time teaching — explain the principle + mechanics at the moment of each action.
5. Recovery-precedent view — show past times the market dropped like this (similar event/headline) and recovered.
6. Expert co-signs the decision — coach shares accountability, making it an on-the-record joint call with reasoning + precedent attached.
7. Decision replay after a dip — replay the reasoning + precedent "we both agreed on" so past-calm-self talks down present-panicked-self.

**SHOULD — v1 if time:**
8. Missed-growth meter — running total of growth forgone by holding uninvested cash, making the cost of inaction visible.
9. Headline contextualizer — when scary news breaks, compare it to similar past events and their actual market outcomes.

**COULD — later:**
10. Strategy curriculum — teach the exact principles the app's own management runs on and the behavioral traps it guards against.
11. Financial-literacy quiz/assessment — gauge each user's level and personalize the curriculum to start where they are.
13. Agreement-based progression — track how often the user's supervised decisions match the coach's; consistent alignment earns more autonomy.
14. Karate-belt tiers — gamified competence tiers (white→black belt) earned as the user's calls align; each belt unlocks more autonomy.

**WON'T — this time:**
12. "You take the reins" mode — reversed propose-and-approve where the user decides and the coach supervises; most complex, depends on progression existing first.

## 4. Key Synthesis Insights

- **v1 is one weapon (defeat regret), fired across time**: before (#5, #9), during (#6), and after (#7) — with reasoning + precedent (#1, #2) as the ammunition.
- **#8 is the pincer's other half**: the missed-growth meter attacks omission bias, not regret — the second front of the same battle.
- **Apprenticeship arc / "make itself unnecessary" north star**: the later pile #6 → #13 → #14 → #12 forms an apprenticeship progression; the product's honest endgame is to make itself unnecessary.
- **Teaching = reasoning in another hat**: #4 and #10 are #1 wearing a different hat — trust and education are the same feature.

## 5. Decisions That Ripple Into the PRD

- **Audience shift**: do NOT build the product (or its education) around the creator's specific knowledge. Design for ANY user, meeting each where they are, assessed via the literacy quiz. This turns Ballast from a personal tool into a product — ripples directly into the PRD/brief audience framing. Market is still "maybe."
- **Design invariants (non-negotiable):** no black-box recommendations; precedent-backed claims only (cite evidence or don't claim); coach has the final word (coach-is-boss); no FOMO alerts.
