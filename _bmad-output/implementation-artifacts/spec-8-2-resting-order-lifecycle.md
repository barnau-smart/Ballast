---
title: 'Story 8.2: Resting-Order Lifecycle + GTC + Cancel (Order Interface Expansion — Story B1 of 3)'
type: 'feature'
created: '2026-08-04'
baseline_revision: '42f085c17423ba181b1c51999f39d9025c00a646'
final_revision: 'ca36eb605f9ea3b717e5cde2a71e2b41f470b2f7'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: [oversized]
---

<intent-contract>

## Intent

**Problem:** Story 8.1 only allows LIMIT orders that fill immediately (a marketable guard refuses anything else), there is no way to place a resting "buy near the low and wait" order, no GTC so an order can wait across days, and no way to cancel — this is the actual capability the whole order-interface expansion exists for, and it also leaves the Epic 6 partial-fill terminality question open.

**Approach:** Relax the marketable guard so a non-marketable limit becomes a legitimate **resting** order that co-signs `pending` (never a phantom fill) and resolves later through the EXISTING Story 6.7 durable reconcile-by-ref path; accept `Duration.GTC` at the validation gate and override duration on the limit builder; add a new `cancel_order` port surface (the only new broker verb) with its sole owner in `coach/execution.py` and a `POST /decisions/{id}/cancel` endpoint mirroring `/reconcile`; and make a `partial` fill re-reconcilable up to `filled` (advance-only), closing the Epic 6 action item.

## Boundaries & Constraints

**Always:**
- A non-immediately-filled order co-signs with a `pending` outcome carrying a stable `broker_ref` — NEVER a phantom fill. It resolves only through the existing `reconcile_pending_decision` → `get_order_status_by_ref` → `record_reconciliation` path (reuse, do not rebuild).
- `execute_approved_order` stays the SOLE caller of `place_order`; the new cancel owner in `coach/execution.py` is the SOLE caller of `broker.cancel_order` (AD-7). `decision_record.py` stays the sole writer (AD-6).
- Keep the sub-share refusal (`floor(amount/limit_price) < 1` → `OrderNotPlaceableError`) in both adapters. Keep `is_index_core(symbol)` for every order.
- A cancelled order maps to `OrderStatus.REJECTED` (reuse the closed 5-member contract; Schwab already normalizes CANCELED→REJECTED) and is not re-placeable. Cancel is idempotent + per-user scoped + refuses a terminal/settled order calmly (422 envelope, NEVER a 500).
- `partial` becomes re-reconcilable but strictly forward-only: it may advance to `filled` (or a larger-qty `partial`) but must NEVER regress to `pending`/`timeout`.
- Backward compatible: 8.1 marketable-limit + all market flows and defaults (MARKET/REGULAR/DAY) are byte-for-byte unchanged; `DECISION_RECORD_SCHEMA_VERSION` stays `1` (omit/emit-when-default already emits `duration: "gtc"`).

**Block If:**
- The installed schwab-py `client.cancel_order` signature is not `(order_id, account_hash)` returning a DELETE with no body — HALT (design assumes the verified 1.5.1 shape).
- Making `partial` non-terminal cannot be done without regressing a documented invariant that isn't a simple test-expectation update — HALT.

