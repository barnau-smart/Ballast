---
title: 'Story 6.4 — Fixed-Point Money Serialization Pass'
type: 'bugfix'
created: '2026-08-01'
status: 'done'
baseline_revision: 'ce0898b7a0641411ca182c5fe7534744d7cd4d24'
final_revision: '99a826e61d2ba4c726f067b69db4cff1d9c5471c'
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-6-context.md'
warnings: [oversized]
---

<intent-contract>

## Intent

**Problem:** Money/Decimal values are serialized to the wire with bare `str(Decimal)` in several places, and Pydantic serializes `Decimal`-typed response fields the same way — so an extreme or tiny value surfaces as exponent notation (`"1E+27"`, `"1E-8"`) instead of a plain decimal. This violates the epic invariant "no exponent notation may cross the wire" and the deferred 4.6/4.7 money-format item. Verified: `str(Decimal("1E29")/Decimal("100"))` → `"1E+27"`; Pydantic 2.13 emits `{"cash":"1E+27","q":"1E-8"}` for `Decimal` fields.

**Approach:** Introduce ONE shared fixed-point formatter (`format(value, "f")`) as the single money→wire serializer, plus a reusable Pydantic annotated money type for `Decimal`-typed response fields. Route every money/Decimal wire-serialization site through it and delete the duplicated ad-hoc `str()`/helper logic. Behavior-only: no schema, magnitude, or precision changes.

## Boundaries & Constraints

**Always:**
- A single shared formatter (`format(value, "f")`) is the ONLY money→wire serializer; the pre-existing `coach.decision_record._money` and `api.coach._money_str` become thin delegates to it (identical output — the co-sign snapshot/replay strings stay byte-identical).
- Every monetary/`Decimal` value crossing an HTTP boundary renders as fixed-point: no `E+`/`E-` exponent, no binary float, decimal scale preserved (`Decimal("100.00")` → `"100.00"`).
- All emitted strings round-trip cleanly through the documented `Decimal(str(...))` consumer — `Decimal(wire_str)` equals the original value.
- `None` optional money fields (`avg_price`, `cost_basis`, `window_return`) stay `None`/`null` on the wire.

**Block If:**
- Unifying the two existing helpers would change any already-emitted serialized string (a byte difference in a persisted snapshot or `/approve` outcome). It must not — both already use `format(value, "f")`; verify, don't assume.

**Never:**
- Do not cap, clamp, round, quantize, or otherwise alter money magnitude/precision (Story 4.6 deliberately rejected a size cap as contradicting FR11 "never block").
- Do not touch inbound request parsing (`amount: Decimal` on request models) or broker-inbound `Decimal(str(...))` parsing in `brokers/schwab_adapter` — those are correct.
- Do not change response schemas, field names, or wire types beyond the string formatting; do not introduce `float` anywhere.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Extreme large amount | `Decimal("1E29")/Decimal("100")` in `order_intent.amount` | `"1000000000000000000000000000"` (no `E+`) | No error expected |
| Tiny fractional value | `Decimal("1E-8")` holding quantity | `"0.00000001"` (no `E-`) | No error expected |
| Normal money | `Decimal("100.00")` | `"100.00"` (scale preserved) | No error expected |
| Optional None | `avg_price` / `cost_basis` / `window_return` = `None` | `null` / `None` preserved (not `"None"`) | No error expected |
| Round-trip | any emitted wire string `s` | `Decimal(s) == original` | No error expected |
| Evidence stat Decimal | large/tiny `Decimal` in `EvidenceRecord.stats` | fixed-point string via `_json_safe` | No error expected |

</intent-contract>

## Code Map

