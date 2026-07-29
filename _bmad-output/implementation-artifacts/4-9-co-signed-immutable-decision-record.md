---
title: 'Story 4.9 — Co-signed Immutable Decision Record'
type: 'feature'
created: '2026-07-29'
status: 'done'
baseline_revision: 'cb66b38101b20b2ab19a20d34144e405f6f52c48'
review_loop_iteration: 0
followup_review_recommended: false
final_revision: '025ee6a7722e403c976a166db17e2f8e5b968a34'
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-4-context.md'
warnings: ['oversized']
---

<intent-contract>

## Intent

**Problem:** The coach can propose and execute, but keeps NO on-the-record memory: `/api/coach/approve` places an order and returns the reconciled outcome without ever persisting anything (4.6/4.7/4.8 deliberately deferred all persistence to 4.9). There is no immutable, co-signed decision record capturing the blessed reasoning + evidence + uncertainties the user actually approved — so nothing exists for replay (4.10), and a co-sign is not yet a shared "on the record" call (FR16, AD-5, AD-6).

**Approach:** Introduce ONE immutable per-user `decision_record` table written ONLY by the Coach Engine (AD-6, new `coach/decision_record.py`). `/recommend` persists the blessed recommendation as a **proposed** record — an immutable snapshot of `action_label + reasoning + full evidence records + uncertainties + proposed order_intent`, stamped with a `schema_version` for replay durability (AD-5) — and returns its `decision_id`. `/approve` now carries that `decision_id`: on a successful execution it **co-signs** the referenced record exactly once (proposed→cosigned, writing `co_signed_at`, the executed order_intent, the idempotency key, and the reconciled outcome) while the recommendation snapshot itself is NEVER mutated. Re-approving an already-cosigned decision returns the recorded outcome and never re-touches the broker (persisted record + stored key close the cross-request double-place gap). All existing 4.6/4.7/4.8 execution gates and the 4.3 recommend behavior are unchanged; the record links to the EXACT recommendation the user saw rather than re-deriving one or trusting client-authored content.

## Boundaries & Constraints

**Always:**
- **Recommend persists a proposed record and returns `decision_id`.** Each `/recommend` call writes exactly ONE proposed `DecisionRecord` via the fail-closed `ScopedRepository` (owner stamped from `get_scope`). The immutable `recommendation_snapshot` embeds `action_label`, `reasoning`, the proposed `order_intent` (or `null`), the FULL evidence records (AD-12 `EvidenceRecord.to_dict()` shape — precedent snapshotted at decision time, never recomputed later), and `uncertainties`, plus a `schema_version` (AD-5). The response gains `decision_id`.
- **Coach Engine is the SOLE writer of decision records (AD-6).** Every `DecisionRecord` read/write goes through `coach/decision_record.py`; `api/coach.py` delegates and never constructs or writes the model itself. A grep canary locks this (mirrors the AD-7 sole-caller canary).
- **Approve co-signs by reference, one-shot.** `/approve` requires a `decision_id`; it loads the record through the `ScopedRepository` (a row not owned by the caller is invisible → 404). On a SUCCESSFUL `execute_approved_order`, the referenced record transitions proposed→cosigned, writing `co_signed_at`, the executed `order_intent`, the `idempotency_key`, and the reconciled `OrderOutcome` EXACTLY once; `recommendation_snapshot`/`schema_version`/`created_at` are never altered.
- **Idempotent re-approve (no double-place).** Approving an already-cosigned `decision_id` returns the RECORDED outcome and NEVER re-calls the broker — the persisted record + stored idempotency key make co-sign cross-request idempotent (closes the 4.8-noted "cross-request persisted idempotency key (4.9)").
- **Refusals write no co-sign.** A pre-placement refusal (entry-gate 409, placement-time integrity 409, or v1-scope 422) leaves the referenced record **proposed**; a decision record is co-signed only when the order was actually placed and reconciled (any of the five honest `OrderOutcome` statuses counts as co-signed).
- **Conventions.** UUID primary key; tz-aware UTC timestamps; money in persisted JSON as fixed-point decimal strings via `format(Decimal, "f")` (never binary float, never `E+` exponent notation); JSON snapshot column; per-user isolation via `OwnedEntityMixin` + `ScopedRepository`; table created by the existing `create_db_and_tables()`/`Base.metadata.create_all` convention. Structured logs never log secrets/tokens.
- **All 4.6/4.7/4.8 invariants hold unchanged:** explicit-approval-only, the `require_live_broker_session` entry gate, placement-time session+provider integrity, the v1-scope gate, a single `place_order` + reconcile, the AD-7 sole-caller canary. Approve keeps its existing client-supplied `order_intent` execution seam.
- **All ACs pass offline** with `BROKER_ADAPTER=fake`, `LLM_ADAPTER=fake` — zero network, zero credentials. Existing tests stay green.

