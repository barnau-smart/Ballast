/**
 * Presentation helpers for the plain-English portfolio (Story 2.4).
 *
 * AD-1: this is presentation copy + formatting only — no business logic. The
 * backend (`GET /api/portfolio`, Story 2.3) is the source of truth for the
 * numbers; these helpers only decide how to *say* and *format* them so a
 * beginner understands what they hold, with no unexplained jargon (NFR6).
 */

/**
 * Short, warm, jargon-free descriptions for the funds a v1 user is likely to
 * hold (the Story 2.3 fake set + common broad-market funds). Any term that
 * could confuse (e.g. "bond") is explained in the sentence itself.
 */
const SYMBOL_DESCRIPTIONS = {
  VTI: 'The whole U.S. stock market — a tiny piece of almost every public U.S. company.',
  VXUS: 'Stocks from companies outside the U.S. — the rest of the world in one holding.',
  BND: 'U.S. bonds — steady loans to the government and big companies that pay you interest.',
  VOO: 'The 500 largest U.S. companies (the S&P 500) in a single holding.',
  BNDX: 'Bonds from outside the U.S. — steady interest-paying loans from around the world.',
  VT: 'The entire world stock market — U.S. and international companies together.',
}

/**
 * A plain-English description for a holding. Unknown symbols never render as a
 * bare, unexplained ticker — they get a calm generic sentence so the dashboard
 * is always understandable.
 */
export function describeHolding(symbol) {
  return (
    SYMBOL_DESCRIPTIONS[symbol] ??
    'A fund in your account. Ask the coach anytime to explain what it holds.'
  )
}

/** Format a numeric dollar value as USD currency (e.g. 2500 → "$2,500.00"). */
export function formatCurrency(value) {
  const number = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(number)) return '$0.00'
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
  }).format(number)
}

/**
 * Round a dollar value to integer cents. Summing in cents avoids the float
 * drift you'd get adding raw dollar floats, so displayed totals stay exact.
 */
function toCents(value) {
  const number = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(number)) return 0
  return Math.round(number * 100)
}

/**
 * Total portfolio value = sum of holding market values + cash, computed in
 * integer cents (no float rounding error) and returned as a dollar number.
 */
export function totalValue(holdings, cash) {
  const cents =
    holdings.reduce((sum, h) => sum + toCents(h.market_value), 0) + toCents(cash)
  return cents / 100
}

/** Sum of a set of holdings' market values (dollars), summed in cents. */
export function holdingsValue(holdings) {
  return holdings.reduce((sum, h) => sum + toCents(h.market_value), 0) / 100
}

/**
 * Split holdings into the index core vs. the rest, driven by the backend's
 * `is_core` flag (Story 2.5, FR6). Presentation-only — the backend owns the
 * classification (AD-1).
 */
export function partitionByCore(holdings) {
  const core = []
  const rest = []
  for (const holding of holdings) {
    ;(holding.is_core ? core : rest).push(holding)
  }
  return { core, rest }
}

/**
 * Split holdings into user-tagged parked (money-market) cash-equivalents vs.
 * genuine investment holdings, then partition the genuine holdings into the
 * index core vs. the rest (Story 9.1, Epic 9). Parked is pulled out FIRST so a
 * money-market fund never renders as a stock-like "mover" — it's cash, not a bet.
 * Presentation-only — the backend `is_parked`/`is_core` flags own the
 * classification (AD-1).
 */
export function partitionByCash(holdings) {
  const parked = []
  const invested = []
  for (const holding of holdings) {
    ;(holding.is_parked ? parked : invested).push(holding)
  }
  const { core, rest } = partitionByCore(invested)
  return { parked, core, rest }
}

/** Plain-English explainer of what "parked cash" means (NFR6, no jargon). */
export const PARKED_EXPLAINER =
  'Money-market funds you’ve told us to treat as cash — money set aside, not ' +
  'invested in the market. We show it as cash, not as a stock that moves up or down.'

/** Plain-English explainer of what "your index core" means (NFR6). */
export const CORE_EXPLAINER =
  'Your index core is the steady base of your portfolio — broad, low-cost funds ' +
  'that hold a little of the whole market at once, instead of betting on any ' +
  'single company.'

/** Gentle framing for holdings outside the core — not "bad", just not the base. */
export const NON_CORE_EXPLAINER =
  'Outside your core — not bad, just not part of that steady, broad base.'

/**
 * Direction of a holding vs its cost basis: 'up' above cost, 'down' below,
 * or null when there's nothing honest to say — cost basis unknown, or the
 * value is exactly even (we don't imply a gain on a break-even holding).
 * NOTE: a 'down' holding is shown sky-blue ▼ — NEVER red/pink (hard color rule).
 */
export function gainDirection(holding) {
  // Parked money-market funds are cash, not a bet — never show an up/down
  // "since you bought" indicator for them, regardless of cost basis (Story 9.1).
  if (holding.is_parked) return null
  if (holding.cost_basis == null) return null
  const marketValue = Number(holding.market_value)
  const costBasis = Number(holding.cost_basis)
  if (marketValue === costBasis) return null
  return marketValue > costBasis ? 'up' : 'down'
}
