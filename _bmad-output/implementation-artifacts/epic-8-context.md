# Epic 8 Context: Order Interface Expansion

<!-- Generated from planning artifacts (Story 8.1 + the 8.2/8.3 scope briefs — Epic 8 is a lightweight ad-hoc epic not in the formal epics file). Regenerate with compile-epic-context if planning docs change. -->

## Goal

Expand Ballast's ordering interface beyond whole-share MARKET orders to the order types a beginner actually needs — priced limits and the ability to place a resting order and cancel it — while keeping the app calm, honest, and never letting the LLM coach set a price. The epic lands as a small, self-contained backend-to-UI progression sequenced *before* the Story 7.7 real-money gate. It exists to deliver one core capability: "buy near the low and wait" (a resting limit the human executes, the AI only narrates), plus the safety scaffolding to expose these footguns to a beginner without alarming them. It also closes the Epic 6 partial-fill terminality open action item.

## Stories

- Story 8.1 (A, done): Marketable limit orders + full order-model enums (forward-compat)
- Story 8.2 (B1, current): Resting-order lifecycle + GTC + cancel
- Story 8.3 (C): Order-entry UI + beginner footgun warnings + "AI suggests & populates the order" backend

## Requirements & Constraints

- **The LLM coach proposes only MARKET orders — never a limit/stop price.** All order-type / limit_price / stop_price / session / duration values are human-entered overrides on the `/approve` path. This is enforced structurally by keeping the recommendation output schema market-only. In 8.3 the "AI suggest & populate" button softens this to *populate-not-execute*: the backend computes the price deterministically, the LLM only narrates the reasoning — the human still executes.
- **Backward compatible.** Existing whole-share MARKET flow stays byte-for-byte unchanged: a MARKET intent serializes to exactly `{symbol, side, amount}`, and the decision-record schema version does not bump. New order fields are additive and omit-when-default.
- **Money is always `Decimal` / fixed-point strings, never float.** Limit prices reach the broker as fixed-point strings.
- **`is_index_core(symbol)` restriction applies to every order type**, and `amount` must be finite and `> 0`.
- **Calm, honest, never-red voice (NFR8).** Refusals are calm HTTP 422 envelopes with claim release, never a raw 500 and never a phantom fill. A beginner footgun is a dismissable *warning*, not a scary error — the user can proceed with informed consent.
- **Zero-money seam proof.** The resting-order lifecycle (place → pending → cancel → never fills → costs nothing) must run deterministically against the fake adapter and stand as the documented live-broker rehearsal before any real money.
- **B2 features stay rejected.** STOP / STOP_LIMIT and extended sessions (AM/PM) remain rejected by the validation gate across the whole epic (deferred/likely cut as beginner footguns). Only marketable limit (8.1), then resting limit + GTC (8.2) are enabled.

## Technical Decisions

