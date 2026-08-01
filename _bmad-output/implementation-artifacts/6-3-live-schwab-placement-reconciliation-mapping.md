---
title: 'Story 6.3 — Live Schwab Placement & Reconciliation Mapping'
type: 'feature'
created: '2026-08-01'
status: 'done'
baseline_revision: 'ba9d81619b04dc10869828da77f8acf431d499b4'
final_revision: 'cab63c67528b0cf77e3e7d6a1099aab5e08932d5'
review_loop_iteration: 0
followup_review_recommended: true
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-6-context.md'
warnings: [oversized]
---

<intent-contract>

## Intent

**Problem:** `SchwabAdapter.place_order` / `get_order_status` / `fetch_portfolio` are credential-gated stubs that raise `SchwabNotConfiguredError` — the Coach can only move fake money. The real dollar→share sizing, the authenticated trading-client construction from the decrypted stored token, and the Schwab-JSON→`OrderOutcome` mapping do not exist; `broker_ref` is not a queryable column; and the Story 6.1 NULL-`idempotency_key` pre-flight guard is still deferred.

**Approach:** Wire `SchwabAdapter.place_order`/`get_order_status` against schwab-py 1.5.1 behind the existing execution-owner gates — build an authenticated trading client from the decrypted per-user token, size a **whole-share market order** (`floor(amount/ask)`, refuse calmly if < 1 share), place exactly once, and map Schwab order JSON → the normalized `OrderOutcome`/`OrderStatus`. Add `broker_ref` as a queryable column, add the NULL-`idempotency_key` pre-flight guard, and surface `pending` on a no-`order_id` timeout (never guess). `fetch_portfolio` stays deferred (Story 6.5). All ACs prove offline against a **mocked** schwab-py client (the Story 6.2 injection pattern); the one live paid placement is a manual go-live step.

## Boundaries & Constraints

**Always:**
- Only `brokers/schwab_adapter/` imports `schwab-py`, lazily inside methods (AD-8 sole-caller structural test stays green). `coach/execution.py` remains the sole caller of `place_order`/`get_order_status` (AD-7) and its session-integrity + provider-match + v1-index-scope + `amount>0` gates fire unchanged.
- Sizing is a **whole-share MARKET order**: fetch a Schwab quote (`get_quote`), `quantity = floor(amount / ask)`; `BUY → equity_buy_market(symbol, qty)`, `SELL → equity_sell_market(symbol, qty)`. Resolve the account hash (`get_account_numbers`) before placing; cache hash + client per adapter instance.
- Place **exactly once** per call (no poll/retry/wait-loop in the adapter). Reconcile the placed order via an in-instance `idempotency_key → order_id` cache + a single `get_order(order_id, hash)` read. `get_order_status` for an unknown/`None`-order_id key returns honest `PENDING` (`filled_qty=0`, `broker_ref=None`) — **never** `get_orders_for_account`, never attribute-matching, never a phantom fill.
- Build the trading client via `schwab.auth.client_from_access_functions(api_key, app_secret, token_read_func, token_write_func)` — **no disk, no network at construction**; the decrypted token is held in-memory only and never logged. The per-user token is loaded through the fail-closed `ScopedRepository` (AD-10) and decrypted with `decrypt_token`.
- Map broker outcomes truthfully: HTTP-error placement response (`resp.is_error` / `UnsuccessfulOrderException`) → `OrderStatus.REJECTED`; `httpx`/SDK **transport** exceptions (timeout/connect) → `OrderStatus.TIMEOUT` (indeterminate) with **no raw exception leaking the port and no phantom fill**; Schwab status strings → the `OrderStatus` enum; `broker_ref` = the Schwab order id. Money stays `Decimal`.
- Persist `broker_ref` as a **queryable column** on `decision_record` (in addition to the existing `cosign_snapshot` JSON) so a later explicit reconcile (Story 6.7) can find the order.
- NULL-`idempotency_key` pre-flight guard: `approve()` refuses calmly and **releases the claim** BEFORE any placement if the persisted `record.idempotency_key is None` (converts a post-fill crash into a pre-fill refusal).
- All ACs pass offline with zero credentials and zero network (mocked `client_from_access_functions` returning a crafted fake `Client`).