- `ballast/backend/money.py` (NEW) -- shared `format_money(value: Decimal) -> str` returning `format(value, "f")`, with a docstring stating the no-`E+`/no-float wire contract and `Decimal(str(...))` round-trip. Also export a reusable Pydantic annotated money type (e.g. `WireMoney = Annotated[Decimal, PlainSerializer(...)]` that serializes a `Decimal` via `format_money` and passes `None` through) for `Decimal`-typed response fields. Stdlib-only imports (no cycles).
- `ballast/backend/coach/decision_record.py` -- `_money` (line ~70) becomes `return format_money(value)` (import from `money`). Keep the name/signature; call sites (`_order_intent_json`, `cosign`) unchanged. Output must stay byte-identical.
- `ballast/backend/api/coach.py` -- `_money_str` (line ~255) delegates to `format_money`. FIX `_order_intent_out` (line 238): `amount=str(intent.amount)` → `amount=format_money(intent.amount)` (the exact 4.7-ledger bug on the `/recommend` `order_intent.amount` path).
- `ballast/backend/precedent/evidence.py` -- `_json_safe` (line 53): `return str(value)` for `Decimal` → `return format_money(value)` so evidence `stats` Decimals never emit exponent (reaches the wire via `EvidenceOut`, decisions replay, precedent endpoints).
- `ballast/backend/precedent/missed_growth.py` -- `to_dict` (lines 92/95/103): `idle_cash`, `window_return`, `forgone_growth` bare `str(...)` → `format_money(...)` (None-guarded for `window_return`).
- `ballast/backend/api/portfolio.py` -- `HoldingOut.quantity`/`market_value`/`cost_basis` and `PortfolioOut.cash` are `Decimal`-typed → Pydantic emits `E+`/`E-`. Apply the shared `WireMoney` annotated type (or an equivalent `field_serializer` using `format_money`) so they render fixed-point; keep them string-on-wire and `Decimal(str(...))`-consumable.
- `ballast/backend/tests/test_money.py` (NEW) -- unit tests for `format_money`: large (`1E+27`→plain), tiny (`1E-8`→plain), normal scale preserved, negative, zero, round-trip via `Decimal(str(...))`.
- `ballast/backend/tests/test_coach_api.py` -- assert `/recommend` `order_intent.amount` is fixed-point for an extreme amount (no `E` in the JSON string).
- `ballast/backend/tests/test_portfolio.py` -- assert portfolio `cash`/holding `quantity`/`market_value` are fixed-point for extreme/tiny values (no `E` in JSON).
- `ballast/backend/tests/test_missed_growth.py` + `tests/test_precedent.py` -- assert `MissedGrowthEstimate.to_dict()` money fields and `EvidenceRecord` stat Decimals never contain `E`.

## Tasks & Acceptance

**Execution:**
- [x] `ballast/backend/money.py` -- add shared `format_money` + `WireMoney` annotated Pydantic type (single source of truth).
- [x] `ballast/backend/coach/decision_record.py` -- `_money` delegates to `format_money`; verify snapshot output byte-identical.
- [x] `ballast/backend/api/coach.py` -- `_money_str` delegates to `format_money`; fix `_order_intent_out` `amount` to fixed-point.
- [x] `ballast/backend/precedent/evidence.py` -- `_json_safe` Decimal branch uses `format_money`.
- [x] `ballast/backend/precedent/missed_growth.py` -- `to_dict` money fields use `format_money` (None-safe).
- [x] `ballast/backend/api/portfolio.py` -- apply `WireMoney`/`field_serializer` to the four `Decimal` fields.
- [x] `ballast/backend/tests/test_money.py` -- unit-test the formatter (I/O matrix rows).
- [x] `ballast/backend/tests/test_coach_api.py` + `tests/test_portfolio.py` + `tests/test_missed_growth.py` + `tests/test_precedent.py` -- add "no `E` on the wire" + round-trip assertions for each serializer.

**Acceptance Criteria:**
- Given any money/`Decimal` value serialized by any endpoint (`/recommend` `order_intent.amount`, `/approve` `filled_qty`/`avg_price`, portfolio `cash`/`quantity`/`market_value`/`cost_basis`, missed-growth `idle_cash`/`forgone_growth`, evidence `stats`), when the value is extreme-large or tiny-fractional, then the wire string contains no `E`/exponent and no binary float, and `Decimal(wire_str)` equals the original.
- Given the two pre-existing helpers (`_money`, `_money_str`), when unified onto the shared formatter, then every previously-emitted string (persisted co-sign snapshots and `/approve` outcomes) is byte-identical — the co-sign replay invariant is unbroken.
- Given `None` optional money fields, when serialized, then they remain `null`/`None` (never `"None"`).
- Given the full suite, when it runs, then it passes with zero network/credentials and no response schema/field-name changes.

