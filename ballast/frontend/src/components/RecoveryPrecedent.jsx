import { useEffect, useState } from 'react'
import { MarketIndicator } from './MarketIndicator.jsx'
import { useReducedMotion } from '../hooks/useReducedMotion.js'
import { apiFetch } from '../lib/session.js'
import {
  directionAndMagnitude,
  formatPct,
  recoveryPhrase,
  sourceLine,
} from '../lib/precedent.js'
import './RecoveryPrecedent.css'

/**
 * The recovery-precedent view (Story 3.3, FR15 / UX-DR5).
 *
 * Presentation-only (AD-1): on mount it fetches the backend's evidence record
 * (`GET /api/precedent/recovery`) and RENDERS it — it computes no market number
 * and invents no statistic. The backend is the sole source of the figures.
 *
 * Calm + honest + accessible, always:
 * - drops/drawdowns → sky-blue ▼, recoveries/forward-gains → green ▲, via
 *   `MarketIndicator`. NEVER red/pink, NEVER color alone — every glyph is paired
 *   with a sign + a real text label.
 * - always cites `source` + `as_of`.
 * - never a dead end: an `event-precedent` record → the matched-drops block; a
 *   `strategy` record → the strategy-default rationale; a fetch failure → a calm
 *   static fallback rationale (mirrors `Dashboard`). Never an error screen.
 * - respects `prefers-reduced-motion` (the disclosure expands instantly).
 * - pull-only: rendered when the user opens the Coach surface, never pushed.
 */

// Calm static fallback shown ONLY on transport failure (non-2xx / network
// error). The engine always returns a record, so a real response is always
// renderable; this covers the case where the backend is unreachable. It reads
// like the strategy default — reassuring, never an error dump.
const FETCH_FALLBACK = {
  headline: 'Stay the course with your plan.',
  body:
    'We could not reach the market history just now. That does not change the ' +
    'plan: broad, steady index investing has recovered from every past drop ' +
    'given time. Make your regular contribution and check back in a moment.',
}

export function RecoveryPrecedent() {
  const [status, setStatus] = useState('loading')
  const [record, setRecord] = useState(null)
  const [expanded, setExpanded] = useState(false)
  const reduced = useReducedMotion()

  useEffect(() => {
    let active = true
    apiFetch('/api/precedent/recovery')
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return res.json()
      })
      .then((data) => {
        if (!active) return
        setRecord(data)
        setStatus('ready')
      })
      .catch(() => {
        if (!active) return
        // Fail quiet: a calm static fallback rationale, never an error screen.
        setStatus('failed')
      })
    return () => {
      active = false
    }
  }, [])

  if (status === 'loading') {
    return (
      <div className="ballast-precedent" data-testid="precedent-loading">
        <p className="ballast-precedent__note">
          Looking back at how drops like this one have played out…
        </p>
      </div>
    )
  }

  if (status === 'failed') {
    return (
      <div className="ballast-precedent" data-testid="precedent-fallback">
        <p className="ballast-precedent__headline">{FETCH_FALLBACK.headline}</p>
        <p className="ballast-precedent__body">{FETCH_FALLBACK.body}</p>
      </div>
    )
  }

  // status === 'ready' — a real record is always present here.
  if (record?.kind === 'event-precedent') {
    return (
      <EventPrecedent
        record={record}
        expanded={expanded}
        onToggle={() => setExpanded((v) => !v)}
        reduced={reduced}
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

function EventPrecedent({ record, expanded, onToggle, reduced }) {
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
            aria-controls="precedent-windows"
            onClick={onToggle}
            data-testid="precedent-toggle"
          >
            {expanded ? 'Hide the matched drops' : 'See the matched drops'}
          </button>

          {expanded ? (
            <ul
              id="precedent-windows"
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
