---
baseline_commit: fec8ae7982a37800b04ae266fe90df6bb60ec60d
---

# Story 8.1: Marketable Limit Orders & Full Order-Model Enums (Order Interface Expansion — Story A of 3)

Status: review

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a user approving a trade,
I want to place a marketable LIMIT order (with an explicit limit price) through the same propose→approve→place→reconcile→cosign path I already use for market orders,
so that I can cap the price I pay/accept on an index-fund trade that still fills immediately — while the app stays calm and honest and the Coach never guesses a price for me.

**Feature context.** This is **Story A of a 3-story split** ("expand the ordering interface") that lands as a small **new Epic 8**, sequenced *before* the Story 7.7 real-money gate (see [[order-interface-expansion-plan]], [[epic7-go-live-plan]]):

- **A (this story)** — marketable LIMIT orders + the full order-model enums (forward-compat). Regular session, DAY only, fills immediately. Backend + API + tests, **no UI**.
- **B (deferred)** — resting-order lifecycle (working/pending co-sign + async re-reconcile), STOP/STOP_LIMIT, cancel, extended sessions (AM/PM), GTC. Resolves the 6.7 partial-fill terminality decision.
- **C (deferred)** — order-entry UI (`CoachConsult.jsx` / the SPA coach card) + extend the Story 4.5 self-destructive warnings for after-hours/stop/GTC footguns.

## Acceptance Criteria

1. **Marketable BUY LIMIT fills end-to-end (API + fake adapter).** Via `POST /api/coach/recommend` then `POST /api/coach/approve`, a user approves a marketable BUY LIMIT that fills immediately end-to-end; the co-signed `DecisionRecord`'s `cosign_snapshot.order_intent` **truthfully carries `order_type: "limit"` and the `limit_price`** (fixed-point string), and `cosign_snapshot.outcome.avg_price == limit_price`. The `/approve` response is HTTP 200 `filled`.
2. **A non-marketable limit is refused calmly (not a 500).** A BUY limit priced below the ask (or a SELL limit priced above the bid) is refused **before any order is recorded**, surfaced as a calm HTTP 422 whose message points the user to *"resting limit orders are coming later"* — never a raw 500, never a phantom fill. The atomic claim is released so the decision stays retryable.
3. **Deferred order features are each rejected with an explicit "not supported yet" error.** An `order_intent` with `order_type` in {`stop`, `stop_limit`}, OR `session` in {`am`, `pm`}, OR `duration == gtc` is rejected with a calm, explicit *"not supported in this version"* HTTP 422 — **before any broker call** — at both the API schema boundary and the execution gate.
4. **Schwab adapter builds the correct `equity_*_limit` payload (DAY / regular).** With a mocked schwab-py client (no live call), a BUY/SELL LIMIT calls `equity_buy_limit` / `equity_sell_limit` with `(symbol, floor(amount/limit_price), format_money(limit_price))` — the price is passed as a **fixed-point string** (see §CRITICAL below), and the built spec (`.build()`) asserts `orderType == "LIMIT"`, `price == format_money(limit_price)` (the string, e.g. `"100.00"` — **not** the raw `Decimal`), `session == "NORMAL"`, `duration == "DAY"`. The market path still calls `equity_buy_market` / `equity_sell_market` unchanged.
5. **Existing whole-share MARKET flow is byte-for-byte unchanged (regression tests green).** All existing tests pass untouched: `test_coach_api.py` (incl. the `schema_version == 1` and byte-identical snapshot assertions), `test_schwab_adapter.py` market cases, `test_recoverable_placement.py` (`_ORDER_INTENT_JSON` unchanged), `test_migrations.py`. A MARKET order's persisted `order_intent` snapshot remains exactly `{symbol, side, amount}` (no new keys).

### Field-requirement matrix (the gate contract)

| order_type | limit_price | stop_price | session | duration | Result |
|---|---|---|---|---|---|
| `market` (default) | MUST be absent/None | MUST be absent/None | `regular` | `day` | valid (existing flow) |
| `limit` | **REQUIRED, > 0** | MUST be absent/None | `regular` | `day` | valid → marketable guard at placement |
| `stop`, `stop_limit` | — | — | — | — | **reject: "not supported in this version"** (Story B) |
| any | — | — | `am` or `pm` | — | **reject: "not supported in this version"** (Story B) |
| any | — | — | — | `gtc` | **reject: "not supported in this version"** (Story B) |

Also unchanged and still enforced: `is_index_core(symbol)` must be true, and `amount` must be finite and `> 0`.

## Tasks / Subtasks