**Never:**
- No new `OrderStatus` enum member (no `cancelled`) — cancel reuses `REJECTED`.
- Do NOT enable `Session.AM/PM` or `OrderType.STOP/STOP_LIMIT` — they stay rejected by `validate_order_intent` exactly as in 8.1.
- No UI / order-entry form / beginner warnings (Story 8.3). No "AI suggests & populates the order" backend (Story 8.3). The LLM never proposes a non-market order — `RECOMMENDATION_OUTPUT_SCHEMA` stays market-only.
- Do NOT rebuild the reconcile/idempotency/atomic-claim machinery; do NOT scheduler-wire the 7.2 reclaimer/pruner. Do NOT move code into/out of the schwab post-placement fence.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Resting buy (fake) | non-marketable BUY LIMIT, `floor(amount/limit)>=1` | placement `PENDING`, stable `broker_ref`; co-signs `pending` (no fill) | No error |
| Resting reconcile | seeded ref → `FILLED` later, `/reconcile` | truthful `filled` via `get_order_status_by_ref`→`record_reconciliation` | No error |
| Sub-share resting | non-marketable LIMIT, `floor(amount/limit)<1` | `OrderNotPlaceableError` → calm 422, claim released | Refusal (pre-placement) |
| GTC limit | LIMIT + `duration=gtc` | builder `.build()` → `duration=GOOD_TILL_CANCEL`; cosign snapshot carries `duration:"gtc"` | No error |
| Cancel resting | `pending` cosigned order, `/cancel` | broker cancels → maps `REJECTED`; persisted, not re-placeable | No error |
| Cancel terminal | already `filled`/`rejected` order, `/cancel` | calm 422 "already settled" — broker NOT called | Refuse calmly |
| Cancel idempotent | `/cancel` twice | 2nd sees terminal `rejected` → calm 422, no double broker call | Refuse calmly |
| Cancel no order id | `broker_ref is None`, `/cancel` | calm 422 (cannot cancel unknown order); broker NOT called | Refuse calmly |
| Partial advance | effective `partial`, re-read `FILLED` | `record_reconciliation` advances to `filled` | No error |
| Partial regress guard | effective `partial`, re-read `PENDING` | ignored — stays `partial` (no regression) | No-op |
| Marketable limit | marketable LIMIT (8.1) | fills immediately — unchanged | No error |
| Deferred features | `stop`/`stop_limit`/`am`/`pm` | still rejected (`OrderNotSupportedError` → 422) | Refusal, unchanged |

</intent-contract>

## Code Map

- `ballast/backend/brokers/port.py` — `BrokerPort` (add abstract `async cancel_order(broker_ref) -> OrderOutcome` after `get_order_status_by_ref`, ~L261); `OrderStatus` closed 5-member (unchanged); `OrderNotPlaceableError`.
- `ballast/backend/brokers/fake_adapter.py` — `_limit_fill` (L190–240): flip non-marketable refusal → deterministic `PENDING` resting outcome with `broker_ref=f"fake-order-{key}"`, index in `_orders_by_ref`; keep sub-share refusal. Add `cancel_order` (after L302) → `REJECTED`, update `_orders_by_ref`. `seed_order_status_by_ref` (L262) reused by tests.
- `ballast/backend/brokers/schwab_adapter/adapter.py` — LIMIT branch (L384–451): remove the two marketable-guard blocks (and the now-unused quote read/reference) so non-marketable limits are placed and mapped via `_map_order` (working→`PENDING`); keep sub-share refusal + fixed-point-string price. Add `.set_duration(GOOD_TILL_CANCEL)` when `intent.duration==GTC`. Add `cancel_order` (~L698) mirroring `get_order_status_by_ref`: `client.cancel_order(int(broker_ref), hash)` then read-back+`_map_order`, same fence.
- `ballast/backend/coach/execution.py` — `validate_order_intent` (L188–191): remove the `Duration.GTC` rejection arm ONLY. Add cancel owner (mirror `reconcile_pending_decision` L294–367): session gate first, terminal-short-circuit, sole caller of `broker.cancel_order`. `INDETERMINATE` (L67) stays `{TIMEOUT, PENDING}`.
- `ballast/backend/coach/decision_record.py` — `_TERMINAL_STATUSES` (L615): remove `"partial"` → `{filled, rejected}`. `record_reconciliation` (L649–699): add advance-only guard so `partial` accepts `filled`/larger `partial` but ignores an incoming `pending`/`timeout`.
- `ballast/backend/api/coach.py` — add `POST /decisions/{decision_id}/cancel` mirroring `/reconcile` (L779–866): scoped load→404; `status != cosigned`→422; live-session gate; delegate to cancel owner; persist `rejected` under `lock_decision` via `record_reconciliation`; calm envelopes for `SessionIntegrityError`/`SchwabNotConfiguredError`; terminal/no-ref→calm 422.

## Tasks & Acceptance