**Block If:**
- A stakeholder wants the CLIENT to supply the reasoning/evidence/uncertainties for the persisted record (client-authored snapshot) — violates the trust model (the SPA holds no business logic; only backend-blessed content may be recorded). HALT.
- A stakeholder wants `/approve` to RE-RUN the pipeline and snapshot a fresh recommendation (not the one the user saw) — dishonest co-sign. HALT.
- A stakeholder wants the decision record editable/mutable after co-sign, or wants an already-cosigned decision to re-place an order. HALT.
- Satisfying any AC would require a live Schwab or Anthropic call (real key/network/paid tokens). HALT.

**Never:**
- No replay/history read endpoints, Decisions surface, or dip-screen chip (Story 4.10). No React UI.
- No change to `OrderOutcome`/`OrderIntent`/`Recommendation`/`EvidenceRecord` field shapes, the coach pipeline logic, the validation gate, or the entry/scope/integrity gates.
- No persistence inside `run_coach_pipeline` (keep the pipeline pure and unit-testable) — persistence lives in the API layer via the coach writer module.
- No Alembic/migration framework (follow the existing `create_all` convention); no cross-user (`Scope.system()`) access to decision records.
- Do NOT edit the append-only `deferred-work.md`.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Recommend persists proposal | authed user; any decision | returns the blessed recommendation PLUS `decision_id`; exactly one **proposed** `DecisionRecord` (immutable snapshot + `schema_version`) is written, owner-scoped | No error (200) |
| Approve co-signs | authed; live+provider-matched session; proposed record owned by user; in-scope `order_intent`; matching `decision_id` | order placed once and reconciled (4.7); referenced record → **cosigned** (`co_signed_at`, executed order_intent, idempotency_key, reconciled outcome written once); snapshot untouched; returns the reconciled `OrderOutcome` | No error (200) |
| Re-approve already cosigned | record already `cosigned` | returns the RECORDED outcome; `place_order`/`get_order_status` never called again (no duplicate placement) | No error (200) |
| Approve unknown/foreign `decision_id` | `decision_id` absent or owned by another user | 404; broker never called; nothing co-signed | 404 |
| Approve, expired session | valid `decision_id`; session expired at entry | entry gate raises 409 `RECONNECT_MESSAGE` before load/execute; record stays proposed; broker untouched | 409 (entry gate) |
| Approve, out-of-scope intent | valid `decision_id`; live session; non-index symbol / non-positive amount | `OrderScopeError` → 422 before any broker call; record stays proposed | 422 (scope) |
| Sole-writer canary | grep backend tree | `DecisionRecord(...)` construction / writes appear ONLY in `coach/decision_record.py` (model defined in `db/models.py`) | No error |

</intent-contract>

## Code Map