- [x] **Task 1 — Order model: full enums + extended `OrderIntent` (AC: 1, 3, 5)** — `ballast/backend/coach/recommendation.py`
  - [x] Add three `str`-backed enums (mirror the `OrderSide(str, Enum)` style already in the file): `OrderType` {`MARKET="market"`, `LIMIT="limit"`, `STOP="stop"`, `STOP_LIMIT="stop_limit"`}, `Session` {`REGULAR="regular"`, `AM="am"`, `PM="pm"`}, `Duration` {`DAY="day"`, `GTC="gtc"`}.
  - [x] Extend the frozen `OrderIntent` dataclass with **optional, defaulted** fields *after* the existing `symbol, side, amount` (so positional construction and every existing call site stay valid): `order_type: OrderType = OrderType.MARKET`, `limit_price: Decimal | None = None`, `stop_price: Decimal | None = None`, `session: Session = Session.REGULAR`, `duration: Duration = Duration.DAY`. Money fields are `Decimal` (never float).
  - [x] **Do NOT touch `RECOMMENDATION_OUTPUT_SCHEMA` or `recommendation_from_output`'s `order_intent` parsing** — the LLM contract stays market-only (`additionalProperties: false`, only `symbol/side/amount`). This is what *structurally* enforces the locked decision "the LLM coach never sets a limit price." `recommendation_from_output` constructs `OrderIntent(...)` with the 3 fields; the new fields take their MARKET defaults automatically. Add a one-line comment stating this is deliberate.
- [x] **Task 2 — Execution gate: field-matrix + deferred-feature rejection (AC: 1, 2, 3)** — `ballast/backend/coach/execution.py`
  - [x] Add `class OrderNotSupportedError(ValueError)` next to `OrderScopeError` (calm, deferred-feature refusal; docstring: mapped by the API to a calm 422, "not supported in this version").
  - [x] Add a pure, synchronous helper `validate_order_intent(intent: OrderIntent) -> None` (no I/O) enforcing the field-requirement matrix: reject `order_type in {STOP, STOP_LIMIT}`, `session in {AM, PM}`, `duration == GTC` with `OrderNotSupportedError`; for `MARKET` require `limit_price is None and stop_price is None`; for `LIMIT` require `limit_price is not None and limit_price.is_finite() and limit_price > 0` **and** `stop_price is None`. Field-matrix violations (bad price presence/absence) raise `OrderScopeError` (already a 422). Keep the messages calm and specific.
  - [x] Call `validate_order_intent(canonical_intent_or_intent)` inside `execute_approved_order` **after** the `_assert_session_integrity` check and the existing `is_index_core` + `amount > 0` gate, **before** `place_order`. Order matters: integrity first, then scope, then order-shape validation, then place — so the broker is never touched on any rejection. Preserve the existing symbol canonicalization (`replace(order_intent, symbol=normalized_symbol)` keeps the new fields via dataclass `replace`).
- [x] **Task 3 — Fake adapter: deterministic limit fill + marketable guard (AC: 1, 2)** — `ballast/backend/brokers/fake_adapter.py`
  - [x] Branch `place_order` on `order_intent.order_type`. **MARKET path stays byte-for-byte unchanged** (`filled_qty = amount / FAKE_FILL_PRICE`, fractional, no flooring, no <1 refusal — do NOT "improve" it; AC 5).
  - [x] LIMIT branch: treat `FAKE_FILL_PRICE` (`Decimal("100.00")`) as the deterministic reference quote for **both** ask and bid. Marketable check: BUY refuse if `limit_price < FAKE_FILL_PRICE`; SELL refuse if `limit_price > FAKE_FILL_PRICE`. On non-marketable, raise `OrderNotPlaceableError` with the deferral message ("…this limit isn't immediately fillable; resting limit orders are coming later — no order was placed."). Import `OrderNotPlaceableError` from `brokers.port`.
  - [x] LIMIT sizing: `quantity = floor(amount / limit_price)` (use `(amount / limit_price).to_integral_value(rounding=ROUND_FLOOR)` → int); if `< 1`, raise `OrderNotPlaceableError` calmly (mirror the schwab sub-minimum message). `filled_qty = Decimal(quantity)`, `avg_price = limit_price`. **Add `ROUND_FLOOR` to the import** — `fake_adapter.py:15` currently imports only `Decimal` (`from decimal import Decimal`); change to `from decimal import Decimal, ROUND_FLOOR`. (The schwab adapter already imports `ROUND_FLOOR` at `adapter.py:23` — no change there.)
  - [x] Keep the existing idempotency keying (`self._orders` / `self._orders_by_ref`) and the `broker_ref = f"fake-order-{idempotency_key}"` derivation for the LIMIT branch too — a re-place with a seen key returns the recorded outcome (no double-record). Stay fully deterministic (no wall-clock, no randomness).