- **Single execution path (respect the responsibility split):** propose (`/recommend`) → approve (`/approve`) → `coach.execution.execute_approved_order` → `BrokerPort.place_order` → reconcile → cosign. `execute_approved_order` is the *sole* caller of `place_order` (AD-7); `decision_record.py` is the *sole* writer of snapshots (AD-6).
- **Where each responsibility lives:** order value objects + enums in `coach/recommendation.py` (frozen dataclass, `OrderType`/`Session`/`Duration` str-enums); pre-placement no-I/O gates (`validate_order_intent`, field-requirement matrix, deferred-feature rejection) in `coach/execution.py`; sizing + the marketable/quote-dependent guard in the *adapters* (per-broker, since the quote is broker-specific); JSON snapshot serialization in `decision_record.py` (`_order_intent_json`, omit-when-default); HTTP schema + error→envelope mapping in `api/coach.py` (`OrderIntentIn`/`OrderIntentOut`).
- **The engine gate (`validate_order_intent`) is the single authoritative gate.** Deferred-feature (stop/stop_limit/am/pm/gtc) rejection lives there so its explicit "not supported in this version" message reaches the user; the pydantic boundary validator keeps only the market/limit price-shape checks (a raw `ValueError` there gets swallowed by FastAPI's generic "Request validation failed" handler).
- **No DB migration.** Order detail lives in the JSON `recommendation_snapshot` / `cosign_snapshot` columns; adding keys is not a schema change. `broker_ref` VARCHAR(64) is untouched.
- **schwab-py builders:** use the convenience builders (`equity_buy_limit`/`equity_sell_limit`); GTC is a one-line `.set_duration(GOOD_TILL_CANCEL)` override on the returned builder. Pass the price as a fixed-point **string** — `set_price` truncates a `Decimal`/float via binary-float math. There are NO `equity_*_stop` convenience builders (a hand-rolled generic `OrderBuilder` would be needed) — the main reason STOP/STOP_LIMIT is deferred to B2.
- **Resting-order lifecycle (8.2):** relax 8.1's non-marketable refusal so a non-marketable limit co-signs `pending`/working (never a phantom fill) carrying a stable `broker_ref`, then resolves through the *existing* Story 6.7 durable path (`reconcile_pending_decision` → `get_order_status_by_ref` → `record_reconciliation`) — enable it, do not rebuild it. The sub-share sizing refusal stays. The fake adapter must deterministically return the pending outcome (seed-able for a later reconcile).
- **Cancel (8.2)** is the only genuinely new port surface — apply AD-6/AD-7 sole-writer/sole-caller rigor: add `cancel_order(broker_ref)` to `BrokerPort` + both adapters, a new Coach-Engine cancel owner (sole caller of `broker.cancel_order`), and a `POST /api/coach/decisions/{id}/cancel` endpoint mirroring the `/reconcile` pattern (per-user scoped, live-session gated). Cancel is idempotent, scoped, and refuses a terminal order calmly (422, never a 500); a cancelled order maps to rejected/cancelled and is not re-placeable.
- **Partial-fill terminality (8.2, closes the Epic 6 action item):** a `partial` becomes NON-terminal for reconcile — it may advance to `filled` but the monotonic guard must still prevent regression to `pending`. Encode with a test.

## UX & Interaction Patterns

- **Order-entry form (8.3)** lives in the SPA coach card (extend the Story 4.11 `CoachConsult.jsx` surface, do not build a parallel one). Fields: order type, limit price, stop price, session, duration — bound to the finalized `OrderIntentIn` fields. Form defaults to MARKET; stop/after-hours/GTC are progressively disclosed, never the default. Money inputs decimal-safe.
- **Client-side mirror of the field-requirement matrix** catches malformed combinations before submit, but the backend gate stays authoritative (defense in depth); surface the backend's calm 422 messages inline.
- **Beginner footgun warnings (8.3)** extend the Story 4.5 self-destructive-move detector: calm, plain-English, dismissable warnings for after-hours/extended session, STOP, GTC, and non-marketable (resting) limits. Each lets the user proceed with informed consent.
- **Resting/cancel lifecycle surfaced in the Decisions view:** a resting order shows as `pending`/working with a cancel affordance calling the 8.2 `/cancel` endpoint; a cancelled order updates honestly.

## Cross-Story Dependencies

- **Strictly sequential.** 8.2 builds on 8.1's final enum shapes, adapter branch structure, and test patterns — author its full story only after 8.1 is `done` (ideally code-reviewed). 8.3 depends on the frozen A+B `OrderIntentIn`/`OrderIntentOut` contract and the actual backend error messages — do not spec its form fields until 8.2 is done.
- **8.3 depends on 8.2's `/cancel` endpoint** for the Decisions-view cancel control, and on the resting-order `pending`/working states 8.2 introduces.
- **Reuses prior epics:** Story 6.7 durable reconcile-by-ref machinery (8.2 enables, never rebuilds); Story 4.11 SPA coach card and Story 4.5 warning framework (8.3 extends both).
- **Epic-level ordering:** the whole epic is sequenced before the Story 7.7 real-money gate and is independent of the 7.x live-money seams. The 8.2 zero-money seam proof is the rehearsal that de-risks that gate.
