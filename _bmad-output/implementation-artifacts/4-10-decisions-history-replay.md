---
title: 'Story 4.10 — Decisions History & Replay'
type: 'feature'
created: '2026-07-29'
status: 'done'
final_revision: '0f71eaed12f62bfc9985da095f47f9c36fdb4569'
baseline_revision: '79f7b9fbfce350aff705779888c8e87cc6cdeb1e'
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-4-context.md'
warnings: ['oversized']
---

<intent-contract>

## Intent

**Problem:** Story 4.9 made every co-signed approval an immutable on-the-record `decision_record`, but nothing reads it back: there is no way to list the decisions a user co-signed, and no way to replay the exact reasoning + precedent + uncertainties they blessed. The **Decisions** surface is still a placeholder and the epic's closing promise — "replays the original co-signed reasoning + precedent verbatim" (FR16, AD-5, epic UX) — is unfulfilled.

**Approach:** Add READ-ONLY history + replay over the existing immutable record. Extend the sole reader/writer module (`coach/decision_record.py`, AD-6) with a `list_cosigned_decisions` reader; add two GET endpoints to `api/coach.py` (`/decisions` list, `/decisions/{decision_id}` detail) that return the frozen snapshots **verbatim** — never re-deriving, re-running the pipeline, or recomputing precedent. Build the Decisions surface (`routes/Decisions.jsx`) to list co-signed decisions and, on selection, render the replay **inline** (no new route — the app holds exactly six surfaces) through the existing `PrecedentEvidence` component so the replayed data-block cannot drift from the live coach card.

## Boundaries & Constraints

**Always:**
- **Read-back goes through the sole owner module (AD-6).** Every `DecisionRecord` read stays in `coach/decision_record.py`; `api/coach.py` delegates and never constructs, queries, or writes the model. The 4.9 grep canary (`DecisionRecord(` only in `coach/decision_record.py` + its `db/models.py` definition + the test canary) still holds.
- **List returns only co-signed decisions, newest first, per-user.** `GET /api/coach/decisions` returns this user's `status == "cosigned"` records ordered by `co_signed_at` descending, read through the fail-closed `ScopedRepository` (a foreign row is never visible). Each summary carries `decision_id`, `action_label`, `symbol` (from the executed intent), `co_signed_at`, and the reconciled `outcome_status`.
- **Detail replays the frozen snapshot verbatim.** `GET /api/coach/decisions/{decision_id}` loads the owned record via the existing `load_decision` and returns its stored `recommendation_snapshot` (action_label, reasoning, full evidence records, uncertainties, proposed order_intent) and `cosign_snapshot` (executed order_intent + reconciled outcome) exactly as persisted, plus `schema_version`, `status`, `created_at`, `co_signed_at`. It reads the record — it never recomputes, re-runs `run_coach_pipeline`, or re-hydrates precedent.
- **Per-user isolation.** An unknown or foreign `decision_id` is a 404 (invisible under the user scope); the list never spans owners. No `Scope.system()` access to decision records.
- **Replay is presentation-only and reuses the coach data-block.** The Decisions surface renders the fixed sequence verbatim — `action_label` → **Why** (reasoning) → **precedent data-block(s)** → **uncertainty callout** → **co-sign zone** — mounting `PrecedentEvidence` for each snapshot evidence record (whose `{id,kind,statement,stats,source,as_of}` shape it already consumes). Reasoning and uncertainties are real DOM text; losses/amounts never render red; color is never the sole signal.
- **Calm, honest states.** Loading is calm ("looking back at your record…", no spinner urgency); an empty history shows a gentle invite, never an error; a fetch failure degrades calmly (no error screen). Full keyboard path with visible focus; `prefers-reduced-motion` respected (inherited via `PrecedentEvidence`).
- **All ACs pass offline** with `BROKER_ADAPTER=fake`, `LLM_ADAPTER=fake` (backend) and stubbed `fetch` (frontend) — zero network, zero credentials. Existing tests stay green.

