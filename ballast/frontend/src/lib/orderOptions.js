/**
 * Story 8.3 — the client-side order-options matrix mirror + footgun warnings.
 *
 * A PURE, testable module (no React, no I/O). It is the single home for:
 *  - `DEFAULT_OPTIONS`: the blessed MARKET defaults.
 *  - `validateOrderMatrix(options)`: a pre-submit convenience MIRROR of the
 *    backend field-requirement gate (`OrderIntentIn._validate_order_matrix` +
 *    engine `validate_order_intent`). Returns `{ok, detail}` using the EXACT
 *    backend message strings so a client block reads identically to a backend
 *    422. The backend remains the AUTHORITATIVE gate (defense in depth).
 *  - `buildOrderIntent(base, options)`: composes the `/approve` `order_intent`.
 *    Defaults are OMITTED so a MARKET+REGULAR+DAY order stays byte-identical
 *    `{symbol, side, amount}` (backward compat). `limit_price` travels as the
 *    EXACT validated decimal string — never coerced through Number/float.
 *  - `deriveWarnings(options)`: calm, plain-English, never-red footgun warnings
 *    for the human's approve-time choices (a client-side sibling of the 4.5
 *    `MoveWarning{kind, risk}` voice — a `{kind, message}` shape).
 *
 * Only LIMIT and GTC are informed-consent footguns (see spec Design Notes):
 * STOP/STOP_LIMIT and AM/PM are NOT submittable and render disabled with a calm
 * "not available in this version" note — they never reach a proceed-anyway path.
 */

// A clean positive decimal string (no exponent/hex/trailing-dot) — mirrors the
// existing `DECIMAL_RE` used for `amount` in CoachConsult.jsx. `Number()` alone
// would pass '1e3'/'0x10'/'5.'. Rejects '', '5.', '1e3', '0', negatives.
const DECIMAL_RE = /^\d+(\.\d+)?$/

export const DEFAULT_OPTIONS = Object.freeze({
  order_type: 'market', // 'market' | 'limit' | 'stop' | 'stop_limit'
  limit_price: '',
  // Defensive mirror of the backend gate for a DEFERRED STOP story only —
  // intentionally UI-unreachable in this version (no field renders it). Kept so
  // the matrix mirror stays a faithful reflection of the backend.
  stop_price: '',
  session: 'regular', // 'regular' | 'am' | 'pm'
  duration: 'day', // 'day' | 'gtc'
})

// A positive, finite decimal string, else null. The returned value is the EXACT
// validated string (validated == sent — never a float).
function validPrice(raw) {
  const p = String(raw ?? '').trim()
  if (!DECIMAL_RE.test(p) || Number(p) <= 0) return null
  return p
}

function hasPrice(raw) {
  return String(raw ?? '').trim() !== ''
}

/**
 * The client-side mirror of the backend field-requirement matrix. Returns
 * `{ok: boolean, detail: string|null}` with the EXACT backend message strings.
 *
 * STOP/STOP_LIMIT/AM/PM are NOT submittable in this version — they are surfaced
 * disabled in the UI, but if one somehow reaches here it is treated as not-ok
 * with the calm backend "not supported in this version yet" note (never a
 * proceed-anyway footgun).
 */
export function validateOrderMatrix(options) {
  const o = { ...DEFAULT_OPTIONS, ...(options ?? {}) }

  // Deferred features first — an explicit "not supported yet" refusal, matching
  // the backend engine gate ordering.
  if (o.order_type === 'stop' || o.order_type === 'stop_limit') {
    return {
      ok: false,
      detail:
        "Stop and stop-limit orders aren't supported in this version yet.",
    }
  }
  if (o.session === 'am' || o.session === 'pm') {
    return {
      ok: false,
      detail:
        "Extended-hours (pre-market / after-hours) sessions aren't supported in this version yet.",
    }
  }

  // GTC is coupled to LIMIT (a market order fills immediately).
  if (o.order_type !== 'limit' && o.duration === 'gtc') {
    return { ok: false, detail: "A market order can't be good-till-canceled." }
  }

  if (o.order_type === 'market') {
    if (hasPrice(o.limit_price) || hasPrice(o.stop_price)) {
      return {
        ok: false,
        detail: "A market order can't carry a limit or stop price.",
      }
    }
    return { ok: true, detail: null }
  }

  // order_type === 'limit' — the only remaining supported type.
  if (hasPrice(o.stop_price)) {
    return { ok: false, detail: "A limit order can't carry a stop price." }
  }
  if (validPrice(o.limit_price) === null) {
    return {
      ok: false,
      detail: 'A limit order needs a limit price greater than zero.',
    }
  }
  return { ok: true, detail: null }
}

/**
 * Compose the `/approve` `order_intent` from the blessed MARKET base
 * (`{symbol, side, amount}`) and the human's approve-time options. Defaults are
 * OMITTED so a MARKET+REGULAR+DAY order stays byte-identical `{symbol, side,
 * amount}`. LIMIT adds `order_type:'limit'` + `limit_price`. `duration:'gtc'`
 * is added only when GTC is chosen on a LIMIT.
 *
 * This function does NOT validate — the CALLER must already have validated the
 * options (via `validateOrderMatrix` / the Approve gate). It only trims the
 * price string and passes it through as-is: never coerced through Number/float,
 * so the string on the form is the string on the wire.
 */
export function buildOrderIntent(base, options) {
  const o = { ...DEFAULT_OPTIONS, ...(options ?? {}) }
  const intent = { ...base }

  if (o.order_type === 'limit') {
    intent.order_type = 'limit'
    // The exact validated string (no Number()/float round-trip).
    intent.limit_price = String(o.limit_price ?? '').trim()
    if (o.duration === 'gtc') {
      intent.duration = 'gtc'
    }
  }

  return intent
}

/**
 * Calm, plain-English, never-red footgun warnings for the live approve-time
 * options — the client-side sibling of the 4.5 `MoveWarning{kind, risk}` voice
 * (`{kind, message}`). Only LIMIT and GTC are informed-consent footguns; MARKET
 * produces none. The user can dismiss any warning and proceed with informed
 * consent.
 */
export function deriveWarnings(options) {
  const o = { ...DEFAULT_OPTIONS, ...(options ?? {}) }
  const warnings = []

  if (o.order_type === 'limit') {
    warnings.push({
      kind: 'resting-limit',
      message:
        'A limit order may not fill right away — it rests until your price is reached, or you cancel it.',
    })
    if (o.duration === 'gtc') {
      warnings.push({
        kind: 'gtc',
        message:
          'This order stays open for days — check on it regularly, or cancel it.',
      })
    }
  }

  return warnings
}