- [x] **Task 4 — Schwab adapter: limit builders + bid/ask marketable guard (AC: 2, 4)** — `ballast/backend/brokers/schwab_adapter/adapter.py`
  - [x] In `place_order`, import `equity_buy_limit, equity_sell_limit` alongside the existing market builders (lazy import inside the method, AD-8).
  - [x] Generalize the quote read so a SELL limit can see the **bid**: `_quote_ask` today returns only `askPrice`. Add bid extraction (e.g. refactor to `_quote_ask_bid(client, symbol) -> tuple[Decimal, Decimal]` reading `quote["askPrice"]` and `quote["bidPrice"]`, or add a sibling `_quote_bid`). The MARKET path must keep sizing on the **ask** exactly as today. Reuse the existing unusable-quote refusal (`OrderNotPlaceableError` on missing/non-finite/≤0). For a LIMIT, only the side-relevant leg needs to be usable (buy→ask, sell→bid).
  - [x] LIMIT branch: `quantity = floor(amount / limit_price)` (same `ROUND_FLOOR` idiom as the market path); refuse `< 1` share via `OrderNotPlaceableError`. Marketable guard: BUY refuse if `limit_price < ask`; SELL refuse if `limit_price > bid` — message points to resting limits coming later. Then `builder = equity_buy_limit(symbol, quantity, format_money(limit_price)) if BUY else equity_sell_limit(symbol, quantity, format_money(limit_price))`.
  - [x] **CRITICAL — pass the limit price as a STRING, not a `Decimal`.** schwab-py's `OrderBuilder.set_price` (`schwab/orders/generic.py`) only stores a value verbatim when it is a `str`; a `Decimal`/float falls through to `truncate_float` → **binary-float** truncation (violates the project's Decimal money-discipline) and emits a deprecation `UserWarning`. So pass `format_money(limit_price)` (fixed-point string). Add `from money import format_money` to the adapter's imports (it is NOT currently imported there). `.build()` then emits `price` as the string `"100.00"` — AC 4 asserts against that string.
  - [x] Everything after `client.place_order(...)` (the post-placement fence, `_map_order`, reconcile, timeout/reject mapping) is **shared and unchanged** — the limit branch only changes how the builder + quantity are constructed. Do not duplicate the fence.
  - [x] Keep the passive `_preflight_capture("quote", data)` tap in the quote read (Story 7.6 must keep working).
- [x] **Task 5 — Snapshot serialization: truthful for LIMIT, byte-identical for MARKET (AC: 1, 5)** — `ballast/backend/coach/decision_record.py`
  - [x] Extend `_order_intent_json(order_intent)` to add `order_type`, `limit_price`, `stop_price`, `session`, `duration`. **Omit-when-default** (this is the key regression-safety decision — see Dev Notes §Serialization): emit `order_type` only when `!= MARKET`, `session` only when `!= REGULAR`, `duration` only when `!= DAY`, and `limit_price`/`stop_price` only when not `None`. Money via `_money`/`format_money`. Result: a MARKET intent serializes to exactly `{symbol, side, amount}` as today (AC 5); a LIMIT intent adds `order_type: "limit"` + `limit_price: "..."` (AC 1).
  - [x] **Do NOT bump `DECISION_RECORD_SCHEMA_VERSION`** (stays `1`): MARKET records are byte-identical and LIMIT records are a pure additive superset; replay is verbatim `dict` passthrough (`get_decision`), so no version-adaptation is needed and `test_coach_api.py`'s `schema_version == 1` assertion stays green. (If a reviewer insists on a bump, it must come with updating that assertion — but the recommendation is: keep it 1. Flagged as Question #1.)
  - [x] `_recovery_cosign_snapshot` (`decision_record.py:409`) passes the proposed `order_intent` JSON through verbatim — it already works with the richer dict, no change needed. Confirm. **Known degraded-recovery behavior (out of scope, do NOT try to "fix" here):** the recovery snapshot takes the executed intent from `recommendation_snapshot['order_intent']`, which is the coach's MARKET proposal — the human LIMIT override is entered at `/approve` and never enters the proposed snapshot. So a crash-orphaned LIMIT order forward-recovered by the Story 7.2 reclaimer would snapshot as MARKET. This is pre-existing (limit fields are human-entered by design) and not in this story's ACs; leave a one-line code comment noting it so a future reviewer doesn't flag it as a regression.
