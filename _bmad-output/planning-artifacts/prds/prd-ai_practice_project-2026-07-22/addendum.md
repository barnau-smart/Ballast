# Ballast PRD — Addendum

Technical-how, deferred scope, and depth that belongs downstream (architecture / UX) but not in the PRD's requirement narrative. Companion to `prd.md`.

## 1. Technical direction (for architecture, not fixed requirements)

- **AI coach model:** an LLM powers the coach's plain-English reasoning and teaching. Default to the latest, most capable Claude model (Anthropic API). The LLM *composes explanations*; it does **not** originate factual precedent — precedent is retrieved from real market data and passed to the model, enforcing FR13 (precedent-or-don't-claim). Architect so the model physically cannot surface an unbacked precedent claim.
- **Market-data source (precedent engine):** needs historical index/ETF prices and a way to identify "similar past events" (drawdown magnitude and/or event type). Candidates to evaluate: a historical price API plus a curated event library. This is the backbone of FR13/FR15/FR19/FR20 and deserves an explicit architecture decision.
- **Brokerage integration:** Schwab Trader API (Individual) via OAuth2; the `schwab-py` community wrapper is a likely starting point. Wrap Schwab behind a **broker-adapter interface** so Alpaca (or others) can be added later without touching coach logic. Note the operational realities: multi-day app approval, ~7-day refresh-token expiry (NFR4), ~120 order req/min per account.
- **Auth & multi-tenancy:** email+password auth with strict per-user data isolation; brokerage tokens stored as high-sensitivity secrets (NFR1).
- **Delivery:** web app (dashboard + conversational coach). Weekly digest via email only in v1.
- **Teaching = reasoning surfaced:** just-in-time teaching (FR18) and reasoning-on-every-call (FR12) are the *same* engine viewed two ways — architect them as one subsystem, not two, so explanations and lessons stay consistent.

## 2. Deferred scope (post-v1 roadmap)

**Next release — The Guru (leashed):**
- **Paper mode first:** fake-money simulator where the guru pitches ideas with mandatory counter-evidence; zero real-money risk; simplest to build; no added regulatory exposure.
- **Then real-money capped satellite:** configurable risk dial (user sets satellite size + appetite), coach obeys but always voices its opinion as risk rises, plus an optional **self-locked "Ulysses" ceiling** (cooldown-to-raise) so calm-user protects impulsive-user. Guru never touches the core; coach-final-word.

**Later (the apprenticeship arc):**
- #10 Strategy-derived curriculum (teach the exact principles the app runs on + behavioral traps).
- #11 Financial-literacy quiz → adaptive personalization per user.
- #13 Agreement-based progression (track how often the user's supervised calls match the coach; consistent alignment earns more autonomy).
- #14 Karate-belt competence tiers (gamified; unlock more autonomy / higher guru dial).
- #12 "You take the reins" (reversed propose-and-approve — user decides, coach supervises); most complex, depends on progression existing first.
- North star: these form an apprenticeship whose endgame is to make Ballast progressively *less necessary* — a teacher that works itself out of a job.

## 3. Regulatory detail

- Personal/private educational tool = low exposure.
- The RIA line: personalized securities advice and/or trade execution *for other people*, especially if marketed. Multi-user *architecture* is fine; a multi-user *public offering* is the trigger to get compliance review. Paper-mode and educational framing reduce exposure for the guru.

## 4. Rejected direction (carried from the brief)

The original concept — a market-timing engine using economic signals + congressional-trade (Capitol Trades) data to time S&P entries — was rejected on the evidence (SPIVA ~92% of pros trail the index; DALBAR behavior gap; Vanguard lump-sum > DCA ~68%; Barber-Odean active traders ~-6.5%/yr; NANC/KRUZ outperformance was a tech tilt; congressional data is stale + stock-specific). Ballast keeps the *emotional* goal and drops the *timing* premise. See the brief's addendum for full evidence.
