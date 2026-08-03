# Story 4.11: Live coach card — propose → approve/decline in the SPA

Status: done

<!-- Completes Epic 4's unbuilt centerpiece. Frontend-only; backend propose/approve shipped in 4.6. -->

## Story

As **a user who just asked the coach a money question**,
I want **one calm card that shows the recommendation, the why, the precedent, and what's uncertain — then lets me Approve & Co-sign or say "not now" in place**,
so that **the product's emotional centerpiece (the shared, on-the-record decision) actually exists in the app, not just in the backend and the replay screen.**

## Context

Epic 4 delivered the backend propose/approve endpoints (Story 4.6) and the Decisions **replay** UI (Story 4.10), but the **live coach card** — the ask → recommendation → **Approve & Co-sign** / decline surface — was never built in the SPA. It is the UX "emotional centerpiece" (EXPERIENCE.md) and the climax of user journey UJ-1, fully specified in `mockups/coach-card.html`. `routes/Coach.jsx` today renders only the two read-only reference views. This story completes the planned surface; it is **frontend-only** (the backend contract is stable and unchanged).

## Acceptance Criteria

1. **Ask → recommendation.** Given a signed-in user, when they submit the coach form (free-text question; optional symbol/amount/side), then `POST /api/coach/recommend` runs and the result renders in the designed fixed sequence: echoed question → `> {action_label}` → **// why** (reasoning) → **// what the record shows** (each `evidence[]` via `PrecedentEvidence`) → **// what I can't know** (`uncertainties[]` via `UncertaintyCallout`). Reasoning and uncertainty are real DOM text, never collapsed by default.
2. **Concrete order → co-sign zone.** Given the user's inputs form a complete order (symbol + amount + side present), when the recommendation renders, then a dashed-divider co-sign zone shows the framing ("✎ I'll put my name on this with you."), a primary **Approve & Co-sign** action, and an equally-prominent **not now** decline.
3. **Approve executes the user's stated order.** When the user approves, then `POST /api/coach/approve` is called exactly once with `decision_id` (from `/recommend`) and `order_intent` = `recommendation.order_intent` when present, else `{symbol, side, amount}` from the form; `amount` is sent as a decimal string. The reconciled `OrderOutcome` renders calmly: any of `filled/partial/rejected/timeout/pending` shown honestly (never coerced to an error, never a phantom success).
4. **Decline is free.** When the user picks "not now", then no network call is made, nothing is penalized/warned, and the card returns to a calm ready state.
5. **Question-only → guidance only.** Given no concrete order (e.g., a question with no amount/side), when the recommendation renders, then NO approve control appears — the honest "the coach isn't recommending a trade" state.
6. **Calm degraded states.** Approve on a non-live session → a calm reconnect message with a link to Onboarding (from a 409), order stays retryable; a 422 refusal (e.g., amount buys < 1 whole share) shows the backend's calm `detail` verbatim; a `/recommend` transport failure → a calm fallback ("we couldn't reach the coach just now… your plan hasn't changed"); a 401 → a calm prompt to sign in. Never a raw 500/stack.
7. **Design + a11y fidelity.** Built to `mockups/coach-card.html` using the existing `ballast-terminal` tokens (no hardcoded colors/sizes — stylelint passes). Market up green ▲ / down sky-blue ▼ (never red); brand-red used ONLY for the primary co-sign action (matching the theme's established primary-action treatment). `prefers-reduced-motion` respected; full keyboard path (ask → review → approve/decline) with visible focus; a calm "looking at the record…" thinking state (no spinner urgency).
8. **No regressions.** `npm test` and `npm run lint:css` green; existing Coach reference views (RecoveryPrecedent, HeadlineContextualizer) still render unchanged below the new card.

## Tasks / Subtasks

- [ ] **Task 1 — CoachCard presentation** (AC: 1, 7)
  - [ ] `ballast/frontend/src/components/CoachCard.jsx` (+`.css`) — pure presentation of a `RecommendResponse` in the fixed sequence, reusing `PrecedentEvidence` (per `evidence[]`) and `UncertaintyCallout`. Mirror `DecisionReplay`'s structure/testids for the live shape. Terminal `termbar` header + echoed-question + `>` recommendation per the mockup.
- [ ] **Task 2 — CoachConsult orchestration** (AC: 1–6)
  - [ ] `ballast/frontend/src/components/CoachConsult.jsx` (+`.css`) — the "Ask the coach" form (question + optional symbol/amount/side with guided placeholder), the `/recommend` and `/approve` calls via `apiFetch`, the co-sign zone (Approve & Co-sign + not now), the outcome render, and every degraded/error state. Owns state; sole caller of the two endpoints; `mounted` ref guard (mirror `HeadlineContextualizer`).
- [ ] **Task 3 — mount on route** (AC: 8)
  - [ ] `ballast/frontend/src/routes/Coach.jsx` — render `<CoachConsult />` ABOVE the existing reference views; leave them intact.
- [ ] **Task 4 — tests** (AC: 1–6, 8)
  - [ ] `ballast/frontend/src/test/coach-consult.test.jsx` — unit-test every I/O-matrix row (mock `apiFetch`): question-only hides approve; concrete order shows it; approve success renders outcome; approve sends `order_intent = recommendation.order_intent ?? form values`; 409 reconnect + Onboarding link; 422 verbatim refusal; recommend-failure fallback; decline makes no call.

### Review Findings

_Code review 2026-08-03 (Blind Hunter + Edge Case Hunter + Acceptance Auditor on commit 086b3ac). 10 patch, 0 defer, 9 dismissed as noise._

- [x] [Review][Patch] Editable order after blessing → co-sign a different order than shown, under a stale `decision_id` (HIGH) [CoachConsult.jsx: `orderIntent`/`question` derived from live form] — snapshot the order + question at ask time; invalidate the recommendation when the form is edited.
- [x] [Review][Patch] Non-fill outcomes (rejected/timeout/pending) dressed as success with the "I'll replay this" chip — phantom-success (HIGH) [CoachConsult.jsx outcome block] — only filled/partial get the on-the-record/replay framing; others render honestly.
- [x] [Review][Patch] `/approve` 401 unhandled → generic "try again" forever instead of sign-in (MED) [CoachConsult.jsx onApprove] — route 401 to the sign-in prompt like recommend does.
- [x] [Review][Patch] Indeterminate approve failure (500/parse/network) asserts "Nothing was placed" → invites a duplicate live order (MED) [CoachConsult.jsx onApprove catch] — honest "couldn't confirm — check Decisions before retrying"; keep the pre-placement 422 as "nothing placed".
- [x] [Review][Patch] 409 conflates reconnect (no live session) vs in-progress (concurrent approve) → wrong "Reconnect Schwab" link for in-progress (MED) [CoachConsult.jsx 409 branch] — show detail verbatim; link only for the session case.
- [x] [Review][Patch] `amount` accepts `1e3`/`0x10`/`5.` via `Number()` and sends the raw string (MED) [CoachConsult.jsx `hasConcreteOrder`] — validate as a clean decimal string so validated == sent.
- [x] [Review][Patch] Double-click Approve can fire two POSTs (state guard is async) (MED) [CoachConsult.jsx onApprove] — add an in-flight ref guard.
- [x] [Review][Patch] Approve allowed with a missing `decision_id` (MED) [CoachConsult.jsx] — require `decision_id` for the co-sign control.
- [x] [Review][Patch] Outcome/error region not announced to assistive tech (MED) [CoachConsult.jsx] — `role="status"`/`aria-live` on the result region; `aria-hidden` on decorative glyphs.
- [x] [Review][Patch] Test gaps: non-filled outcome honesty, approve-500 indeterminate, snapshot-invalidation-on-edit, double-click guard (MED) [coach-consult.test.jsx] — add coverage.

_Dismissed (9): CoachCard non-string field guard (backend contract + validation gate guarantee strings); unbounded `detail` length (backend-owned calm copy); React `key={id ?? i}` (matches DecisionReplay convention, needs malformed data); unmount-mid-request live placement (inherent; backend reconciles); decline-then-re-ask "already declined" state (not a defect); partial-order no-hint (fields labeled optional); reduced-motion/glow fidelity (theme intentionally dropped glow); whitespace-only question sent raw (harmless free text); replay-chip-pre-approval mockup nuance (post-fill placement is more honest)._

## Dev Notes

### Backend contract (stable — do NOT modify backend)
- `POST /api/coach/recommend` — `RecommendRequest { symbol?: str, question: str="", amount?: Decimal, side?: OrderSide }` → `RecommendResponse { decision_id, action_label, reasoning, evidence: EvidenceOut[], uncertainties: str[], order_intent?: {symbol, side, amount(str)} }`. Depends on auth scope only (works in degraded mode; no live session needed). [Source: `ballast/backend/api/coach.py`::recommend ~370]
- `POST /api/coach/approve` — `ApproveRequest { decision_id: UUID, order_intent: {symbol, side, amount} }` → `ApproveResponse { status, filled_qty(str), avg_price?(str), broker_ref? }`. Requires a **live brokerage session** → 409 `RECONNECT_MESSAGE` otherwise (calm). 422 on out-of-scope / `OrderNotPlaceableError` (e.g. amount < 1 share). Idempotent re-approve returns the recorded outcome. [Source: `ballast/backend/api/coach.py`::approve ~412]
- `side` ∈ {`buy`,`sell`} — **verify against** `ballast/backend/coach/recommendation.py`::`OrderSide` before wiring the `<select>`. `amount` crosses the wire as a decimal STRING (Pydantic parses `Decimal`); never send a JS float.
- Self-destructive-move warnings arrive **woven into `reasoning`** (no separate field) — rendering reasoning covers FR11. [Source: recommend docstring]

### Reuse / patterns (read these first)
- `ballast/frontend/src/lib/session.js`::`apiFetch` — attaches bearer + base URL; use for both calls.
- `ballast/frontend/src/components/PrecedentEvidence.jsx` — render each `evidence[]` record (pass a distinct `idPrefix` per instance to keep disclosure DOM ids unique).
- `ballast/frontend/src/components/UncertaintyCallout.jsx` — render `uncertainties[]`.
- `ballast/frontend/src/components/HeadlineContextualizer.jsx` — the reference for form + fetch + `mounted` ref + fail-quiet.
- `ballast/frontend/src/components/DecisionReplay.jsx` — the reference for the fixed coach-card sequence + outcome line format (`status · filled {qty} @ {avg_price}`).

### Design (build to the mockup)
- `_bmad-output/planning-artifacts/ux-designs/ux-ai_practice_project-2026-07-22/mockups/coach-card.html` is the visual spec. Structure: `.termbar` (`ballast:~$ coach --review …`) → echoed `.ask` → `.rec` (`>` + action_label) → `// why` prose (soft-white body font) → `// what the record shows` data-block → `// what I can't know` (violet) → **co-sign zone** (dashed top border, `--line-red`): note "✎ I'll put my name on this with you." + **APPROVE & CO-SIGN** (primary, brand-red) + **not now** (ghost) → replay chip ("↻ if it dips, I'll replay this back to you").
- **Use `ballast/frontend/src/theme/tokens.css` variables — no hardcoded values** (stylelint enforces). The mockup's `:root` hexes are the intended palette; map them to the implemented `--ballast-*` tokens. **Verify** whether a brand-red / co-sign token exists in the implemented theme; if brand-red was dropped in the shipped `ballast-terminal` theme, match the theme's existing primary-action treatment rather than inventing a hex.

### Product decision (ratified — do not re-litigate)
The order approved is the **user's stated order** (`recommendation.order_intent ?? form {symbol, side, amount}`); a null recommendation `order_intent` (the default plan) does not erase the order the user came in with. Warn-not-block (Epic 4.5): if the coach flagged a self-destructive move, that caution shows in `reasoning` above the co-sign zone — the user keeps autonomy via the explicit approval gate (FR8/FR9). No concrete order on the table ⇒ no approve control.

### Project Structure Notes
- New files live under `ballast/frontend/src/components/` + `.../test/`, matching existing naming. Presentation-only (AD-1): compute no money/market number client-side; render what the backend blessed. Frontend-only — do NOT touch any backend file, `sprint-status.yaml`, or `_bmad-output/`.

### References
- [Source: planning-artifacts/ux-designs/.../EXPERIENCE.md] — Coach = "emotional centerpiece"; fixed card sequence; "Approve & Co-sign"; "Not now always equally easy"; coach-thinking / order-outcome / degraded state patterns; a11y floor.
- [Source: planning-artifacts/ux-designs/.../DESIGN.md] — coach-card + co-sign-zone shapes; brand-red discipline.
- [Source: planning-artifacts/epics.md] — FR7/FR8 propose-and-approve; FR12/FR14 reasoning+uncertainties; UX-DR4 coach-card sequence.

## Verification

**Commands:**
- `cd ballast/frontend && npm test` — new `coach-consult` tests + existing suite green.
- `cd ballast/frontend && npm run lint:css` — no stylelint violations (all values from tokens).

**Manual check (fakes):** db + backend (`BROKER_ADAPTER=fake`, `LLM_ADAPTER=fake`) + frontend up, signed in, fake Schwab linked → ask "buy $500 VTI — should I?" → coach card renders → **Approve & Co-sign** → outcome shows `filled … @ …` → the decision appears in Decisions with a verbatim replay.

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m]