**Block If:**
- A live Schwab API call (real creds / real network / real money) is required to satisfy any acceptance criterion. All ACs are offline (mocked client). The one-time live paid placement against Schwab is a documented manual go-live step behind real `SCHWAB_*` creds + a live session — a credentials decision. If an AC cannot be met without it, HALT.

**Never:**
- No fractional shares, no notional/dollar order type, no custom retry/backoff, no `get_orders_for_account` fuzzy matching, no auto-search on a no-`order_id` timeout.
- No **durable cross-request** timeout reconciliation — that is Story 6.7. 6.3 surfaces `pending` in-request and persists the queryable `broker_ref` only.
- No `fetch_portfolio` / balances mapping (Story 6.5). No change to `OrderIntent`, the `OrderOutcome`/`OrderStatus` shapes, the port method signatures, or the fake adapter's behavior.
- Do not narrow the `approve()` claim-release net: any pre-placement failure must still release the claim (retryable), never strand a record in `cosigning`.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Happy buy | mocked client: `get_quote` ask=100, `place_order` returns Location w/ order_id, `get_order` status `FILLED` filledQty=2 avgPrice=100 | `OrderOutcome{FILLED, filled_qty=2, avg_price=100, broker_ref=<order_id>}`; `equity_buy_market(sym,2)` built (`floor(250/100)=2`) | No error |
| Sell path | `side=SELL`, ask=50, amount=200 | `equity_sell_market(sym,4)` built and placed | No error |
| Working order | `get_order` status `WORKING`/`QUEUED`/`PENDING_ACTIVATION` | `OrderOutcome{PENDING, filled_qty=0, broker_ref=<order_id>}`; reconcile re-reads once, still `PENDING` | No error |
| Sub-minimum | ask=500, amount=100 → `floor(100/500)=0` | raise `OrderNotPlaceableError` (calm reason); **no order placed** | typed; `approve()` → calm 422 + release claim |
| Unusable quote | ask missing / ≤ 0 | raise `OrderNotPlaceableError`; no order placed | typed; calm 422 + release claim |
| Broker rejection | `place_order` returns `resp.is_error` (4xx) | `OrderOutcome{REJECTED, filled_qty=0, broker_ref=None}` | truthful REJECTED at HTTP 200 |
| Transport timeout | `place_order`/`get_order`/hash raises `httpx.TimeoutException`/`ConnectError` | `OrderOutcome{TIMEOUT, ...}` → reconcile → `get_order_status(key)` | no raw exception escapes port; no phantom fill |
| No order_id | placement 2xx but no `Location` header (`extract_order_id → None`) | `OrderOutcome{TIMEOUT, broker_ref=None}`; cache `key→None`; reconcile → `PENDING`; cosign persists `broker_ref=None` | never auto-search |
| Reconcile unknown key | `get_order_status(key)` where key ∉ cache or maps to `None` | `OrderOutcome{PENDING, filled_qty=0, broker_ref=None}` | never search/guess |
| NULL idempotency_key | won claim but `record.idempotency_key is None` | `approve()` releases claim, calm 422; broker NEVER called | pre-flight refusal |

</intent-contract>

## Code Map

