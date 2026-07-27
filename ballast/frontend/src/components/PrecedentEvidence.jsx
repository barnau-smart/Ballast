import { useState } from 'react'
import { MarketIndicator } from './MarketIndicator.jsx'
import { useReducedMotion } from '../hooks/useReducedMotion.js'
import {
  directionAndMagnitude,
  formatPct,
  recoveryPhrase,
  sourceLine,
} from '../lib/precedent.js'
import './PrecedentEvidence.css'

/**
 * The single source of truth for rendering a Precedent Engine `EvidenceRecord`
 * as a calm, color-honest data-block (Story 3.5; extracted from Story 3.3's
 * `RecoveryPrecedent`). Both calming views — the passive recovery-precedent view
 * and the on-demand headline contextualizer — render THROUGH this component, so
 * they cannot drift on the color/honesty rules.
 *
 * Presentation-only (AD-1): it RENDERS the record's numbers, computing none.
 * Calm + honest + accessible, always:
 * - drops/drawdowns → sky-blue ▼, forward-gains → green ▲, via `MarketIndicator`.
 *   NEVER red/pink, NEVER color alone — every glyph is paired with a sign + a
 *   real text label; the label carries the UNSIGNED magnitude (MarketIndicator
 *   owns the single sign glyph, so we never embed one — that doubled the sign).
 * - always cites `source` + `as_of`.
 * - never a dead end: an `event-precedent` record → the matched-drops block; a
 *   `strategy` record → the strategy-default rationale.
 * - respects `prefers-reduced-motion` (the disclosure expands instantly).
 *
 * Owns its OWN expand state so it is a self-contained record renderer.
 *
 * `idPrefix` namespaces the disclosure's DOM `id` / `aria-controls` so two
 * instances mounted on the same surface (the recovery view AND the headline
 * contextualizer both live on Coach) never collide on a duplicate `id`, which
 * would be invalid HTML and would break the screen-reader `aria-controls`
 * association. Defaults to `'precedent'` to preserve Story 3.3's ids/behavior.
 */
export function PrecedentEvidence({ record, idPrefix = 'precedent' }) {
  const [expanded, setExpanded] = useState(false)
  const reduced = useReducedMotion()

  if (record?.kind === 'event-precedent') {
    return (
      <EventPrecedent
        record={record}
        expanded={expanded}
        onToggle={() => setExpanded((v) => !v)}
        reduced={reduced}
        windowsId={`${idPrefix}-windows`}
      />
    )
  }

  // Strategy fallback (no band match / all-time high / insufficient data) — the
  // rationale, never an empty state.
  return (
    <div className="ballast-precedent" data-testid="precedent-strategy">
      <p className="ballast-precedent__headline">{record?.statement}</p>
      <p className="ballast-precedent__source" data-testid="precedent-source">
        {sourceLine(record)}
      </p>
    </div>
  )
}

function EventPrecedent({ record, expanded, onToggle, reduced, windowsId }) {
  const stats = record.stats ?? {}
  const drawdownPct = formatPct(stats.initial_drawdown_pct)
  const forward = directionAndMagnitude(stats.forward_return_1yr_median)
  const windows = Array.isArray(stats.windows) ? stats.windows : []

  return (
    <div className="ballast-precedent" data-testid="precedent-event">
      <p className="ballast-precedent__headline" data-testid="precedent-statement">
        {record.statement}
      </p>

      <dl className="ballast-precedent__stats">
        {drawdownPct ? (
          <div className="ballast-precedent__stat" data-testid="precedent-drawdown">
            <dt className="ballast-precedent__stat-term">Where it stands now</dt>
            <dd className="ballast-precedent__stat-value">
              <MarketIndicator
                direction="down"
                label={`${drawdownPct} below its recent peak`}
              />
            </dd>
          </div>
        ) : null}

        {forward ? (
          <div className="ballast-precedent__stat" data-testid="precedent-forward">
            <dt className="ballast-precedent__stat-term">
              What came next, on average
            </dt>
            <dd className="ballast-precedent__stat-value">
              <MarketIndicator
                direction={forward.direction}
                label={`${forward.magnitude} median one-year return after similar drops`}
              />
            </dd>
          </div>
        ) : null}
      </dl>

      <p className="ballast-precedent__source" data-testid="precedent-source">
        {sourceLine(record)}
      </p>

      {windows.length > 0 ? (
        <div className="ballast-precedent__disclosure">
          <button
            type="button"
            className="ballast-precedent__toggle"
            aria-expanded={expanded}
            aria-controls={windowsId}
            onClick={onToggle}
            data-testid="precedent-toggle"
          >
            {expanded ? 'Hide the matched drops' : 'See the matched drops'}
          </button>

          {expanded ? (
            <ul
              id={windowsId}
              className={
                reduced
                  ? 'ballast-precedent__windows'
                  : 'ballast-precedent__windows ballast-precedent__windows--animated'
              }
              data-testid="precedent-windows"
            >
              {windows.map((w, i) => (
                <PrecedentWindow
                  key={`${w.peak_date}-${w.trough_date}`}
                  window={w}
                  index={i}
                />
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}

function PrecedentWindow({ window: w, index }) {
  const drawdownPct = formatPct(w.drawdown_pct)
  const forward = directionAndMagnitude(w.forward_return_1yr)

  return (
    <li className="ballast-precedent__window" data-testid={`precedent-window-${index}`}>
      <p className="ballast-precedent__window-dates">
        Peaked {w.peak_date}, bottomed {w.trough_date}
        {w.recovery_date ? `, recovered ${w.recovery_date}` : ''}
      </p>
      <div className="ballast-precedent__window-stats">
        {drawdownPct ? (
          <MarketIndicator direction="down" label={`${drawdownPct} drawdown`} />
        ) : null}
        <span className="ballast-precedent__window-recovery">
          {recoveryPhrase(w)}
        </span>
        {forward ? (
          <MarketIndicator
            direction={forward.direction}
            label={`${forward.magnitude} over the following year`}
          />
        ) : null}
      </div>
    </li>
  )
}