## Review Triage Log

### 2026-08-01 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 1 (low 1)
- defer: 1
- reject: 7
- addressed_findings:
  - `[low]` `[patch]` Both reviewers circled non-finite handling of the new `money.py` "single source of truth" contract: the module/`format_money` docstrings asserted `Decimal(format_money(x)) == x` unconditionally, but that is false for a non-finite `x` (`format(Decimal('NaN'),'f') == 'NaN'`, and `NaN != NaN`). Scoped the round-trip claim to "every finite `x`" and documented that non-finite is rejected upstream (Pydantic `WireMoney` rejects it at validation; unreachable at the engine-computed call sites) rather than sanitized in `format_money`. Docstring-only, no behavior change; full suite still 357 passed.
- notes: Deferred (1) — a non-finite (`NaN`/`Infinity`) `avg_price` from malformed Schwab order JSON would serialize verbatim (`"NaN"`) on the `/approve` wire and into the immutable co-sign snapshot; root cause is `brokers/schwab_adapter/adapter.py::_extract_avg_price`'s direct-field branch lacking the `is_finite()` guard its sibling `_decimal_or_zero` has. NOT caused by 6.4 (the pre-existing helpers already emitted `"NaN"` via `format(value,'f')`; this pass only unified them and surfaced it); unreachable with current fixtures and never via the Pydantic `WireMoney` fields (Pydantic rejects non-finite). Logged for the Schwab adapter / go-live hardening (6.3/6.7). Rejected (7) — the `WireMoney` "passes None through" doc nit (the existing text already qualifies it to `Decimal | None` fields); `WireMoney` not defending against lossy `float` coercion (upstream sources are `Decimal`-typed columns/dataclasses; not reachable); the `_json_safe` evidence-id hash-input change (differs from `str()` ONLY for exponent-form Decimals, which never occur since every `stats` value is `_q()`-quantized to 4dp or `int` — ids are unchanged for all reachable data); coach-API money fields (`amount`/`filled_qty`/`avg_price`) kept as bare `str` populated via `format_money` rather than the `WireMoney` annotation (a DELIBERATE spec decision — those fields were already `str` and correctly formatted; output is identical and protected); the `_money`/`_money_str` thin-delegate "dead abstraction" (kept ON PURPOSE per the spec, for the byte-identity-documentation seam between snapshot and live outcome); and two pre-existing signed-zero cosmetics (`forgone_growth`/portfolio field could render `"-0.00"`/`"-0"`) that are unchanged by this diff (`str(Decimal('-0.00'))` already produced `"-0.00"`) and not broker-reachable.

## Design Notes

- **Round-trip is already clean either way; the fix is the wire contract.** `Decimal("1E+27") == Decimal("1000...0")`, so the consumer never broke — but the epic invariant forbids `E+`/`E-` on the wire (readability + the documented `Decimal(str(...))` fixed-point contract). This pass makes the *emitted form* obey it everywhere.
- **Golden formatter + Pydantic pattern:**
  ```python
  # money.py
  def format_money(value: Decimal) -> str:
      """Fixed-point wire form — never binary float, never E+/E-."""
      return format(value, "f")

  WireMoney = Annotated[Decimal, PlainSerializer(
      lambda v: None if v is None else format_money(v), return_type=str, when_used="json")]
  ```
  In `portfolio.py`: `cash: WireMoney`, `quantity: WireMoney`, `cost_basis: WireMoney | None = None`.
- **Why one shared home.** `_money` (coach) and `_money_str` (api) are byte-identical duplicates today; a future edit to one would silently diverge the snapshot vs. live-outcome strings that Story 4.9 requires be identical. Collapsing to `money.format_money` makes that invariant structural, not coincidental.
- **`_json_safe` covers non-money Decimals too** (evidence stats). Fixed-point is correct and safe for all Decimals, so routing the whole Decimal branch through `format_money` closes the exponent gap for statistics as well — no separate money/non-money branching.

