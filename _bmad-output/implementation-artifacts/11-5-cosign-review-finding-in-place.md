# Story 11.5: Co-sign a review finding in place (fix the review→coach whiplash)

Status: ready-for-spec

<!-- Epic 11 (Fiduciary-Grade Portfolio Review), story 5. MONEY-PATH — mints a co-signable
     SELL decision. HARD GATE (docs/dev-loop-policy.md): NOT ready-for-dev. Needs an approved
     bmad-spec + MasterB sign-off before dev, and independent adversarial review before merge.
     Do NOT let the loop pick this up (sprint-status stays out of ready-for-dev). -->

## Story

As a beginner who just got a portfolio-review recommendation,
I want to review and co-sign that exact order in place — adjusting it if I wish — without the coach second-guessing it,
so that the app acts on its own advice instead of contradicting itself and eroding my trust.

## Context — the advisory whiplash (confirmed in code)

A user runs **"Review my portfolio"**, clicks **"Fill in this order"** on a finding (populates the shared order controls with the SELL), then asks the Coach about it — and the coach **steers away**. Root cause, verified:

- Co-sign requires a `decision_id`, and the ONLY way to mint one today is `POST /api/coach/recommend` — the **coach pipeline** (`coach/pipeline.py`).
- That pipeline's FR11 `detect_self_destructive_moves` fires **`PANIC_SELL` on any `side="sell"`**, and `_SYSTEM` instructs it to warn about "selling into a downturn." So it treats a **fiduciary rebalance / de-speculation SELL like a naked panic sell**.
- The **concentration trim** and the **bond-floor SELL** carry no protecting signal; the **cost-switch** is only partly shielded by `switch_to` (Story 10.5).

Net: to act on a review finding, the user is forced through an engine tuned to *discourage* the exact action the review just recommended → **the product contradicts itself on the same trade.** In a fiduciary tool, coherence beats conversational flexibility.

**Fix (MasterB decision 2026-08-16 — "Option 1, co-sign in place"):** let the user act on a review finding WITHOUT routing through the coach. A **"Review & co-sign"** affordance on the finding mints a **review-origin** co-signable decision (not via `/recommend`), populates the SAME editable order controls (adjust amount; switch MARKET→LIMIT to set a price + TIF via the Story 8.3 order model), shows the finding's OWN fiduciary narration as the rationale, and the human co-signs via the existing `/approve` spine. **Adjustable ✅, never auto-placed ✅, no contradicting second engine ✅.**

## ⚠️ Open decisions for the spec (resolve BEFORE dev)

1. **UI placement:** inline co-sign on the review card, vs. "Review & co-sign" scrolls to / focuses the existing shared order controls (already editable) with the finding's order pre-filled + its narration shown. (Recommended: reuse the existing controls + co-sign step; minimal new UI.)
2. **Server contract for the review-origin decision:** extend `/recommend` with `origin="review"` (+ finding kind/symbol) that re-derives the finding and skips the coach narration/FR11 path, vs. a dedicated review-approve mint endpoint. Either way, the decision's `order_intent` is **server-derived**, never client-trusted.
3. **The "ask about this" escape hatch:** keep a context-aware "ask the coach about this" (deferred Option 2 — pass origin so the coach reinforces, not contradicts), OR simply remove the re-ask invitation for review orders in v1. (Recommended: remove/□de-emphasize for v1; Option 2 is a separate story.)

## Cardinal money-path requirement

The review-origin decision mint **MUST re-derive the finding SERVER-SIDE** from the user's own cached holdings (re-run `build_review` for the scoped user; match the requested finding by kind + symbol) and **NEVER trust a client-posted order/amount/side**. This is exactly where **Story 10.5's HIGH bug lived** — a client-trusted `switch_to` bypassed the scope gate. Per-user fail-closed scoping (AD-10). The `/approve` spine is reused UNCHANGED — approve-time cash-cover/margin checks, whole-share sizing, idempotency, and the immutable `decision_record` all preserved.

## Acceptance Criteria (draft — spec finalizes)

