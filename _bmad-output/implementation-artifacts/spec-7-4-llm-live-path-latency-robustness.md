---
title: 'LLM live-path latency & robustness hardening'
type: 'bugfix'
created: '2026-08-01'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: ['oversized']
baseline_revision: '8d09ea6785f6dd29f4f2bf3e6f789d8f6df99321'
final_revision: '19f405592370fbd6296d3c9f3fe92af983ed27f4'
---

<intent-contract>

## Intent

**Problem:** On the credential-gated live LLM path (`LLM_ADAPTER=anthropic`) two robustness gaps remain. (1) `AnthropicGateway.complete()` constructs a fresh `anthropic.Anthropic(api_key=...)` on **every** call (`anthropic_adapter.py:124`) with **no** `timeout=` or `max_retries=`, so it forfeits HTTP-connection reuse and a hung Anthropic call blocks the interactive `/recommend` for up to the SDK's ~10-minute default before Story 6.2's typed-error handling can degrade to the default plan — a practical dead-end for an interactive user. (2) The port's "no raw exception escapes" invariant on the non-streaming path holds only by an **unpinned numeric coincidence** — `STREAMING_MAX_TOKENS = 16000` happens to sit below the SDK's non-streaming `ValueError` ceiling and the two routed model ids happen not to be in the SDK's per-model cap dict — with no test pinning it, so a future threshold bump or a routing change to a capped model id would let a raw `ValueError` (not an `anthropic.APIError`, so not fenced by line 159) escape `complete()`.

**Approach:** Construct the Anthropic client **once** (connection reuse, not per-call) with an explicit request timeout and retry budget read from `Settings`, tuned to the interactive `/recommend` envelope so a hung call degrades to the default plan in seconds. Pin the `STREAMING_MAX_TOKENS` ↔ SDK non-streaming-ceiling coupling with a regression test that reads the SDK's *real* limits for every model `route_model` can return, so a threshold/route change that would let a raw `ValueError` escape fails the suite instead of shipping. No new functional requirements, no schema change, no new LLM behavior — the gateway stays the sole Anthropic caller (AD-6) with deterministic Opus-4.8/Sonnet-4.6 routing.

## Boundaries & Constraints

**Always:**
- The Anthropic SDK client is built at most once per `AnthropicGateway` instance (cached on the instance, reused across `complete()` calls); the same gateway (and thus its pooled client) is reused across requests via the factory (connection reuse, not per-call).
- The client is constructed with an explicit `timeout` and `max_retries` sourced from `Settings` (`LLM_REQUEST_TIMEOUT_SECONDS`, `LLM_MAX_RETRIES`), tuned so a hung call surfaces as a typed `LLMTransportError` (which the coach pipeline degrades to the default plan) in seconds — not ~10 minutes.
- SDK import and client construction stay LAZY (first `complete()`), never at module import or at `AnthropicGateway.__init__` — importing this module or the factory must never load the `anthropic` SDK, and the credential gate (`LLMNotConfiguredError` when `ANTHROPIC_API_KEY` is absent) still fires at construction before any SDK touch.
- `STREAMING_MAX_TOKENS` must stay strictly below the SDK's effective non-streaming `max_tokens` ceiling for EVERY model `route_model` can return, pinned by a test that derives that ceiling from the installed SDK's real limits (the per-model cap dict and the time-based bound) — never a hardcoded literal.
- The gateway remains the sole `anthropic` caller (AD-6) with deterministic routing (`route_model`: Opus-4.8 for `hard_reasoning`, Sonnet-4.6 otherwise); the typed-error contract (a raw `anthropic.*`, `json.JSONDecodeError`, or `StopIteration` NEVER escapes the port) is unchanged and un-weakened.
- The fake path is unaffected: `LLM_ADAPTER=fake` never imports the SDK, never constructs a client, and needs no new config.

**Block If:**
- Delivering an explicit timeout/retry would require changing the `LLMRequest`/`LLMResponse` port contract, the routing, or the typed-error taxonomy — HALT rather than redefine the port.