**Block If:**
- A stakeholder wants replay to RE-RUN the pipeline or RECOMPUTE precedent (show fresh numbers, not the co-signed snapshot) — dishonest replay, violates AD-5. HALT.
- A stakeholder wants the Decisions surface to show other users' decisions, or a cross-user/admin history (`Scope.system()`). HALT.
- A stakeholder wants a NEW top-level route/surface (breaking the "exactly six surfaces" invariant in `App.jsx`) for replay. HALT.
- Satisfying any AC would require a live Schwab or Anthropic call (real key/network/paid tokens). HALT.

**Never:**
- No **dip-screen replay chip** — it needs a dip/loss-state detection primitive that does not exist yet (the Dashboard has no "user is in a dip" signal); building it would fork this into a second surface concern. The primary replay entry point (from a decision on the Decisions surface, per epic UX) ships; the "or surfaced gently as a chip during a dip" secondary entry point is a follow-up.
- No new WRITE/mutation path, no editing or deleting records, no re-signing. No pruning/retention of `proposed` rows and no composite `(owner_id, co_signed_at)` index (both are deferred-ledger scaling items, not this story).
- No change to `/recommend` or `/approve`, the execution gates (entry-409, integrity-409, scope-422, single-placement, AD-7 sole-caller), the pipeline, the validation gate, or the `DecisionRecord` / snapshot / `OrderOutcome` / `EvidenceRecord` field shapes.
- No Alembic/migration framework. No React state library (follow the existing `useState`/`useEffect`/`apiFetch` pattern). Do NOT edit the append-only `deferred-work.md`.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| List co-signed history | authed user with N cosigned + M proposed records | `GET /decisions` → the N cosigned summaries only, ordered `co_signed_at` desc; proposed rows excluded; owner-scoped | 200 |
| Empty history | authed user, no cosigned records | `GET /decisions` → empty list | 200 |
| Replay a decision | authed; owned cosigned `decision_id` | `GET /decisions/{id}` → verbatim `recommendation_snapshot` (action_label, reasoning, evidence[], uncertainties, order_intent) + `cosign_snapshot` (executed order_intent + reconciled outcome) + `schema_version`/`status`/`created_at`/`co_signed_at`; nothing recomputed | 200 |
| Unknown / foreign id | `decision_id` absent or owned by another user | 404; nothing leaked | 404 |
| Isolation | user A lists/reads; user B's records exist | A never sees B's decisions in the list or via detail | 200 list excludes B; 404 on B's id |
| Decisions surface — populated | list returns cosigned decisions | renders a selectable list; selecting one replays it inline (action_label → reasoning → `PrecedentEvidence` per evidence → uncertainty callout → co-sign zone) | calm fallback on fetch error |
| Decisions surface — empty | list returns `[]` | gentle "no decisions yet" invite copy (not an error) | — |

</intent-contract>

## Code Map