1. **Co-sign a review finding without `/recommend`.** From a review finding, the user can reach the co-sign step with the finding's SELL order pre-filled, via a review-origin decision mint — the coach pipeline (`/recommend`, FR11, panic-sell narration) is NOT invoked. No advisory whiplash.
2. **Server-derived, never client-trusted.** The minted decision's `order_intent` is re-derived + verified server-side from the caller's own holdings (kind + symbol match against a fresh `build_review`); a client-posted symbol/amount/side/switch_to that does not match a genuine current finding is rejected (no scope-widening, no fabricated order). Per-user scoped (AD-10).
3. **Still adjustable.** The human can edit the order before co-signing — amount, and MARKET→LIMIT price + TIF via the Story 8.3 order model — exactly as the shared controls allow today.
4. **Never auto-placed.** Nothing is placed until an explicit Approve & Co-sign; populate-don't-submit preserved end-to-end. The finding's own fiduciary narration is shown as the rationale at co-sign.
5. **Reuse the /approve spine unchanged.** Approve-time cash-cover/margin checks (Epic 10), whole-share sizing, idempotency, and the immutable `decision_record` are unchanged; the placement path is identical to a normal co-sign.
6. **No contradiction path remains for review orders.** Per open-decision (3): the review order no longer routes into the contradicting coach (re-ask removed/de-emphasized for v1, or made context-aware if Option 2 is folded in).
7. **Money-safety + review.** Fixed-point money, no XSS, calm copy. Backend + frontend suites green on `ballast_test`. Independent adversarial review before merge (money-path).

## Dev Notes

### Reuse / exact touch points
- **`allocation/review.py`** — `build_review` (re-derive the caller's findings server-side) + the finding's typed `OrderIntent` (already SELL/MARKET, whole-share, dust-dropped).
- **`api/coach.py`** — the `/approve` spine (unchanged) + the **Story 10.5 server-side `switch_to` verification pattern** (`_verify_switch_to` / the recommend→decision mint) is the template for "re-derive + verify server-side, never trust client." The review-origin mint should mirror it.
- **`api/allocation.py`** — `read_review` / `ReviewFindingOut` (the finding the UI already renders + its `order`).
- **`ballast/frontend/src/components/CoachConsult.jsx`** — the review finding render + `onFillOrder` (:908) + the shared editable order controls + `onApprove` (:523) co-sign step. The change: a "Review & co-sign" path that mints a review-origin decision (so `onApprove`'s `decision_id` precondition is met without `/recommend`), not the free-text "Ask the coach" (`onAsk`, :441).

### What must be preserved
- Populate-don't-submit; AD-10 scoping; approve-time cash-cover/margin (Epic 10); whole-share sizing; idempotency; immutable `decision_record`.
- The Story 8.3 order-model editability (MARKET/LIMIT/price/TIF) at co-sign.
- Do NOT weaken FR11 / the coach's panic-sell guard for the *normal* coach path — this story routes AROUND it for review orders, it does not disable it.

### Testing standards
- Backend: `cd ballast/backend && DATABASE_URL=postgresql://ballast:ballast@localhost:5432/ballast_test BALLAST_ALLOW_DIRTY_BROKERAGE_DB=1 uv run pytest -q` (⚠️ **ballast_test DB only**).
- Frontend: `cd ballast/frontend && npm test`.
- Must include: a test that a client-posted order NOT matching a current finding is rejected (the 10.5-class scope-gate test), and that co-signing a review finding places via `/approve` without any `/recommend` call.

### References
- [Source: _bmad-output/planning-artifacts/epics.md#Epic 11]
- [Source: ballast/backend/coach/pipeline.py] — FR11 `detect_self_destructive_moves` / `PANIC_SELL` / `_SYSTEM` (the anti-sell bias this routes around).
- [Source: ballast/backend/api/coach.py] — `/approve` spine + Story 10.5 server-side `switch_to` verification (the "never trust client" template).
- [Source: _bmad-output/implementation-artifacts/10-5-cost-switch-linked-sell-buy-pair.md] — the HIGH client-trusted-scope bug this story must not repeat.
- [Source: ballast/frontend/src/components/CoachConsult.jsx] — `onFillOrder` / `onAsk` / `onApprove`.
- [Source: docs/dev-loop-policy.md] — money-path spec-approval + independent-review hard gate.

## Dev Agent Record

### Agent Model Used

### Debug Log References

### Completion Notes List

### File List

## Change Log

- 2026-08-16 — Story created via bmad-create-story (Epic 11, story 5). MONEY-PATH → ready-for-spec (NOT ready-for-dev); needs approved bmad-spec + MasterB sign-off before dev. Fixes the review→coach advisory whiplash by co-signing review findings in place via a server-derived review-origin decision (Option 1). 3 open decisions flagged for the spec.