**Execution:**
- [x] `ballast/backend/brokers/port.py` -- add abstract `async cancel_order(self, broker_ref: str) -> OrderOutcome` -- new port surface (both adapters must implement).
- [x] `ballast/backend/coach/execution.py` -- relax `validate_order_intent` to ACCEPT `Duration.GTC` (keep STOP/STOP_LIMIT/AM/PM rejected); add the cancel owner as sole caller of `broker.cancel_order` with a session-integrity gate + terminal short-circuit -- GTC + cancel lifecycle.
- [x] `ballast/backend/brokers/fake_adapter.py` -- resting `PENDING` outcome for a non-marketable limit (stable `broker_ref`, indexed for reconcile), keep sub-share refusal; deterministic `cancel_order`→`REJECTED` -- offline resting + cancel stand-in.
- [x] `ballast/backend/brokers/schwab_adapter/adapter.py` -- drop the marketable guard (place resting limits, map via `_map_order`), keep sub-share refusal; `.set_duration(GOOD_TILL_CANCEL)` for GTC; `cancel_order` via `client.cancel_order`+read-back -- live resting + GTC + cancel.
- [x] `ballast/backend/coach/decision_record.py` -- make `partial` non-terminal + advance-only guard in `record_reconciliation` (no regression to pending/timeout) -- close Epic 6 partial-fill decision.
- [x] `ballast/backend/api/coach.py` -- add per-user-scoped, live-session-gated `POST /decisions/{id}/cancel` mirroring `/reconcile`; idempotent + calm terminal/no-ref refusal -- HTTP cancel boundary.
- [x] `ballast/backend/tests/` -- unit + integration tests covering every I/O Matrix row, incl. the zero-money seam proof (place resting → confirm `pending` → cancel → never fills → costs nothing) documented as the live-broker rehearsal; assert all 8.1 + existing tests stay green.

**Acceptance Criteria:**
- Given a non-marketable resting BUY LIMIT (fake adapter), when approved, then it co-signs `pending` with a stable `broker_ref` (never a phantom fill), and a later `/reconcile` resolves it truthfully to the seeded terminal status.
- Given a LIMIT with `duration=gtc`, when the schwab builder builds, then `.build()` yields `duration == GOOD_TILL_CANCEL` (no live call) and the cosign snapshot additively carries `duration: "gtc"`; a DAY limit still omits `duration` (schema_version stays 1).
- Given a `pending` cosigned resting order, when `POST /decisions/{id}/cancel`, then the broker cancels, it maps to `rejected`, is not re-placeable; a second cancel and a cancel of a `filled`/`rejected`/no-`broker_ref` order each return a calm 422 (never a 500); cancel is per-user scoped (another user → 404).
- Given an effective `partial` outcome, when re-reconciled, then it advances to `filled` (or a larger-qty `partial`) but a `pending`/`timeout` re-read never regresses it; the Epic 6 partial-fill action item is closed with a test.
- Given the fake adapter, when the zero-money seam proof runs (place resting → confirm `pending` mapping → cancel → never fills), then it passes deterministically and is documented as the live-broker rehearsal.
- Given the full backend suite, when run, then all 8.1 and existing tests stay green; market + marketable-limit flows are unchanged; STOP/STOP_LIMIT/AM/PM stay rejected.

## Spec Change Log

_No bad_spec loopbacks. The review pass resolved all its findings as auto-fix patches (see Review Triage Log); the spec's intent-contract and task shape held._

## Review Triage Log

