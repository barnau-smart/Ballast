const MONTHS = [
  'Jan',
  'Feb',
  'Mar',
  'Apr',
  'May',
  'Jun',
  'Jul',
  'Aug',
  'Sep',
  'Oct',
  'Nov',
  'Dec',
]

/**
 * Format an ISO-8601 timestamp/date into a calm, human day label (e.g.
 * `"Jul 28, 2026"`) for a beginner-facing surface — never the raw wire form
 * with microseconds and a `+00:00` offset.
 *
 * Deterministic and timezone/locale-independent: it reads the leading
 * `YYYY-MM-DD` (already UTC on our wire) rather than routing through the
 * `Date`/`Intl` machinery, so it renders identically in every environment
 * (and in tests). Returns the raw date portion if the input is unparseable,
 * and `''` for a null/empty input.
 */
export function formatDay(iso) {
  if (!iso) return ''
  const datePart = String(iso).slice(0, 10)
  const [year, month, day] = datePart.split('-')
  const monthIndex = Number(month) - 1
  if (!year || Number.isNaN(monthIndex) || !MONTHS[monthIndex] || !day) {
    return datePart
  }
  return `${MONTHS[monthIndex]} ${Number(day)}, ${year}`
}