- [x] **Task 6 — API schema boundary: carry + validate the new fields (AC: 1, 2, 3)** — `ballast/backend/api/coach.py`
  - [x] Extend `OrderIntentIn` with the new optional fields (`order_type: OrderType = OrderType.MARKET`, `limit_price: Decimal | None = None`, `stop_price: Decimal | None = None`, `session: Session = Session.REGULAR`, `duration: Duration = Duration.DAY`). Import the enums from `coach.recommendation`.
  - [x] Add a Pydantic `model_validator(mode="after")` on `OrderIntentIn` that enforces the same field-requirement matrix (deferred-type / session / duration rejections and the market/limit price-presence rules) so a bad shape is a 422 at the boundary *before* the engine. Keep the messages consistent with the engine's. (Two layers by design — the spec calls for validating the matrix "at the schema boundary too"; the engine gate remains the authoritative one.) **Extend the pydantic import**: `api/coach.py:55` currently imports only `BaseModel` → change to `from pydantic import BaseModel, model_validator` (no existing validator in the file to copy; the pattern is standard pydantic v2).
  - [x] In `approve`, construct the engine `OrderIntent` carrying **all** the new fields from `body.order_intent` (not just symbol/side/amount) so the limit price actually reaches the adapter and the cosign snapshot.
  - [x] Extend `OrderIntentOut` (and `_order_intent_out`) so the recommend/replay wire shape can carry the new fields when present (omit-when-default / null-when-None, mirroring the snapshot). The Coach never proposes a limit, so `/recommend` output stays market-only in practice, but keep the schema forward-compatible.
  - [x] The `OrderNotSupportedError` from the engine must be mapped to a calm 422 in `approve` (add an `except OrderNotSupportedError` arm that **releases the claim** then raises `HTTPException(422, ...)`, symmetric with the existing `OrderScopeError` arm). **Placement matters:** put the new arm ABOVE the trailing `except Exception:` catch-all at `api/coach.py:560` (which releases the claim then re-raises → 500). Since `OrderNotSupportedError(ValueError)` is not caught by any existing typed arm, without this ordering it would fall through to the 500 path. `OrderNotPlaceableError` (non-marketable / sub-share) is already mapped to 422 with claim-release — reuse it. Also import `OrderNotSupportedError` from `coach.execution` (extend the existing `from coach.execution import (...)` block at `api/coach.py:80`).
- [x] **Task 7 — Tests (AC: 1–5)** — see Test Scope below. Add unit + adapter + integration coverage; keep every existing test green.

## Dev Notes

### Architecture: where each responsibility lives (READ THIS FIRST)

The single execution path is **`propose (/recommend) → approve (/approve) → coach.execution.execute_approved_order → BrokerPort.place_order → _reconcile → cosign`**. `execute_approved_order` (`coach/execution.py:145`) is the *sole* caller of `place_order` (AD-7). Responsibilities split cleanly, and this story must respect that split:

- **Structural order shape / trust** → `coach/recommendation.py` (`OrderIntent`, enums). Frozen value objects, money is `Decimal`.
- **Pre-placement gates (no I/O)** → `coach/execution.py::execute_approved_order`. Runs in this exact order and the broker is **never** touched if any fails: (1) `_assert_session_integrity`; (2) `is_index_core(symbol)` + `amount` finite & `> 0`; (3) **NEW** `validate_order_intent` (field matrix + deferred-feature rejection). Only then `place_order`.
- **Sizing + the marketable guard (needs a live quote)** → the **adapter** `place_order` (both `schwab_adapter/adapter.py` and `fake_adapter.py`). This is deliberately per-adapter because the quote is broker-specific. `execute_approved_order` does **not** size and does **not** fetch quotes.
- **Persistence / snapshots** → `coach/decision_record.py` (sole writer, AD-6). `_order_intent_json` is the only place the intent shape is serialized; `cosign` and `_recovery_cosign_snapshot` reuse it.
- **HTTP boundary** → `api/coach.py` (schemas + error→envelope mapping). Never calls the broker directly (AD-7).

### Files to touch (all UPDATE — no new files required except tests)

| File | Change |
|---|---|
| `ballast/backend/coach/recommendation.py` | +3 enums; extend `OrderIntent` with 5 optional defaulted fields. Leave LLM schema/parser market-only. |
| `ballast/backend/coach/execution.py` | +`OrderNotSupportedError`; +`validate_order_intent`; wire it into `execute_approved_order` before `place_order`. |
| `ballast/backend/brokers/fake_adapter.py` | Branch `place_order` on `order_type`; add LIMIT fill (avg_price=limit_price, floor sizing, marketable guard vs `FAKE_FILL_PRICE`). MARKET path untouched. |
| `ballast/backend/brokers/schwab_adapter/adapter.py` | Branch `place_order`; add limit builders + bid extraction for the sell-side guard; floor sizing on limit_price. MARKET path + post-placement fence untouched. |
| `ballast/backend/coach/decision_record.py` | Extend `_order_intent_json` (omit-when-default). Keep `DECISION_RECORD_SCHEMA_VERSION = 1`. |
| `ballast/backend/api/coach.py` | Extend `OrderIntentIn`/`OrderIntentOut` + matrix `model_validator`; carry new fields into the engine `OrderIntent` in `approve`; map `OrderNotSupportedError` → 422 (release claim). |

### Current-state notes on the code being modified (so nothing regresses)