- `ballast/backend/brokers/port.py` -- ADD vendor-neutral `OrderNotPlaceableError(ValueError)` (raised when a whole-share market order cannot be constructed: `< 1` share, or no usable quote). Document it as a deliberate, calm pre-placement refusal distinct from the transport→`TIMEOUT` rule. No signature changes.
- `ballast/backend/brokers/schwab_adapter/adapter.py` -- IMPLEMENT `place_order` + `get_order_status`. `__init__(self, *, token_read_func: Callable[[], dict] | None = None)` (default `None` keeps auth-only usage working; trading methods require it → `SchwabNotConfiguredError` if `None`). Add `_trading_client()` (lazy `from schwab import auth`; `auth.client_from_access_functions(...)` with an in-memory no-op `token_write_func`; cache per instance), `_account_hash(client)` (lazy `get_account_numbers` → first `hashValue`; cache), `_quote_ask(client, symbol)` (`get_quote` → ask; raise `OrderNotPlaceableError` if missing/≤0), `_orders: dict[str,str|None]` (in-instance idempotency cache), and `_map_order(order_json) -> OrderOutcome` (Schwab `status` string → `OrderStatus`; extract `filledQuantity`/avg fill price as `Decimal`). `place_order`: build client → hash → ask → `qty=floor(amount/ask)` (refuse if `<1`) → `equity_buy_market`/`equity_sell_market` → `client.place_order(hash, spec)` → `resp.is_error` → REJECTED else `Utils(client,hash).extract_order_id(resp)` → cache `key→order_id` → single `get_order` read → `_map_order` (or TIMEOUT if `order_id is None`). Wrap `httpx`/SDK transport errors → `OrderStatus.TIMEOUT`, never let a raw exception escape. `get_order_status(key)`: `order_id=self._orders.get(key)`; `None`/absent → `PENDING`; else `get_order` → `_map_order`; transport → `TIMEOUT`.
- `ballast/backend/brokers/factory.py` -- ADD `get_execution_broker(scope, session, broker=Depends(get_broker)) -> BrokerPort`: pass fake through unchanged; when `broker` is a `SchwabAdapter`, load the current user's `BrokerageToken` via `ScopedRepository`, `decrypt_token` access+refresh, build the schwab-py token dict (`{access_token, refresh_token, token_type:"Bearer", expires_at:<epoch>}`), and return `SchwabAdapter(token_read_func=lambda: token_dict)`. Import `SchwabAdapter`/`decrypt_token`/repo lazily inside the function. Composing `get_broker` as a sub-dependency keeps `dependency_overrides[get_broker]` working in existing tests.
- `ballast/backend/api/coach.py` -- In `approve()`: switch `broker` dep from `get_broker` to `get_execution_broker`. After winning the claim + reloading (~line 447), ADD the NULL-`idempotency_key` pre-flight guard: `if key is None: await release_claim(...); raise HTTPException(422, <calm message>)` BEFORE building intent / calling `execute_approved_order`. ADD `except OrderNotPlaceableError as exc: await release_claim(...); raise HTTPException(422, str(exc)) from exc` before the generic `except Exception`.
- `ballast/backend/db/models.py` -- ADD `broker_ref: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)` to `DecisionRecord`. (Schema built via `create_all`; note it will not ALTER an existing table — go-live caveat, same as `idempotency_key`.)
- `ballast/backend/coach/decision_record.py` -- In `cosign()`: set `record.broker_ref = outcome.broker_ref` alongside the existing `cosign_snapshot` write (keep the JSON `outcome.broker_ref` too).
- `ballast/backend/.env.example` -- Note that `BROKER_ADAPTER=schwab` + real `SCHWAB_*` creds + a live session is the go-live placement path (mirrors the 6.2 LLM note).
- `ballast/backend/tests/test_schwab_adapter.py` (NEW) -- Mocked-SDK adapter tests: monkeypatch `schwab.auth.client_from_access_functions` (and the `schwab.orders.equities` builders / `schwab.utils.Utils.extract_order_id`) so the adapter's lazily-imported symbols return a crafted fake `Client`. Cover every I/O-matrix adapter row (happy buy, sell builder, working→pending, sub-minimum refusal, unusable quote, broker rejection, transport→TIMEOUT with no raw leak, no-`order_id`→TIMEOUT, reconcile unknown key→PENDING). Keep/extend the structural sole-caller test (only `schwab_adapter` imports `schwab`).
- `ballast/backend/tests/test_coach_api.py` -- ADD: NULL-`idempotency_key` approve → 422 + claim released + broker never called; a broker double raising `OrderNotPlaceableError` → 422 + claim released; assert `broker_ref` persists as a column after a cosign.
- `ballast/backend/tests/test_brokerage.py` (or a factory test) -- ADD `get_execution_broker`: fake passthrough; schwab path decrypts a stored token and constructs a `SchwabAdapter` with a `token_read_func` (mock `decrypt_token`).

