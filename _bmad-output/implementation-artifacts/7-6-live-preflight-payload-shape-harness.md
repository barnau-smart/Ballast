# Story 7.6: Live pre-flight payload-shape harness (read-only, zero orders)

Status: ready-for-dev

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As **MasterB (the operator taking Ballast live)**,
I want **a read-only pre-flight harness that runs the live Schwab + live Anthropic read paths, captures the RAW provider payloads (redacted), and reports where our guessed field mappings drift from reality — without placing any order**,
so that **I can confirm 5 of the 6 live payload shapes and fix any drift before a single dollar moves in the Story 7.7 real-money pass, instead of discovering a mismapped field mid-trade.**

## Context

Stories 7.1–7.5 hardened the money path behind gates, but every live JSON field mapping (token, account-numbers, balance/positions, quote, order-status, Anthropic structured-output) is still *guessed* against docs — nothing has parsed a real payload. Story 7.7 (the real-money gate, formerly 7.6) is where those guesses meet reality, but it does so *while placing a real order* — the worst moment to discover a renamed field.

This story builds the tool that moves that discovery **before** the trade for everything reachable without an order. It is the loop-safe half of the go-live spike: the harness code, redaction, and drift logic are fully offline-testable with synthetic payloads (the loop builds and tests it); only the one live *run* is credential-gated, and that run **places zero orders**.

**The one seam it cannot cover:** the order-status/fill mapping (`_map_order`) requires a placed order to read. That stays with Story 7.7's single real order. This gap is deliberate and must be stated in the drift report, never silently omitted.

## Acceptance Criteria

1. **Zero-order guarantee (structural).** The harness never calls `place_order` (nor any order-mutating client method — cancel/replace). This is enforced structurally, not by convention: the harness code path has no reference to `place_order`, and a test asserts that invoking the full harness against a mocked live broker records **zero** order-placement calls.
2. **Opt-in, default-OFF capture.** Raw-payload capture is gated behind an explicit opt-in (a `PREFLIGHT_CAPTURE_DIR` setting/flag, empty by default). With it unset, production/normal runs capture nothing and adapter behavior is byte-for-byte unchanged (the taps are passive no-ops). A test asserts no capture file is written when the flag is unset.
3. **Redaction.** Every captured payload is redacted before it touches disk: OAuth `access_token`/`refresh_token` values, the raw `accountNumber` and `hashValue`, and any obvious PII are masked; only structural shape (keys, nesting, value *types*, array lengths) survives. A test feeds a payload containing a token + account number through the capture sink and asserts the secret substrings are absent from the written file.
4. **Five read-only seams captured + drift-checked.** A live (or mocked-live in tests) run drives, in order: (a) token load/decrypt, (b) `get_account_numbers` → `_account_hash`, (c) `fetch_portfolio` → `securitiesAccount` balance/positions, (d) a `_quote_ask` read for one broad ETF, (e) `/recommend` → Anthropic `_parse_message`. For each, the RAW payload is captured and a drift-comparison checks **every field our mapper actually reads** against the captured shape, emitting `OK` / `MISSING` / `RENAMED-CANDIDATE` / `TYPE-MISMATCH` per field.
5. **Drift report is the deliverable.** The run produces one human-readable report (per-seam field table + an overall PASS/DRIFT verdict) plus the redacted raw captures. On a fully on-shape set of payloads the verdict is PASS; on a payload with a renamed/missing field the verdict is DRIFT and the offending field is named with its expected path.
6. **Order-status seam explicitly declared out of scope.** The report includes an explicit line stating the order-status/fill mapping (`_map_order`: `status`/`filledQuantity`/`quantity`/`avgFillPrice`) is **not** confirmed by this read-only harness and is deferred to Story 7.7's single real order — never silently omitted.
7. **Offline-testable.** All of the above is proven offline with synthetic on-shape and drifted payloads through the mocked adapters — no network, no credentials, no paid call in the test suite. The live run is documented in a runbook (Dev Notes) for the human, not executed by the loop.
8. **No regressions / no schema change.** No migration, no new DB column, no change to the production parse/mapping behavior of any adapter. Full backend suite stays green.

## Tasks / Subtasks