- **`OrderIntent` is `@dataclass(frozen=True)`** with exactly `symbol, side, amount` today (`recommendation.py:40`). Adding defaulted fields *after* these keeps every existing positional/keyword construction valid (call sites: `execution.py`, `fake_adapter.py`, `schwab_adapter`, `api/coach.py`, `decision_record` reads). `dataclasses.replace(intent, symbol=...)` (used in `execute_approved_order`) preserves the new fields.
- **The fake MARKET path does NOT floor and does NOT refuse `< 1` share** — it returns fractional `amount / FAKE_FILL_PRICE` (`fake_adapter.py:165`). Whole-share flooring lives ONLY in the schwab adapter. This asymmetry is existing and intentional for the offline stand-in; **do not add flooring to the fake market path** or AC 5 breaks. The new fake LIMIT branch *does* floor (per spec sizing).
- **`_quote_ask` (`schwab_adapter/adapter.py:742`) returns only `askPrice`.** The MARKET path sizes on the ask for both buy and sell. A SELL-limit marketable guard needs the **bid** (`bidPrice`) — you must extend the quote read. Keep the existing unusable-ask refusal semantics for the market path and the buy-limit path.
- **The schwab post-placement region is a bare-`except Exception` FENCE** (`adapter.py:454`, `:505`) protecting against a duplicate REAL order. The limit branch only changes builder + quantity construction *before* `client.place_order`; do NOT move code into or out of the fence, and do NOT let the marketable guard / sizing raise anything other than `OrderNotPlaceableError` (a deliberate pre-placement refusal, caught before the fence exactly like the market sub-minimum case at `adapter.py:387`).
- **`OrderNotPlaceableError`** (`brokers/port.py:119`) is the established calm pre-placement refusal → API 422 + claim release (`api/coach.py:545`). Reuse it for both the sub-share refusal and the non-marketable refusal; only the *message* differs. Its docstring mentions "whole-share market order" — you may broaden the wording, but that's optional.
- **`approve` already releases the atomic claim on `OrderScopeError`/`OrderNotPlaceableError`/`SchwabAccountSelectionError`/any pre-placement exception** (`api/coach.py:540–568`). Add the `OrderNotSupportedError` arm to the same pattern.

### Serialization — the omit-when-default decision (regression-critical)

AC 1 ("cosign record truthfully carries `order_type=limit` + `limit_price`") and AC 5 ("MARKET flow byte-for-byte unchanged") are in tension *if* `_order_intent_json` always emits the new keys. Two existing tests are the guardrails that force the resolution:
- `tests/test_coach_api.py:1806` asserts `schema_version == 1`.
- `tests/test_recoverable_placement.py` pins `_ORDER_INTENT_JSON` and the byte-identical proposed/cosign snapshot.

**Resolution (locked recommendation): omit-when-default.** Emit `order_type/session/duration` only when non-default and `limit_price/stop_price` only when non-`None`. A MARKET intent → `{symbol, side, amount}` (identical to today, AC 5 holds, schema_version stays 1). A LIMIT intent → adds `order_type: "limit"`, `limit_price: "<fixed-point>"` (AC 1 holds). This is the extension of the spec's own "omit None cleanly" instruction to the enum defaults, and it's the only approach that satisfies both ACs without editing regression tests. See Question #1 if a reviewer wants a schema-version bump instead.

### schwab-py builder facts (verified against the installed SDK)

`schwab/orders/equities.py` (installed version) exposes:
- `equity_buy_market(symbol, quantity)` / `equity_sell_market(symbol, quantity)` — used today.
- `equity_buy_limit(symbol, quantity, price)` / `equity_sell_limit(symbol, quantity, price)` — **use these**. They build an `OrderBuilder` with `OrderType.LIMIT`, `.set_price(price)`, `Session.NORMAL`, `Duration.DAY`, `OrderStrategyType.SINGLE`, single equity leg. This exactly matches the story's "DAY / regular / marketable-only" constraint with **no extra configuration** — do not hand-roll a builder.
- **`price` MUST be a fixed-point STRING (`format_money(limit_price)`), never a `Decimal`.** `OrderBuilder.set_price` (`schwab/orders/generic.py`) stores a value verbatim ONLY when `isinstance(price, str)`; a `Decimal` (or float) hits `truncate_float` → binary-float truncation + a deprecation `UserWarning` (both wrong for this money-disciplined codebase). `.build()` then serializes `price` as a string regardless, so an AC-4 assertion must compare against the string `"100.00"`, not `Decimal("100.00")` (`Decimal("100.00") == "100.00"` is `False`).
- For AC 4, the cleanest assertion is two-layered: (a) record the `(symbol, quantity, price)` args passed to the builder (extend `_record_builders` in `test_schwab_adapter.py:162` with **new 3-arg** recorder funcs for the limit builders — the market recorders are 2-arg); and (b) in a separate focused test, call the REAL `equity_buy_limit("VOO", 2, "100.00").build()` and assert `orderType == "LIMIT"`, `price == "100.00"`, `session == "NORMAL"`, `duration == "DAY"` — proving the payload shape without a network call.

### Locked decisions (do NOT re-litigate — from [[order-interface-expansion-plan]])

