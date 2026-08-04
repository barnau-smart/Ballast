---
title: 'Story 8.3: Order-Entry UI + Beginner Footgun Warnings (Order Interface Expansion — Story C of 3)'
type: 'feature'
created: '2026-08-04'
baseline_revision: '0a7616b92cf90fb1d312411c7f38e91629b00d91'
final_revision: 'f5f33b3d8ec1b8e2758071a431811bb01ee0eb70'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: [oversized]
---

<intent-contract>

## Intent

**Problem:** Stories 8.1 + 8.2 built the full order backend (marketable limit, resting limit, GTC, cancel) but every new `OrderIntentIn` field is only reachable by hand-crafting the `/approve` request — there is no UI. A beginner cannot compose a limit/resting/GTC order, is never warned about the footguns those choices carry, and cannot see or cancel a resting (working) order.

**Approach:** Extend the existing Story 4.11 `CoachConsult` approve surface (do NOT build a parallel one) with a progressively-disclosed order-options override — the human's override lane while the LLM still proposes MARKET only. Add a pure client-side module that mirrors the backend field-requirement matrix and derives calm, dismissable footgun warnings (the client-side sibling of the 4.5 warning voice). Surface the resting/working lifecycle in the Decisions view with a Cancel control calling 8.2's `/cancel`. This is a **pure-frontend** story: the entire backend contract (`OrderIntentIn`, `/approve`, `/decisions/{id}/cancel`, error envelopes) is frozen from 8.1/8.2 and is not touched.

## Boundaries & Constraints

