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
 * Keyed on the dollar figure's sign so it stays consistent with the glyph the
 * component derives from the same figure. Only used for a NON-zero figure (the
 * component suppresses the indicator entirely when the figure is $0.00). */
export function amountLabel(estimate) {
  const amount = formatUsd(estimate?.forgone_growth)
  if (amount == null) return null
  const rose = Number(estimate?.forgone_growth) >= 0
  return rose ? `${amount} of growth not captured` : `${amount} of loss avoided`
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
