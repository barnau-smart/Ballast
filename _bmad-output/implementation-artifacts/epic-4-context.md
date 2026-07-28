# Epic 4 Context: The Coach — Propose, Approve, Execute, Co-sign & Replay

<!-- Generated from planning artifacts. Regenerate with compile-epic-context if planning docs change. -->

## Goal

This epic builds the product's core loop: a trustworthy Coach Engine that produces reasoned, evidence-backed recommendations, executes only user-approved trades safely, and keeps an immutable on-the-record memory of every decision. It wires together the LLM Gateway (the sole controlled path to Claude), the recommendation pipeline `retrieve → compose → validate → surface` with a structural validation gate that makes any unbacked or black-box recommendation physically un-surfaceable, propose-and-approve execution with honest reconciled order outcomes and approval-to-placement integrity, honest self-destructive-move warnings that never block, just-in-time teaching, a strategy-backed default-plan fallback so the coach is never a dead-end, and the co-signed immutable decision record plus its verbatim replay. It is what turns Ballast from a portfolio viewer into a coach the user can trust structurally rather than on faith.

## Stories

- Story 4.1: LLM Gateway
- Story 4.2: Recommendation object & validation gate
- Story 4.3: Coach pipeline & default-plan fallback
- Story 4.4: Just-in-time teaching
- Story 4.5: Self-destructive-move warnings
- Story 4.6: Propose-and-approve execution
- Story 4.7: Order outcomes & reconciliation
- Story 4.8: Approval→placement integrity
- Story 4.9: Co-signed immutable decision record
- Story 4.10: Decisions history & replay

## Requirements & Constraints

- The user initiates every decision (e.g. "should I invest $X?") and receives a concrete recommendation to approve or decline. Nothing is unprompted (pull-not-push): no alerts, badges, or FOMO nudges.
- No trade executes without explicit user approval. On approval the order is placed and execution confirmed. v1 order scope is buying/holding a small set of broad index funds/ETFs; selling only as coach-guided rebalancing; no options, shorting, or complex orders.
- Trust invariants that must be structurally enforced (not merely discouraged): every recommendation carries plain-English reasoning (no black box); every factual/precedent claim is backed by a real retrieved evidence record and never fabricated by the LLM; every recommendation explicitly states what is uncertain. A recommendation failing any of these must be impossible to surface.
- Execution reliability: order rejection, partial fills, and timeouts are handled and clearly reported; the user always sees the true reconciled state; no phantom or duplicate orders.
- Session integrity: if the Schwab session/token expires between approval and placement, do not place a stale or partial order — re-establish a live session and re-confirm intent first.
- Self-destructive-move handling: warn and explain the risk before rash actions (panic sell, over-concentration, oversized lump), but never block — the coach advises, the user decides.
- Never a dead-end: when there is no confident special call, return the strategy-backed default plan ("stick to your plan" / make the regular contribution) plus a plain reason.
- Coach voice (a reviewable acceptance criterion, not just "no jargon"): patient, warm, honest, plain-spoken; written for a self-aware beginner; explicit uncertainty phrasing; never hype, condescending, or alarmist.
- Responsiveness target: coach reviews and precedent lookups return within a few seconds to feel conversational.

## Technical Decisions