- [ ] **Task 1 — Capture sink + redaction** (AC: #2, #3)
  - [ ] Add `ballast/backend/preflight/capture.py`: a `PayloadCapture` sink that, when `PREFLIGHT_CAPTURE_DIR` is set, writes `<seam>.json` (redacted); when unset, is a no-op. Redact `access_token`/`refresh_token` values, mask `accountNumber`/`hashValue`, drop obvious PII — preserve keys/types/nesting/array-lengths only.
  - [ ] Add `PREFLIGHT_CAPTURE_DIR: str = ""` to `ballast/backend/api/config.py` (empty = OFF), documented alongside the other operational flags.
- [ ] **Task 2 — Passive capture taps at the 5 read seams** (AC: #4)
  - [ ] `brokers/schwab_adapter/adapter.py::_to_broker_tokens` (~851) — tap raw token dict.
  - [ ] `brokers/schwab_adapter/adapter.py::_account_hash` (~643) — tap `get_account_numbers()` list.
  - [ ] `brokers/schwab_adapter/adapter.py::fetch_portfolio` (~234) — tap `resp.json()` `securitiesAccount` body.
  - [ ] `brokers/schwab_adapter/adapter.py::_quote_ask` (~725) — tap quote `resp.json()`.
  - [ ] `llm/anthropic_adapter.py::_parse_message` (~189) — tap the raw response content/text before `json.loads`.
  - [ ] Each tap calls the sink only when capture is enabled; otherwise it must not run (no overhead, no behavior change).
- [ ] **Task 3 — Drift comparison** (AC: #4, #5, #6)
  - [ ] Add `ballast/backend/preflight/drift.py`: declare, per seam, the exact field paths our mappers read (see Dev Notes → Field Map) and compare against a captured payload, emitting per-field `OK`/`MISSING`/`RENAMED-CANDIDATE`/`TYPE-MISMATCH`. `RENAMED-CANDIDATE` = an expected key is absent but a sibling key of the right type exists (heuristic hint, not a guarantee).
  - [ ] Emit the explicit "order-status/fill NOT confirmed here → Story 7.7" line.
- [ ] **Task 4 — Read-only orchestrator** (AC: #1, #4, #5)
  - [ ] Add `ballast/backend/preflight/run.py`: a callable/CLI that, given a live-configured session, drives token-decrypt → account-numbers → `fetch_portfolio` → one `_quote_ask` → `/recommend`, collects captures, runs drift, and writes the report. It must **only** invoke read methods — no `place_order`, no cancel/replace.
- [ ] **Task 5 — Offline tests** (AC: #1, #2, #3, #7, #8)
  - [ ] Add `ballast/backend/tests/test_preflight.py`: synthetic on-shape payloads → PASS; drifted (renamed/missing/type-changed) payloads → DRIFT with the field named; capture-off → no files; redaction → secrets absent; zero-`place_order` assertion via a mocked broker spy.
  - [ ] Run the full backend suite; confirm no regression and no behavior change with capture OFF.

## Dev Notes

### The six live-payload seams (real anchors, from source)

| # | Seam | File:line | Fields our mapper reads |
|---|------|-----------|--------------------------|
| 1 | OAuth token | `brokers/schwab_adapter/adapter.py::_to_broker_tokens` ~851–873 | `access_token`, `refresh_token`, `expires_at` **or** `expires_in` |
| 2 | Account numbers | `brokers/schwab_adapter/adapter.py::_account_hash` ~643–714 | list of `{accountNumber, hashValue}` |
| 3 | Balance/positions | `brokers/schwab_adapter/adapter.py::fetch_portfolio` ~195–298 | `securitiesAccount.currentBalances.cashBalance`; `positions[].instrument.symbol`, `positions[].longQuantity`, `positions[].marketValue` |
| 4 | Quote ask | `brokers/schwab_adapter/adapter.py::_quote_ask` ~716–746 | `[symbol].quote.askPrice` |
| 5 | Anthropic output | `llm/anthropic_adapter.py::_parse_message` ~189–231 | `stop_reason`, `content[].type=="text"`.`text`, then `json.loads(text)` validated vs `coach/recommendation.py::RECOMMENDATION_OUTPUT_SCHEMA` (~78–98) |
| 6 | **Order status (OUT OF SCOPE — 7.7)** | `brokers/schwab_adapter/adapter.py::_map_order` ~748–848 | `status`, `filledQuantity`, `quantity`, `avgFillPrice`/`averagePrice`/`price`/`orderActivityCollection` — **needs a placed order; not reachable read-only** |

Seams 1–5 are the drift targets for this story. Seam 6 is declared, not confirmed (AC #6).

### Gating / factory (how live is selected — do not change behavior)

- `api/config.py`: `BROKER_ADAPTER` (~48, `"fake"`→`"schwab"`), `LLM_ADAPTER` (~81, `"fake"`→`"anthropic"`), `ANTHROPIC_API_KEY` (~87), `SCHWAB_CLIENT_ID`/`SECRET`/`CALLBACK_URL`/`ACCOUNT_ID` (~53–63), `TOKEN_ENCRYPTION_KEY` (~44).
- `brokers/factory.py::get_broker` (~33–52) and `llm/factory.py::get_llm_gateway` (~42–82, memoized) route fake vs live by those flags — the harness swaps adapters purely via config, touching no caller code.

### Endpoints the harness drives (read-only)

- `api/portfolio.py::refresh_portfolio` (~106–126) → `reconcile_portfolio` → `broker.fetch_portfolio()`.
- `api/coach.py::recommend` (~371–409) → `run_coach_pipeline` → `gateway.complete()` → Anthropic `_parse_message`. **Note:** `/recommend` makes a real *paid* Anthropic call — that is expected and is the point (confirm the structured-output shape). It moves no money and places no order. Do **not** drive `approve` (~413+) — that is the order path and belongs to 7.7.

### I/O & edge-case matrix

| Scenario | State | Expected |
|----------|-------|----------|
| Capture OFF (default) | `PREFLIGHT_CAPTURE_DIR` unset | taps are no-ops; no files; adapters byte-for-byte unchanged |
| Capture ON, on-shape payloads | all 5 seams match guessed mappings | report verdict PASS; redacted captures written |
| Capture ON, renamed field | e.g. `cashBalance`→`cashBalanceValue` | verdict DRIFT; seam 3 field `cashBalance` = MISSING (+ RENAMED-CANDIDATE hint) |
| Capture ON, type drift | e.g. `askPrice` returns a string | verdict DRIFT; seam 4 `askPrice` = TYPE-MISMATCH |
| Secret in payload | token/account number present | written capture contains neither secret substring |
| Order path | anything | harness never calls `place_order` (spy asserts 0) |
| Order-status seam | no order placed | report states it is out of scope → 7.7 |

### Testing standards

- pytest under `ballast/backend/tests/`, `test_*.py`, `@pytest.mark.asyncio` for async. Mock the schwab-py client and inject the LLM adapter exactly as `test_schwab_adapter.py` / `test_coach_api.py` do — "zero credentials, zero network, zero paid orders." No live markers exist today; do not add a live-network test (the live run is a human runbook step, not a CI test).

### Human runbook (the live run — NOT a loop task)

Once creds exist (Schwab dev app approved + a *linked, not necessarily funded* account + a paid `ANTHROPIC_API_KEY`):
1. Set `BROKER_ADAPTER=schwab`, `LLM_ADAPTER=anthropic`, the `SCHWAB_CLIENT_ID/SECRET/CALLBACK_URL` + `TOKEN_ENCRYPTION_KEY` + `ANTHROPIC_API_KEY` vars, `DATABASE_URL` (the SAME DB used below), and `PREFLIGHT_CAPTURE_DIR=<a gitignored dir>`.
2. **Link the account once** via the app's OAuth "Connect Schwab" flow so an encrypted token is stored in `brokerage_token`. The pre-flight expects **exactly one linked account** (a dedicated operator user); it refuses on zero or multiple.
3. Run the harness: `cd ballast/backend && python -m preflight.run`. It binds that single linked operator's decrypted token (`bind_operator_token`, added 2026-08-06 — the CLI has no request scope), drives the 5 read seams, and writes `preflight-report.txt` + redacted `<seam>.json` captures to `PREFLIGHT_CAPTURE_DIR`. Exit 0 = PASS, 1 = DRIFT/INCOMPLETE, 2 = misconfig (dir unset / no bindable token).
4. Read the report. If DRIFT: fix the named mapping(s) — **that fix is loop-runnable** as a follow-up, since you now have the real (redacted) shape. Re-run until PASS.
5. Only then proceed to Story 7.7 (the one real order), which confirms the remaining order-status/fill seam.
- Never commit the capture dir or any env value. The captures are redacted but still local-only.

> **Follow-up landed 2026-08-06 (commit e0b50c7):** the CLI originally built the broker via the bare `get_broker()` (no token), so the four Schwab read seams would have failed "no token bound" and only the LLM seam would confirm live. `bind_operator_token` now binds the single linked operator's token in `main()`; the token/account/balance/quote seams drive for real. (Caveat unchanged: seam 1 checks Ballast's *reconstructed* token dict, not Schwab's raw OAuth payload — that shape is only seen at link time.)

### Project Structure Notes

- New package `ballast/backend/preflight/` (`capture.py`, `drift.py`, `run.py`) keeps the harness isolated from production request paths; the only edits to existing files are the tiny passive taps (Task 2) + the one config flag. No changes under `api/` request handlers beyond what the harness calls read-only.
- `PREFLIGHT_CAPTURE_DIR` should be added to `.gitignore` coverage if a concrete default dir name is chosen.

### References

- [Source: _bmad-output/implementation-artifacts/epic-7-context.md#Technical Decisions] — pre-flight harness decision + zero-order boundary.
- [Source: _bmad-output/implementation-artifacts/spec-7-5-live-read-robustness-multi-account-safety.md] — `fetch_portfolio` offload, `_account_hash` selection, `OrderOutcome.account_ref`.
- [Source: _bmad-output/implementation-artifacts/bmad-dev-auto-result-7-7-gated-live-exercise-real-money.md] — the real-money gate this harness de-risks; suggested the guarded read-only dry-run.

## Dev Agent Record

### Agent Model Used

### Debug Log References

### Completion Notes List

### File List