- `ballast/backend/coach/decision_record.py` -- MODIFY: add `async def list_cosigned_decisions(*, scope: Scope, session: AsyncSession) -> list[DecisionRecord]` — `ScopedRepository(DecisionRecord, scope, session).list()`, filter `record.status == "cosigned"`, sort by `co_signed_at` descending (in Python; `co_signed_at` is set for every cosigned row). SOLE reader of the model; no new writes. Reuses existing `load_decision`.
- `ballast/backend/api/coach.py` -- MODIFY: add two READ endpoints on the existing `router` (prefix `/api/coach`), delegating to the module above (never touching `DecisionRecord`): `@router.get("/decisions", response_model=DecisionListResponse)` → `list_cosigned_decisions(...)` mapped to `DecisionSummaryOut[]`; `@router.get("/decisions/{decision_id}", response_model=DecisionDetailResponse)` → `load_decision(...)` (→ 404 via `HTTPException` if `None`) returning the stored snapshots verbatim. Add Pydantic models `DecisionSummaryOut {decision_id: str, action_label: str, symbol: str | None, co_signed_at: str, outcome_status: str}`, `DecisionListResponse {decisions: list[DecisionSummaryOut]}`, `DecisionDetailResponse {decision_id: str, schema_version: int, status: str, created_at: str, co_signed_at: str | None, recommendation_snapshot: dict, cosign_snapshot: dict | None}` (snapshots passed through verbatim as stored JSON). Use `scope`/`session` deps (`get_scope`, `get_async_session`); NO broker/live-session dep on reads. Serialize timestamps via `.isoformat()`.
- `ballast/backend/db/models.py` -- REFERENCE: `DecisionRecord` (immutable; `status`, `recommendation_snapshot`, `cosign_snapshot`, `co_signed_at`, `created_at`, `schema_version`). Unchanged.
- `ballast/backend/db/repository.py`, `ballast/backend/db/scope.py`, `ballast/backend/api/deps.py` -- REFERENCE: `ScopedRepository.list()/get()` (fail-closed per-user), `Scope`/`get_scope`. Reused, unchanged.
- `ballast/backend/tests/test_coach_api.py` -- MODIFY: add tests for the two read endpoints (list newest-first, cosigned-only, isolation; detail verbatim; unknown/foreign 404). Reuse the `ensure_tables` fixture, `_recommend_decision_id`, `_decision_rows`, auth helpers, and a cosign via the existing happy-path `/approve`.
- `ballast/frontend/src/routes/Decisions.jsx` -- MODIFY: replace the placeholder with the real surface — fetch `GET /api/coach/decisions` on mount (`useState`/`useEffect`/`apiFetch`, active-flag cleanup, per Dashboard); states: loading (calm), empty (gentle invite), ready (selectable decision list), fetch-error (calm fallback). Selecting a decision fetches `GET /api/coach/decisions/{id}` and renders `<DecisionReplay>` inline (selected-id state; no route change).
- `ballast/frontend/src/components/DecisionReplay.jsx` (+ `DecisionReplay.css`) -- NEW: verbatim replay panel from a detail payload — renders `recommendation_snapshot.action_label` → **Why** (`reasoning` as DOM text) → one `PrecedentEvidence` per `recommendation_snapshot.evidence` record (unique `idPrefix` per index to avoid `id` collisions) → `<UncertaintyCallout>` → co-sign zone (`co_signed_at` + `cosign_snapshot.outcome` status/executed intent as calm mono text, never red). Presentation-only.
- `ballast/frontend/src/components/UncertaintyCallout.jsx` (+ `UncertaintyCallout.css`) -- NEW: violet callout (`--ballast-color-uncertainty`) rendering the `uncertainties` string list as real DOM text; always present on a replay.
- `ballast/frontend/src/routes/Decisions.css` -- NEW: list + selection styling using design tokens only (no raw hex).
- `ballast/frontend/src/components/PrecedentEvidence.jsx`, `ballast/frontend/src/lib/precedent.js`, `ballast/frontend/src/lib/session.js` -- REFERENCE: `PrecedentEvidence` (consumes the `{id,kind,statement,stats,source,as_of}` snapshot evidence shape verbatim), precedent helpers, `apiFetch`. Reused, unchanged.
- `ballast/frontend/src/test/decisions.test.jsx` -- NEW: stub `fetch` (`vi.stubGlobal`) for list + detail; assert cosigned list renders, selection replays reasoning + evidence via `PrecedentEvidence` + uncertainty callout + co-sign metadata, empty-state invite, and no red/pink for any amount/loss.

## Tasks & Acceptance