## Verification

**Commands:**
- `cd ballast/backend && uv run python -m pytest tests/test_money.py tests/test_coach_api.py tests/test_portfolio.py tests/test_missed_growth.py tests/test_precedent.py -q` -- expected: all pass; money serialized fixed-point, no `E` on the wire.
- `cd ballast/backend && uv run python -m pytest -q` -- expected: full suite green (no regressions; snapshot/replay strings unchanged).
- `cd ballast/backend && uv run python -c "from money import format_money; from decimal import Decimal; assert format_money(Decimal('1E29')/Decimal('100'))=='1000000000000000000000000000'; assert format_money(Decimal('1E-8'))=='0.00000001'; print('ok')"` -- expected: prints `ok`.

## Auto Run Result

Status: done

**Change:** Closed the deferred 4.6/4.7 money-format item (Epic 6 "no `E+` on the wire" invariant). Introduced one shared fixed-point formatter as the single money→wire serializer and routed every money/`Decimal` serialization site through it, so an extreme (`1E29/100`) or tiny (`1E-8`) value now renders plain fixed-point instead of exponent notation everywhere it crosses an HTTP boundary. Empirically confirmed the bug first: Pydantic 2.13 emitted `{"cash":"1E+27","q":"1E-8"}` for `Decimal` fields, and bare `str(Decimal)` did the same.

**Files changed:**
- `ballast/backend/money.py` (NEW) -- `format_money(value)` = `format(value,"f")` single serializer + reusable `WireMoney = Annotated[Decimal, PlainSerializer(...)]` for `Decimal`-typed response fields; stdlib+Pydantic only (no import cycles).
- `ballast/backend/api/coach.py` -- `_money_str` delegates to `format_money`; fixed `_order_intent_out` `amount=str(...)` → `format_money(...)` (the exact 4.7-ledger bug on `/recommend`).
- `ballast/backend/api/portfolio.py` -- `HoldingOut.quantity/market_value/cost_basis` + `PortfolioOut.cash` retyped `Decimal` → `WireMoney` (Pydantic was emitting exponent).
- `ballast/backend/coach/decision_record.py` -- `_money` delegates to `format_money` (snapshot output byte-identical).
- `ballast/backend/precedent/evidence.py` -- `_json_safe` Decimal branch `str()` → `format_money()`.
- `ballast/backend/precedent/missed_growth.py` -- `to_dict` `idle_cash`/`window_return`(None-safe)/`forgone_growth` → `format_money`.
- `ballast/backend/tests/test_money.py` (NEW) + additions to `test_coach_api.py`, `test_portfolio.py`, `test_missed_growth.py`, `test_precedent.py` -- per-serializer "no `E` on the wire" + exact-string + `Decimal(str(...))` round-trip + None-passthrough assertions.

**Review findings:** 1 patch applied (low — scoped the new `money.py` round-trip contract docstring to finite values after both reviewers circled non-finite; docstring-only, no behavior change). 1 deferred (non-finite `avg_price` from malformed Schwab JSON serializes as `"NaN"` on the wire + into the immutable snapshot; root cause is the Schwab adapter's `_extract_avg_price` missing the `is_finite()` guard its sibling has — pre-existing, not caused by 6.4, unreachable today; logged to deferred-work for 6.3/6.7 go-live hardening). 7 rejected (by-spec-design asymmetries, unreachable/quantized cases, and pre-existing signed-zero cosmetics unchanged by this diff).

**Verification:** Full suite `357 passed, 2 warnings` (pre-existing deprecation warnings only; zero network, zero credentials). Targeted serializer set `100 passed`. Smoke: `format_money(1E29/100)=='1000000000000000000000000000'`, `format_money(1E-8)=='0.00000001'` → `ok`. No response schema/field-name changes; co-sign snapshot / `/approve` replay strings byte-identical (helpers already used `format(value,"f")`).

**Residual risks:** The single deferred non-finite `avg_price` path (money-bearing, but unreachable with finite broker data and blocked at the Pydantic `WireMoney` boundary). `create_all` schema note is unrelated to this pass. No behavior change to any finite money value.