### 2026-08-04 — Second follow-up review pass
- intent_gap: 0
- bad_spec: 0
- patch: 3: (high 0, medium 0, low 3)
- defer: 0
- reject: 12: (high 0, medium 0, low 12)
- addressed_findings:
  - `[low]` `[patch]` `CancelResponse` docstring enumerated only `rejected`/`timeout`/`pending` as possible statuses, but a cancel that LOSES the race (order `filled`/`partial`-fills just before the DELETE applies) reads the true state back through `_map_order` — exactly as `/reconcile` does — and persists it truthfully. Behavior was already correct (both reviewers rated this "high" but on verification the money truth is honest and within the documented B1 remainder-out-of-scope limit); the documented contract was incomplete. Fixed the docstring to state the race-window fill is surfaced and persisted (`needs_reconfirmation=True` for any non-`rejected`).
  - `[low]` `[patch]` No HTTP-boundary test covered the cancel endpoint's dedicated `SchwabNotConfiguredError → calm 409` handler (every other cancel branch was tested). Added `test_cancel_config_fault_is_calm_409_persists_nothing` (mirrors the reconcile config-fault test): 409 RECONNECT_MESSAGE, nothing persisted.
  - `[low]` `[patch]` The zero-money seam-proof test comment overclaimed it "rehearses the LIVE-broker resting-order lifecycle" — it exercises only `FakeBrokerAdapter` (which shares no code with the live Schwab DELETE + read-back). Softened the comment to say it proves the port-contract SHAPE offline and cross-references the schwab cancel tests for the live round-trip.
  - Rejected (verified, no action): (1) `SchwabAccountSelectionError → TIMEOUT` in `cancel_order` is a FALSE POSITIVE — `SchwabAccountSelectionError` subclasses `SchwabNotConfiguredError`, so it is caught by the `except SchwabNotConfiguredError: raise` arm and surfaces DISTINCTLY as a calm 409, not laundered. (2) `partial → rejected` with EQUAL `filled_qty` is the CORRECT terminal representation of a partial-then-canceled order — `filled_qty` is preserved in the snapshot, no shares erased; the advance-only guard already blocks a SMALLER-qty read. (3) cancel read-back `FILLED` reported with `needs_reconfirmation=True` is truthful and merely conservative. (4) `cancel_order` garbage/non-integer-ref `broker_ref=None` and `_effective_filled_qty` garbage-string→500 are near-unreachable (owner only passes a real integer ref; snapshots are `_money`-written) — the garbage-ref path was already assessed and rejected in the prior pass. (5) idempotency "overstated" for an indeterminate `timeout`/`pending` cancel is safe (re-issuing a DELETE is harmless). (6) GTC-market adapter defense-in-depth and downward-restatement logging are speculative future-caller/observability enhancements. (7) REJECTED-reuse being "user-hostile at the surface" and the absence of a `cancelled` member are LOCKED intent-contract decisions; surface relabeling is Story 8.3 UI scope. (8) duplicate refuse branches are cosmetic. (9) `cancel` against a `timeout`-status placement is guarded (broker_ref `None` → refused up front). The `lock_decision`-None-silent-skip and unlocked load-then-lock concurrency findings are ALREADY in the deferred-work ledger — not re-added.

### 2026-08-04 — Follow-up review pass
- intent_gap: 0
- bad_spec: 0
- patch: 2: (high 1, medium 0, low 1)
- defer: 0
- reject: 6
- addressed_findings:
  - `[high]` `[patch]` `partial` advance-only guard only blocked `pending`/`timeout`, not a smaller-qty `partial` (or any re-read with fewer filled shares) — a stale/racing read could overwrite a confirmed partial and ERASE real filled shares, contradicting the docstring/AC ("advance to filled or a larger-qty partial"). Fixed: `record_reconciliation` now refuses any incoming outcome carrying fewer `filled_qty` than already confirmed (new `_effective_filled_qty` helper); share count is monotonic toward settlement. Unit test extended with a smaller-partial regression step; spec Design-Notes pseudocode synced. (This resolves the same finding the prior code-review pass had DEFERRED — the ledger entry is left untouched for the orchestrator to close.)
  - `[low]` `[patch]` The deferred-features parametrize dropped `{"duration":"gtc"}` when GTC became accepted, removing the HTTP-boundary assertion that a GTC **market** order is refused. Fixed: added `test_approve_gtc_market_rejected_422_no_broker` (422, broker untouched, claim released) to lock the LIMIT-only-GTC backward-compat invariant at the API layer.
  - Rejected/already-ledgered (no action): cancel-race read-back returning `FILLED` is handled honestly (persists the real fill, `needs_reconfirmation=True`); `_map_order` preserves `filled_qty` on a CANCELED→REJECTED map so cancelling a partial never erases shares; schwab `cancel_order` empty/non-integer-ref null path is near-unreachable (owner only calls with a real integer ref); fake `_orders` staleness after cancel is a test-seam, not production; STOP+GTC precedence is correct (unsupported checked first); `is_index_core` still gates every order (unchanged). The unlocked load-then-lock concurrency and the `lock_decision`-None-silent-skip findings are ALREADY in the deferred-work ledger — not re-added.

