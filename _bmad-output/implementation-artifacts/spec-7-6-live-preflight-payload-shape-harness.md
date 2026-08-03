---
title: 'Live pre-flight payload-shape harness (read-only, zero orders)'
type: 'feature'
created: '2026-08-03'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: ['oversized']
baseline_revision: '659b7b03bab5f297937b251b065f499eacdc0ca2'
final_revision: '6f813333d5aa655a9ea999699ae833336f0a977c'
---

<intent-contract>

## Intent

**Problem:** Every live JSON field mapping on the money path — Schwab OAuth token (`_to_broker_tokens`), account-numbers (`_account_hash`), balance/positions (`fetch_portfolio` → `securitiesAccount`), quote ask (`_quote_ask`), and Anthropic structured-output (`_parse_message` → `RECOMMENDATION_OUTPUT_SCHEMA`) — is still *guessed* against docs; nothing has parsed a real payload. Story 7.7 (the one real order) is currently the first moment those guesses meet reality, and it does so *while placing a real order* — the worst moment to discover a renamed/missing/retyped field.

**Approach:** Build an opt-in, default-OFF, **read-only** pre-flight harness (`ballast/backend/preflight/`) that drives the five read-reachable live seams, captures each RAW provider payload as a **redacted shape skeleton** (keys + value-types + array-lengths only — no leaf values survive, so secrets cannot leak), and runs a per-seam drift comparison against the exact field paths our mappers read, emitting `OK`/`MISSING`/`RENAMED-CANDIDATE`/`TYPE-MISMATCH` per field plus an overall PASS/DRIFT verdict. The harness code, redaction, and drift logic are fully offline-testable with synthetic payloads (the loop builds and tests it); only the one live *run* is credential-gated and it places **zero** orders — enforced structurally (the harness code path never references `place_order`). The sixth seam (order-status/fill, `_map_order`) needs a placed order and is explicitly declared out of scope → Story 7.7.

## Boundaries & Constraints

**Always:**
- **Zero-order guarantee is structural, not conventional:** the harness code path has no reference to `place_order` (the sole order-mutating client method — no cancel/replace/amend exists). A test asserts a full harness run against a mocked-live broker spy records **zero** `place_order` calls.
- **Default-OFF capture:** raw-payload capture is gated behind `Settings.PREFLIGHT_CAPTURE_DIR` (empty string = OFF). Unset → taps are passive no-ops, no file is written, and adapter behavior is byte-for-byte unchanged.
- **Total redaction by construction:** every captured payload is reduced to a shape skeleton (each leaf replaced by its type name; arrays replaced by `{type: array, len: N, item: <shape of first element>}`). No `access_token`/`refresh_token`/`accountNumber`/`hashValue`/PII leaf value ever reaches disk.
- **Read-only orchestration:** the run drives only read methods — token decrypt → `get_account_numbers`/`_account_hash` → `fetch_portfolio` → one `_quote_ask` → `/recommend` (a real *paid* Anthropic call, moves no money). It must never drive `approve` (the order path).
- **No schema change / no regression:** no migration, no new DB column, no change to any adapter's production parse/mapping behavior. Full backend suite stays green.

**Block If:**
- The five seams cannot be tapped without modifying production parse/mapping behavior (i.e. a passive tap is impossible at a seam) — HALT `blocked`, blocking condition `seam not passively tappable`.
- Investigation shows an order-mutating client method beyond `place_order` exists that the read path already invokes — HALT `blocked`, blocking condition `zero-order guarantee not structurally achievable`.

