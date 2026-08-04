# Story 8.3 (BRIEF): Order-Entry UI + Beginner Footgun Warnings (Order Interface Expansion — Story C of 3)

Status: backlog — planning brief only

> **This is a scoping brief, NOT a ready-for-dev story.** Generate the ultimate-context story via `/bmad-create-story 8-3` **after Stories 8.1 and 8.2 are `done`**, so it reflects the final API shapes (order_type/limit_price/stop_price/session/duration on `OrderIntentIn`/`OrderIntentOut`) and the actual error messages the backend surfaces. This brief locks scope + decisions.

## Feature / Goal

Stories A + B built the full order backend (limit, stop, stop-limit, sessions, GTC, cancel) but every new field is human-entered on the raw `/approve` API with **no UI**. Story C adds the **order-entry surface in the SPA** so a user can actually compose these orders — and, because Ballast is a *calming coach for beginners*, wraps the beginner footguns (after-hours, stop, GTC, non-marketable limits) in explicit, plain-English **safety warnings**, extending the Story 4.5 self-destructive-move detector.

## Locked design decisions (honor; from [[order-interface-expansion-plan]])

- The UI is the human's override surface; the **LLM coach still proposes only MARKET orders**. The form defaults to MARKET and the user opts into complexity deliberately.
- Keep the calm/honest/never-red voice (NFR8). A footgun gets a *warning*, not a scary error; the user can still proceed with eyes open.
- Beginner-first: the default path (market, or a marketable limit) is one tap; stop/after-hours/GTC are progressively disclosed, never the default.

## Scope

- **Order-entry form** in the SPA coach card / `CoachConsult.jsx` (confirm the live surface — Story 4.11 built the SPA coach card; this extends it). Fields: order type (market/limit/stop/stop-limit), limit price, stop price, session (regular/AM/PM), duration (day/GTC) — wired to the `OrderIntentIn` fields A + B exposed. Money inputs are decimal-safe (never float).
- **Client-side mirror of the field-requirement matrix** so a malformed combination is caught before submit — but the backend gate stays authoritative (A's `validate_order_intent` + the `OrderIntentIn` validator). Surface the backend's calm 422 messages (non-marketable→now-resting in B, sub-share refusal, etc.) inline.
- **Beginner footgun warnings — extend Story 4.5** (`coach/*` self-destructive-move detector; find where 4.5 lives). New warning triggers: after-hours/extended session ("your order may fill at a surprising price when markets are thin"), STOP/STOP_LIMIT ("a stop can sell into a dip"), GTC ("this order stays open for days — you may forget it"), and a non-marketable limit ("this won't fill now; it rests until the price is reached — or cancel it"). Each is calm, plain-English, and dismissable.
- **Surface the working/cancel lifecycle from B**: show a resting order as `pending`/working in the Decisions view with a cancel affordance (the B `/cancel` endpoint).

## Out of scope (Story C)

- Any new order *type* or backend behavior (all in A + B).
- The LLM proposing non-market orders.
- New broker/port surface.

## Draft acceptance criteria

1. A user can compose + submit each supported order (market, marketable limit, resting limit, stop, stop-limit; regular/AM/PM; day/GTC) from the SPA; the request carries the correct `OrderIntentIn` fields and the outcome renders honestly.
2. An invalid field combination is caught client-side with a calm inline message mirroring the backend matrix; the backend gate still rejects it if bypassed (defense in depth).
3. Each footgun (after-hours, stop, GTC, non-marketable limit) shows an explicit, dismissable, plain-English warning extending the 4.5 detector — the user can proceed with informed consent; the voice stays calm/never-red.
4. A resting order shows as working in Decisions with a cancel control that calls B's `/cancel`; a cancelled order updates honestly.
5. Existing coach-card / Decisions UI behavior + all backend tests unchanged.

## Risks / notes for the create-story pass

- Depends on the final A+B API contract — do not spec the form fields until B's `OrderIntentIn`/`OrderIntentOut` are frozen.
- Reuse the Story 4.11 coach-card component + Story 4.5 warning framework rather than building parallel surfaces (avoid the "done epic missed its centerpiece" pattern from the Epic 4 retro).
- Frontend test coverage: component/interaction tests for the form + warnings; confirm the SPA's existing test setup.

## References

- [Source: sprint-status.yaml action_items epic 4] — Story 4.11 built the SPA coach card (the surface to extend); 4.5 = self-destructive-move warnings (the detector to extend).
- [Source: ballast/backend/api/coach.py] — `OrderIntentIn`/`OrderIntentOut` (the contract the form binds to, finalized in A + B).
- [Memory: [[order-interface-expansion-plan]], [[fake-mode-vs-real-coaching]]] — beginner-coach framing; fake demo mode for exercising the UI without real money.
