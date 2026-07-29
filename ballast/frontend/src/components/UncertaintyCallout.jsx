import './UncertaintyCallout.css'

/**
 * The violet uncertainty callout (epic UX): always present on a recommendation
 * or its replay, it names — in plain, real DOM text — what was explicitly
 * uncertain when the decision was blessed. Color is the violet
 * `--ballast-color-uncertainty` token; it is never the sole signal (a heading
 * plus the list text carry the meaning for screen readers).
 *
 * Presentation-only (AD-1): it renders the `uncertainties` string list the
 * backend blessed, computing nothing. An empty/missing list renders nothing.
 */
export function UncertaintyCallout({ uncertainties }) {
  const items = Array.isArray(uncertainties) ? uncertainties : []
  if (items.length === 0) return null

  return (
    <section className="ballast-uncertainty" data-testid="uncertainty-callout">
      <p className="ballast-uncertainty__heading">What was uncertain</p>
      <ul className="ballast-uncertainty__list">
        {items.map((item, i) => (
          <li key={i} className="ballast-uncertainty__item">
            {item}
          </li>
        ))}
      </ul>
    </section>
  )
}