**Never:**
- Never call `place_order`, cancel, replace, or drive the `approve` endpoint.
- Never write any capture file when `PREFLIGHT_CAPTURE_DIR` is unset.
- Never write a raw leaf value (token, account number, hash, PII) to disk.
- Never add a live-network / paid test to the suite — the live run is a human runbook step in Dev Notes, not CI.
- Never silently omit the order-status/fill seam from the report — it must carry an explicit "not confirmed here → Story 7.7" line.
- Do NOT confirm the `_map_order` (order-status/fill) seam — it is out of scope (unreachable read-only).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Capture OFF (default) | `PREFLIGHT_CAPTURE_DIR` unset | Taps are no-ops; no files written; adapters byte-for-byte unchanged | No error |
| Capture ON, on-shape | All 5 seams match guessed mappings | Report verdict PASS; redacted shape skeletons written per seam | No error |
| Capture ON, renamed field | e.g. `cashBalance` → `cashBalanceValue` | Verdict DRIFT; seam 3 `cashBalance` = MISSING + RENAMED-CANDIDATE hint (sibling key of right type) | No error |
| Capture ON, missing field | expected key absent, no matching sibling | Verdict DRIFT; field = MISSING (no rename hint) | No error |
| Capture ON, type drift | e.g. `askPrice` returns a string | Verdict DRIFT; seam 4 `askPrice` = TYPE-MISMATCH | No error |
| Secret in payload | token/account number/hash present | Written skeleton contains none of the secret substrings (only type names) | No error |
| Token one-of | payload has `expires_in` but not `expires_at` (or vice-versa) | Both accepted as OK (one-of satisfied); neither reported MISSING | No error |
| Order path | any state | Harness never calls `place_order` (spy asserts 0) | No error |
| Order-status seam | no order placed | Report states `_map_order` is out of scope → Story 7.7 (never omitted) | No error |

</intent-contract>

## Code Map

- `ballast/backend/api/config.py` -- `Settings` (~line 44–102): add `PREFLIGHT_CAPTURE_DIR: str = ""` in the operational-flags block, documented like `BROKER_ADAPTER`/`LLM_ADAPTER` (empty = OFF).
- `ballast/backend/preflight/__init__.py` -- NEW: package marker.
- `ballast/backend/preflight/capture.py` -- NEW: `PayloadCapture` sink + `to_shape()` shape-skeleton reducer. When `PREFLIGHT_CAPTURE_DIR` set, writes `<seam>.json` (redacted skeleton); when unset, `capture(seam, payload)` is a no-op. `to_shape` handles dicts (keys→shape), lists (`{type:"array", len, item}`), scalars (→ type name), and SDK objects (via `__dict__`/attribute read for the Anthropic `Message`).
- `ballast/backend/preflight/drift.py` -- NEW: per-seam declared field maps (the exact paths our mappers read — see Design Notes → Field Map) + `compare(seam, shape) -> list[FieldResult]` emitting `OK`/`MISSING`/`RENAMED-CANDIDATE`/`TYPE-MISMATCH`, and the explicit order-status out-of-scope line. `RENAMED-CANDIDATE` = expected key absent but a sibling of the expected type exists.
- `ballast/backend/preflight/run.py` -- NEW: read-only orchestrator/CLI (`python -m preflight.run`). Given a live-configured session, drives token-decrypt → `_account_hash` → `fetch_portfolio` → one `_quote_ask` (one broad ETF) → `/recommend`, collects captures, runs drift, writes the human-readable report + redacted captures. Invokes ONLY read methods — no `place_order`/cancel/replace, no `approve`.
- `ballast/backend/brokers/schwab_adapter/adapter.py` -- passive capture taps (call sink only when capture enabled): `_to_broker_tokens` (~851), `_account_hash` (~644 raw list), `fetch_portfolio` (~234 `resp.json()` body), `_quote_ask` (~726 `resp.json()`).
- `ballast/backend/llm/anthropic_adapter.py` -- passive tap in `_parse_message` (~189–222): capture the raw `resp` shape (`stop_reason`, `content[]` block types + `text`) and the parsed `json.loads(text)` output shape.
- `ballast/backend/tests/test_preflight.py` -- NEW: offline coverage for every I/O-matrix row (mocked schwab client + injected LLM adapter; zero network/credentials/paid orders).
- `.gitignore` -- add a capture-output ignore entry (e.g. `preflight-captures/`) so redacted local captures are never committed.

## Tasks & Acceptance

