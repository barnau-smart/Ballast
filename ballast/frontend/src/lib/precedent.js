/**
 * Presentation helpers for the recovery-precedent view (Story 3.3, FR15).
 *
 * AD-1: this is formatting + copy only — NO market computation. The backend
 * (`GET /api/precedent/recovery`, the Precedent Engine) is the sole source of
 * every number; these helpers only decide how to *say* and *format* the values
 * the record already carries. They never derive a statistic or invent a figure.
 *
 * The engine emits JSON-safe values (Decimals as strings, dates as ISO-8601),
 * so every input here is a string; we parse only to format for display.
 */

/**
 * Format a decimal-fraction string as an unsigned percent to one decimal place,
 * e.g. `"0.0801"` → `"8.0%"`. Returns `null` for a missing/unparseable value so
 * callers can omit the line rather than render a blank or "NaN%".
 */
export function formatPct(decimalStr) {
  if (decimalStr == null) return null
  const n = Number(decimalStr)
  if (!Number.isFinite(n)) return null
  return `${(n * 100).toFixed(1)}%`
}

/**
 * Resolve a SIGNED decimal-fraction string into a calm direction indicator:
 * the sign of the value decides the direction (down = sky-blue ▼, up = green
 * ▲) and the label carries the UNSIGNED magnitude — `MarketIndicator` supplies
 * the single sign glyph, so we never embed one here (that would double it).
 *
 * Crucially this keeps color honest: a NEGATIVE forward return (a drop that
 * kept falling a year on) renders as a sky-blue ▼ loss, never a green gain.
 * Returns `null` for a missing/unparseable value so the caller omits the line.
 */
export function directionAndMagnitude(decimalStr) {
  if (decimalStr == null) return null
  const n = Number(decimalStr)
  if (!Number.isFinite(n)) return null
  return {
    direction: n < 0 ? 'down' : 'up',
    magnitude: `${(Math.abs(n) * 100).toFixed(1)}%`,
  }
}

/**
 * The always-present citation line: `"{source} · as of {as_of}"`. The view must
 * cite `source` + `as_of` on every state, so this is never omitted.
 */
export function sourceLine(record) {
  const source = record?.source ?? 'the record'
  const asOf = record?.as_of ?? 'an unknown date'
  return `${source} · as of ${asOf}`
}

/**
 * Human phrasing for whether a single historical episode recovered back to its
 * pre-drop peak. `recovered === false` (or a null recovery date) means the
 * episode had not yet returned to breakeven in the data — we say so plainly
 * rather than showing a blank.
 */
export function recoveryPhrase(window) {
  if (window?.recovered && window?.recovery_days != null) {
    const days = window.recovery_days
    return `recovered to breakeven in ${days} trading ${days === 1 ? 'day' : 'days'}`
  }
  return 'not yet recovered back to breakeven'
}
