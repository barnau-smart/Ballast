import { useEffect, useState } from 'react'
import { apiFetch } from '../lib/session.js'
import { PrecedentEvidence } from './PrecedentEvidence.jsx'
// The `.ballast-precedent*` styles (shared with PrecedentEvidence and used here
// for the loading/fallback states) now live in PrecedentEvidence.css.
import './PrecedentEvidence.css'

/**
 * The recovery-precedent view (Story 3.3, FR15 / UX-DR5).
 *
 * Presentation-only (AD-1): on mount it fetches the backend's evidence record
 * (`GET /api/precedent/recovery`) and RENDERS it via the shared
 * `PrecedentEvidence` block — it computes no market number and invents no
 * statistic. The backend is the sole source of the figures.
 *
 * Calm + honest + accessible, always (the color/sign/honesty rules now live in
 * `PrecedentEvidence`, shared with the headline contextualizer so the two
 * calming views cannot drift):
 * - drops/drawdowns → sky-blue ▼, recoveries/forward-gains → green ▲. NEVER
 *   red/pink, NEVER color alone.
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

  // status === 'ready' — a real record is always present here. The shared
  // `PrecedentEvidence` block renders the event-precedent or strategy record.
  return <PrecedentEvidence record={record} />
}
