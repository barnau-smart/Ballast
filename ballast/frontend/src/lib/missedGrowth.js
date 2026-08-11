/**
 * Presentation helpers for the missed-growth meter (Story 3.4, FR19).
 *
 * AD-1: formatting + copy ONLY — NO market computation. The backend
 * (`GET /api/precedent/missed-growth`, the deterministic engine) is the sole
 * source of every number; these helpers only decide how to *say* and *format*
 * the values the estimate already carries. They never derive a figure.
 *
 * The engine emits JSON-safe values (Decimals as strings, dates as ISO-8601),
 * so every numeric input here is a string; we parse only to format for display.
 *
 * We reuse `formatPct` / `directionAndMagnitude` from `lib/precedent.js` (the
 * sibling recovery-precedent view) so the meter shares the same honest
 * sign/color derivation. Citation phrasing is local (below) because the meter's
 * degraded states may carry no `as_of`.
 */

import { directionAndMagnitude, formatPct } from './precedent.js'

export { directionAndMagnitude, formatPct }

/**
 * The citation line. Always cites `source`; appends "· as of {date}" ONLY when
 * the estimate carries a date. The degraded states (`no_idle_cash`, and any
 * pre-load path) have no `as_of`, so we omit the date rather than render a
 * phantom "as of an unknown date". Never a dead end, never a fake date.
 */
export function sourceLine(estimate) {
  const source = estimate?.source
  if (!source) return ''
  return estimate?.as_of ? `${source} · as of ${estimate.as_of}` : source
}

/**
 * Format a decimal-dollar string as US currency, e.g. `"1234.50"` → `"$1,234.50"`.
 * The magnitude is UNSIGNED (the sign lives on the `MarketIndicator` glyph, never
 * doubled here). Returns `null` for a missing/unparseable value so callers can
 * omit the line rather than render "$NaN".
 */
export function formatUsd(decimalStr) {
  if (decimalStr == null) return null
  const n = Number(decimalStr)
  if (!Number.isFinite(n)) return null
  return `$${Math.abs(n).toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`
}

/** The short label for the forgone-growth `MarketIndicator` (unsigned amount).
 * The "loss avoided" framing keys on the MARKET direction (`window_return`), not
 * the figure's sign — otherwise a rising market where parked yield outpaced it
 * (`forgone_growth < 0`, `window_return > 0`) would falsely read "loss avoided"
 * beneath an honest "the market rose, you came out ahead" statement (the exact
 * contradiction the backend statement was fixed to avoid). Only used for a
 * NON-zero figure (the component suppresses the indicator when it's $0.00). */
export function amountLabel(estimate) {
  const amount = formatUsd(estimate?.forgone_growth)
  if (amount == null) return null
  const forgone = Number(estimate?.forgone_growth)
  if (forgone >= 0) {
    // Idle cash sat out gains it could have captured.
    return `${amount} of growth not captured`
  }
  // Idle cash came out ahead. Call it "loss avoided" ONLY when the market
  // actually fell; if the market rose (or we can't tell), your parked yield
  // simply outpaced it — never claim a loss that didn't happen.
  const windowReturn = Number(estimate?.window_return)
  const marketFell = Number.isFinite(windowReturn) && windowReturn < 0
  return marketFell
    ? `${amount} of loss avoided`
    : `${amount} ahead, after yield`
}

/**
 * The calm protected-reserve line (Story 9.2), e.g.
 * "$2,000.00 stayed protected, as you set it". Returns `null` when the user has
 * NOT decided a reserve (`reserved == null`) or it resolves to $0 — we NEVER
 * fabricate a reserve figure for a never-decided reserve. Non-alarmist,
 * information not pressure; the protected amount reassures, it does not nudge.
 */
export function reserveLine(estimate) {
  if (estimate?.reserved == null) return null
  const reserved = Number(estimate.reserved)
  if (!Number.isFinite(reserved) || reserved <= 0) return null
  // Never claim to protect more money than actually existed: cap the displayed
  // figure at the user's real cash (settlement + parked). A reserve set larger
  // than the account (fully-reserved) shouldn't read "$5,000 stayed protected"
  // when only $1,000 was ever there.
  const held = Number(estimate.settlement_cash) + Number(estimate.parked)
  const protectedAmt = Number.isFinite(held) ? Math.min(reserved, held) : reserved
  if (!(protectedAmt > 0)) return null
  const amount = formatUsd(String(protectedAmt))
  if (amount == null) return null
  return `${amount} stayed protected, as you set it.`
}

/**
 * The disclosed money-market yield note (Story 9.2), shown ONLY when parked
 * money is actually in the calc (`parked > 0`). States the assumption out loud
 * so the figure is never a lie by omission — e.g. "Counting your parked
 * money-market cash as already earning about 4% a year." Returns `null` when
 * there is no parked money or no apy to disclose.
 */
export function yieldNote(estimate) {
  const parked = Number(estimate?.parked)
  if (!Number.isFinite(parked) || parked <= 0) return null
  const apy = Number(estimate?.money_market_apy)
  if (!Number.isFinite(apy)) return null
  const pct = (apy * 100).toLocaleString('en-US', { maximumFractionDigits: 1 })
  return `Counting your parked money-market cash as already earning about ${pct}% a year.`
}

/** The window-context line, e.g. "Benchmark: VTI · +14.0% over ~252 trading days". */
export function windowLine(estimate) {
  const parts = []
  if (estimate?.benchmark) parts.push(`Benchmark: ${estimate.benchmark}`)
  const dm = directionAndMagnitude(estimate?.window_return)
  if (dm) {
    const sign = dm.direction === 'down' ? '−' : '+'
    parts.push(
      `${sign}${dm.magnitude} over ~${estimate?.trading_days ?? '252'} trading days`,
    )
  }
  return parts.join(' · ')
}