## Tasks & Acceptance

**Execution:**
- [x] `ballast/backend/brokers/port.py` -- add `OrderNotPlaceableError`; document the calm-refusal vs transport→TIMEOUT distinction.
- [x] `ballast/backend/brokers/schwab_adapter/adapter.py` -- implement `place_order`/`get_order_status` per Code Map (client build, hash, quote sizing, whole-share market order, exactly-once place, in-instance key→order_id cache, `_map_order`, transport→TIMEOUT no-leak, sub-minimum/unusable-quote refusal).
- [x] `ballast/backend/brokers/factory.py` -- add `get_execution_broker` (fake passthrough; schwab token decrypt + `token_read_func` bind).
- [x] `ballast/backend/db/models.py` + `ballast/backend/coach/decision_record.py` -- add queryable `broker_ref` column; write it in `cosign()`.
- [x] `ballast/backend/api/coach.py` -- switch to `get_execution_broker`; add NULL-`idempotency_key` pre-flight guard + `OrderNotPlaceableError` calm-422 handling (both release the claim).
- [x] `ballast/backend/.env.example` -- document the `BROKER_ADAPTER=schwab` go-live path.
- [x] `ballast/backend/tests/test_schwab_adapter.py` -- add the mocked-SDK adapter tests (every I/O-matrix adapter row) + sole-caller structural test.
- [x] `ballast/backend/tests/test_coach_api.py` + `ballast/backend/tests/test_brokerage.py` -- add NULL-key guard, `OrderNotPlaceableError` release, `broker_ref` column persistence, and `get_execution_broker` token-bind tests.

**Acceptance Criteria:**
- Given `BROKER_ADAPTER=schwab`, a live session, and a mocked schwab-py client, when an in-scope order is approved, then the adapter builds an authenticated client from the decrypted token (no disk/network at construction), resolves the account hash, sizes a whole-share market order (`floor(amount/ask)`), places exactly once, and returns a normalized `OrderOutcome` with `broker_ref` set to the Schwab order id (FR8–FR10, FR22, FR23, AD-7/AD-11/AD-13).
- Given a placement that raises a transport error or returns no `order_id`, when the Coach Engine reconciles, then the outcome is surfaced as `pending`/`timeout`→`pending` with no raw exception escaping the port, no phantom or duplicate order, `broker_ref` persisted as a queryable column (`None` when unknown), and no fuzzy attribute-matching — durable cross-request reconciliation left to Story 6.7 (NFR3).
- Given a decision whose persisted `idempotency_key` is `None`, when it is approved, then `approve()` refuses calmly (422), releases the claim (retryable), and never touches the broker (Story 6.1 pre-flight guard).
- Given an amount that buys less than one whole share (or no usable quote), when the adapter sizes the order, then it raises `OrderNotPlaceableError`, no order is placed, the claim is released, and the user gets a calm clear message.
- Given the default config, when the whole suite runs, then it passes with zero credentials and zero network, only `schwab_adapter` imports the SDK, and the fake adapter path is unchanged (AD-8).

## Review Triage Log