**Never:**
- Never set a timeout so short it would spuriously downgrade a legitimate Opus hard-reasoning completion to the default plan (the default must clear a normal interactive structured call with margin); the value stays env-tunable for 7.6 real-world tuning.
- Never widen the transport `except` to swallow programming/`ValueError`s (the prior review deliberately narrowed `(anthropic.APIError, ValueError)` → `anthropic.APIError`); the coupling is enforced by the pinning test, not by re-broadening the catch.
- Never introduce a new `LLMError` subtype, a schema/migration change, a background scheduler, or streaming-surface leakage; never log the API key or prompt/response bodies.
- Never construct or reuse a client across two DIFFERENT API keys (a key change must yield a fresh client, so a rotated key is honored, not served from a stale pool).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Client built once, reused | Configured gateway, two `complete()` calls | `anthropic.Anthropic(...)` constructed exactly once; second call reuses the cached client | n/a |
| Explicit timeout/retry passed | `LLM_REQUEST_TIMEOUT_SECONDS`/`LLM_MAX_RETRIES` set | Client ctor receives those exact `timeout=`/`max_retries=` values | n/a |
| Hung call degrades fast | Live call exceeds the timeout | SDK raises `APITimeoutError` (an `anthropic.APIError`) within the budget → `LLMTransportError` → coach default plan | Fenced by existing `except anthropic.APIError` |
| Cross-request reuse | `get_llm_gateway()` twice, same key, `LLM_ADAPTER=anthropic` | Same cached `AnthropicGateway` instance returned (pooled client reused) | n/a |
| Key change | `ANTHROPIC_API_KEY` changes between factory calls | A fresh gateway/client built for the new key (no stale-pool reuse) | n/a |
| Streaming ceiling pinned | For each routed model | `STREAMING_MAX_TOKENS` < the SDK's effective non-streaming ceiling | Test fails loudly if a threshold/route/SDK change breaks it |
| Unconfigured | No `ANTHROPIC_API_KEY` | `LLMNotConfiguredError` at construction (no SDK import) | Unchanged gate |
| Fake path | `LLM_ADAPTER=fake` | `FakeLLMGateway`, no SDK, no client, no new config read | Unchanged |

</intent-contract>

## Code Map

- `ballast/backend/api/config.py` -- `Settings`: **add** `LLM_REQUEST_TIMEOUT_SECONDS: float = 60.0` and `LLM_MAX_RETRIES: int = 2` in the `--- LLM / Story 4.1 ---` block, with docstrings noting the interactive-`/recommend` envelope and 7.6 tuning. 60s clears a normal Opus structured completion with margin yet is far below the SDK's ~10 min; 2 retries makes the SDK default explicit.
- `ballast/backend/llm/anthropic_adapter.py` -- `AnthropicGateway.__init__` reads the two new settings alongside `_api_key`. `complete()` builds the client ONCE (lazy, on first call) with `timeout=`/`max_retries=` and caches it on the instance (e.g. `self._client`); subsequent calls reuse it. The lazy SDK import + `LLMNotConfiguredError` gate + the `STREAMING_MAX_TOKENS` routing and typed-error handling are otherwise unchanged.
- `ballast/backend/llm/factory.py` -- `get_llm_gateway`: memoize the `AnthropicGateway` at module level keyed by the current `ANTHROPIC_API_KEY` so the pooled client is reused across requests; a key change rebuilds; the fake branch stays uncached. Add a module-level cache-reset helper for test isolation.
- `ballast/backend/llm/models.py` -- `route_model` / `DEFAULT_MODEL` / `HARD_REASONING_MODEL` (referenced; the set of routable model ids the pinning test iterates; unchanged).
- `ballast/backend/llm/port.py` -- `LLMTransportError` and the typed-error contract (referenced; unchanged).
- `ballast/backend/tests/test_llm_gateway.py` -- **UPDATE** the SDK-double factories (`_install_fake_client` at ~379, and the "must-not-be-called" doubles at ~585/~605) to accept the new `timeout`/`max_retries` kwargs (e.g. `def _factory(*, api_key, **_kwargs)`) so existing tests stay green; **ADD** the new coverage below.

## Tasks & Acceptance