### Debug Log References

- Initial `npm test`: parse error in `CoachCard.jsx` — a JSDoc line contained `**//`, whose embedded `*/` closed the block comment early. Fixed by rewriting the comment as plain prose.
- Second `npm test`: one self-inflicted test-guard failure — a `/…|500/` regex matched the `$500` in a placeholder, not a real error dump. Tightened to `/traceback|internal server error/i`.

### Completion Notes List

- Built the live coach card to `mockups/coach-card.html`: termbar → echoed question → `>` action_label → why → precedent (shared `PrecedentEvidence`) → uncertainty (shared `UncertaintyCallout`) → dashed co-sign zone (Approve & Co-sign + "not now") → replay chip.
- Approve sends the user's stated order (`recommendation.order_intent ?? form values`) + `decision_id`; amount as a decimal string. 409 → calm reconnect + Onboarding link (retryable); 422 → backend `detail` verbatim; 401 → sign-in prompt; recommend transport failure → calm fallback. Question-only (no concrete order) → guidance only, no approve control. "not now" makes no network call and dismisses the card.
- Brand-red used ONLY on the co-sign action + the dashed `--ballast-color-line-red` divider; outcome/loss values are calm terminal text (never red). All CSS via `--ballast-*` tokens (stylelint clean).
- Verification: `npm test` → 76 passed (13 files, incl. new `coach-consult.test.jsx` covering every I/O row); `npm run lint:css` → clean. Frontend-only; no backend/`sprint-status`/`_bmad-output` code touched by the implementation.
- Code review (2026-08-03, 3 adversarial layers): all 10 patch findings fixed, 9 dismissed as noise, 0 deferred. Key fixes: snapshot the order+question at ask time and invalidate the card on edit (no co-signing a different order than shown / no stale `decision_id`); honest outcomes (only filled/partial get the replay framing; rejected/timeout/pending shown truthfully); 401→sign-in and 5xx/network→"couldn't confirm, check Decisions" (never falsely "nothing was placed"); 409 reconnect-vs-in-progress split; strict decimal-string amount validation; in-flight ref guard against double-approve; `decision_id` guard on the co-sign control; `role="status"` on result/outcome regions + `aria-hidden` on decorative glyphs. Tests grew 76→85 (`npm test` 85 passed; `npm run lint:css` clean).
- Still pending: live manual run against the fake-adapter stack (servers up) — recommended before merge/demo.

### File List

- `ballast/frontend/src/components/CoachConsult.jsx` (new) — form + recommend/approve orchestration + co-sign zone + outcome + degraded states.
- `ballast/frontend/src/components/CoachConsult.css` (new)
- `ballast/frontend/src/components/CoachCard.jsx` (new) — presentation of a live recommendation via the shared components.
- `ballast/frontend/src/components/CoachCard.css` (new)
- `ballast/frontend/src/routes/Coach.jsx` (edit) — mount `<CoachConsult />` above the reference views.
- `ballast/frontend/src/test/coach-consult.test.jsx` (new) — I/O-matrix coverage.