**Execution:**
- [x] `ballast/backend/coach/decision_record.py` -- Add `list_cosigned_decisions(*, scope, session)` (scoped `list()`, filter cosigned, sort `co_signed_at` desc). -- Keeps the Coach Engine the sole reader/writer (AD-6) and supplies honest, owner-scoped history.
- [x] `ballast/backend/api/coach.py` -- Add `GET /decisions` (list summaries) and `GET /decisions/{decision_id}` (verbatim detail, 404 on unknown/foreign) with their response models, delegating to the module; no writes, no broker dep. -- Exposes read-back/replay without re-deriving or mutating the immutable record (FR16, AD-5).
- [x] `ballast/frontend/src/routes/Decisions.jsx` + `Decisions.css` -- Build the real Decisions surface: fetch list, render selectable history with calm loading/empty/error states, replay the selected decision inline. -- Fulfills the Decisions surface (epic UX) within the six-surface constraint.
- [x] `ballast/frontend/src/components/DecisionReplay.jsx` + `.css` and `UncertaintyCallout.jsx` + `.css` -- Render the verbatim coach-card sequence from the detail payload, reusing `PrecedentEvidence`; add the violet uncertainty callout. -- Replays the original co-signed reasoning + precedent verbatim, color-honest and accessible.
- [x] `ballast/backend/tests/test_coach_api.py` -- Add: (a) list returns only cosigned, newest-first, owner-scoped; (b) detail returns the verbatim snapshots + metadata; (c) unknown & foreign `decision_id` → 404; (d) cross-user isolation (B's decision absent from A's list, 404 on detail). -- Locks read-back honesty + per-user isolation offline.
- [x] `ballast/frontend/src/test/decisions.test.jsx` -- Add: populated list renders + selection replays (reasoning, `PrecedentEvidence`, uncertainty callout, co-sign metadata); empty-state invite; never red/pink for amounts. -- Locks the surface's calm, honest, accessible rendering offline.

**Acceptance Criteria:**
- Given a user who has co-signed decisions (and has un-cosigned `proposed` records), when they call `GET /api/coach/decisions`, then they receive only their co-signed decisions, newest-first by `co_signed_at`, with no other user's decisions ever included (per-user `ScopedRepository`).
- Given a co-signed `decision_id` owned by the caller, when they call `GET /api/coach/decisions/{decision_id}`, then the response reproduces the persisted `recommendation_snapshot` (action_label, reasoning, full evidence records, uncertainties) and `cosign_snapshot` (executed order_intent + reconciled outcome) VERBATIM with its `schema_version` — nothing is recomputed, the pipeline is not re-run, and the record is not mutated (AD-5).
- Given AD-6 (Coach Engine sole owner of decision records), when the backend tree is grepped, then all `DecisionRecord` reads/writes remain inside `coach/decision_record.py` (model defined in `db/models.py`); `api/coach.py` only delegates.
- Given the Decisions surface with co-signed decisions, when the user opens it and selects a decision, then it replays that decision inline — action_label, the original reasoning, each precedent data-block through `PrecedentEvidence`, the violet uncertainty callout, and the co-sign outcome — as real DOM text, with no red/pink for any amount and color never the sole signal; an empty history shows a gentle invite, not an error.
- Given the default offline configuration (`BROKER_ADAPTER=fake`, `LLM_ADAPTER=fake`; stubbed `fetch`), when the backend and frontend suites run, then the history/replay behaviors pass with zero credentials and zero network; the 4.6–4.9 execution/persistence behaviors and the "exactly six surfaces" route set still hold; and no dip-screen chip, new write path, or new top-level route was added.

## Spec Change Log

## Review Triage Log

### 2026-07-29 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 5: (high 0, medium 1, low 4)
- defer: 0
- reject: 15
- addressed_findings:
  - `[medium]` `[patch]` List-fetch failure previously fell back to the empty-invite state, so a user WITH history whose `GET /decisions` failed was dishonestly told "no decisions yet." Added a distinct calm `decisions-load-error` state ("couldn’t load your record just now… nothing is lost") separate from the truly-empty invite; the empty invite now renders only on a successful empty response. Test updated to assert the two states are distinct.
  - `[low]` `[patch]` Raw ISO-8601 timestamps (`2026-07-28T15:00:00+00:00`) were rendered verbatim to a beginner surface. Added `lib/datetime.js::formatDay` (deterministic, tz/locale-independent) and routed the list-meta and replay co-sign date through it → "Jul 28, 2026". New test asserts the humanized day and that the raw wire time never reaches the DOM.
  - `[low]` `[patch]` The replay "Co-signed" section rendered its heading unconditionally, so a (UI-unreachable but API-possible) non-cosigned detail payload would show a "Co-signed" heading with an empty body. Gated the entire co-sign section on `detail.co_signed_at` so the label never appears without co-sign data (honesty).
  - `[low]` `[patch]` Re-selecting the same decision after a detail-fetch error was a no-op (the effect keyed only on `selectedId`), so the "pick it again in a moment" retry instruction did nothing for the most-likely-clicked item. Added a `reloadKey` nonce bumped on every selection so re-selecting the same id re-fires the fetch; new test verifies the retry actually re-requests.
  - `[low]` `[patch]` The inline replay reveal produced no screen-reader announcement. Added `aria-live="polite"` to the replay region so activating a list item announces the newly rendered replay.