**Execution:**
- [x] `ballast/backend/api/config.py` -- add `LLM_REQUEST_TIMEOUT_SECONDS: float = 60.0` and `LLM_MAX_RETRIES: int = 2` to `Settings` with documented defaults. -- makes the live-path timeout/retry budget explicit and env-tunable without a code change (mirrors the `DECISION_*`/`SMTP_*` tunable pattern).
- [x] `ballast/backend/llm/anthropic_adapter.py` -- read the two settings in `__init__`; in `complete()` construct `anthropic.Anthropic(api_key=..., timeout=..., max_retries=...)` once (lazy, cached on the instance) and reuse it on later calls. -- gives connection reuse and a bounded interactive latency so a hung call degrades to the default plan in seconds; preserves the lazy-import/no-SDK-at-construction and credential-gate contracts.
- [x] `ballast/backend/llm/factory.py` -- memoize the anthropic gateway keyed by the current API key (fake uncached; key change rebuilds; add a reset helper). -- reuses the pooled client across requests so "constructed once, not per-call" holds on the live `/recommend` path, while a rotated key is still honored.
- [x] `ballast/backend/tests/test_llm_gateway.py` -- update the SDK-double factories to accept `timeout`/`max_retries`; add tests: (a) the client is constructed exactly once across two `complete()` calls on one gateway; (b) the ctor receives the configured `timeout`/`max_retries`; (c) `get_llm_gateway()` returns the same cached anthropic gateway across calls with one key and a fresh one when the key changes; (d) the fake path reads/needs no new config and never builds a client. -- pins client-reuse, the timeout/retry wiring, and the factory memoization/key-change behavior.
- [x] `ballast/backend/tests/test_llm_gateway.py` -- add a regression test that, for every model `route_model` can return (both `hard_reasoning` branches), asserts `STREAMING_MAX_TOKENS` is strictly below the installed SDK's effective non-streaming `max_tokens` ceiling for that model, derived from the SDK's real limits (per-model cap dict and the time-based bound), not a hardcoded number. -- fails the suite if a future threshold bump, a routing change to a capped model id, or an SDK upgrade would let a raw `ValueError` escape `complete()`.

**Acceptance Criteria:**
- Given `LLM_ADAPTER=anthropic` and a configured gateway, when `complete()` is invoked more than once, then the Anthropic SDK client is constructed exactly once and reused, and it was constructed with the `timeout` and `max_retries` from `Settings`.
- Given a live call that hangs, when it exceeds `LLM_REQUEST_TIMEOUT_SECONDS`, then the SDK raises within the budget and the failure surfaces as an `LLMTransportError` the coach degrades to the default plan — never a multi-minute stall.
- Given `get_llm_gateway()` is called repeatedly under `LLM_ADAPTER=anthropic`, when the API key is unchanged, then the same gateway (pooled client) is reused; when the key changes, then a fresh gateway/client is built.
- Given the pinning test, when `STREAMING_MAX_TOKENS` is raised toward/above any routed model's SDK non-streaming ceiling, or `route_model` gains a capped model id, or the SDK lowers a limit, then the test fails.
- Given the full backend suite, when it runs, then the new coverage is green and no existing test regresses (routing, typed-error hardening, fake-path, credential gate, the large-`max_tokens` streaming route, and the SDK-double-based tests all stay green).

## Spec Change Log

_No bad_spec loopbacks — the intent contract and spec held through review. All review findings were caused-by-this-change patches (auto-fixed in the review pass), one forward-looking defer, or rejects; no code re-derivation from an amended spec._

## Review Triage Log