### 2026-08-01 — Follow-up review pass (2)
- intent_gap: 0
- bad_spec: 0
- patch: 4 (high 2, medium 0, low 2)
- defer: 1
- reject: 7
- addressed_findings:
  - `[high]` `[patch]` Both reviewers (THE double-order guard): `place_order`'s post-placement region (extract-order-id → status read → `_map_order`) sat under a CURATED exception blocklist `(httpx.HTTPError, UnsuccessfulOrderException, AccountHashMismatchException, ValueError, ArithmeticError, KeyError, IndexError, TypeError)`. A blocklist can always miss a type — an SDK-specific error, an `AttributeError` from an unexpected response shape, a `RuntimeError` — and a miss AFTER a successful placement escapes the port, letting `approve` release the atomic claim and re-place a SECOND real order (Schwab honors no client key). Fixed by making it a FENCE, not a blocklist: split the `try` so the single placement write is the last statement of the first (typed, pre-placement) `try`, and everything after the write runs in a second `try` whose `except Exception:` returns TIMEOUT with the (possibly `None`) `order_ref` PRESERVED and cached. Pre-placement config errors (`SchwabNotConfiguredError`) and the calm `OrderNotPlaceableError` still surface unchanged. Added regression tests: a `RuntimeError` on the status read and an `AttributeError` in `extract_order_id`, both post-placement, now surface TIMEOUT/pending with the order placed exactly once.
  - `[high]` `[patch]` Both reviewers: `get_order_status`'s reconcile read had the SAME narrow-blocklist gap (even narrower — no `AttributeError`, no SDK types) over an order KNOWN to have been placed, so a raw leak there let `approve` re-place. Fixed identically — the reconcile read is now `except Exception:` returning TIMEOUT with the known `order_ref` preserved. Added a `RuntimeError`-on-reconcile regression test.
  - `[low]` `[patch]` Edge Case Hunter: `_account_hash` did `accounts[0].get("hashValue")`, so a non-dict first element (a bare string/number in a malformed body) raised a raw `AttributeError` past the port — the code comment falsely claimed a malformed body was already caught. Added an `isinstance(first, dict)` guard raising a clear `SchwabNotConfiguredError` (pre-placement, no order placed). Added a regression test.
  - `[low]` `[patch]` Blind Hunter: the NaN-`filledQuantity` test asserted `status in (TIMEOUT, PENDING)` — a disjunction that would hide a regression flipping the NaN path between the map-defensive PENDING and the fence TIMEOUT. Tightened to the exact deterministic `is OrderStatus.PENDING`.