- **Module ownership (one owner per concern).** Coach Engine (`backend/coach/`) is the sole writer of decision records and orchestrates the pipeline. LLM Gateway (`backend/llm/`) is the sole caller of the Anthropic API. Precedent Engine (`backend/precedent/`, built in Epic 3) is the sole source of market statistics. Broker Port (`backend/brokers/`) is the sole path to brokerage state. No module bypasses an owner.
- **Recommendation is a validated structured object** with required fields `{action_label, order_intent?, reasoning, evidence[], uncertainties[]}`. `action_label` is the human-readable call; `order_intent` (optional) is the typed executable payload `{symbol, side, amount}` handed to the Broker Port. Produced via `retrieve → compose → validate → surface`. The validation gate rejects any object missing reasoning, missing uncertainties, or citing an evidence ID not present in the retrieved set; only passing objects may be returned. `reasoning` IS the just-in-time teaching — one field, not a separate subsystem.
- **Precedent is code-retrieved, LLM-cited-only.** The LLM receives retrieved evidence records as input and may cite only IDs it was handed; it never computes or recalls a number. Prompt assembly and the citation-validity check are owned by the Coach Engine, not the LLM Gateway. The LLM Gateway only transports a typed request/response and applies deterministic model routing: Opus 4.8 for flagged hard-reasoning cases, Sonnet 4.6 otherwise. The LLM must emit the Recommendation schema via structured output / tool-use.
- **Evidence Record Contract** (fixed shape, from Precedent Engine): `{id, kind: event-precedent|strategy, statement, stats{}, source, as_of}` with a stable ID. Two backing types: event-precedent (tactical) and strategy (always-available base). The validator and the decision snapshot both depend on this shape.
- **Single execution path:** `propose → user-approve → Coach Engine → Broker Port → reconcile → persist outcome`. No other module places orders.
- **Broker Port Contract:** normalized `OrderOutcome {status: filled|partial|rejected|timeout|pending, filled_qty, avg_price, broker_ref}` plus `get_order_status(idempotency_key)`. Every order carries a client idempotency key; retries reuse it so a timeout never double-places. Reconciliation always uses `get_order_status`, never optimistic assumptions.
- **Session lifecycle:** the ~7-day Schwab refresh-token expiry is tolerated with no data loss. Read/coach continue in degraded mode, but execution requires a live session; an order is never placed on an expired session, and an approval→placement expiry forces re-auth plus re-confirm before placing.
- **One immutable decision record powers recommend, co-sign, and replay.** On approval the blessed Recommendation (with its evidence + uncertainties snapshot) is persisted immutably, carrying `schema_version` for replay durability. Co-sign = writing that record; replay = reading it back. No feature re-derives or mutates it. `DECISION_RECORD.recommendation_snapshot` embeds the evidence/uncertainties; precedent is snapshotted at decision time, not recomputed later.
- **Conventions:** presentation/logic split is the enforcement boundary — the React SPA holds no business logic and may only render what the backend blessed. UUID primary keys; ISO-8601 UTC timestamps; money as integer minor units or `Decimal`, never binary float; JSON-over-REST with a consistent error envelope; the Recommendation schema is the canonical coach output contract; structured logs that never log secrets or raw tokens.
- **Reserved seam (do not build):** a future guru injects as a `SuggestionSource` at the pipeline's retrieve/compose stage — never at surface or execution, never bypassing the validation gate or the single execution path. Leave this boundary open.

## UX & Interaction Patterns

- **Coach card** renders a fixed, non-reorderable sequence: `action_label` → **Why** (reasoning) → **precedent data-block** → **uncertainty callout** → **co-sign zone**. Reasoning and uncertainty are never collapsed by default — the "why" is never hidden behind a click. Visual signature: coach card has a 3px red left-border.
- **Precedent data-block** — near-black green-phosphor mono panel; expandable to underlying instances; always cites source + as-of date; up in green, down in sky-blue, with ▲/▼ signs. If no precedent qualifies, it is replaced by the strategy-default rationale, never an empty state.
- **Uncertainty callout** — violet; always present on a recommendation.
- **Approve & Co-sign** — a single deliberate primary action (button-primary: red fill + glow) that both executes and persists the co-signed record; routes to a calm confirmation. Declining ("Not now") is always equally easy and never penalized. The co-sign zone is a dashed red-tinted divider.
- **Explain more** — inline teaching expander on any term or reasoning step; pulls, never interrupts.
- **Replay** — from a decision in the Decisions surface, or surfaced gently as a chip when the user opens the app during a dip; replays the original co-signed reasoning + precedent verbatim.
- **State patterns:** coach-thinking (calm, "looking at the record…", no spinner urgency); no-precedent/no-confident-call → default-plan recommendation + reason; explicit honest order outcomes for filled / partial / rejected / timeout / pending (never a phantom success); session-expired → degraded mode with a calm re-auth prompt and no order attempted; dip/loss screens stay calm with losses in sky-blue (never red) and replay one tap away.
- **Accessibility floor:** full keyboard path through the coach loop (ask → review → approve/decline) with visible focus; color-independence (never color alone for up/down/status); reasoning and uncertainty are real DOM text for screen readers; precedent data-blocks have text equivalents; `prefers-reduced-motion` respected.
- **Surfaces:** the coach loop lives on the **Coach** surface (the emotional centerpiece); co-signed history and replay live on **Decisions**.

## Cross-Story Dependencies

- The Coach Engine depends on the **Precedent Engine and Evidence Record Contract from Epic 3** as the source of all market statistics and evidence records; the validation gate (4.2) and decision snapshot (4.9) rely on that fixed evidence shape.
- Execution stories (4.6–4.8) depend on the **Broker Port + SchwabAdapter and the session/degraded-mode work from Epic 2** (Broker Port Contract, encrypted tokens, re-auth flow).
- Within the epic: 4.1 (LLM Gateway) and 4.2 (Recommendation object + validation gate) are foundational to 4.3 (pipeline), which in turn feeds 4.4 (teaching) and 4.5 (warnings). 4.6 (propose-and-approve) precedes 4.7 (outcomes/reconciliation) and 4.8 (approval→placement integrity). 4.9 (co-signed record) is written at approval time in 4.6's flow and is the data source read back by 4.10 (replay).