**Always:**
- The form DEFAULTS to MARKET (the blessed intent). Limit / GTC are opt-in progressive disclosure, never the default; the composed MARKET intent stays byte-identical `{symbol, side, amount}` so backward compat holds.
- Money inputs are decimal-safe: reuse the existing strict `DECIMAL_RE = /^\d+(\.\d+)?$/`; `limit_price` travels as the exact validated string, never a float/`Number`.
- The client-side matrix is a pre-submit convenience MIRROR only; the backend gate stays authoritative. Any backend calm 422/409 `detail` string is surfaced verbatim inline (reuse the existing approve-handler 422/409 rendering).
- Footgun warnings are calm, plain-English, dismissable, never-red (NFR8): a LIMIT warns it may rest (not fill now); a GTC warns it stays open for days. The user can dismiss and proceed with informed consent.
- STOP/STOP_LIMIT and AM/PM are NOT submittable — they render as a calm "not available in this version" affordance (mirroring the backend's `not supported in this version yet` refusal), never as a proceed-anyway footgun.
- The Cancel control appears ONLY for an effectively-`pending` order that carries a `broker_ref`; it calls `POST /api/coach/decisions/{id}/cancel` and reflects the response honestly (`rejected` on success; `needs_reconfirmation:true` → an honest "state unclear" note; calm 422 "can no longer be cancelled" surfaced verbatim). It never claims a clean cancel for an indeterminate outcome.
- Effective status is `reconciliation_snapshot.outcome.status ?? cosign_snapshot.outcome.status` (newest-known wins), matching backend `effective_outcome_status`.

**Block If:**
- The frozen `OrderIntentIn` field set, enum values, or the `/approve` / `/cancel` route contracts differ from what 8.1/8.2 shipped (re-verify against `api/coach.py` before building) — HALT `contract drift`.

**Never:**
- No backend change. No new endpoint, no schema/port change, no touching `coach/*`, `brokers/*`, `decision_record.py`, or `api/coach.py`. (If any backend edit seems required, the plan is wrong — HALT.)
- The LLM coach still proposes MARKET only — do NOT surface order-type fields on the `/recommend` path or in `RECOMMENDATION_OUTPUT_SCHEMA`. Order-model overrides live ONLY on the human approve surface.
- **"AI suggest & populate the order" backend is OUT of scope** — deferred to a dedicated Story 8.4. It needs a product decision on the deterministic "near the low" pricing formula that must not be invented here. (See Design Notes.)
- No STOP/STOP_LIMIT/AM/PM submission path; no partial-cancel; no new order type or broker behavior.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Default market approve | order-options untouched | `order_intent` = `{symbol, side, amount}` (byte-identical); no warnings | No error |
| Marketable/any limit | type=LIMIT, valid `limit_price` | intent carries `order_type:"limit"`, `limit_price:"<string>"`; calm "may rest" warning shown, dismissable | No error |
| Resting + GTC | type=LIMIT + duration=GTC | intent adds `duration:"gtc"`; both "may rest" + "stays open for days" warnings | No error |
| Limit missing price | type=LIMIT, blank/`0`/`5.` price | submit disabled; inline mirror msg "A limit order needs a limit price greater than zero." | Blocked pre-submit |
| Limit with stop price | type=LIMIT + stop_price present | mirror blocks: "A limit order can't carry a stop price." | Blocked pre-submit |
| Stop / after-hours | user opens type/session menu | STOP/STOP_LIMIT/AM/PM shown disabled + calm "not available in this version" | Not submittable |
| Backend refuses bypassed combo | client mirror bypassed, 422 from `/approve` | surface backend `detail` verbatim inline; no phantom success | Calm refusal |
| Working order in Decisions | effective status `pending`, has `broker_ref` | row shows "working"; Cancel control visible | No error |
| Cancel success | Cancel on working order → `rejected` | row updates to cancelled/rejected honestly; Cancel hidden | No error |
| Cancel indeterminate | `/cancel` → `needs_reconfirmation:true` | honest "state unclear — re-check" note; no clean-cancel claim | Honest surface |
| Cancel terminal/no-ref | `/cancel` → calm 422 | surface "This order can no longer be cancelled…" verbatim; row unchanged | Calm refusal |
| Session lapsed on approve/cancel | 409 `RECONNECT_MESSAGE` | reuse existing reconnect handling | Calm reconnect |

</intent-contract>

## Code Map

- `ballast/frontend/src/lib/orderOptions.js` — NEW pure module. `DEFAULT_OPTIONS`; `validateOrderMatrix(options)` mirrors `OrderIntentIn._validate_order_matrix` + engine `validate_order_intent` (returns `{ok, detail}`); `buildOrderIntent(base, options)` composes the `/approve` `order_intent` (omits defaults so MARKET stays `{symbol,side,amount}`, `limit_price` as exact string); `deriveWarnings(options)` returns `[{kind, message}]` for LIMIT-may-rest + GTC-stays-open (calm voice mirroring `MoveWarning.risk`). No React, no I/O — unit-testable.
- `ballast/frontend/src/components/CoachConsult.jsx` — extend the APPROVE section (after `CoachCard`, before the Approve button, ~L360–420). Add a collapsed "Order options" disclosure: order-type select (Market default; Limit; Stop/Stop-limit disabled w/ calm note), `limit_price` decimal input (reuse `DECIMAL_RE`, shown only for Limit), duration Day/GTC toggle (Limit only), session (Regular; AM/PM disabled w/ calm note). Compute `deriveWarnings`+`validateOrderMatrix` from live options; render dismissable warnings; disable Approve when mirror `!ok`. In `onApprove` (L159–216) build `order_intent` via `buildOrderIntent` instead of the current `{symbol,side,amount}` prefer-blessed literal.
- `ballast/frontend/src/routes/Decisions.jsx` — compute effective status; when `pending` + `broker_ref`, render a "working" label + Cancel control (`POST /api/coach/decisions/{id}/cancel` via `apiFetch`). On response, re-fetch/patch the row honestly: `rejected` → cancelled; `needs_reconfirmation` → "state unclear" note; 422 → surface `detail` verbatim; 409 → reconnect. Read-only replay otherwise unchanged.
- `ballast/frontend/src/components/*.css` (CoachCard.css or a small `OrderOptions.css`) — calm, never-red styling for the options controls + warnings, reusing theme tokens (`--ballast-color-*`, no raw colors); disabled affordance for unsupported options.
- `ballast/frontend/src/test/order-options.test.js`, `coach-consult.test.jsx` (extend), `decisions.test.jsx` (extend) — Vitest + Testing Library, `vi.stubGlobal('fetch', …)` route-by-URL pattern.

## Tasks & Acceptance

**Execution:**
- [x] `ballast/frontend/src/lib/orderOptions.js` -- NEW pure module: `validateOrderMatrix`, `buildOrderIntent` (defaults omitted, decimal-string price), `deriveWarnings` (LIMIT + GTC calm text) -- single testable home for the client-side matrix mirror + footgun warnings.
- [x] `ballast/frontend/src/components/CoachConsult.jsx` -- add the progressively-disclosed order-options override to the approve section; render dismissable warnings; gate Approve on the mirror; build `order_intent` via `buildOrderIntent`; STOP/STOP_LIMIT/AM/PM disabled with calm not-available copy -- the human override surface.
- [x] `ballast/frontend/src/routes/Decisions.jsx` -- surface effectively-`pending` orders as "working" with a Cancel control calling `/decisions/{id}/cancel`; reflect `rejected`/`needs_reconfirmation`/calm-422/409 honestly -- resting/cancel lifecycle.
- [x] `ballast/frontend/src/components/*.css` -- calm never-red styling + disabled affordance, theme tokens only -- voice compliance. (`OrderOptions.css` + `routes/Decisions.css`)
- [x] `ballast/frontend/src/test/` -- unit tests for `orderOptions.js` (every matrix row + both warnings + default-market byte-identity), extend `coach-consult.test.jsx` (limit/GTC compose + warning dismiss + Approve gating + 422 passthrough) and `decisions.test.jsx` (working label, cancel success, needs_reconfirmation, calm-422); assert all existing frontend tests stay green.

**Acceptance Criteria:**
- Given the approve step with order-options untouched, when the user approves, then `order_intent` is byte-identical `{symbol, side, amount}` and no warnings show (MARKET backward compat intact).
- Given the user selects LIMIT with a valid `limit_price` (and optionally GTC), when composing, then `order_intent` carries `order_type:"limit"` + `limit_price` as the exact decimal string (+ `duration:"gtc"` if chosen), a calm dismissable "may rest" (and "stays open for days") warning is shown, and the user can dismiss and proceed.
- Given an invalid combination (limit without a positive price, a stop price on a limit), when composing, then the client mirror blocks submit with the matching backend message; and if the mirror is bypassed, the backend 422 `detail` is surfaced verbatim (defense in depth).
- Given the user opens the type/session menus, when they view STOP/STOP_LIMIT/AM/PM, then those are disabled with a calm "not available in this version" note and cannot be submitted.
- Given a decision whose effective status is `pending` with a `broker_ref`, when the Decisions view renders it, then it shows as "working" with a Cancel control; on cancel it updates honestly (`rejected` → cancelled; `needs_reconfirmation` → "state unclear"; calm 422 → verbatim, row unchanged).
- Given the full frontend suite, when run, then all existing tests stay green and no backend file is modified.

## Spec Change Log

_No bad_spec loopbacks. The single review pass resolved every finding as an auto-fix patch, a defer, or a verified reject; the intent-contract and task shape held._

## Review Triage Log

### 2026-08-04 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 13: (high 0, medium 4, low 9)
- defer: 2: (high 0, medium 0, low 2)
- reject: 9: (high 0, medium 0, low 9)
- addressed_findings:
  - `[medium]` `[patch]` P1 — a confirmed cancel left the Decisions LIST row showing a stale `pending` badge (list fetched once on mount) while the detail said "cancelled" — a self-contradiction on a beginner-money app. Now patches the matching list entry's `outcome_status` to `rejected` on a confirmed cancel.
  - `[medium]` `[patch]` P2 — a cancel that LOST the race (backend 200 `needs_reconfirmation:true` with `status:filled`/`partial`) collapsed to a generic "state unclear" note, discarding the backend's known-true fill. Now surfaces an honest "this filled before the cancel took effect" note (distinct `decisions-cancel-filled`) and patches the list row to the true status.
  - `[medium]` `[patch]` P3 — dismissed footgun warnings persisted across order-type toggles, so LIMIT→MARKET→LIMIT left the resting/GTC warning permanently suppressed (undercutting informed consent). Now clears `dismissed` when leaving LIMIT so re-entry re-warns.
  - `[medium]` `[patch]` P4 — the cancel handler guarded only on async `cancelPhase`, so a rapid double-click could fire two POSTs. Added a synchronous `cancellingRef` guard mirroring the established `placingRef` pattern (backend cancel is idempotent, but the sync guard matches convention).
  - `[low]` `[patch]` P5 — `effectiveStatus` and `brokerRef` were derived via two independent `??` chains (could cross recon status with cosign broker_ref). Now picks one `effectiveOutcome = reconOutcome ?? cosignOutcome` and reads both fields from it.
  - `[low]` `[patch]` P6 — a cancel response resolving after the user switched decisions could set state on the wrong decision; added a `reqId`/`selectedIdRef` guard.
  - `[low]` `[patch]` P7 — added a belt-and-suspenders `if (!isWorking) return` at the top of `onCancel`.
  - `[low]` `[patch]` P8 — order-type/session `onChange` now ignore disabled/unsupported values (`stop`/`stop_limit`/`am`/`pm`), making the disabled options truly unselectable via any path.
  - `[low]` `[patch]` P9 — associated the disabled-Approve reason with the control via `aria-describedby` → the mirror-block `id` (a11y honesty for screen-reader users).
  - `[low]` `[patch]` P10 — softened the `buildOrderIntent` docstring's "EXACT validated" over-promise (the caller validates via the Approve gate; the function trims + passes through).
  - `[low]` `[patch]` P11 — commented the defensive UI-unreachable `stop_price` mirror field (deferred STOP story).
  - `[low]` `[patch]` P12 — added a component-level test proving a float-hostile money string (`100.00`) survives the real form→state→serialize path unchanged.
  - `[low]` `[patch]` P13 — added a test proving the `reconciliation_snapshot` outcome wins over the `cosign_snapshot` (a `filled` reconcile hides the Cancel control).
  - Deferred (2): (1) the client re-derives effective-status precedence that the backend already owns — a future improvement would add `effective_outcome_status` to the detail response, but that is a backend change outside this pure-frontend story; (2) a `reloadKey` bump after a confirmed cancel could momentarily re-show the working UI if reconcile lags — narrow, does not fire in the normal cancel flow.
  - Rejected (9, all verified against the frozen 8.1/8.2 contract, no action): a cancel returning `canceled`/`cancelled` (the closed 5-member `OrderStatus` + Schwab CANCELED→REJECTED normalization guarantee `rejected`); a `partial` showing no Cancel (correct — 8.2 backend refuses partial cancel with a calm 422); distinguishing format-vs-magnitude in the limit-price message (the mirror MUST use the backend string verbatim); 401/403→reconnect (409 is the documented reconnect signal, "unclear" is a safe fallback); reconOutcome-null-status fallthrough, MARKET+GTC composer drop, unknown-order_type→limit branch, trim-vs-backend byte-identity, and stale-cancelMessage resurface (all near-unreachable or by-design).

### 2026-08-04 — Review pass (follow-up)
- intent_gap: 0
- bad_spec: 0
- patch: 4: (high 0, medium 2, low 2)
- defer: 1: (high 0, medium 1, low 0)
- reject: 10: (high 0, medium 0, low 10)
- addressed_findings:
  - `[medium]` `[patch]` FP1 — the NEW cancel handler read the calm 409/422 reason via `data?.detail ?? data?.message`, but the app-wide error envelope is `{error:{type,message}}` (`api/app.py::_error_response`), so the backend reason was dropped and the UI fell back to hardcoded copy. (User impact today was small — the 422 fallback is byte-identical to the backend string — but the 409 reconnect message diverged and the new tests validated a *fictional* `{detail}` envelope, giving false confidence.) Now reads `data?.error?.message ?? data?.detail ?? data?.message`; updated the 422 test to the real envelope and added a 409 test asserting the backend reconnect message is surfaced verbatim.
  - `[medium]` `[patch]` FP2 — the cancel-vs-fill race branch lumped `status:"partial"` with `"filled"` and showed "nothing was called off. Your position reflects the fill." A partial fill (reachable via `api/coach.py:982-991`: broker cancelled=True but read-back reports `partial`) means only some shares filled and the rest WAS called off — the full-fill copy misstates real money movement. Split into a distinct `partial-first` phase with honest copy ("part of this order filled … the rest was called off"); added tests for both the previously-untested `filled-first` and new `partial-first` branches.
  - `[low]` `[patch]` FP3 — the GTC footgun warning re-armed only on an order-type change (leaving LIMIT), so dismiss-GTC → Day → GTC while staying on LIMIT left it permanently suppressed, undercutting informed consent on the duration-toggle path. The duration `onChange` now clears the `gtc` dismissed kind so re-selecting GTC re-warns; added a re-arm test.
  - `[low]` `[patch]` FP4 — the client-side extended-hours mirror string used a curly apostrophe (`aren’t`, U+2019) while the backend (`coach/execution.py:186`) uses a straight `aren't`, falsifying the module's documented "EXACT backend message strings" contract (UI-unreachable, but a latent byte-equality divergence). Replaced with the straight apostrophe to match the backend verbatim.
  - Deferred (1): the pre-existing, app-wide `readDetail` in `CoachConsult.jsx` (unchanged since Story 4.11, so NOT caused by this story) reads the same `data.detail`/`data.message` shape the backend never emits, so the APPROVE path's verbatim 409/422 surfacing silently falls back too — logged for a focused envelope-unification pass.
  - Rejected (10, verified, no action): list rows render raw `outcome_status` for ALL decisions (pre-existing style; a cancel patch conforming to it is correct, not a new self-contradiction); "may rest" warning shown while price invalid (both surfaces honest); multiple `role="status"` live regions (minor a11y); the `if (!isWorking) return` guard being effectively tautological (harmless); GTC "stays open for days" copy (directionally true + sound advice); a late cancel landing on a re-opened SAME decision (same order id → correct outcome); a genuinely-pending order without a `broker_ref` showing no Cancel (by-design per the spec invariant — nothing to cancel); `patchListStatus` no-op for rows absent from the non-paginated list (unreachable today); build/validate shape divergence on `session`/`stop_price` (UI-unreachable defensive fields); and the client-side effective-status precedence re-derivation (already logged as a prior-pass defer — not re-added).

## Design Notes

**Warnings are a client-side mirror, not a backend extension (deliberate, evidence-based).** The 4.5 detector (`coach/pipeline.py::detect_self_destructive_moves`) is backend-only, fires at `/recommend` time on a `CoachDecision` with no order-type fields, and embeds text into the frozen `reasoning` snapshot. The 8.3 footguns are pure functions of the human's *approve-time form choices*, needed live before submit for informed consent — they structurally cannot ride the frozen recommend-time path. So `deriveWarnings` reuses the 4.5 *voice and shape* (a `{kind, message}` sibling of `MoveWarning{kind, risk}`) client-side. The backend 4.5 detector remains the authority for propose-path behavioral warnings (panic-sell/concentration/lump); this is its human-override-lane sibling, rendered inline in the SAME CoachConsult card (not a parallel surface — honoring the Epic 4 retro concern).

**Only LIMIT and GTC are informed-consent footguns.** The brief's draft AC-3 also listed STOP and after-hours, but the epic invariant keeps STOP/STOP_LIMIT/AM/PM *rejected* by the gate across the whole epic — a beginner cannot "proceed" with them. Presenting them as proceed-anyway warnings would be dishonest. They are therefore shown disabled with the backend's calm not-supported copy. "Non-marketable → resting" cannot be determined client-side (marketability needs a live broker quote the SPA does not hold), so the honest, always-true LIMIT warning is "may not fill right away — it rests until your price is reached, or you cancel it."

**"AI suggest & populate" is deferred to Story 8.4 (product decision required).** The epic-context title lists it under 8.3, but the 8.3 brief's locked scope omits it and it requires an unspecified deterministic "buy near the low" pricing formula (what window? what discount?) plus an LLM-narration path — an independently shippable capability and a product/financial decision that must not be invented in a UI story. Recommendation: ship this UI story now; scope 8.4 for AI-suggest-and-populate with MasterB's pricing-heuristic decision. Logged for the orchestrator to raise.

**Order-model overrides live on the APPROVE step, not `/recommend`.** The recommendation stays MARKET-only; the options disclosure sits between `CoachCard` and the Approve button so the human deliberately upgrades the blessed MARKET intent to a limit/resting/GTC order before co-signing.

## Verification

**Commands:**
- `cd ballast/frontend && npm test` -- expected: full Vitest suite green, incl. new `order-options.test.js` and the extended coach-consult / decisions tests; no existing test regressed.
- `cd ballast/frontend && npm run build` -- expected: production build succeeds (no unused-import / syntax breakage).
- `cd ballast/backend && git status --porcelain ballast/backend` -- expected: EMPTY (proves zero backend change).

**Manual checks:**
- In `orderOptions.js`, confirm a default (MARKET) compose returns exactly `{symbol, side, amount}` with no extra keys, and `limit_price` is the exact string (not `Number`-coerced).

## Auto Run Result

Status: done (follow-up review pass)

**Summary:** A follow-up adversarial + edge-case review of the shipped Story 8.3 pure-frontend order-entry UI. Two hunters (Blind Hunter, Edge Case Hunter) ran in parallel over the baseline→HEAD diff (~1.6k lines, 8 frontend files). Findings were verified against the frozen 8.1/8.2 backend contract before triage. Four patches were applied, one issue deferred (pre-existing, out of scope), and ten rejected as verified non-issues.

**Files changed (this pass):**
- `ballast/frontend/src/routes/Decisions.jsx` — cancel error read now parses the real `{error:{message}}` envelope (FP1); the cancel-vs-fill race splits `partial` from `filled` with honest copy + a new `partial-first` note (FP2).
- `ballast/frontend/src/components/CoachConsult.jsx` — GTC footgun warning re-arms on any duration toggle, not only on leaving LIMIT (FP3).
- `ballast/frontend/src/lib/orderOptions.js` — extended-hours mirror string curly→straight apostrophe to match the backend byte-for-byte (FP4).
- `ballast/frontend/src/test/decisions.test.jsx` — 422 test moved to the real envelope; added 409-verbatim, `filled-first`, and `partial-first` tests.
- `ballast/frontend/src/test/coach-consult.test.jsx` — added the GTC re-arm test.
- `_bmad-output/implementation-artifacts/deferred-work.md` — 1 new defer entry (pre-existing approve-path `readDetail` envelope mismatch).

**Review findings breakdown:** patch 4 (medium 2, low 2); defer 1 (medium 1); reject 10 (low 10); intent_gap 0; bad_spec 0.

**Verification:**
- `cd ballast/frontend && npm test` → 14 files, **131 tests passed** (incl. 5 new/updated).
- `cd ballast/frontend && npm run build` → production build succeeded.
- `git status --porcelain ballast/backend` → **empty** (zero backend change; pure-frontend invariant holds).

**Residual risks:** FP2 introduces a new user-facing `partial-first` copy branch (covered by a new test but not seen by an independent reviewer); it fires only on a rare cancel-vs-partial-fill race. The pre-existing approve-path error-envelope mismatch (deferred) means the APPROVE surface still doesn't surface backend 409/422 reasons verbatim until the envelope-unification pass lands.