### 2026-08-04 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 5: (high 1, medium 2, low 2)
- defer: 3
- reject: 4
- addressed_findings:
  - `[high]` `[patch]` Cancel destroyed partial-fill money truth — a `partial` (now non-terminal) flowed to `broker.cancel_order`, whose `rejected` (fake: `filled_qty=0`) overwrote the partial snapshot. Fixed: `cancel_pending_decision` now refuses a `partial` calmly (422, broker untouched, snapshot intact). Test added.
  - `[medium]` `[patch]` GTC accepted on a MARKET order (would be placed as DAY → intent/order divergence). Fixed: `validate_order_intent` rejects `GTC` on any non-LIMIT order (`OrderScopeError`). Test added.
  - `[medium]` `[patch]` Cancel reported an unconfirmed broker outcome (`timeout` / still-`pending` read-back) as a clean 200 success. Fixed: added `needs_reconfirmation` to `CancelResponse` (mirrors `/reconcile`), True unless the outcome is a confirmed `rejected`; outcome still persisted honestly. Tests added.
  - `[low]` `[patch]` Fake `cancel_order` clobbered an already-terminal ref (`filled`→`rejected/0`). Fixed: returns the existing terminal outcome unchanged (idempotent defense-in-depth). Tests added.
  - `[low]` `[patch]` Stale docstring called `partial` terminal in `reconcile_pending_decision`. Fixed (docs only).

## Design Notes

**Cancel of a `partial` is refused (review decision, 2026-08-04).** Because 8.2 makes `partial` non-terminal, cancel must NOT touch the broker for a partially-filled order — a broker `rejected` would overwrite the real filled shares. B1 refuses it calmly (422); remainder-cancel + fill-preservation is out of scope.

