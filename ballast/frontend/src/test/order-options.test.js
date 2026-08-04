import { describe, it, expect } from 'vitest'
import {
  DEFAULT_OPTIONS,
  validateOrderMatrix,
  buildOrderIntent,
  deriveWarnings,
} from '../lib/orderOptions.js'

const BASE = { symbol: 'VTI', side: 'buy', amount: '500' }

function opts(overrides) {
  return { ...DEFAULT_OPTIONS, ...overrides }
}

describe('orderOptions.validateOrderMatrix — backend matrix mirror', () => {
  it('MARKET defaults are ok with no detail', () => {
    expect(validateOrderMatrix(DEFAULT_OPTIONS)).toEqual({
      ok: true,
      detail: null,
    })
  })

  it('MARKET with a limit price is blocked (exact backend message)', () => {
    const r = validateOrderMatrix(opts({ limit_price: '100' }))
    expect(r.ok).toBe(false)
    expect(r.detail).toBe("A market order can't carry a limit or stop price.")
  })

  it('MARKET with a stop price is blocked (exact backend message)', () => {
    const r = validateOrderMatrix(opts({ stop_price: '90' }))
    expect(r.ok).toBe(false)
    expect(r.detail).toBe("A market order can't carry a limit or stop price.")
  })

  it('LIMIT with a valid positive price is ok', () => {
    const r = validateOrderMatrix(opts({ order_type: 'limit', limit_price: '99.50' }))
    expect(r).toEqual({ ok: true, detail: null })
  })

  it('LIMIT + GTC with a valid price is ok', () => {
    const r = validateOrderMatrix(
      opts({ order_type: 'limit', limit_price: '99.50', duration: 'gtc' }),
    )
    expect(r).toEqual({ ok: true, detail: null })
  })

  it.each([
    ['', 'blank'],
    ['0', 'zero'],
    ['5.', 'trailing dot'],
    ['1e3', 'exponent'],
    ['-5', 'negative'],
  ])('LIMIT with a non-positive/malformed price (%s) is blocked', (price) => {
    const r = validateOrderMatrix(opts({ order_type: 'limit', limit_price: price }))
    expect(r.ok).toBe(false)
    expect(r.detail).toBe('A limit order needs a limit price greater than zero.')
  })

  it('LIMIT carrying a stop price is blocked (exact backend message)', () => {
    const r = validateOrderMatrix(
      opts({ order_type: 'limit', limit_price: '99', stop_price: '90' }),
    )
    expect(r.ok).toBe(false)
    expect(r.detail).toBe("A limit order can't carry a stop price.")
  })

  it('MARKET + GTC is blocked (GTC coupled to LIMIT)', () => {
    const r = validateOrderMatrix(opts({ duration: 'gtc' }))
    expect(r.ok).toBe(false)
    expect(r.detail).toBe("A market order can't be good-till-canceled.")
  })

  it.each(['stop', 'stop_limit'])(
    'unsupported order type %s is not ok with a calm not-supported note',
    (order_type) => {
      const r = validateOrderMatrix(opts({ order_type }))
      expect(r.ok).toBe(false)
      expect(r.detail).toMatch(/aren.t supported in this version/i)
    },
  )

  it.each(['am', 'pm'])(
    'unsupported session %s is not ok with a calm not-supported note',
    (session) => {
      const r = validateOrderMatrix(opts({ session }))
      expect(r.ok).toBe(false)
      expect(r.detail).toMatch(/aren.t supported in this version/i)
    },
  )
})

describe('orderOptions.buildOrderIntent — compose /approve order_intent', () => {
  it('default (MARKET) compose is byte-identical {symbol, side, amount} with no extra keys', () => {
    const intent = buildOrderIntent(BASE, DEFAULT_OPTIONS)
    expect(intent).toEqual({ symbol: 'VTI', side: 'buy', amount: '500' })
    expect(Object.keys(intent).sort()).toEqual(['amount', 'side', 'symbol'])
  })

  it('undefined options → still the blessed MARKET intent', () => {
    expect(buildOrderIntent(BASE)).toEqual({
      symbol: 'VTI',
      side: 'buy',
      amount: '500',
    })
  })

  it('LIMIT adds order_type + limit_price as the EXACT string (no float coercion)', () => {
    const intent = buildOrderIntent(
      BASE,
      opts({ order_type: 'limit', limit_price: '99.50' }),
    )
    expect(intent).toEqual({
      symbol: 'VTI',
      side: 'buy',
      amount: '500',
      order_type: 'limit',
      limit_price: '99.50',
    })
    // The price is the exact string, not a Number — trailing zeros preserved.
    expect(intent.limit_price).toBe('99.50')
    expect(typeof intent.limit_price).toBe('string')
    expect(String(Number('99.50'))).not.toBe(intent.limit_price)
  })

  it('LIMIT does NOT add duration when Day (the default)', () => {
    const intent = buildOrderIntent(
      BASE,
      opts({ order_type: 'limit', limit_price: '99.50', duration: 'day' }),
    )
    expect(intent).not.toHaveProperty('duration')
  })

  it('LIMIT + GTC adds duration:"gtc"', () => {
    const intent = buildOrderIntent(
      BASE,
      opts({ order_type: 'limit', limit_price: '99.50', duration: 'gtc' }),
    )
    expect(intent.duration).toBe('gtc')
    expect(intent.order_type).toBe('limit')
    expect(intent.limit_price).toBe('99.50')
  })
})

describe('orderOptions.deriveWarnings — calm footgun warnings', () => {
  it('MARKET produces no warnings', () => {
    expect(deriveWarnings(DEFAULT_OPTIONS)).toEqual([])
  })

  it('LIMIT produces a single calm "may rest" warning', () => {
    const w = deriveWarnings(opts({ order_type: 'limit', limit_price: '99' }))
    expect(w).toHaveLength(1)
    expect(w[0].kind).toBe('resting-limit')
    expect(w[0].message).toMatch(/may not fill right away/i)
    // Never-red / calm voice: no alarming words.
    expect(w[0].message).not.toMatch(/danger|error|warning!|risk/i)
  })

  it('LIMIT + GTC produces both "may rest" and "stays open for days"', () => {
    const w = deriveWarnings(
      opts({ order_type: 'limit', limit_price: '99', duration: 'gtc' }),
    )
    expect(w.map((x) => x.kind)).toEqual(['resting-limit', 'gtc'])
    expect(w[1].message).toMatch(/stays open for days/i)
  })

  it('GTC on a MARKET order (invalid combo) still produces no warnings', () => {
    // Warnings are informed-consent for LIMIT/GTC-on-limit; a bare MARKET+GTC is
    // a matrix error, not a footgun to consent to.
    expect(deriveWarnings(opts({ duration: 'gtc' }))).toEqual([])
  })
})