### 2026-08-01 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 3: (high 0, medium 2, low 1)
- defer: 1: (high 0, medium 1, low 0)
- reject: 5
- addressed_findings:
  - `[medium]` `[patch]` Reviewers (verified against installed anthropic 0.120.2) found the SDK only runs its non-streaming large-`max_tokens` `ValueError` guard when `client.timeout == DEFAULT_TIMEOUT` (`messages.py:1030`); the story's explicit `timeout=60.0` makes `client.timeout != DEFAULT_TIMEOUT`, so that raw `ValueError` is now structurally unreachable on the live path (shipped code is strictly *safer*, not buggy) — but the original canary's stated rationale was thereby falsified and its implementation scraped SDK source via `inspect.getsource`+regex+`eval` (fragile: could ERROR on cosmetic SDK edits or silently pass while validating its own re-derived formula). Rewrote the canary to call the SDK's REAL `_calculate_nonstreaming_timeout(STREAMING_MAX_TOKENS, MODEL_NONSTREAMING_TOKENS.get(model))` per routed model (the exact call `create()` makes) — network-free, self-validating (a control assertion proves the guard has teeth), and tracking any SDK/limit/formula change — and documented that the explicit timeout independently bypasses this guard so the streaming-route + canary are defense-in-depth, not the sole guard.
  - `[medium]` `[patch]` Factory hardening: (a) built the gateway BEFORE swapping the single-slot cache so a construction that raises (blank/undecryptable key) can no longer evict a healthy pooled gateway other requests are reusing; (b) widened the cache key from `ANTHROPIC_API_KEY` to the full transport identity `(api_key, timeout, max_retries)` so a retuned timeout/retry budget rebuilds instead of silently serving a stale-config pool (the client bakes those in at build) — closing the "env-tunable but frozen until key change/restart" gap.
  - `[low]` `[patch]` Config bounds: `LLM_REQUEST_TIMEOUT_SECONDS = Field(default=60.0, gt=0)` and `LLM_MAX_RETRIES = Field(default=2, ge=0)` — a 0/negative timeout or negative retry budget now fails loud (consistent with the module's fail-loud posture) instead of reaching the SDK as a degenerate value; doc-noted the value applies when the pooled client is (re)built.
- notes: Blind Hunter + Edge Case Hunter on the committed-to-be 7.4 diff. **1 defer (medium):** the now process-shared gateway/client and the module-level factory cache have no explicit locking — benign under today's single-threaded event loop (sync `get_llm_gateway`/`complete()` have no `await` points), but if Story 7.5 offloads `complete()` or the read path onto a threadpool, the lazy `self._client` build and the cache read-modify-write become real races; logged for 7.5. **5 rejects:** (a) "the `>STREAMING_MAX_TOKENS` streaming branch is dead code" — it is NOT dead; it still routes genuinely-large requests to streaming, avoiding a 60s-timeout on a big non-streaming call, and is live defense-in-depth if the explicit timeout is ever removed; (b) "the per-model-cap limb of the canary is dead because both routed ids are absent from `MODEL_NONSTREAMING_TOKENS`" — the `min(time_bound, cap)` correctly covers a FUTURE route to a capped id, which is the point of pinning it; (c) `_parse_message` `AttributeError` on an unexpected SDK Message shape escaping the transport fence — PRE-EXISTING, unchanged by this diff, and backstopped by the coach pipeline's broad `except`; (d) the "no key raises without importing SDK" test being toothless because the test module imports `anthropic` at top — PRE-EXISTING test property, behavior still correct; (e) the `**_kwargs`-widened doubles "swallowing" a missing transport arg — the two dedicated tests assert `timeout`/`max_retries` equality, so a dropped kwarg is caught. Verification: `pytest tests/test_llm_gateway.py` → 34 passed; full backend suite → 446 passed.

## Design Notes

- **Where the client lives.** The client (an httpx connection pool) must outlive a single request for reuse to matter. Two layers: (1) cache it on the gateway instance so repeated `complete()` calls don't rebuild it; (2) memoize the gateway in the factory (single slot, keyed by the full transport identity `(api_key, timeout, max_retries)`) so the same instance — and pool — serves successive `/recommend` requests. Keying on the transport identity keeps the fix connection-reusing in prod (stable config) while a rotated key OR a retuned budget transparently rebuilds — avoiding a stale-config pool and keeping `get_settings()`'s deliberately-uncached, env-driven behavior intact. The gateway is built before the slot is swapped, so a construction that raises never evicts a healthy pooled instance.
- **Lazy still.** Build the client on first `complete()` (after the lazy `import anthropic`), not in `__init__` — constructing it in `__init__` would force the SDK import at construction and break the "importing the module/factory never loads the SDK" contract and the no-key-still-imports-clean tests. `__init__` only reads the (cheap) settings and runs the credential gate.
- **Timeout is a tunable, not a hardcode.** Picking the exact seconds is a product/ops call finalized against real latency at Story 7.6; here it is a documented `Settings` default (clears a normal interactive Opus structured completion with margin, far under the SDK's ~10 min) that ops can override by env. This is why it is not a `Block If` — the value is configurable and the go-live tuning is a separate gated step.
- **Pin, don't re-broaden.** The prior review narrowed the transport `except` to `anthropic.APIError` on purpose (over-catching `ValueError` hides programming errors). So the large-`max_tokens` `ValueError` risk is closed by a *test* that calls the SDK's real guard (`_calculate_nonstreaming_timeout`, the exact function `create()` invokes) per routed model — a canary that fires on any threshold/route/SDK drift — rather than by widening the catch. Calling the SDK's own function (not re-deriving `21333`) means an SDK upgrade that lowers a limit or reshapes the formula also trips it. **Interaction (found in review):** the explicit client `timeout` independently disables this SDK guard entirely (the SDK only runs it when `client.timeout == DEFAULT_TIMEOUT`), so on the live path the raw `ValueError` is already unreachable — the streaming route + canary are defense-in-depth that keep the port safe if the explicit timeout is ever removed.
- **Test-double gotcha.** Adding `timeout=`/`max_retries=` to the client ctor breaks the existing `_factory(*, api_key)` SDK doubles (they accept only `api_key`) — every anthropic test would `TypeError`. Widen them to `(*, api_key, **_kwargs)` (or capture the kwargs where asserted) as part of this change.

## Verification

**Commands:**
- `cd ballast/backend && uv run python -m pytest tests/test_llm_gateway.py -v` -- expected: new client-reuse, timeout/retry-wiring, factory memoization/key-change, and `STREAMING_MAX_TOKENS`-ceiling pinning tests pass; all existing gateway/routing/typed-error/streaming tests stay green. (Actual: 34 passed.)
- `cd ballast/backend && uv run python -m pytest` -- expected: full backend suite green, no regression (requires `docker compose up -d db`). (Actual: 446 passed.)

## Auto Run Result

Status: done

**Summary of implemented change:** Story 7.4 — hardened the credential-gated live LLM path (`LLM_ADAPTER=anthropic`). (1) `AnthropicGateway` now builds its `anthropic.Anthropic` client ONCE (lazily on first `complete()`, cached on the instance) with an explicit `timeout`/`max_retries` from `Settings`, so a hung call surfaces as a typed `LLMTransportError` the coach degrades to the default plan in seconds instead of the SDK's ~10-minute default. (2) The factory memoizes the gateway (single slot keyed by the transport identity `(api_key, timeout, max_retries)`) so the pooled client is reused across `/recommend` requests; a rotated key or retuned budget rebuilds, and the gateway is built before the slot is swapped so a raising construction never evicts a healthy pool. (3) The `STREAMING_MAX_TOKENS` ↔ SDK non-streaming-ceiling coupling is pinned by a canary that calls the SDK's REAL `_calculate_nonstreaming_timeout` per routed model. Lazy-import / no-SDK-at-construction and the credential gate are preserved; no schema/migration/new-error-type/scheduler change; the gateway stays the sole Anthropic caller (AD-6) with deterministic Opus-4.8/Sonnet-4.6 routing.

**Files changed:**
- `api/config.py` — added `LLM_REQUEST_TIMEOUT_SECONDS: float = Field(60.0, gt=0)` and `LLM_MAX_RETRIES: int = Field(2, ge=0)` (fail-loud bounds); imported `pydantic.Field`.
- `llm/anthropic_adapter.py` — `__init__` reads the two settings and sets `self._client = None`; `complete()` builds the client once with `timeout`/`max_retries` and reuses it.
- `llm/factory.py` — single-slot memo of the anthropic gateway keyed by `(api_key, timeout, max_retries)`; build-before-evict; `_reset_llm_gateway_cache()` test helper.
- `tests/test_llm_gateway.py` — widened SDK-double factories to accept the new kwargs; added client-built-once, ctor-args, factory pooling/rebuild, and fake-path tests; behavioral `STREAMING_MAX_TOKENS`-ceiling canary calling the SDK's real guard.
- `tests/test_coach_pipeline.py` — widened one pre-existing SDK double to `**_kwargs` (same ctor-kwargs root cause).

**Review findings breakdown:** 0 intent_gap, 0 bad_spec, 3 patch (2 medium, 1 low), 1 defer (medium), 5 reject. Patches applied this pass: rewrote the ceiling canary from fragile `inspect.getsource`+regex+`eval` source-scraping to a behavioral probe of the SDK's real `_calculate_nonstreaming_timeout` (self-validating; documents that the explicit timeout independently disables the SDK guard, so the streaming route + canary are defense-in-depth); factory build-before-evict + transport-identity cache key (a raising build can't evict a healthy pool; a retuned budget rebuilds instead of serving a stale-config pool); config `Field` bounds. Deferred (medium): the process-shared gateway/client and module-level factory cache have no locking — benign under today's single-threaded event loop but a real race if Story 7.5 offloads the call onto a threadpool; logged for 7.5. Rejects were an incorrectly-alleged "dead" streaming branch (it's live defense-in-depth), the intentionally-future-covering per-model-cap canary limb, a pre-existing backstopped `_parse_message` shape-drift, a pre-existing toothless import test, and adequately-covered `**_kwargs` doubles.

**Verification:** `uv run python -m pytest tests/test_llm_gateway.py` → 34 passed; full backend suite `uv run python -m pytest` → 446 passed (against docker Postgres), 2 unrelated pre-existing deprecation warnings. The pivotal review finding (explicit timeout disables the SDK's non-streaming `ValueError` guard) was verified empirically against the installed anthropic 0.120.2 before triage.

**Residual risks:** (1) The deferred locking gap (above) — no runtime impact today, must be closed if/when the LLM call moves off the event loop in 7.5. (2) The exact `LLM_REQUEST_TIMEOUT_SECONDS` default (60s) is a placeholder tuned by reasoning, not measured latency — it is env-tunable and the real value is confirmed against live latency at the Story 7.6 gated exercise. (3) The whole live LLM path remains credential-gated and unexercised against the real API until 7.6.

**Follow-up review recommended:** false — the review-pass fixes are localized to the LLM gateway seam, fully covered by the green suite (34 gateway / 446 total), and none change the port contract, schema, or data path; the core config/adapter/factory behavior was already correct and the patches are strict robustness/coherence improvements.
