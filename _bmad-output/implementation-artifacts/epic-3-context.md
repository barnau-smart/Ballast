# Epic 3 Context: See the Record (Precedent Engine + Calming Views)

<!-- Generated from planning artifacts. Regenerate with compile-epic-context if planning docs change. -->

## Goal

Epic 3 builds the deterministic precedent backbone of Ballast and the user-facing tools that calm the scary moments. The Precedent Engine ingests decades of real market history into a local `market_daily` store, then computes historically similar drops on demand and returns them as fixed-shape evidence records. On top of that engine sit three calming views: a recovery-precedent view that shows drops like the current one have recovered, a missed-growth meter that quantifies the cost of sitting in cash, and a headline contextualizer that reframes a scary headline against comparable historical drawdowns. This epic matters because it is the sole, trustworthy source of every market fact the coach will ever cite — precedent that a developer can prove is real and reproducible, never fabricated by an LLM. It underpins the trust invariant that all factual claims must be data-backed (the coach's citations in Epic 4 draw entirely from what this engine produces).

## Stories

- Story 3.1: Market-data ingestion → `market_daily`
- Story 3.2: Drawdown matching & Evidence Record Contract
- Story 3.3: Recovery-precedent view
- Story 3.4: Missed-growth meter
- Story 3.5: Headline contextualizer

## Requirements & Constraints

- All factual market statistics must originate from this engine as evidence records; no market number may be computed or recalled by an LLM. This is a structural trust invariant, not a guideline.
- Precedent matching must be fully deterministic and involve no LLM call — same inputs always yield the same matched windows and stats.
- Market data is ingested as derived analytics computed from the source, not redistributed raw vendor data.
- Ingestion runs as a scheduled job under an explicit SYSTEM scope (not a user scope) and must tolerate source hiccups/outages gracefully without corrupting the store.
- Precedent and calming-view lookups must return quickly enough to feel conversational (target: within a few seconds).
- Every calming view cites its source and an as-of date; screen-reader text equivalents are required for all data blocks.
- Calming views must never be alarmist, never pressure the user, and never be sent unprompted (pull-not-push). The missed-growth meter is information stated once, calmly — not a nudge. The headline contextualizer only responds on demand.
- When no precedent qualifies, views must fall back to a strategy-default rationale rather than showing an empty state.
- The headline contextualizer reframes to drawdown-keyed precedent only; it must never classify or interpret the news event itself.

## Technical Decisions

- **Precedent Engine ownership (AD-3):** The engine is the single source of market statistics. It exposes evidence records with stable IDs; downstream (Epic 4) may cite only IDs it was handed. No module bypasses this owner to reach market data.
- **Evidence Record Contract (AD-12):** Every evidence record has the fixed shape `{id, kind: event-precedent|strategy, statement, stats{}, source, as_of}` with a stable ID. All producers emit this exact shape; the Epic 4 recommendation validator and immutable decision snapshot both depend on it, so the contract must not drift.
- **Two evidence kinds:** `event-precedent` (tactical, matched from history) and `strategy` (the always-available consistency baseline used for the no-qualifying-precedent fallback).
- **Matching inputs:** drawdown magnitude plus velocity; v1 matching is drawdown-band based only. Event-category / event-taxonomy tagging is explicitly a later enrichment, not part of this epic. Exact drawdown-band definitions are a build-time detail, not fixed at the spine level.
- **Data model:** `market_daily` is keyed by `symbol` + `day`. Precedent is computed from `market_daily` at request/decision time and snapshotted by the caller — never re-derived later from the same records.
- **Hexagonal boundary (AD-8):** The Tiingo client is a swappable adapter behind a port; engine logic depends on interfaces, not vendor specifics. Market data flows only via the Precedent Engine over `market_daily` — never `yfinance` or other ad-hoc sources in production.
- **Data source:** Tiingo EOD is the primary feed (Stooq/backup available), refreshed by a daily job.
- **Module conventions:** domain-named packages — precedent logic lives in `precedent/` (drawdown matching, missed-growth over `market_daily`); ingestion lives in `marketdata/`. Interfaces are suffixed `Port`, adapters `Adapter`.

## UX & Interaction Patterns

- **Precedent data-block:** rendered as a calm data-block; expandable to reveal the underlying matched instances; always cites source + as-of date. If no precedent qualifies, the block is replaced by the strategy-default rationale — never left empty.
- **Color independence:** market up uses green ▲, market down uses sky-blue ▼ — never red, and never color alone; always pair an icon/sign/label. Losses are shown in sky-blue to keep dip/scary moments calm.
- **Calmest when it matters most:** the scarier the moment (a dip, a headline), the more legible and serene the screen becomes — no urgency, no motion, no red.
- **Missed-growth meter:** a quiet, always-available figure framed strictly as information ("your idle cash has sat out ~$X of growth"), stated once, calmly.
- **Headline contextualizer:** user pastes/asks about a headline on demand; the response is drawdown-keyed precedent, never event classification.
- **Accessibility:** precedent data-blocks need real text equivalents in the DOM (not image-only) so screen readers can read the stats; respect `prefers-reduced-motion`.

## Cross-Story Dependencies

- Story 3.1 (`market_daily` ingestion) is the foundation for all other stories — matching and every calming view read from it.
- Story 3.2 (drawdown matching + Evidence Record Contract) depends on 3.1 and produces the evidence records consumed by Stories 3.3 and 3.5.
- Stories 3.3 and 3.5 both consume matched drawdown evidence; 3.4 reads `market_daily` for forgone-growth estimates.
- Downstream dependency: Epic 4's Coach Engine, recommendation validation gate, and immutable decision snapshot all depend on the Evidence Record Contract fixed shape defined here — changes to that contract ripple into Epic 4.
- The strategy-default fallback surfaced in Stories 3.3/3.5 aligns with Epic 4's default-plan behavior (the `strategy` evidence kind is shared).
