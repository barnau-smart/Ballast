---
title: 'Story 11.5 — Co-sign a review finding in place (fix the review→coach whiplash)'
type: 'feature'
created: '2026-08-16'
status: 'draft'                 # OPEN QUESTIONS unresolved — NOT approved for dev
money_path: true
context:
  - '{project-root}/_bmad-output/implementation-artifacts/11-5-cosign-review-finding-in-place.md'
  - '{project-root}/_bmad-output/implementation-artifacts/spec-11-2-bond-floor-risk-capacity.md'
  - '{project-root}/_bmad-output/implementation-artifacts/10-5-cost-switch-linked-sell-buy-pair.md'
---

<intent-contract>

## Intent

**Problem:** A user runs "Review my portfolio," fills a finding's SELL, then asks the Coach about it — and the coach steers away. Confirmed in code: co-sign requires a `decision_id`, mintable ONLY via `POST /api/coach/recommend` (the coach pipeline), whose FR11 `detect_self_destructive_moves` fires `PANIC_SELL` on any `side="sell"` and whose system prompt warns about "selling into a downturn." So a fiduciary rebalance/de-speculation SELL is treated like a naked panic sell. The concentration trim + bond-floor SELL are unprotected; the cost-switch is only partly shielded by `switch_to` (Story 10.5). The product contradicts its own recommendation on the same trade → trust erosion.

**Approach (MasterB decision — Option 1):** co-sign a review finding IN PLACE via a **server-derived review-origin decision**, with NO coach detour. Reuse the existing immutable-proposal writer (`record_proposal`) + the `/approve` spine + the editable Story-8.3 order controls. The finding's own already-gate-validated narration is re-blessed and persisted as the proposal snapshot; the human adjusts (amount / MARKET→LIMIT price / TIF) and co-signs; nothing auto-places.

## ⚠️ OPEN QUESTIONS — MasterB resolves BEFORE dev (do not default)