- Order type / limit_price / stop_price / session / duration are **human-entered overrides on the `/approve` path**. The LLM coach proposes only simple MARKET `OrderIntent`s and MUST NOT set a limit price. (Enforced structurally by leaving `RECOMMENDATION_OUTPUT_SCHEMA` market-only.)
- Keep the `is_index_core(symbol)` restriction for limit orders too (it already runs for every intent in `execute_approved_order`).
- Backward compatible via defaults (`order_type=MARKET`, `session=REGULAR`, `duration=DAY`); existing market flow unchanged.
- **No DB migration** — order detail lives in the JSON `recommendation_snapshot` / `cosign_snapshot` columns. Confirm this holds: adding keys to a JSON column is not a schema change, and no new SQL column is introduced. (`broker_ref` VARCHAR(64) is untouched by this story.)
- Story A is fill-immediate: the marketable guard is what keeps it so. Resting/working limits, cancel, stop, AM/PM, GTC are all **Story B**.

### Out of scope (Story A) — hard boundaries

- Resting/working limit orders, STOP & STOP_LIMIT execution, cancel (BrokerPort has no cancel today), async re-reconcile of a working order, extended sessions, GTC → **Story B**.
- Any UI / order-entry form / beginner-safety warnings for the new types → **Story C**.
- The Coach/LLM ever proposing a non-market order → never (this story keeps it structurally impossible).
- Changing the reconcile/idempotency/atomic-claim machinery — limit orders flow through the *existing* place→reconcile→cosign path untouched.

### Testing standards

