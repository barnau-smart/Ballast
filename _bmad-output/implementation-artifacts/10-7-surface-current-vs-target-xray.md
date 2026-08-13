# Story 10.7: Surface the current-vs-target x-ray in the deploy card

Status: review
baseline_commit: 6df402f

<!-- HARD GATE (docs/dev-loop-policy.md): scope APPROVED by MasterB 2026-08-13 (deferred
     UI-completeness item from the Epic 10 Group-C review). Display-only over
     engine-sourced numbers; no order/exec change. -->

## Story

As a beginner reading a deploy recommendation,
I want to see my current mix vs my chosen target (and my unclassified holdings) alongside the recommended buy,
so that I understand WHY the coach suggests it — not just the move.

## Context

The deploy engine already computes the full x-ray (`Plan.current` per-class weight/value, `Plan.target_weights`, `Plan.unclassified_*`), and `/plan` + `/narration` serialize `current` + `unclassified` — but **`target_weights` is dropped from the wire**, and the deploy card (`CoachConsult.jsx`) renders only the recommended move (`primary_order`/`status`/`reason`), not the breakdown. The Epic 10 Group-C review flagged this as a UI-completeness gap vs spec intent ("holdings surfaced honestly"). This story surfaces the computed analysis; it is **display-only** — no engine, order, or exec change.

## Acceptance Criteria

1. **`target_weights` on the wire.** `PlanOut` (`api/allocation.py`) gains `target_weights: dict[str, str]`, serialized in `_plan_out` from `plan.target_weights` as fixed-point strings (same fraction form as `CurrentSleeveOut.weight`). Empty dict when the plan has no target (e.g. `no_target`). Covers both `GET /api/allocation/plan` and the deploy card's `/narration` source (both use `_plan_out`).
2. **X-ray rendered in the deploy card.** On a `deploy` (and, where sensible, a no-action) plan, `CoachConsult.jsx` renders a compact "where you are vs your target" strip: per asset class, current % vs target %, plus the `unclassified` sleeve (value + symbols) shown as "not counted toward your target mix". Read from `data.plan.current` / `data.plan.target_weights` / `data.plan.unclassified`.
3. **Honest + calm.** Weights render as percents (fraction ×100) via the existing fixed-point strings — no client float math beyond ×100 for display, no fabricated numbers, calm copy (no FOMO words). Unclassified is framed honestly (surfaced, excluded from the math — never presented as a problem to fix here).
4. **No collateral change.** No change to the engine, `/approve`, order controls, or the money-safety contracts (populate-don't-submit, scoping, no-XSS, fixed-point). The `/plan` wire stays fixed-point strings; `target_weights` is additive (no existing field changes).
5. **Degrade-safe render.** Missing/empty `target_weights` or `current` (a `no_target`/`no_cash` plan) renders calmly (e.g. "pick a target to see your mix vs. it") — never a crash or a blank/NaN.

## Tasks / Subtasks

- [x] Task 1 — Serialize `target_weights` (AC: 1, 4)
  - [x] Added `target_weights: dict[str, str]` to `PlanOut`; populated in `_plan_out` from `plan.target_weights` (fixed-point strings). Covers `/plan` + `/narration` (both use `_plan_out`).
- [x] Task 2 — Render the x-ray strip (AC: 2, 3, 5)
  - [x] `AllocationXray` component in `CoachConsult.jsx` renders current-vs-target per class (`xrayPct` = fraction ×100, non-finite → "—") + the unclassified sleeve, held in `deployPlan` state, shown in the deployed + no-action blocks. Returns null when there's no target and no unclassified (calm degrade). Calm token-based CSS.
- [x] Task 3 — Tests (AC: all)
  - [x] Backend +2 (`target_weights` serialized for deploy; empty for `no_target`). Frontend +1 (the x-ray strip renders current-vs-target + unclassified from a stubbed deploy plan; calm-copy). Backend 838 + frontend 197 green.

## Dev Notes

### Reuse / exact touch points
- **`api/allocation.py`:** `PlanOut` (:74), `_plan_out` (:170) — add `target_weights`. `CurrentSleeveOut.weight` (:71) is the fraction form via `format_money(vals["weight"])`; match it.
- **`allocation/engine.py`:** `Plan.target_weights` is the per-class fraction dict (already computed; the Group-B review noted it was a wire-dead field — this story consumes it).
- **`CoachConsult.jsx`:** the deploy render block (`deploy === 'deployed'` / no-action) — add the x-ray strip beside the populated controls / narration. Reuse `formatMixWeights`-style non-finite guarding (`Settings.jsx`) if handy.
- **`money.py:format_money`** for the wire strings.

### What must be preserved
- Money-safety contracts verified clean in Group C — display-only change, no order/exec path touched.
- `/plan` wire additivity — do not alter existing fields; `target_weights` is new.
- Calm-copy tone bar (mirrors the review test FORBIDDEN list).

### Testing standards
- Backend: `cd ballast/backend && DATABASE_URL=postgresql://ballast:ballast@localhost:5432/ballast_test BALLAST_ALLOW_DIRTY_BROKERAGE_DB=1 uv run pytest -q` (⚠️ ballast_test DB only).
- Frontend: `cd ballast/frontend && npm test`.

### References
- [Source: _bmad-output/implementation-artifacts/spec-10-4-analysis-buckets-concentration-cost.md#Review Findings — Independent Group C] — the deferred "unclassified/current not surfaced in the deploy UI" item this story closes.
- [Source: ballast/backend/api/allocation.py#PlanOut] · [Source: ballast/backend/allocation/engine.py#Plan]
- [Source: docs/dev-loop-policy.md] — per-story spec-approval hard gate.

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (dev-story, in-chat)

### Completion Notes List

- Backend: `PlanOut` gains `target_weights` (fixed-point fraction strings), serialized in `_plan_out` — additive, both `/plan` and the deploy card's `/narration` source now carry it. The Group-B "wire-dead `Plan.target_weights`" is now consumed.
- Frontend: `AllocationXray` renders "US stocks: 60% now · target 60%" per class + the unclassified sleeve ("Not counted toward your target mix: $500.00 (TSLA)"), from a new `deployPlan` state set on the deployed/no-action branches and cleared at fetch start. Percents are the only client math (fraction ×100); non-finite → "—"; no chosen target → renders nothing (calm degrade).
- Display-only: no engine/`/approve`/order-control change; money-safety contracts (populate-don't-submit, scoping, no-XSS — React-escaped text, fixed-point strings) untouched. `/plan` wire is additive.

### File List

- `ballast/backend/api/allocation.py` — `PlanOut.target_weights` + `_plan_out` serialization.
- `ballast/backend/tests/test_allocation_engine.py` — +2 serialization tests.
- `ballast/frontend/src/components/CoachConsult.jsx` — `AllocationXray` + `xrayPct` + `deployPlan` state + render in deployed/no-action blocks.
- `ballast/frontend/src/components/CoachConsult.css` — calm token-based x-ray styles.
- `ballast/frontend/src/test/deploy-cash.test.jsx` — +1 x-ray render test.

## Change Log

- 2026-08-13 — Story 10.7 implemented: surface the current-vs-target x-ray + unclassified sleeve in the deploy card. Added `target_weights` to `PlanOut`; `AllocationXray` renders the breakdown. Display-only; closes the Group-C deferred UI-completeness item. Backend 838 + frontend 197 green.