- rejected (unreachable given the code's invariants, or by-design):
  - "Cosigned row with `co_signed_at IS NULL` crashes the list sort / `isoformat()`" and "tz-naive vs tz-aware `co_signed_at` mix crashes the sort": `cosign()` is the canary-locked SOLE writer (no Alembic, no other write path) and always sets `status="cosigned"`, `co_signed_at`, and `cosign_snapshot` together using `datetime.now(timezone.utc)`. No code path yields a cosigned row with a null/naive `co_signed_at`; hardening an impossible state is inconsistent with the project's convention+canary approach (mirrors 4.9's rejection of DB-level immutability enforcement).
  - "Empty `outcome_status` renders dangling `· ·`", "missing executed `side`/`symbol`/`amount` renders `undefined`", "missing `outcome.status` renders `Outcome: undefined`", "missing `action_label`/`reasoning` renders an empty Why": all guarded by the same sole-writer completeness of `cosign_snapshot` and by the 4.2 validation gate (a blessed recommendation always has non-empty `action_label`/`reasoning`) — unreachable for a real record.
  - "UncertaintyCallout returns null on an empty list, violating the 'always present' invariant": the 4.2 validation gate makes any surfaceable recommendation carry ≥1 uncertainty, so the snapshot always has uncertainties and the callout always renders; the null-guard is defensive-only.
  - "Red-tinted co-sign divider violates the no-red-for-losses rule": the dashed red-tinted co-sign divider is the EXPLICIT epic UX signature (epic-4-context.md); amounts/outcomes render as calm `--ballast-color-text` mono, and no loss/market value is colored red. The rule targets market up/down coloring, which correctly uses sky-blue/green via `MarketIndicator`.
  - "Outcome/executed intent lacks `MarketIndicator` direction": a fill quantity/price is not a directional market move (up/down), so `MarketIndicator` does not apply; the buy/sell direction is already shown as real text.
  - "`schema_version != 1` served as v1": only v1 exists (`DECISION_RECORD_SCHEMA_VERSION = 1`); passing it through verbatim is the intended durability design (the client can branch), not a defect.
  - "Detail endpoint returns an owned `proposed` record (should 404)": returning the caller's OWN record (owner-scoped, status field truthful) is the spec's chosen `load_decision` design; there is no cross-user leak, and the UI honesty concern is fully addressed by the co-sign-section gate above.
  - "No pagination / unbounded list growth / missing composite index": already recorded in `deferred-work.md` (the 4.9 entry explicitly covers the 4.10 history query's retention + `(owner_id, created_at)` index) and scoped out by this offline single-user story.
  - Noise: `encodeURIComponent` on server-issued UUIDs; orphaned `selectedId` after a list refetch (the list only fetches once on mount); `recommendation_snapshot: dict` non-optional (column is `nullable=False`).

## Design Notes

- **Read the record, never re-derive it.** Replay's entire honesty rests on returning the frozen `recommendation_snapshot`/`cosign_snapshot` exactly as 4.9 persisted them. The evidence ids are deterministic content hashes (`make_id`), so the snapshot is a faithful, reproducible capture — there is no reason to recompute, and recomputing would show numbers the user never blessed. The detail endpoint therefore passes the stored JSON through verbatim (`dict` passthrough models), not re-serialized domain objects.
- **Reuse `PrecedentEvidence` verbatim.** The snapshot's `evidence[]` items are `EvidenceRecord.to_dict()` (`{id, kind, statement, stats, source, as_of}`) — precisely what `PrecedentEvidence` consumes for both `event-precedent` and `strategy` kinds — so the replayed data-block cannot drift from the live coach card's color/honesty rules. Give each mounted instance a distinct `idPrefix` (e.g. `replay-precedent-{i}`) to keep DOM ids unique.
- **Offline evidence is strategy-kind.** In fake mode the pipeline falls back to the default plan (fake evidence ids miss the retrieved set), so co-signed snapshots carry `strategy` evidence and a `null` proposed order_intent while the executed `cosign_snapshot.order_intent` is the client-supplied one (the 4.9 two-facet seam). Replay honestly renders both facets; `PrecedentEvidence`'s strategy branch covers the offline path, and the `event-precedent` branch covers production without code change.
- **Inline replay, not a new surface.** `App.jsx` fixes exactly six surfaces; replay lives as selected-decision state on Decisions (list ⇄ replay), mirroring the existing per-surface `useState`/`apiFetch` pattern rather than adding a route.
- **Ordering in Python is sufficient here.** `ScopedRepository.list()` is unordered; sorting cosigned rows by `co_signed_at` in the reader avoids touching shared repository infra. A composite `(owner_id, co_signed_at)` index and `proposed`-row retention are real scaling concerns already recorded in the deferred ledger — out of scope for this offline, single-user story.

## Verification

**Commands:**
- `cd ballast/backend && .venv/bin/python -m pytest tests/test_coach_api.py -q` -- expected: all pass (existing gates/persistence + new history/replay/isolation tests); offline, no network/credentials.
- `cd ballast/backend && .venv/bin/python -m pytest -q` -- expected: full suite green (was 258 after 4.9; new tests added, zero regressions).
- `cd ballast/backend && .venv/bin/python -c "import coach.decision_record, coach.pipeline, sys; assert 'anthropic' not in sys.modules"` -- expected: exit 0 (no SDK import on the fake/default path).
- `cd ballast/frontend && npm test -- --run` -- expected: Vitest suite green including the new `decisions.test.jsx`.
- `cd ballast/backend && git status --short` -- expected: only Code-Map files changed; revert any `uv.lock` drift before finishing (known drift gotcha).

## Auto Run Result

Status: done

**Summary of implemented change:** Delivered the epic's closing capability — read-only Decisions history & verbatim replay over the immutable co-signed decision record (FR16, AD-5/AD-6). Backend: extended the sole reader/writer `coach/decision_record.py` with `list_cosigned_decisions` (owner-scoped `ScopedRepository.list()` → filter `cosigned` → sort `co_signed_at` desc) and added two READ endpoints on the existing `/api/coach` router — `GET /decisions` (newest-first cosigned summaries) and `GET /decisions/{decision_id}` (the frozen `recommendation_snapshot` + `cosign_snapshot` returned VERBATIM as stored JSON, 404 for unknown/foreign) — both delegating to the owner module (never touching the model), with no broker/live-session dependency, so history reads work in degraded mode. Frontend: replaced the Decisions placeholder with the real surface (list + inline verbatim replay as selected-decision state — no new route, six surfaces preserved), reusing `PrecedentEvidence` for each snapshot evidence record and adding a violet `UncertaintyCallout`; reasoning/uncertainties render as real DOM text, the co-sign zone is the epic's dashed red-tinted divider with calm (never red) amounts. The dip-screen chip was deliberately scoped out (no dip-detection primitive exists); the primary Decisions entry point ships.

**Files changed:**
- `ballast/backend/coach/decision_record.py` (MODIFIED) — added `list_cosigned_decisions` reader; module stays the AD-6 sole reader/writer.
- `ballast/backend/api/coach.py` (MODIFIED) — added `DecisionSummaryOut`/`DecisionListResponse`/`DecisionDetailResponse` (snapshots as `dict` passthrough), `_decision_summary_out`, and the `GET /decisions` + `GET /decisions/{decision_id}` handlers delegating to the owner module.
- `ballast/backend/tests/test_coach_api.py` (MODIFIED) — +6 tests: list cosigned-only/newest-first/owner-scoped, empty history, verbatim detail, unknown 404, foreign 404, cross-user isolation.
- `ballast/frontend/src/routes/Decisions.jsx` (MODIFIED) + `Decisions.css` (NEW) — real Decisions surface: list + inline replay, calm loading / gentle empty invite / distinct calm load-error states, `aria-live` replay region, retry-on-re-select.
- `ballast/frontend/src/components/DecisionReplay.jsx` + `.css` (NEW) — verbatim coach-card replay (action_label → Why → `PrecedentEvidence`×N → `UncertaintyCallout` → gated co-sign zone).
- `ballast/frontend/src/components/UncertaintyCallout.jsx` + `.css` (NEW) — violet uncertainty callout (real DOM text; color never the sole signal).
- `ballast/frontend/src/lib/datetime.js` (NEW, review patch) — deterministic `formatDay` humanizing co-sign timestamps.
- `ballast/frontend/src/test/decisions.test.jsx` (NEW) — list renders + inline replay, empty invite, distinct load-error, humanized date, retry-re-fires.

**Review findings breakdown:** 0 intent gaps, 0 bad-spec loopbacks, 5 patches applied (1 medium: list-fetch failure no longer dishonestly reports "no decisions"; 4 low: humanized timestamps, gated co-sign heading, retry-on-re-select, `aria-live` on the replay region), 0 newly deferred (pagination/index/retention already in `deferred-work.md` from 4.9), 15 rejected (crash/undefined branches unreachable under the canary-locked sole-writer + 4.2 validation-gate invariants; the red-tinted co-sign divider is the explicit epic UX signature; fills aren't directional so no `MarketIndicator`; `schema_version` passthrough is the intended durability design; owned-`proposed` detail is spec-designed and non-leaking; plus noise).

**Verification performed (independently re-run):**
- `.venv/bin/python -m pytest tests/test_coach_api.py -q` → 46 passed (+6).
- `.venv/bin/python -m pytest -q` (full suite) → 264 passed, 0 failed (was 258 after 4.9; +6; zero regressions).
- `.venv/bin/python -c "import coach.decision_record, coach.pipeline, sys; assert 'anthropic' not in sys.modules"` → offline OK.
- `npm test -- --run` → 64 passed / 11 files (new `decisions.test.jsx`); `npm run lint:css` clean (token enforcement on the new CSS).
- `git status --short` → only Code-Map files + the review-added `lib/datetime.js` changed; `uv.lock` clean (no drift).
- Structural: all `DecisionRecord` construction confined to `db/models.py` + the test canary; all `ScopedRepository(DecisionRecord, …)` access confined to `coach/decision_record.py` (AD-6 holds); `App.jsx` unchanged (six surfaces intact).

**Follow-up review recommendation:** false — the final pass made only localized, low-consequence presentation-layer patches (one medium honesty fix + four low) with no behavior/API/security/data change and no re-derivation; the core read-back, verbatim-replay, sole-reader, and per-user-isolation behaviors reviewed clean and are locked by new tests.

**Residual risks:** (1) `GET /decisions` loads all of a user's cosigned rows with no pagination and no `(owner_id, co_signed_at)` index, and `proposed` rows are never pruned — real scaling/retention concerns already recorded in `deferred-work.md` (from 4.9); no v1 consequence (single-user, offline). (2) `GET /decisions/{id}` will return the caller's OWN `proposed` record (200, `cosign_snapshot=null`) if fetched directly by id; not reachable via the UI (the list surfaces cosigned only) and no cross-user leak, but a direct API consumer sees it — the replay UI now gates its "Co-signed" section so it never mislabels such a record. (3) Real Schwab `place_order`/`get_order_status` and the live-LLM `order_intent`-emitting path remain credential-gated stubs, so offline snapshots carry strategy-kind evidence with a null proposed intent (the executed intent is the client-supplied one); replay renders both facets honestly.