- `ballast/backend/db/models.py` -- MODIFY: add `class DecisionRecord(OwnedEntityMixin, Base)` (`__tablename__ = "decision_record"`) with columns: `id` (UUID pk, `default=uuid.uuid4`), `schema_version: int` (not null), `recommendation_snapshot` (JSON, not null — the immutable blessed snapshot), `status: str(16)` (not null, default `"proposed"`), `created_at` (`DateTime(timezone=True)`, not null), `co_signed_at` (`DateTime(timezone=True)`, nullable), `idempotency_key: str(64)` (nullable), `cosign_snapshot` (JSON, nullable — executed order_intent + reconciled outcome). Import `JSON` from `sqlalchemy`. Class docstring: IMMUTABLE — snapshot columns write-once at propose; co-sign fills the nullable co-sign columns exactly once; sole writer is `coach/decision_record.py` (AD-6, AD-5). `owner_id` comes from the mixin.
- `ballast/backend/coach/decision_record.py` -- NEW: the SOLE writer/reader module for `DecisionRecord`. Define `DECISION_RECORD_SCHEMA_VERSION = 1`. `_money(v: Decimal) -> str` → `format(v, "f")` (fixed-point, no `E+`). `_snapshot(blessed: BlessedRecommendation) -> dict` → `{action_label, reasoning, order_intent: {symbol, side(value), amount(_money)} | None, evidence: [r.to_dict() for r in blessed.evidence], uncertainties: [...]}`. `async def record_proposal(blessed, *, scope, session) -> DecisionRecord` — `ScopedRepository(DecisionRecord, scope, session).add(schema_version=..., recommendation_snapshot=_snapshot(blessed), status="proposed", created_at=<now UTC>)`; returns the flushed row (caller commits). `async def load_decision(decision_id: UUID, *, scope, session) -> DecisionRecord | None` — via `ScopedRepository.get`. `def cosign(record, *, order_intent: OrderIntent, outcome: OrderOutcome, idempotency_key: str) -> None` — guard `record.status == "proposed"`; set `status="cosigned"`, `co_signed_at=<now UTC>`, `idempotency_key`, `cosign_snapshot={order_intent:{symbol,side.value,_money(amount)}, outcome:{status.value, filled_qty:_money, avg_price:_money|None, broker_ref}}` (caller commits). Uses tz-aware UTC (`datetime.datetime.now(datetime.timezone.utc)`).
- `ballast/backend/api/coach.py` -- MODIFY: (recommend) after `blessed`, `record = await record_proposal(blessed, scope=scope, session=session)`, `await session.commit()`, add `decision_id=str(record.id)` to `RecommendResponse`. (approve) add `decision_id: UUID` to `ApproveRequest`; in the handler load the record via `load_decision` (→ 404 if `None`); if `record.status == "cosigned"` return the recorded outcome (rebuilt from `cosign_snapshot`) WITHOUT calling the broker; else mint `key = mint_idempotency_key()`, call `execute_approved_order(intent, broker=..., broker_session=..., idempotency_key=key)`, then `cosign(record, order_intent=intent, outcome=outcome, idempotency_key=key)`, `await session.commit()`, return the outcome. Keep the existing `SessionIntegrityError`→409 and `OrderScopeError`→422 arms (they occur BEFORE co-sign, so no record is written). Add `decision_id` to `RecommendResponse`; `ApproveResponse` shape unchanged. Update module/handler docstrings (persistence lands HERE in 4.9).
- `ballast/backend/db/repository.py` -- REFERENCE: `ScopedRepository` (`add`/`get`, fail-closed per-user) — the docstring already anticipates `decision_record → Epic 4`. Reused, unchanged.
- `ballast/backend/db/scope.py`, `ballast/backend/api/deps.py` -- REFERENCE: `Scope` / `get_scope` (per-user), `require_live_broker_session`, `RECONNECT_MESSAGE`. Unchanged.
- `ballast/backend/coach/validation.py` -- REFERENCE: `BlessedRecommendation{action_label, order_intent, reasoning, evidence: tuple[EvidenceRecord], uncertainties}` — source of the snapshot. Unchanged.
- `ballast/backend/coach/execution.py` -- REFERENCE: `execute_approved_order` (accepts `idempotency_key`), `mint_idempotency_key`, `OrderScopeError`, `SessionIntegrityError`. Unchanged.
- `ballast/backend/coach/recommendation.py` -- REFERENCE: `OrderIntent{symbol, side: OrderSide, amount: Decimal}`, `OrderSide`. Unchanged.
- `ballast/backend/precedent/evidence.py` -- REFERENCE: `EvidenceRecord.to_dict()` (JSON-safe AD-12 shape). Unchanged.
- `ballast/backend/db/session.py` -- REFERENCE: `get_async_session`, `create_db_and_tables` (`Base.metadata.create_all`) — the new table is created the same way. Unchanged.
- `ballast/backend/tests/test_coach_api.py` -- MODIFY: add `DecisionRecord.__table__.create(checkfirst=True)` to the `ensure_tables` fixture; add a `_recommend_decision_id(client, headers)` helper (POST `/recommend`, return `decision_id`); thread a `decision_id` into every `/approve` body (the existing scope-422 / non-positive-422 / session-409 gate tests stay — the client still supplies the executed `order_intent`, they just prefix a real `decision_id`); add the new tests (see Tasks).