**Execution:**
- [x] `ballast/backend/api/config.py` -- add `PREFLIGHT_CAPTURE_DIR: str = ""` with an operational-flag docstring. -- opt-in, default-OFF gate consistent with the existing `*_ADAPTER` flag pattern (AC #2).
- [x] `ballast/backend/preflight/__init__.py` + `ballast/backend/preflight/capture.py` -- add `PayloadCapture` sink + `to_shape()` reducer; write `<seam>.json` skeleton only when `PREFLIGHT_CAPTURE_DIR` set, else no-op. -- total redaction by construction (no leaf values on disk) + default-OFF passivity (AC #2, #3).
- [x] `ballast/backend/preflight/drift.py` -- declare per-seam field maps and `compare()` emitting `OK`/`MISSING`/`RENAMED-CANDIDATE`/`TYPE-MISMATCH`; include the order-status out-of-scope line. -- moves mapping-drift discovery before the trade (AC #4, #5, #6).
- [x] `ballast/backend/brokers/schwab_adapter/adapter.py` -- add passive capture taps at `_to_broker_tokens`, `_account_hash`, `fetch_portfolio`, `_quote_ask`; each runs the sink ONLY when capture enabled, otherwise not at all. -- captures 4 of the 5 read seams with zero behavior change when OFF (AC #4, #8).
- [x] `ballast/backend/llm/anthropic_adapter.py` -- add a passive capture tap in `_parse_message` for the raw `resp` shape + parsed output shape, guarded by capture-enabled. -- captures the 5th (Anthropic structured-output) seam (AC #4, #8).
- [x] `ballast/backend/preflight/run.py` -- add the read-only orchestrator/CLI driving the 5 read seams in order, collecting captures, running drift, writing the report; NO `place_order`/cancel/replace/`approve`. -- the deliverable that produces the drift report (AC #1, #4, #5).
- [x] `.gitignore` -- add the capture-output dir ignore. -- redacted captures stay local-only (AC #3 hygiene).
- [x] `ballast/backend/tests/test_preflight.py` -- add offline tests for every I/O-matrix row (on-shape PASS; renamed/missing/type-drift DRIFT with field named; token one-of; capture-OFF no files; redaction secrets-absent; zero-`place_order` spy; order-status out-of-scope line present). -- pins each edge case offline with no network/creds/paid orders (AC #1, #2, #3, #6, #7).

**Acceptance Criteria:**
- Given the full harness run against a mocked-live broker spy, when it executes, then the broker records **zero** `place_order` (and zero cancel/replace) calls, and the harness code path contains no `place_order` reference.
- Given `PREFLIGHT_CAPTURE_DIR` unset, when any tapped seam is exercised, then no capture file is written and the adapter's parsed result is identical to the pre-tap behavior.
- Given a payload containing a real token + account number + hashValue fed through the capture sink, when the skeleton is written, then none of those secret substrings appear in the file (only type names / array lengths).
- Given synthetic on-shape payloads for all 5 seams with capture ON, when the harness runs, then every field reports `OK` and the overall verdict is PASS.
- Given a payload with a renamed field (expected key absent, sibling of expected type present), when drift runs, then that field is `MISSING` with a `RENAMED-CANDIDATE` hint and the verdict is DRIFT; given a retyped field, then it is `TYPE-MISMATCH` and the verdict is DRIFT.
- Given a token payload carrying `expires_in` but not `expires_at` (or vice-versa), when drift runs, then the expiry field is `OK` (one-of satisfied), not MISSING.
- Given any run, when the report is produced, then it includes an explicit line stating the order-status/fill mapping (`_map_order`) is NOT confirmed here and is deferred to Story 7.7.
- Given the full backend suite, when it runs, then all new coverage is green and nothing regresses (adapters unchanged with capture OFF).

## Spec Change Log

_No bad_spec loopbacks — the intent contract and spec held through review. Every review finding was a caused-by-this-change patch (auto-fixed in the review pass), a forward-looking defer, or a reject; no code was re-derived from an amended spec._

## Review Triage Log

### 2026-08-03 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 6: (high 1, medium 5, low 0)
- defer: 2: (high 0, medium 2, low 0)
- reject: 17
- addressed_findings:
  - `[high]` `[patch]` A partial run produced a false PASS: a declared seam with NO capture contributed zero field results and `overall_verdict` returned PASS if the captured fields were OK — so an un-driven money-path seam (gateway/token absent, or a seam whose drive raised) read as *confirmed* before the real-money gate. Fixed: an un-captured declared seam is now `INCOMPLETE` and forces `overall` off PASS (`build_report`/`run.py`, `drift.INCOMPLETE`); test `test_missing_seam_forces_incomplete`.
  - `[medium]` `[patch]` The orchestrator had no per-seam error isolation — one seam raising (a `SchwabReadError`, typed `LLMError`, ambiguous account) aborted the whole run and produced NO report, hiding the seams that did succeed. Fixed: `_drive_default` isolates each seam drive, records `(step, error)`, and `run()` always builds + writes the report (with a "Drive errors" section); a raised custom driver is a backstop. Test `test_drive_error_isolation_still_reports`.
  - `[medium]` `[patch]` Cross-run contamination: `build_report` read whatever `<seam>.json` files existed, so a stale capture from a prior run (for a seam not driven this run) folded into today's verdict → false PASS/DRIFT. Fixed: `run()` clears all declared `<seam>.json` + the report file before driving (`_clear_captures`). Test `test_stale_capture_is_cleared`.
  - `[medium]` `[patch]` Both capture taps called the intentionally-uncached `get_settings()` (a fresh `Settings()` construction, re-reading env) on EVERY parse even when capture was OFF — real per-completion overhead on the production LLM path, contradicting the "byte-for-byte unchanged / zero overhead when OFF" invariant. Fixed: taps now gate on a cheap `os.environ["PREFLIGHT_CAPTURE_DIR"]` check FIRST and construct nothing on the OFF path (`schwab_adapter/adapter.py`, `llm/anthropic_adapter.py`).
  - `[medium]` `[patch]` The structural zero-order guarantee was enforced only for `place_order`/`approve` as bare substrings, while the docstrings promised no `cancel_order`/`replace_order` either — an enforcement/prose gap, and the bare-substring scan was brittle (a docstring word would trip it). Fixed: the guard test now scans for CALL patterns (`.place_order(`/`.cancel_order(`/`.replace_order(`/`.approve(`), covering all order-mutating methods precisely while letting the guarantee docstring name them.
  - `[medium]` `[patch]` The token seam was a self-referential false-PASS: the orchestrator drives `_to_broker_tokens` on the Ballast-RECONSTRUCTED token dict (`factory._token_dict_from_broker_tokens`, which builds exactly the mapper's fields), not Schwab's raw OAuth payload — so a token PASS confirmed nothing about the live shape. Fixed: an explicit `drift.token_reconstructed_caveat_line()` is now in the report and the run/module docstrings, disclosing that the token seam is reconstructed-shape (raw Schwab token captured only at OAuth-link time). Tests `test_token_reconstructed_caveat_*`.
- notes: Blind Hunter (`bmad-review-adversarial-general`) + Edge Case Hunter (`bmad-review-edge-case-hunter`) on the full baseline diff (`659b7b0`). **2 defers** (logged to the ledger): (medium) `to_shape` shapes only element `[0]` of an array, so drift in later `positions[]` elements — or a non-text-first LLM content block — is invisible (a report caveat mitigates; full coverage needs a per-element shape-merge); (medium) a true read-only token drift check needs a tap at the OAuth-exchange boundary rather than the reconstructed steady-state dict. **17 rejects** (by-design or unreachable): pydantic-v2 SDK objects populate `__dict__` (the `__slots__`/property concern is speculative); JSON object keys are always strings (no `str()` collision); `to_shape` only ever emits str/dict/int/None (always JSON-serializable); no declared field ends in `[]` or has a one-of under an array (the `_descend`/`_resolve_parent` array-edge paths are unreachable with the declared maps); `RENAMED-CANDIDATE` is spec'd as a heuristic hint so first-match/coincidental-type is acceptable; `get_quote(symbol)` returns a single-key body (the `[symbol]` >1-key path is not real); the "secrets never leak" claim is about leaf *values* (keys of the 5 seams are non-secret); `.gitignore`/arbitrary-`PREFLIGHT_CAPTURE_DIR` is covered by the runbook's operator guidance; per-call `PayloadCapture` rebuild is negligible ON-path cost; `mkdir`-uncreatable and corrupt-`<seam>.json` are subsumed by the new per-seam isolation + clear-on-run; empty-dict-all-MISSING still yields the correct (DRIFT) verdict; a `bytes` leaf reduces to the name `"bytes"` (no value leak). Verification: `uv run pytest tests/test_preflight.py -q` → 27 passed; `uv run pytest -q` (full backend suite) → 488 passed.

### 2026-08-03 — Review pass (follow-up)
- intent_gap: 0
- bad_spec: 0
- patch: 1: (high 1, medium 0, low 0)
- defer: 2: (high 1, medium 1, low 0)
- reject: 17
- addressed_findings:
  - `[high]` `[patch]` The adapter capture taps gate on a cheap `os.environ["PREFLIGHT_CAPTURE_DIR"]` read while the orchestrator (`run()`/`main()`) resolves the dir from `Settings` — which sources it from a `.env` file (the project's normal mechanism) that never reaches `os.environ`. So an operator configuring the harness via `.env` would get `run()` driving every seam while each tap silently no-ops → an all-INCOMPLETE run with no captures written, defeating the harness's purpose (safe-side — never a false PASS — but a silent dead run). Fixed: `run()` now mirrors the settings-resolved `capture_dir` into `os.environ` before driving, so the taps' cheap env-gate agrees with the settings value regardless of source (`preflight/run.py`); test `test_env_only_capture_dir_still_fires_taps` pins a `.env`-only config (settings set, process env unset) reaching a real PASS with all seams captured.
- notes: Fresh follow-up review pass (spec was `done`, `review_loop_iteration` reset to 0) — Blind Hunter (`bmad-review-adversarial-general`) + Edge Case Hunter (`bmad-review-edge-case-hunter`) on the full baseline diff (`659b7b0`). **2 defers** (NEW ledger entries): (high) `preflight.run.main` builds its broker via `get_broker()`, which returns a token-UNBOUND `SchwabAdapter()` (binding only happens in the async FastAPI deps needing a request DB scope+session), so a real `python -m preflight.run` produces an all-INCOMPLETE report for the four Schwab seams — fails loud+safe (INCOMPLETE, exit 1, never a false PASS) but the CLI runbook can't drive them without token-binding wiring; root cause is the pre-existing standalone-token-binding architectural gap the thin CLI surfaced, and fixing it needs a "how does the standalone run authenticate as a user" decision → folded to the Story 7.7 gate. (medium) the portfolio seam reports a false DRIFT for an all-cash account (empty `positions[]` → declared position subfields read MISSING) — safe-side, needs proper empty-array drift semantics. **17 rejects** (by-design / speculative / already-deferred / unreachable): array-only-`[0]` shape and token-reconstructed-seam are already logged from the prior pass (duplicates); SDK-object shaping via empty-`__dict__`/`__slots__` is speculative (pydantic-v2 `Message` populates `__dict__`); a corrupt/malformed `<seam>.json` crashing `build_report` needs external tampering of files the harness itself writes (valid JSON) and clears stale-first; a tap `mkdir`/`write` OSError is caught by the existing per-seam `_step` isolation during a run and is default-OFF in production; a quote body with >1 top-level key / an `errors` key falls back to the literal placeholder → MISSING → DRIFT (safe-side, prior-rejected); a JSON-`null` capture is impossible (`to_shape(None)` → `"NoneType"` string); `RENAMED-CANDIDATE` sibling-of-type is a spec'd heuristic hint (coincidental match acceptable); one-of-under-array is unreachable with the declared maps; `_clear_captures` unlink OSError is on the harness's own dir (low prob); nested-object-where-leaf already yields TYPE-MISMATCH; empty-skeleton all-MISSING yields the correct DRIFT; the zero-order scan scoped to `preflight/` is correct (adapters legitimately contain `place_order`); dict keys surviving is value-only-redaction by design (the 5 seams' keys are non-secret); the OFF-path "byte-for-byte" prose overhead is a single dict lookup; relative-dir cwd drift needs cwd to change mid-run. Verification: `uv run pytest tests/test_preflight.py -q` → 28 passed; `uv run pytest tests/test_schwab_adapter.py tests/test_llm_gateway.py -q` → 86 passed (no regression); `uv run pytest -q` (full backend suite) → 489 passed.

## Design Notes

**Field Map (the exact paths drift.py checks — verified against source):**

| # | Seam | Tap site | Fields checked |
|---|------|----------|----------------|
| 1 | OAuth token | `_to_broker_tokens` ~851 | `access_token` (str), `refresh_token` (str), **one-of** `expires_at`\|`expires_in` (number) |
| 2 | Account numbers | `_account_hash` ~644 | array of `{accountNumber (str/number), hashValue (str)}` |
| 3 | Balance/positions | `fetch_portfolio` ~234–270 | `securitiesAccount.currentBalances.cashBalance` (number); `securitiesAccount.positions[].instrument.symbol` (str), `positions[].longQuantity` (number), `positions[].marketValue` (number) |
| 4 | Quote ask | `_quote_ask` ~726 | `[symbol].quote.askPrice` (number) |
| 5 | Anthropic output | `_parse_message` ~189–222 | msg `stop_reason` (str), `content[].type=="text"`→`text` (str); parsed output requires `action_label`, `reasoning`, `evidence` (array), `uncertainties` (array) per `RECOMMENDATION_OUTPUT_SCHEMA` |
| 6 | **Order status (OUT OF SCOPE — 7.7)** | `_map_order` ~748+ | `status`, `filledQuantity`, `quantity`, `avgFillPrice`/`averagePrice`/`price` — **needs a placed order; declared not confirmed** |

- **Shape skeleton = redaction + drift substrate in one.** `to_shape()` replaces every leaf with its type name and every array with `{type:"array", len:N, item:<shape of element 0>}`. This makes redaction total (no token/account/hash/PII value can survive) *and* preserves exactly what drift needs (keys, nesting, types, array-lengths). Sibling keys survive, so `RENAMED-CANDIDATE` (expected key absent, sibling of the expected type present) is decidable from the skeleton alone.
- **The Anthropic seam is an SDK object, not a dict.** `_parse_message` reads attributes (`resp.stop_reason`, `resp.content[].type`, `.text`), not dict keys. `to_shape()` must shape SDK objects via their attributes; the tap captures both the message shape and the `json.loads(text)` output shape (the latter is where `RECOMMENDATION_OUTPUT_SCHEMA` drift shows up). `/recommend` makes a real paid call — that is the point; it moves no money and places no order.
- **`place_order` is the sole mutating surface** (verified: no cancel/replace/amend exists on the adapter; `get_order_status`/`get_order_status_by_ref` are read-only). So the structural zero-order guarantee = the harness code path never references `place_order`, backed by a spy assertion.
- **Taps are guarded, not always-on.** Each tap first checks capture-enabled (`PREFLIGHT_CAPTURE_DIR` non-empty) and only then reduces+writes; when OFF the tap body does not run, so there is no overhead and adapter output is byte-for-byte identical (pinned by a capture-OFF test).
- **Live run is a human runbook (Dev Notes of the story file), not a loop task.** The loop builds + offline-tests everything here; the credential-gated live run (real `SCHWAB_*` + `ANTHROPIC_API_KEY` + a linked, not-necessarily-funded account) is executed by the human, who fixes any named drift (loop-runnable follow-up) then proceeds to 7.7.

## Verification

**Commands:**
- `cd ballast/backend && uv run python -m pytest tests/test_preflight.py -v` -- expected: every I/O-matrix row passes (PASS/DRIFT verdicts, capture-OFF no-files, redaction secrets-absent, zero-`place_order` spy, order-status out-of-scope line).
- `cd ballast/backend && uv run python -m pytest tests/test_schwab_adapter.py tests/test_coach_api.py tests/test_llm_gateway.py -q` -- expected: adapters/gateway unchanged with capture OFF; no regression.
- `cd ballast/backend && uv run python -m pytest` -- expected: full backend suite green (requires `docker compose up -d db`).

## Dev Agent Record

**Agent Model Used:** Opus 4.8 (1M context) — claude-opus-4-8[1m]

**Completion Notes:**
- Built the new read-only `preflight/` package (`capture.py`, `drift.py`, `run.py`, `__init__.py`).
- `to_shape()` reduces any payload (dict/list/scalar/SDK-object-via-`__dict__`) to a redacted shape skeleton — no leaf value survives; this is both the redaction mechanism and the drift substrate.
- Added passive, capture-enabled-guarded taps at the 4 Schwab seams (`_to_broker_tokens`, `_account_hash`, `fetch_portfolio`, `_quote_ask`) via a shared `SchwabAdapter._preflight_capture` helper, and the 5th (Anthropic `_parse_message` — both raw message shape and parsed output shape). Each tap checks `capture_enabled(settings)` FIRST and does nothing (no reduce, no I/O) when OFF.
- `PREFLIGHT_CAPTURE_DIR: str = ""` added to `Settings` (empty = OFF), documented like the `*_ADAPTER` flags.
- Zero-order guarantee is structural: the entire `preflight/` package contains no `place_order`/`approve` string (verified by a source-scan test); the orchestrator drives only read methods; a broker-spy test asserts zero placements.
- `.gitignore` gained `preflight-captures/` (+ `**/preflight-captures/`).
- The order-status/fill (`_map_order`) seam is explicitly emitted in the report as out-of-scope, deferred to Story 7.7.

**Test commands run:**
- `uv run python -m pytest tests/test_preflight.py -v` -> 22 passed.
- `uv run python -m pytest tests/test_schwab_adapter.py tests/test_coach_api.py tests/test_llm_gateway.py -q` -> 174 passed (no regression).
- `uv run python -m pytest -q` -> 483 passed (full suite, DB available).

**File List:**
- `ballast/backend/api/config.py` (modified — added `PREFLIGHT_CAPTURE_DIR`)
- `ballast/backend/preflight/__init__.py` (new)
- `ballast/backend/preflight/capture.py` (new)
- `ballast/backend/preflight/drift.py` (new)
- `ballast/backend/preflight/run.py` (new)
- `ballast/backend/brokers/schwab_adapter/adapter.py` (modified — 4 passive taps + helper)
- `ballast/backend/llm/anthropic_adapter.py` (modified — passive tap in `_parse_message`)
- `ballast/backend/tests/test_preflight.py` (new)
- `.gitignore` (modified — capture-output ignore)

## Auto Run Result

Status: done

**Summary:** Fresh follow-up review pass on the already-`done` Story 7.6 harness (the prior pass recommended an independent follow-up). Blind Hunter + Edge Case Hunter reviewed the full baseline diff (`659b7b0`). One high-consequence patch applied; two real findings deferred to the ledger; 17 findings rejected. No intent gaps, no spec repairs, no code re-derivation.

**Change implemented this pass (patch):**
- `ballast/backend/preflight/run.py` — `run()` now mirrors the settings-resolved `PREFLIGHT_CAPTURE_DIR` into `os.environ` before driving the seams, so the adapter taps' cheap `os.environ` enable-gate agrees with the orchestrator's `Settings`-resolved value. Without this, configuring the dir via `.env` (which never reaches `os.environ`) made every tap silently no-op → an all-INCOMPLETE run with no captures. Safe-side previously (never a false PASS) but a silent dead run.
- `ballast/backend/tests/test_preflight.py` — added `test_env_only_capture_dir_still_fires_taps`, pinning a `.env`-only configuration (settings set, process env unset) reaching a real PASS with every seam captured.

**Review findings breakdown:**
- Patches applied: 1 (high) — the `.env`/`os.environ` capture-gate divergence above.
- Deferred (NEW ledger entries): 2 —
  - (high) `preflight.run.main` builds its broker via `get_broker()`, which returns a token-UNBOUND `SchwabAdapter()`; the live `python -m preflight.run` therefore yields an all-INCOMPLETE report for the four Schwab seams. Fails loud+safe (never a false PASS); root cause is the pre-existing standalone-token-binding architectural gap. Folded to the Story 7.7 real-money gate.
  - (medium) the portfolio seam reports a false DRIFT for an all-cash account (empty `positions[]` → declared position subfields read MISSING). Safe-side; needs proper empty-array drift semantics.
- Rejected: 17 (by-design / speculative / already-deferred duplicates / unreachable) — see the Review Triage Log entry for the enumerated reasons.

**Verification performed:**
- `uv run python -m pytest tests/test_preflight.py -q` → 28 passed.
- `uv run python -m pytest tests/test_schwab_adapter.py tests/test_llm_gateway.py -q` → 86 passed (no regression).
- `uv run python -m pytest -q` (full backend suite, DB up) → 489 passed.

**Follow-up review recommended:** false — this pass made a single, localized, fully-tested fix; the two remaining items are deferred (not review-driven code changes) and belong to the Story 7.7 gate.

**Residual risks:** The two deferred items above ride into Story 7.7: (1) the CLI cannot drive the Schwab seams until token-binding wiring is added — MasterB will see an all-INCOMPLETE Schwab report if he runs `python -m preflight.run` as-is (it fails loud, never false-confirms); (2) an all-cash linked account will show a false portfolio DRIFT. Both are safe-side (over-warn / dead-run, never false PASS), preserving the go-live gate's core "never falsely confirm" property.