- Framework: `pytest` + `pytest.mark.asyncio` (async adapter/API tests), `httpx`/FastAPI `TestClient` patterns as in `tests/test_coach_api.py`. Run from `ballast/backend` (`uv run pytest` or the project's configured runner).
- Determinism: no wall-clock, no randomness in adapters/tests (the fake is fully deterministic — assert exact `Decimal` values). Money assertions use `Decimal(...)`, never float.
- Mocking schwab-py: follow `tests/test_schwab_adapter.py` (`_FakeClient`, `_install_client`, `_record_builders`, `_set_order_id`, `_configured_schwab_env`) — no live network, ever.
- Keep every existing test green (AC 5). Do not edit `schema_version`/`_ORDER_INTENT_JSON` assertions.

## Test Scope

**Unit — `coach/recommendation.py` + `coach/execution.py` (new: `tests/test_order_intent_validation.py` or extend `test_coach_pipeline`/a new file):**
- Enum values are the lowercase strings above; `OrderIntent` defaults are `MARKET/None/None/REGULAR/DAY`; existing 3-arg construction still works.
- `validate_order_intent`: MARKET with a `limit_price` → `OrderScopeError`; LIMIT without `limit_price` (or `<= 0`, or non-finite) → `OrderScopeError`; LIMIT with a `stop_price` → `OrderScopeError`; `STOP`/`STOP_LIMIT`/`AM`/`PM`/`GTC` each → `OrderNotSupportedError`; a clean MARKET and a clean LIMIT pass.

**Adapter — fake (`tests/` new or extend):**
- Marketable BUY LIMIT (`limit_price >= 100`) → `FILLED`, `filled_qty == floor(amount/limit_price)`, `avg_price == limit_price`, stable `broker_ref`; idempotent re-place with same key returns the same outcome.
- Marketable SELL LIMIT (`limit_price <= 100`) → fills.
- Non-marketable BUY (`limit_price < 100`) and non-marketable SELL (`limit_price > 100`) → `OrderNotPlaceableError` (deferral message), nothing recorded.
- Sub-share LIMIT (`floor(amount/limit_price) < 1`) → `OrderNotPlaceableError`.
- MARKET fill unchanged (regression) — fractional `amount/100`, no refusal.

**Adapter — schwab (`tests/test_schwab_adapter.py`):**
- BUY LIMIT: `equity_buy_limit("VOO", floor(amount/limit_price), "<fixed-point>")`; SELL LIMIT uses `equity_sell_limit`. **Extend `_record_builders` (`test_schwab_adapter.py:162`) with NEW 3-arg recorder funcs** for the limit builders — the existing `_buy`/`_sell` take `(symbol, quantity)` (2 args); the limit builders take `(symbol, quantity, price)`. Do not reuse the 2-arg funcs.
- **SELL-LIMIT fixtures must add `bidPrice`.** The `_FakeClient` quote fixtures today carry `askPrice` only (e.g. `{"VOO": {"quote": {"askPrice": 100}}}`); a SELL-limit marketable guard reads the bid, so add `bidPrice` to those quote dicts for the sell tests.
- Built payload assertion (call the REAL builder, not the recorder): `equity_buy_limit("VOO", 2, "100.00").build()` → `orderType == "LIMIT"`, `price == "100.00"` (string), `session == "NORMAL"`, `duration == "DAY"` (AC 4).
- Non-marketable BUY (limit < ask) and SELL (limit > bid) → `OrderNotPlaceableError`, no `place_order` call.
- Sub-share limit → `OrderNotPlaceableError`.
- Market cases (existing) still green.

**Integration — `tests/test_coach_api.py`:**
- `/recommend` (market) unchanged — still returns a market `order_intent`, `schema_version == 1`, byte-identical snapshot (regression).
- `/approve` with a human LIMIT override (marketable) → 200 `filled`; `GET /decisions/{id}` shows `cosign_snapshot.order_intent.order_type == "limit"` + `limit_price`, and `outcome.avg_price == limit_price` (AC 1).
- `/approve` with a non-marketable limit → 422 (deferral message), claim released → re-approve possible (AC 2).
- `/approve` (or the `OrderIntentIn` boundary) with `stop`/`stop_limit`/`am`/`pm`/`gtc` → 422 "not supported in this version" (AC 3), no broker call.

### Project Structure Notes

- No new modules required beyond test files. All production changes are edits to the six files listed above.
- Enum string values chosen lowercase to match the existing `OrderSide` convention (`"buy"/"sell"`) and to serialize cleanly into JSON snapshots.
- This story adds **Epic 8** to `sprint-status.yaml` (stories 8-1/8-2/8-3), sequenced before the 7.7 real-money gate. Epic 7 remains `in-progress` with only 7.7 backlog; Epic 8 is independent code work that does not touch the 7.x live-money seams.

### References

- [Source: _bmad-output/implementation-artifacts/sprint-status.yaml] — Epic 7 status; 7.7 is the real-money gate this work precedes.
- [Source: _bmad-output/planning-artifacts/epics.md#Story 7.6] — the live-exercise gate (renumbered 7.7 in sprint-status) that follows this epic.
- [Source: ballast/backend/coach/recommendation.py:29-98] — `OrderSide` enum + `OrderIntent` + `RECOMMENDATION_OUTPUT_SCHEMA` (keep market-only).
- [Source: ballast/backend/coach/execution.py:145-218] — `execute_approved_order` gate order + `_reconcile`; where `validate_order_intent` slots in.
- [Source: ballast/backend/brokers/fake_adapter.py:142-177] — fake `place_order` (idempotency + fractional market fill to preserve).
- [Source: ballast/backend/brokers/schwab_adapter/adapter.py:323-475] — schwab `place_order` (floor sizing, `_quote_ask`, post-placement fence); :742 `_quote_ask` (extend for bid).
- [Source: ballast/backend/coach/decision_record.py:85-91, 531-577] — `_order_intent_json`, `cosign`, `DECISION_RECORD_SCHEMA_VERSION`.
- [Source: ballast/backend/api/coach.py:118-135, 519-568] — `OrderIntentIn`/`OrderIntentOut`, `approve` claim/release + error mapping.
- [Source: .venv/.../schwab/orders/equities.py] — `equity_buy_limit`/`equity_sell_limit` signatures + DAY/NORMAL defaults.
- [Source: tests/test_schwab_adapter.py:162-239] — builder-recording + quote-mock patterns for AC 4.
- [Source: tests/test_coach_api.py:1806, tests/test_recoverable_placement.py] — the `schema_version==1` + `_ORDER_INTENT_JSON` regression guardrails (AC 5).
- [Memory: [[order-interface-expansion-plan]], [[epic7-go-live-plan]], [[epic6-live-trade-decisions]]] — locked decisions.

## Resolved Decisions (locked by MasterB 2026-08-04 — do NOT re-litigate)

1. **Serialization: omit-when-default, keep `DECISION_RECORD_SCHEMA_VERSION = 1`.** Emit `order_type/session/duration` only when non-default and `limit_price/stop_price` only when non-`None`. A MARKET intent stays byte-identical `{symbol, side, amount}` (AC 5); a LIMIT intent adds `order_type:"limit"` + `limit_price` (AC 1). No schema-version bump; the `schema_version == 1` / `_ORDER_INTENT_JSON` regression tests stay untouched.
2. **Non-marketable refusal reuses `OrderNotPlaceableError`** (a calm pre-placement 422 with claim release) — no dedicated `NonMarketableLimitError`. Only the message differs ("resting limit orders are coming later"). If Story C's UI later needs to distinguish it, it can branch on the message or a lightweight subtype introduced then.

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (Claude Opus 4.8, 1M context) — bmad-dev-story workflow.

### Debug Log References

- Full backend suite: `uv run pytest` (from `ballast/backend`). One PRE-EXISTING
  environmental flake unrelated to this story: `test_recommend_surfaces_fr11_warning`
  asserts the phrase "outsized share" appears in `/recommend` `reasoning`, but the
  test env has a live `ANTHROPIC` key so the pipeline uses the REAL Anthropic API
  (`POST https://api.anthropic.com/v1/messages`) and the LLM's free-text reasoning
  is non-deterministic. This story touches nothing on the pipeline/reasoning/FR11
  path (only the order model, adapters, snapshot serialization, and API schemas),
  so the flake is out of scope for Story 8.1's ACs.

### Completion Notes List

- **AC 1** — a marketable BUY LIMIT fills end-to-end via `/recommend`→`/approve`;
  the co-signed `cosign_snapshot.order_intent` truthfully carries
  `order_type: "limit"` + `limit_price` (fixed-point string) and
  `outcome.avg_price == limit_price`. (`test_approve_marketable_limit_cosigns_truthfully`)
- **AC 2** — a non-marketable limit is a calm HTTP 422 ("resting limit orders are
  coming later"), the atomic claim is released (record back to `proposed`), and a
  marketable retry then succeeds — never a 500, never a phantom fill.
  (`test_approve_non_marketable_limit_422_releases_claim` + adapter refusal tests)
- **AC 3** — `stop`/`stop_limit`/`am`/`pm`/`gtc` are each rejected with a calm 422
  "not supported in this version" BEFORE any broker call, at the `OrderIntentIn`
  schema boundary AND (unit-covered) the `validate_order_intent` execution gate.
  (`test_approve_deferred_features_rejected_422_no_broker`, `test_order_intent_validation`)
- **AC 4** — the schwab adapter builds `equity_buy_limit`/`equity_sell_limit` with
  `(symbol, floor(amount/limit_price), format_money(limit_price))`; the price is a
  fixed-point STRING and the REAL built payload asserts `orderType == "LIMIT"`,
  `price == "100.00"` (string), `session == "NORMAL"`, `duration == "DAY"`. Market
  path unchanged. (`test_place_order_*_limit_*`, `test_equity_*_limit_builds_*`)
- **AC 5** — the whole-share MARKET flow is byte-for-byte unchanged: all existing
  `test_coach_api.py` (incl. `schema_version == 1` + byte-identical snapshot),
  `test_schwab_adapter.py` market cases, `test_recoverable_placement.py`
  (`_ORDER_INTENT_JSON` unchanged), and `test_migrations.py` pass untouched. A
  MARKET intent's persisted `order_intent` stays exactly `{symbol, side, amount}`
  via the omit-when-default serializer; `DECISION_RECORD_SCHEMA_VERSION` stays `1`.
- **Locked decisions honored**: LLM contract left MARKET-only
  (`RECOMMENDATION_OUTPUT_SCHEMA` untouched); non-marketable refusal reuses
  `OrderNotPlaceableError`; sizing/marketable-guard live in the adapters, the
  field-matrix + deferred-feature gate lives in `execution.py` and is mirrored at
  the API boundary; degraded-recovery snapshot behavior left as-is with an explicit
  code comment.

### File List

- `ballast/backend/coach/recommendation.py` (M) — +`OrderType`/`Session`/`Duration`
  enums; +5 optional defaulted `OrderIntent` fields; LLM schema/parser left market-only.
- `ballast/backend/coach/execution.py` (M) — +`OrderNotSupportedError`;
  +`validate_order_intent`; wired into `execute_approved_order` before `place_order`.
- `ballast/backend/brokers/fake_adapter.py` (M) — LIMIT branch (`_limit_fill`):
  floor sizing, fill at limit price, marketable guard vs `FAKE_FILL_PRICE`; +`ROUND_FLOOR`
  import; MARKET path unchanged.
- `ballast/backend/brokers/schwab_adapter/adapter.py` (M) — LIMIT builders + bid/ask
  extraction (`_read_quote`/`_usable_price`); floor sizing on limit price; price as
  fixed-point string; MARKET path + post-placement fence unchanged.
- `ballast/backend/coach/decision_record.py` (M) — `_order_intent_json` omit-when-default;
  schema version kept at 1; degraded-recovery note on `_recovery_cosign_snapshot`.
- `ballast/backend/api/coach.py` (M) — `OrderIntentIn` fields + matrix `model_validator`;
  `OrderIntentOut`/`_order_intent_out` carry the new fields; `approve` carries all fields
  into the engine intent; `OrderNotSupportedError` → 422 with claim release.
- `ballast/backend/tests/test_order_intent_validation.py` (A) — enum/defaults +
  `validate_order_intent` matrix unit tests.
- `ballast/backend/tests/test_fake_adapter_limit.py` (A) — fake LIMIT fill / refusal /
  idempotency + MARKET-unchanged regression.
- `ballast/backend/tests/test_schwab_adapter.py` (M) — LIMIT builder recorders,
  bid-based sell guard, real-payload assertion, non-marketable/sub-share refusals.
- `ballast/backend/tests/test_coach_api.py` (M) — marketable-limit cosign,
  non-marketable 422 + claim release + retry, deferred-feature 422 (no broker).

## Change Log

- 2026-08-04 — Story 8.1 implemented: marketable LIMIT orders + full order-model
  enums (Story A of the order-interface expansion). Backend + API + tests, no UI.
  All ACs satisfied; existing MARKET flow byte-for-byte unchanged.