## Tasks & Acceptance

**Execution:**
- [x] `ballast/backend/db/models.py` -- Add the immutable `DecisionRecord(OwnedEntityMixin, Base)` model with the columns above. -- The single per-user, insert-then-co-sign immutable table backing recommend/co-sign/replay (AD-5/AD-6).
- [x] `ballast/backend/coach/decision_record.py` -- NEW sole-writer module: `DECISION_RECORD_SCHEMA_VERSION`, `_money`, `_snapshot`, `record_proposal`, `load_decision`, `cosign`. -- Makes the Coach Engine the sole, honest writer of decision records; freezes the blessed snapshot at decision time; fixed-point money in JSON.
- [x] `ballast/backend/api/coach.py` -- `/recommend`: persist a proposed record and return `decision_id`. `/approve`: require `decision_id`, load (404 if absent/foreign), co-sign on success, idempotent re-approve (return recorded outcome, broker untouched), keep the 409/422 refusal arms writing no co-sign. Update docstrings. -- Wires co-sign into the existing propose/approve flow without touching the execution gates.
- [x] `ballast/backend/tests/test_coach_api.py` -- Add `DecisionRecord` to `ensure_tables`; add `_recommend_decision_id` helper; thread `decision_id` into all approve bodies; add tests covering the I/O matrix: (a) recommend persists exactly one proposed record with snapshot fields + `schema_version`, scoped to the user, and returns `decision_id`; (b) happy-path approve co-signs — the referenced record becomes `cosigned` with `co_signed_at`, executed order_intent, `idempotency_key`, and reconciled outcome, while `recommendation_snapshot`/`schema_version` are byte-identical to the proposed snapshot (immutability); (c) replay-readiness — the cosigned record's snapshot contains `reasoning`, evidence list, `uncertainties`, and `schema_version`; (d) idempotent double-approve — second `/approve` with the same `decision_id` returns the recorded outcome and the broker spy shows `place_order` called exactly ONCE total (no double-place); (e) unknown/foreign `decision_id` → 404, broker untouched, nothing persisted; (f) refusals leave the record proposed — expired-session 409 and out-of-scope 422 both leave `status == "proposed"` and the broker untouched; (g) sole-writer canary — grep asserts `DecisionRecord(` construction only in `coach/decision_record.py`. Keep the existing entry-gate/integrity/sole-caller tests green. -- Locks FR16/AD-5/AD-6 + idempotency + per-user isolation structurally, offline.

**Acceptance Criteria:**
- Given I approve a recommendation via `/api/coach/approve` carrying its `decision_id` on a live, in-scope path, when `execute_approved_order` succeeds, then the referenced decision record is co-signed: its immutable `recommendation_snapshot` (action_label, reasoning, full evidence records, uncertainties) and `schema_version` — captured at `/recommend` time — are preserved verbatim and never mutated, and the co-sign metadata (`co_signed_at`, executed order_intent, `idempotency_key`, reconciled `OrderOutcome`) is written exactly once (FR16, AD-5, AD-6).
- Given a decision already co-signed, when `/approve` is called again with the same `decision_id`, then the recorded outcome is returned and the Broker Port is never re-invoked — the record is immutable and co-sign is one-shot (no duplicate placement across requests).
- Given AD-6 (Coach Engine is the sole writer of decision records), when the backend tree is grepped, then `DecisionRecord` is constructed/persisted only within `coach/decision_record.py` (its model defined in `db/models.py`); `api/coach.py` and all other modules only delegate.
- Given the default offline configuration (`BROKER_ADAPTER=fake`, `LLM_ADAPTER=fake`), when the whole suite runs, then every `/recommend` persists a proposed record and every successful `/approve` co-signs it — all per-user through the fail-closed `ScopedRepository` — with zero credentials and zero network; the 4.6/4.7/4.8 execution gates (scope-422, entry/integrity-409, single-placement, AD-7 sole-caller) and 4.3 recommend behavior still hold; and no replay/history endpoint or UI (4.10) was added.