**GTC is a LIMIT-only override (review decision, 2026-08-04).** `validate_order_intent` accepts `GTC` only for LIMIT orders; a MARKET+GTC intent is an `OrderScopeError` (a market order can't rest).

**Unconfirmed cancel outcomes surface honestly (review decision, 2026-08-04).** A live cancel whose broker outcome is `timeout` or still-`pending` returns 200 with `needs_reconfirmation=True` (mirrors `/reconcile`) — never a clean-success claim; the outcome is persisted and stays reconcilable.

**Cancel reuses `REJECTED`, not a new status.** `OrderStatus` is a closed 5-member contract and Schwab's `_REJECTED_STATUSES` already folds `CANCELED`/`EXPIRED`→`REJECTED`. Adding a `cancelled` member would ripple through every mapping; instead a cancel surfaces as `rejected` (terminal, not re-placeable), which the brief explicitly allows ("maps to rejected/cancelled").

**Partial advance-only guard.** Removing `"partial"` from `_TERMINAL_STATUSES` makes `reconcile_pending_decision` re-read the broker for a partial record (desired) AND removes its regression protection in `record_reconciliation` (the monotonic guard is `if _is_terminal(...): return`). Restore forward-only protection explicitly:
```python
current = effective_outcome_status(record)
if current in ("filled", "rejected"):          # fully terminal — never overwrite
    return
if current == "partial":                        # advance-only on shares
    if outcome.status in INDETERMINATE:         # {pending, timeout} — never regress
        return
    if outcome.filled_qty < _effective_filled_qty(record):  # fewer filled shares
        return                                   # stale/racing read must not erase real fills
# else: write reconciliation_snapshot (partial→filled / partial→equal-or-larger partial / pending→*)
```

**GTC override.** `validate_order_intent` only rejects GTC via one arm (execution.py L188–191); delete just that arm. In schwab, after building the 8.1 limit builder, `if intent.duration == Duration.GTC: builder.set_duration(SchwabDuration.GOOD_TILL_CANCEL)` (import `Duration as SchwabDuration` from `schwab.orders.common`). The serializer already emits `duration` when `!= DAY`, so no snapshot/schema change.

**Cancel owner shape** (mirror `reconcile_pending_decision`): `_assert_session_integrity` FIRST; if `effective_outcome_status(record)` is terminal or `record.broker_ref is None` → return a "refused" result WITHOUT touching the broker (API maps to calm 422); else `outcome = await broker.cancel_order(record.broker_ref)` and return it for the API to persist via `record_reconciliation` under `lock_decision` (advancing `pending`→`rejected`).

## Verification

**Commands:**
- `cd ballast/backend && uv run pytest` -- expected: full suite green, including all 8.1 regression tests (`test_coach_api.py` schema_version==1, `test_recoverable_placement.py` `_ORDER_INTENT_JSON`, `test_schwab_adapter.py` market/marketable-limit, `test_migrations.py`).
- `cd ballast/backend && uv run pytest tests/test_fake_adapter_limit.py tests/test_schwab_adapter.py tests/test_order_intent_validation.py tests/test_coach_api.py -q` -- expected: new resting/GTC/cancel/partial tests pass.

**Manual checks:**
- Confirm no new `OrderStatus` member and `DECISION_RECORD_SCHEMA_VERSION == 1` after the change.
- Confirm a MARKET intent's persisted `order_intent` JSON is still exactly `{symbol, side, amount}` (byte-identical, AC regression).

## Auto Run Result

Status: done (second follow-up review pass)

**Summary:** A fresh independent follow-up review (Blind Hunter + Edge Case Hunter, run in parallel with no prior context) of the Story 8.2 diff (baseline `42f085c` → HEAD `a607747`). The two reviewers surfaced 20 raw findings (incl. two independently rated "high" on the live cancel read-back path). On verification against the actual code, the intent contract, and the two prior review passes, every high/medium finding resolved to either correct-by-design behavior, a false positive, a locked intent-contract decision, a near-unreachable path already assessed, or a finding already in the deferred-work ledger. Three genuine LOW patches (documentation + test-coverage honesty only, zero behavior change) were applied.

**Files changed this pass:**
- `ballast/backend/api/coach.py` — `CancelResponse` docstring: document that a race-window `filled`/`partial` read-back is surfaced and persisted truthfully (contract completeness; no code change).
- `ballast/backend/tests/test_coach_api.py` — added `_ConfigFaultCancelAdapter` + `test_cancel_config_fault_is_calm_409_persists_nothing` (locks the cancel endpoint's `SchwabNotConfiguredError → calm 409` handler, previously the only untested cancel branch).
- `ballast/backend/tests/test_fake_adapter_limit.py` — softened the zero-money seam-proof comment's overclaim (it proves the port-contract shape offline; the live Schwab round-trip is covered by the schwab cancel tests).

**Review findings breakdown:** patch 3 (all low) applied; defer 0; reject 12 (all low/none consequence — see the Second follow-up review pass triage log for the verified rejection rationale, incl. the `SchwabAccountSelectionError`-subclass false positive and the `partial → rejected` equal-qty correctness). No `intent_gap`, no `bad_spec`, no repair loopback.

**Verification:**
- `uv run pytest tests/test_coach_api.py -k cancel tests/test_fake_adapter_limit.py -q` → 16 passed (incl. the new config-fault cancel test).
- Full backend suite run in batches (600s harness cap): non-API suite 454 passed / 4 failed; `test_coach_api.py` all 108 passed (one order-dependent flake, `test_recommend_surfaces_fr11_warning`, passes in isolation).
- The 4 non-API failures (`test_llm_gateway.py`, `test_market_ingest.py` "returns fake by default" / "no key raises") are PRE-EXISTING environment/config artifacts: the local dev env has real Anthropic/Tiingo keys configured (the demo config), so the factories return the real adapters. Story 8.2 does not touch `llm`/`market` code or those test files, and neither do this pass's patches — not a regression.

**Residual risks:** The live Schwab cancel read-back edge cases (partial-race, indeterminate `timeout` idempotency) are covered only by fake-adapter and scripted-double tests offline; the real DELETE + read-back round-trip remains a go-live verification item (consistent with the existing "Block If" SDK-shape gate and the deferred-work ledger's live-path items). `followup_review_recommended: false` — this pass made only three localized, low-consequence, no-behavior-change fixes.