- notes: Deferred (1) — a new atomicity gap: `approve` runs `cosign` + `commit` after a real placement outside any try/except and (correctly, for double-order safety) does NOT release the claim on a post-placement failure, so a cosign/commit raise strands the record in `cosigning` with a live order AND no persisted `broker_ref`, unrecoverable even by 6.7 (which keys on `broker_ref`). Rejected (7) — the CROSS-request double-place residual (already an existing 6.7 ledger entry; this pass closes the in-request escape vector but the released-claim-retry-from-a-new-request residual is genuinely 6.7's durable-dedup scope, untouched here), multi-account `accounts[0]` routing (already a 6.7 ledger entry), `broker_ref` production-migration path (already a ledger entry), `broker_ref` `VARCHAR(64)` truncation (speculative — Schwab order ids are short numerics), `_map_order` trusting `filled_qty>=quantity` → FILLED on a live status string (a DELIBERATE prior-pass decision, guarded by `quantity>0`), a negative `filledQuantity` mapping to PENDING (speculative — Schwab does not sign fill quantities), and `int(order_ref)` assuming numeric ids (degrades safely via the fence; id shape is fixture-driven/go-live). `expires_at=None` deref in `get_execution_broker` was re-raised by a reviewer but the column is `nullable=False` and a prior pass already rejected it — not re-actioned.

### 2026-08-01 — Follow-up review pass
- intent_gap: 0
- bad_spec: 0
- patch: 2 (high 0, medium 2, low 0)
- defer: 3
- reject: 9
- addressed_findings:
  - `[medium]` `[patch]` Both reviewers (arithmetic/NaN leak in the transport net): a broker numeric of `NaN`/`Infinity` parses to a valid-but-non-finite `Decimal` WITHOUT raising, so the existing catch never fires — but a later `<=`/`>=` comparison then raises `decimal.InvalidOperation`, which is an `ArithmeticError` and NOT a `ValueError`, so it was outside both methods' catch tuples. A NaN `askPrice` leaked a raw 500 pre-placement; a NaN `filledQuantity` in a status body could escape POST-placement and let `approve` release the claim and re-place a SECOND real order. Fixed at source (reject non-finite ask in `_quote_ask`; sanitize non-finite → 0 in `_decimal_or_zero`) AND belt-and-suspenders (added `ArithmeticError` to the `place_order` and `get_order_status` catch tuples). Added regression tests (NaN ask → calm refusal, no order placed; NaN filled-qty → no leak, order id preserved, placed once).
  - `[medium]` `[patch]` Both reviewers (`_map_order` FILLED-with-zero-quantity): a `"FILLED"` status string with a missing/zero `filledQuantity` forced `OrderStatus.FILLED`, persisting a contradictory "fill" that moved 0 shares with `avg_price=None`. Fixed: require a POSITIVE `filled_qty` before honoring FILLED (from the status string OR the `filled_qty >= quantity` inference, with the `quantity > 0` guard preserved for the missing-quantity partial case) — an incomplete body now degrades to honest PENDING (reconcilable). Added a regression test.
- notes: Deferred (3) — the money-bearing tail of the locked "durable reconciliation is Story 6.7" boundary (an unexpected exception escaping post-placement can still let a released-claim retry double-place, since Schwab honors no client key and the dedupe cache is in-request-only); v1 silently trading `accounts[0]` with no account selection/confirmation (wrong-account risk for multi-account logins); and the queryable `broker_ref` column having no production migration path (`create_all` won't ALTER an existing table). Rejected (9) — the provider-integrity check comparing a hardcoded class constant (can't detect a stale-row divergence; depends on the one-row invariant both funcs guard); `broker_ref` persisted on TIMEOUT/PENDING with no status qualifier (BY DESIGN — the intent persists the queryable ref and carries status in `cosign_snapshot`); `int(order_id)` vs raw-`order_id` type divergence between the two `get_order` call sites (degrades safely via the catch; exact id shape is fixture-driven/go-live per Design Notes); `_extract_avg_price` trusting a `"price"` fallback (best-effort/Optional by prior decision; market-order context; `filled_qty>0`-gated); unbounded whole-share `quantity` from a low quote (notional is inherently bounded by `amount` via `floor(amount/ask)*ask <= amount`); `expires_at=None` deref in `get_execution_broker` (column is `nullable=False`); `int(timestamp())` overflow on extreme datetimes (OAuth expiries are near-present); `int(order_ref)` on a corrupted cache value (caught → TIMEOUT); and a canceled order reporting `filled_qty>=quantity` mapping to FILLED (contradictory broker data; contradicts a deliberate prior "trust a full fill → FILLED" decision).

### 2026-08-01 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 6 (high 0, medium 2, low 4)
- defer: 1
- reject: 5
- addressed_findings:
  - `[medium]` `[patch]` Both reviewers: a landed order's known `order_id` was dropped (`broker_ref=None`) when the status read timed out — in `place_order` AND `get_order_status` — orphaning the order so Story 6.7 could never reconcile it. Fixed: hoisted `order_ref` to the outer scope and PRESERVE it in the timeout branch of both methods (None only when no id was ever obtained).
  - `[medium]` `[patch]` Both reviewers: the port's "no raw exception leaking" invariant held only for `httpx.HTTPError` — a `json.JSONDecodeError` (a `ValueError`), a malformed/empty account-numbers body (`Key/Index/TypeError`), or a non-dict status body (`AttributeError`) would escape; POST-placement that leak lets `approve` release the claim and re-place a SECOND real order (Schwab honors no client key). Fixed: broadened both methods' catch to `(httpx.HTTPError, UnsuccessfulOrderException, AccountHashMismatchException, ValueError, KeyError, IndexError, TypeError)` returning TIMEOUT-with-preserved-ref, and guarded `_map_order` against a non-dict body.
  - `[low]` `[patch]` `_account_hash` assumed a non-empty list with a `hashValue` — added explicit empty-list / missing-hash guards raising a clear `SchwabNotConfiguredError` (config error, never a phantom fill).
  - `[low]` `[patch]` `_map_order` mapped a fully-filled quantity (`filled_qty >= quantity`) to PENDING when the status string was a non-`FILLED` variant — now trusts a full fill → FILLED.
  - `[low]` `[patch]` `get_execution_broker` bound `rows[0]` instead of the max-expiry row the session gate trusts — aligned to `max(rows, key=expires_at)` so the placement client can never be built from a stale token row.
  - `[low]` `[patch]` `int(row.expires_at.timestamp())` assumed tz-aware — normalize a naive datetime to UTC before the epoch conversion.
- notes: Rejected (5) — the pre-POST (hash/quote) transport→terminal-`pending` consequence (challenges the locked "any transport failure → pending, never guess" intent-contract decision; safe/never-phantom, and the F1 fix makes the money-bearing post-POST case reconcilable), `avg_price=None` on a fill (Optional by design), a `client_from_access_functions` build-time leak (deliberately a plain config error outside the transport net), and two cosmetic notes. Deferred (1): a `decrypt_token` failure inside `get_execution_broker` surfaces as a raw 500 rather than the calm reconnect envelope.

## Design Notes

- **Why `get_execution_broker` composes `get_broker`.** The fake/API tests inject via `dependency_overrides[get_broker]`. Making `get_execution_broker` depend on `get_broker` (not replace it) keeps those overrides live: an injected fake/spy is not a `SchwabAdapter`, so it passes straight through untouched. Only the real schwab path pays the per-user token decrypt + `token_read_func` bind. This is the one new seam that hands the decrypted token to the otherwise-scopeless adapter.
- **In-request only; 6.7 owns durability.** The `idempotency_key → order_id` cache lives on the adapter instance (like the fake's `self._orders`) and dies with the request, so cross-request reconciliation is impossible by construction here — exactly the boundary the locked decision draws. A `pending` outcome co-signs (terminal) with `broker_ref` persisted, which is precisely how Story 6.7 later finds and resolves it.
- **Transport→TIMEOUT vs REJECTED vs refusal.** Three distinct failure classes, kept honest: an `httpx`/SDK **transport** exception is indeterminate → `TIMEOUT` (never phantom); an HTTP **error response** from `place_order` is a truthful broker `REJECTED`; a deliberate **can't-size** condition (`<1` share / no quote) is `OrderNotPlaceableError` → calm 422, claim released. Never collapse these.
- **Token dict shape is a go-live risk, not an AC.** We persist only `access_token`/`refresh_token`/`expires_at`; `client_from_access_functions` may want the fuller schwab-py token envelope. Offline tests mock `client_from_access_functions`, so the exact live token round-trip is verified manually at go-live (like 6.2's live paid call). If the minimal dict proves insufficient, persisting the full envelope is a follow-up.
- **Schwab order-JSON field names** (`filledQuantity`, average fill price location) are not verifiable from the SDK source on disk — `_map_order` reads documented Schwab REST fields; the exact mapping is fixture-driven and re-confirmed at go-live. Do not hardcode blindly; keep `_map_order` small and test it against a captured-shape fixture.

## Verification

**Commands:**
- `cd ballast/backend && uv run python -m pytest tests/test_schwab_adapter.py tests/test_coach_api.py -q` -- expected: all pass, zero network, zero credentials.
- `cd ballast/backend && uv run python -m pytest -q` -- expected: full suite green (no regressions; fake path unchanged).
- `cd ballast/backend && uv run python -c "import brokers.schwab_adapter"` -- expected: imports without loading `schwab-py` or crashing on missing creds.
- Confirm the structural sole-caller test still passes (only `schwab_adapter` imports the SDK).

**Manual checks (go-live, out of AC scope):**
- With real `SCHWAB_*` creds + `BROKER_ADAPTER=schwab` + a live session, approve one small in-scope order and confirm a real `OrderOutcome` reconciles truthfully (or surfaces `pending`). This paid placement is a credentials/real-money decision performed manually at go-live — not part of the automated suite.


## Auto Run Result

Status: done — follow-up review pass (2) on a previously-`done` spec.

**Summary of change (this pass).** A fresh two-reviewer adversarial + edge-case review of the frozen Story 6.3 diff. The dominant finding (raised independently by both reviewers) was that the money-bearing post-placement paths in `SchwabAdapter` were guarded by a CURATED exception blocklist rather than a fence: an exception type not in the tuple (an SDK-specific error, an `AttributeError` from an unexpected response shape, a `RuntimeError`) raised AFTER a real order lands would escape the port, and `approve`'s `except Exception` would then release the atomic claim and re-place a SECOND real order (Schwab honors no client key). Converted both the `place_order` post-placement region and the `get_order_status` reconcile read to `except Exception:` fences that return TIMEOUT with the order id PRESERVED, while keeping pre-placement config errors and the calm `OrderNotPlaceableError` surfacing unchanged. Plus two low patches (a non-dict account-body guard; a tightened NaN test assertion) and four new regression tests.

**Files changed (this pass):**
- `ballast/backend/brokers/schwab_adapter/adapter.py` — split `place_order`'s single `try` at the placement boundary; post-placement region is now a bare-`except` fence returning TIMEOUT with `order_ref` preserved/cached. `get_order_status` reconcile read converted to a bare-`except` fence likewise. `_account_hash` now guards a non-dict first element with a clear `SchwabNotConfiguredError`.
- `ballast/backend/tests/test_schwab_adapter.py` — added: non-enumerated (`RuntimeError`) post-placement exception is fenced; `extract_order_id` `AttributeError` is fenced → pending; `get_order_status` `RuntimeError` fenced with ref preserved; non-dict account element → config error. Tightened the NaN-`filledQuantity` assertion to the exact `PENDING`.

**Review findings breakdown (this pass):** 2 high patches (placement + reconcile fences), 2 low patches (account-body guard, test assertion), applied. 1 new deferral (post-placement cosign/commit atomicity gap → strands a placed-but-uncosigned record). 7 rejected (cross-request double-place residual + multi-account routing + `broker_ref` migration are already existing 6.7 ledger entries; the rest speculative or deliberate prior decisions). No intent_gap, no bad_spec — the spec already mandated "no raw exception leaking the port"; the code merely under-implemented it, so these were direct patches with no spec amendment and no implementation loopback.

**Verification.** `cd ballast/backend && uv run pytest -q` → 343 passed (was 337 pre-pass; +6 new/tightened tests, 0 regressions). Targeted: `tests/test_schwab_adapter.py` 26 passed, `tests/test_brokerage.py` 20 passed, `tests/test_coach_api.py` 54 passed. The structural sole-caller (AD-8) test remains green.

**Residual risks.** (1) The CROSS-request double-place residual is unchanged and remains Story 6.7's durable-reconciliation scope (existing ledger entry) — this pass closes the in-request escape but a released-claim retry from a NEW request still rebuilds a fresh adapter with an empty dedupe cache. (2) The new post-placement cosign/commit atomicity gap (deferred this pass) can strand a placed-but-uncosigned record with no persisted `broker_ref`. (3) All go-live token-envelope / Schwab-JSON-field-shape risks noted in Design Notes remain manual go-live checks. No live Schwab call is exercised by the suite.