## Spec Change Log

## Review Triage Log

### 2026-07-29 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 2: (high 0, medium 0, low 2)
- defer: 2
- reject: 11
- addressed_findings:
  - `[low]` `[patch]` Money-serialization consistency: routed `_to_approve_response` (the live `/approve` outcome) through a new fixed-point `_money_str` (`format(Decimal,"f")`) matching the persisted co-sign snapshot's `_money`, so a first approve and its idempotent replay return byte-identical money strings and no `E+` exponent notation ever crosses the wire (also closes the Story-4.7-ledger money-format gap for this path). Reachable only at extreme magnitudes, so the fix is a consistency/honesty guarantee; existing tests stay green.
  - `[low]` `[patch]` Docstring honesty: softened the `api/coach.py` module + `approve()` docstrings so they no longer overclaim absolute "no double-place across requests." They now state the guarantee precisely — a SEQUENTIAL re-approve is idempotent via the persisted `status` early-return — and point the atomic in-flight-concurrency hardening (proposed→cosigned atomic claim + a stable persisted per-decision idempotency key) at real-broker wiring, noting v1 has no live broker so no real order can be double-placed today.
- deferred (real, out of this story's v1 offline scope — see deferred-work.md):
  - Concurrent/in-flight double-approve of the same `decision_id` is not atomically guarded (read-status → place → write is not a single transaction; the idempotency key is minted per-request, not a stable persisted per-decision key; no `SELECT … FOR UPDATE` / conditional `WHERE status='proposed'` update / unique index). No consequence in v1 (Schwab adapter is a credential-gated stub; single-user dev); a proper fix needs `ScopedRepository` locking infra + broker-side key dedup semantics. Deferred to real-broker wiring.
  - Unbounded growth of `proposed` (never-co-signed) decision records — every `/recommend` writes an immutable row with no retention/pruning. Intended design, but a future scaling/retention concern.
- rejected (not this story's problem / noise / out-of-scope-by-design):
  - "No DB-level immutability enforcement (trigger/CHECK) for the snapshot columns" and "add `CHECK(status IN …)` / unique index": the codebase enforces invariants by the sole-writer convention + grep canary + the one-shot `cosign` guard, not DB triggers/CHECK (no such pattern exists anywhere); adding one is out of scope and inconsistent with the project.
  - "No Alembic migration": explicitly a Never in this spec (follow the existing `create_all` convention, as every prior story does).
  - "`execute_approved_order` should return the key it used / persist that": the handler mints the key and passes it in; `execute_approved_order` uses the supplied key deterministically (verified), so the persisted key IS the placed key — folded into the deferred concurrency item, not a separate defect.
  - "Foreign-scope approve attempt has no audit log": the 404 isolation behavior is correct and tested; a security-audit-log surface is a separate cross-cutting concern, not this story.
  - "Non-finite (NaN/Inf) outcome money not guarded" and "pending outcome cosigned as 'final'": unreachable with the fake (fills are finite) / by-design — 4.9 records the reconciled outcome at co-sign time; re-reconciling a pending outcome is replay/reconciliation territory (4.10), explicitly out of scope here.
  - "`_recorded_outcome_response` untyped / no None-guard on `cosign_snapshot`": a cosigned record always has `cosign_snapshot` (set atomically with `status` in `cosign`), so the None branch is unreachable; missing type hint is cosmetic.

## Design Notes

- **Link, don't re-derive or trust the client.** `/approve` carries a `decision_id` so the co-signed record attaches to the EXACT recommendation surfaced at `/recommend` time (the frozen snapshot). It deliberately does NOT re-run the pipeline (that would snapshot a fresh, unseen recommendation — dishonest) and does NOT accept a client-authored snapshot (free-text reasoning can't be authenticated; the SPA holds no business logic). This is the only honest, offline-testable realization of AD-5 "the blessed Recommendation ... is persisted immutably."
- **Two facets, one record — and the offline seam.** The immutable `recommendation_snapshot` is what was PROPOSED (reasoning/evidence/uncertainties/proposed order_intent + `schema_version`); `cosign_snapshot` is what was EXECUTED (the user-approved order_intent + reconciled outcome). In production these order_intents coincide. OFFLINE NUANCE: the fake LLM cannot emit a parseable `order_intent` (its placeholder `amount` fails `Decimal` parse and its fake evidence IDs miss the retrieved set → the pipeline falls back to the default plan with `order_intent=None`), so in tests the proposed snapshot's order_intent is `null` while the co-signed executed intent is the client-supplied one — the record honestly captures BOTH. Keeping the existing client-supplied `order_intent` on `/approve` (the 4.6 seam) is what makes co-sign testable at all offline; a future story can collapse to co-sign-purely-by-`decision_id` once a real LLM emits the matching intent.
- **Immutability semantics.** `recommendation_snapshot`/`schema_version`/`created_at` are write-once at propose; `cosign()` fills the previously-NULL co-sign columns exactly once (guarded on `status == "proposed"`); a cosigned record is only ever READ afterward (re-approve is a pure read that returns the recorded outcome). "No feature re-derives or mutates it" (AD-5) holds at the snapshot level.
- **Sole writer (AD-6) + canary.** `coach/decision_record.py` is the only module that constructs or persists `DecisionRecord`; `api/coach.py` delegates. A grep canary locks this exactly as the AD-7 sole-`place_order`-caller canary does.
- **Fixed-point money.** Persisted JSON money uses `format(Decimal, "f")` so an extreme amount never serializes as `"1E+27"` — proactively avoiding the `str(Decimal)` exponent gap noted in the 4.7 deferred ledger, for this new persisted surface only.

## Verification

**Commands:**
- `cd ballast/backend && .venv/bin/python -m pytest tests/test_coach_api.py -q` -- expected: all pass (existing gates + new persistence/co-sign/idempotency/canary tests); offline, no network/credentials.
- `cd ballast/backend && .venv/bin/python -m pytest -q` -- expected: full suite green (was 251 passing after 4.8; new tests added, zero regressions).
- `cd ballast/backend && .venv/bin/python -c "import coach.decision_record, coach.pipeline, sys; assert 'anthropic' not in sys.modules"` -- expected: exit 0 (no SDK import on the fake/default path).
- `cd ballast/backend && git status --short` -- expected: only Code-Map files changed; revert any `uv.lock` drift before finishing (known drift gotcha).

## Auto Run Result

Status: done

**Summary of implemented change:** Introduced the co-signed IMMUTABLE decision record (FR16, AD-5/AD-6). A new per-user `decision_record` table (`db/models.py`) stores one insert-then-co-sign immutable row. `POST /api/coach/recommend` now persists the blessed recommendation as a **proposed** record — an immutable snapshot of `action_label + reasoning + full evidence records (AD-12 shape) + uncertainties + proposed order_intent`, stamped with `schema_version` for replay durability — and returns its `decision_id`. `POST /api/coach/approve` now carries that `decision_id`: it loads the record through the fail-closed per-user `ScopedRepository` (a foreign/unknown id is invisible → 404); if already cosigned it returns the RECORDED outcome without re-touching the broker (sequential idempotency); otherwise, on a successful `execute_approved_order`, it CO-SIGNS the referenced record exactly once (proposed→cosigned: `co_signed_at`, executed order_intent, idempotency key, reconciled outcome) while the recommendation snapshot is never mutated. The Coach Engine (`coach/decision_record.py`) is the SOLE writer of decision records (AD-6), locked by a grep canary. All 4.6/4.7/4.8 execution gates (explicit-approval-only, entry-gate 409, placement-time integrity, v1-scope 422, single-placement, AD-7 sole-caller) and the 4.3 recommend behavior are unchanged; refusals leave the record proposed. Money in every persisted/returned JSON is fixed-point (`format(Decimal,"f")`, never float, never `E+`).

**Files changed:**
- `ballast/backend/db/models.py` (MODIFIED) — added the immutable `DecisionRecord(OwnedEntityMixin, Base)` model (UUID pk; `schema_version`, `recommendation_snapshot` JSON, `status`, `created_at` write-once at propose; `co_signed_at`, `idempotency_key`, `cosign_snapshot` filled once at co-sign; tz-aware UTC; per-user via the mixin).
- `ballast/backend/coach/decision_record.py` (NEW) — the sole decision-record writer/reader: `DECISION_RECORD_SCHEMA_VERSION`, `_money` (fixed-point), `_snapshot`, `record_proposal` (via `ScopedRepository`), `load_decision` (scoped read), `cosign` (one-shot, guarded on `status=="proposed"`).
- `ballast/backend/api/coach.py` (MODIFIED) — `/recommend` persists a proposed record + returns `decision_id`; `/approve` requires `decision_id`, loads (404 if foreign/unknown), idempotent re-approve returns the recorded outcome (broker untouched), else mints the key, executes, co-signs, commits; the 409/422 refusal arms occur before co-sign. Review patches: added `_money_str` so the live approve outcome is fixed-point and byte-identical to the idempotent replay; docstrings made precise about sequential-vs-concurrent idempotency.
- `ballast/backend/tests/test_coach_api.py` (MODIFIED) — added `DecisionRecord` to `ensure_tables`, a `_recommend_decision_id` helper and a `_decision_rows` reader; threaded a real `decision_id` into every `/approve` body; added refusal-stays-proposed assertions to the scope-422/session-409/provider-409 gate tests; added 7 new tests (recommend persists one proposed record; approve co-signs immutably; replay-ready snapshot; idempotent double-approve no double-place; unknown 404; foreign-owner 404; sole-writer canary).

**Review findings breakdown:** 0 intent gaps, 0 bad-spec loopbacks, 2 patches applied (both low: money-serialization consistency between the live approve path and the idempotent replay path; docstring honesty about sequential-vs-concurrent idempotency), 2 deferred (the atomic in-flight concurrent-approve guard + stable persisted idempotency key + DB unique constraint — gated on real-broker wiring, no v1 consequence since Schwab is a credential-gated stub; and retention/pruning of unbounded never-co-signed proposals), 11 rejected (DB-level immutability triggers/CHECK constraints inconsistent with the codebase, no-Alembic by-design, unreachable None/non-finite branches, out-of-scope audit-log/reconciliation-of-pending, cosmetic typing).

**Verification performed (independently re-run by the orchestrator):**
- `.venv/bin/python -m pytest tests/test_coach_api.py -q` → 40 passed.
- `.venv/bin/python -m pytest -q` (full suite) → 258 passed, 0 failed (was 251 after 4.8; +7 new tests; zero regressions; 1 pre-existing Starlette deprecation warning).
- `.venv/bin/python -c "import coach.decision_record, coach.pipeline, sys; assert 'anthropic' not in sys.modules"` → offline OK (no SDK import on the fake/default path).
- `git status --short` → only the Code-Map files changed; `uv.lock` clean (no drift).
- Structural spot-checks: `DecisionRecord(` construction appears only in `db/models.py` (definition) + the test canary (sole-writer AD-6 holds); `cosign` guards `status != "proposed"` (one-shot immutability).

**Follow-up review recommendation:** false — the final pass produced only two localized, low-consequence patches (a money-string consistency tweak reachable only at extreme magnitudes, and docstring honesty) with no re-derivation and no behavior/API change beyond fixed-point formatting; the core persistence, co-sign, immutability, sole-writer, and per-user-isolation behaviors reviewed clean and are locked by new tests.

**Residual risks:** (1) The in-flight concurrent double-approve guard is not atomic (deferred): two simultaneous `/approve` calls with the same `decision_id` could both place before the first commits. No v1 consequence (Schwab adapter is a credential-gated stub; single-user dev) — deferred to real-broker wiring where money can actually move. (2) `proposed` decision records accumulate without retention (deferred, revisit with 4.10). (3) In offline fake mode the pipeline cannot emit a parseable `order_intent` (fake evidence IDs miss the retrieved set → default-plan fallback), so a proposal snapshot's `order_intent` is `null` while the co-signed executed intent is the client-supplied one; the record captures both facets honestly and they coincide once a real LLM is wired. (4) Replay/history read + the Decisions surface remain Story 4.10; real Schwab `place_order`/`get_order_status` remain credential-gated stubs.