1. **UI placement:** (a) inline co-sign controls on the review card, vs. (b) "Review & co-sign" focuses/scrolls to the EXISTING shared order controls with the finding pre-filled + its narration shown. (Recommended: b — reuse the proven controls + co-sign step; least new UI.)
2. **Endpoint shape:** (a) extend `/recommend` with `origin="review"`, vs. (b) a dedicated review-propose mint endpoint (e.g. `POST /api/coach/propose-review`). (Recommended: b — keeps the coach's FR11/narration path untouched; the mint is a distinct, auditable money-path seam.)
3. **The "ask about this" escape hatch:** (a) keep a context-aware version (deferred Option 2 — pass origin so the coach REINFORCES, not contradicts), vs. (b) simply remove the re-ask invitation for review orders in v1. (Recommended: b for v1; Option 2 is a separate story.)
4. **Test migration:** the new flow replaces fill→ask for review orders, so the Story 10.4/10.5/11.2 frontend fill→ask tests need rewriting to the co-sign-in-place flow (and the 10.5 linked-buy assertions re-expressed against the review-origin mint). Confirm scope: migrate all in this story vs. split.

## Boundaries & Constraints

**Always (hard invariants):**
- **Server-derived order, NEVER client-trusted (CARDINAL).** The mint re-derives the caller's current findings server-side (`build_review`, fail-closed scoped, AD-10) and matches by kind + symbol; the persisted `order_intent` is the finding's SERVER-computed intent. The client sends ONLY the finding identity (kind + symbol) — never an order/amount/side. A request that does not match a genuine current finding mints NOTHING → calm 404. (This is the exact Story-10.5 HIGH-bug class: a client-trusted `switch_to` bypassed the scope gate.)
- **Re-bless through the trust gate.** The finding's narration is re-validated via `validate_recommendation` (a `BlessedRecommendation` is producible only by the gate), then the server order is bound (`replace(order_intent=...)`), then `record_proposal` persists the immutable snapshot + stable idempotency key — identical to the `/recommend` cost-switch path. `switch_to` (cost only) threads through the SAME verified-server-value path.
- **Populate-don't-submit; reuse /approve UNCHANGED.** Nothing auto-places. The human co-signs via the existing `/approve` spine — approve-time cash-cover/margin checks (Epic 10), whole-share sizing, idempotency, and the immutable `decision_record` all preserved. Money is fixed-point `Decimal`→string; per-user scoped (AD-10); UI React-escaped; copy calm/no-FOMO.
- **Independent adversarial review before merge** (money-path). Backend + frontend suites green on `ballast_test`.

**Block If:**
- Any consumed upstream contract is absent/shaped differently — HALT `blocked`: `allocation.review.build_review` (+ `NarratedFinding` / the finding's `OrderIntent` + `switch_to`); `coach.decision_record.record_proposal`; `coach.validation.validate_recommendation` / `BlessedRecommendation`; `coach.recommendation.Recommendation`; the `/approve` spine + the 10.5 server-side `switch_to` verification pattern; `db.scope.Scope`.
- The chosen endpoint shape (OQ2) or UI placement (OQ1) is not yet decided — dev does not start on an unresolved money-path contract.

**Never:**
- No co-sign path for a review order that routes through the coach pipeline / FR11 / panic-sell narration (that reintroduces the whiplash). No client-supplied order/amount/side accepted by the mint. No auto-submit, no live-broker call from the mint (it's propose-only). No weakening of FR11 / the panic-sell guard for the NORMAL coach `/recommend` path — this story routes AROUND it for review orders, it does not disable it. No new order types beyond what Story 8.3 already allows at co-sign.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected | Error handling |
|----------|--------------|----------|----------------|
| Co-sign a current finding | client posts {kind, symbol} matching a live finding | 200: a proposed decision minted from the SERVER finding order; response carries decision_id + order_intent + narration; NOTHING placed | — |
| Non-matching request | {kind, symbol} not a genuine current finding (e.g. a holding under the ceiling) | calm 404; mints nothing (scope gate) | 404 |
| Client tries to inject an order | client posts an amount/side/switch_to | ignored — the mint uses only the server-derived finding order | (n/a; never read) |
| Adjust before co-sign | human edits amount / MARKET→LIMIT price / TIF | the human's Story-8.3 override rides to `/approve`; propose==approve integrity per existing rules | — |
| Approve & co-sign | human co-signs the minted decision | places via the unchanged `/approve` spine (cash-cover/margin/whole-share/idempotency); honest outcome | reconnect 409 if session dropped |
| Cost-switch finding | kind=cost | `switch_to` server-verified onto the snapshot; linked BUY queues on placement via the existing 10.5 path | — |
| Unauth | no session | 401, mints nothing | 401 |
| Isolation | user A posts a finding key | only A's scoped holdings re-derive the finding | scoped repo fail-closed |

</intent-contract>

## Code Map

- `ballast/backend/api/coach.py` — NEW review-origin mint (endpoint shape per OQ2): re-derive via `build_review`, match kind+symbol, build a `Recommendation` candidate from the finding's narration, `validate_recommendation` → `replace(order_intent=finding.order_intent)` → `record_proposal(..., switch_to=finding.switch_to)` → commit → return the existing `RecommendResponse` shape (so the frontend co-sign step consumes it unchanged). No match → 404. Imports needed: `build_review`, `Recommendation`, `validate_recommendation`. `/approve` UNCHANGED.
- `ballast/backend/allocation/review.py` — CONSUME `build_review` + the finding's `OrderIntent`/`switch_to` (no change).
- `ballast/backend/coach/decision_record.py` / `coach/validation.py` / `coach/recommendation.py` — CONSUME `record_proposal` / `validate_recommendation` + `BlessedRecommendation` / `Recommendation`.
- `ballast/frontend/src/components/CoachConsult.jsx` — the review finding action becomes "Review & co-sign" → mint (new endpoint) → set `recommendation` (decision_id + order_intent + narration) + populate the editable controls → existing Approve & Co-sign. Per OQ3, remove/de-emphasize the re-ask for review orders. `onApprove`/`buildOrderIntent`/controls UNCHANGED.
- `ballast/backend/tests/test_allocation_review.py` (or a coach test) — NEW: mint-from-server-derived-finding, 404 scope-gate (no client injection), requires-auth, cost-switch threads switch_to. `ballast/frontend/src/test/portfolio-review.test.jsx` — MIGRATE the fill→ask tests (OQ4) to the co-sign-in-place flow.

## Tasks & Acceptance

**Execution (unchecked — pending OQ resolution + approval):**
- [ ] Resolve OQ1–OQ4 with MasterB; record decisions here.
- [ ] Backend mint endpoint (per OQ2) — server-derive + match + re-bless + record_proposal + switch_to; 404 on no match; commit; RecommendResponse shape.
- [ ] Frontend "Review & co-sign" (per OQ1) — mint → populate editable controls + narration → co-sign; remove/de-emphasize re-ask (per OQ3).
- [ ] Backend tests (mint / 404 scope-gate / auth / cost-switch) + frontend test migration (OQ4).
- [ ] Independent adversarial review before merge.

**Acceptance Criteria:**
- Given a current review finding, when the user co-signs it in place, then a proposed decision is minted from the SERVER-derived finding order (never a client value), the human can still adjust it, and NOTHING places until an explicit Approve & Co-sign — with no coach-pipeline detour.
- Given a client posts a {kind, symbol} that is not a genuine current finding (or injects an order), when the mint runs, then it mints nothing (calm 404) and no `decision_record` is written — the Story-10.5 scope-gate class cannot recur.
- Given a co-signed review finding, when it places, then it goes through the unchanged `/approve` spine (cash-cover/margin/whole-share/idempotency/immutable record) and a cost-switch queues its linked BUY via the existing 10.5 path.
- Given user B's data, when user A mints from a finding key, then only A's scoped holdings re-derive it; `/recommend` + FR11 behavior for the normal coach path is unchanged; full backend `pytest` + frontend `vitest` pass.

## Design Notes

**Why route around, not weaken, FR11.** The coach's panic-sell guard is correct for the normal "should I sell?" path; this story does not touch it. Review findings are fiduciary rebalances already computed + gate-validated, so they get a dedicated mint that reuses the trust gate + proposal writer + approve spine — coherent voice, no whiplash, same safety.

**The 10.5 lesson is the load-bearing constraint.** A prior iteration let a client-trusted `switch_to` bypass the scope gate (HIGH bug, caught in review). Here the entire order is server-derived from a re-run `build_review`; the client contributes only an identity to match. That is what makes the new seam safe.

## Verification

**Commands:**
- Backend: `cd ballast/backend && DATABASE_URL=postgresql://ballast:ballast@localhost:5432/ballast_test BALLAST_ALLOW_DIRTY_BROKERAGE_DB=1 uv run pytest -q`
- Frontend: `cd ballast/frontend && npm test`

## Auto Run Result

_(pending OQ resolution → MasterB approval → dev → independent review)_
