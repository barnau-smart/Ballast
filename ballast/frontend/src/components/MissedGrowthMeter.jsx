import { useEffect, useState } from 'react'
import { MarketIndicator } from './MarketIndicator.jsx'
import { useReducedMotion } from '../hooks/useReducedMotion.js'
import { apiFetch } from '../lib/session.js'
import {
  amountLabel,
  directionAndMagnitude,
  reserveLine,
  sourceLine,
  windowLine,
  yieldNote,
} from '../lib/missedGrowth.js'
import './MissedGrowthMeter.css'

/**
 * The missed-growth meter (Story 3.4, FR19 / UX-DR5).
 *
 * Presentation-only (AD-1): on mount it fetches the backend's estimate
 * (`GET /api/precedent/missed-growth`) and RENDERS it — it computes no market
 * number and invents no figure. The deterministic engine is the sole source.
 *
 * Calm + honest + accessible, always:
 * - the figure's DIRECTION is derived from the value's SIGN via
 *   `MarketIndicator`: a positive figure (growth missed) → green ▲, a negative
 *   figure (loss AVOIDED) → sky-blue ▼. A market decline is NEVER framed as a
 *   "cost" of holding cash. NEVER red/pink, NEVER color alone — every glyph is
 *   paired with a sign + a real text label.
 * - always cites `source` + the window (`as_of` + trailing period).
 * - never a dead end: figure / no-idle-cash / insufficient-history / a calm
 *   static fetch-fail fallback (mirrors `Dashboard` / `RecoveryPrecedent`).
 *   Never an error screen.
 * - respects `prefers-reduced-motion` (the meter appears instantly).
 * - pull-only: rendered when the user opens the Dashboard, never pushed. No
 *   nudge, no FOMO, no urgency, no call to action.
 */

// Calm static fallback shown ONLY on transport failure (non-2xx / network
// error). Informational, never an error dump, never a nudge.
const FETCH_FALLBACK = {
  headline: 'A quiet note on idle cash.',
  body:
    'We could not reach the market history just now, so there is no estimate to ' +
    'show at the moment. Nothing needs doing — check back in a little while.',
}

export function MissedGrowthMeter() {
  const [status, setStatus] = useState('loading')
  const [estimate, setEstimate] = useState(null)
  const reduced = useReducedMotion()

  useEffect(() => {
    let active = true
    apiFetch('/api/precedent/missed-growth')
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return res.json()
      })
      .then((data) => {
        if (!active) return
        setEstimate(data)
        setStatus('ready')
      })
      .catch(() => {
        if (!active) return
        // Fail quiet: a calm static informational fallback, never an error screen.
        setStatus('failed')
      })
    return () => {
      active = false
    }
  }, [])

  if (status === 'loading') {
    return (
      <div className="ballast-missed-growth" data-testid="missed-growth-loading">
        <p className="ballast-missed-growth__note">
          Looking at what idle cash may have missed…
        </p>
      </div>
    )
  }

  if (status === 'failed') {
    return (
      <div className="ballast-missed-growth" data-testid="missed-growth-fallback">
        <p className="ballast-missed-growth__headline">{FETCH_FALLBACK.headline}</p>
        <p className="ballast-missed-growth__body">{FETCH_FALLBACK.body}</p>
      </div>
    )
  }

  // status === 'ready' — a real estimate is always present here.
  if (estimate?.reason === 'no_idle_cash') {
    return (
      <div className="ballast-missed-growth" data-testid="missed-growth-no-cash">
        <p className="ballast-missed-growth__headline">
          Nothing sitting idle right now.
        </p>
        <p className="ballast-missed-growth__body">{estimate?.statement}</p>
        <p className="ballast-missed-growth__source" data-testid="missed-growth-source">
          {sourceLine(estimate)}
        </p>
      </div>
    )
  }

  if (estimate?.reason === 'insufficient_history' || estimate?.sufficient === false) {
    return (
      <div
        className="ballast-missed-growth"
        data-testid="missed-growth-insufficient"
      >
        <p className="ballast-missed-growth__headline">
          Not enough market history yet.
        </p>
        <p className="ballast-missed-growth__body">{estimate?.statement}</p>
        <p className="ballast-missed-growth__source" data-testid="missed-growth-source">
          {sourceLine(estimate)}
        </p>
      </div>
    )
  }

  // Reserve covers all of the user's cash (Story 9.2) — a reassurance, not a
  // non-event. Its own calm block (no "Idle cash…" eyebrow, no $0.00 figure, no
  // yield disclosure — nothing is investable, so the yield assumption is moot).
  // The protected-reserve line reinforces the engine's statement.
  if (estimate?.reason === 'fully_reserved') {
    const reserve = reserveLine(estimate)
    return (
      <div
        className="ballast-missed-growth"
        data-testid="missed-growth-fully-reserved"
      >
        <p className="ballast-missed-growth__headline">Your reserve has you covered.</p>
        <p className="ballast-missed-growth__body">{estimate?.statement}</p>
        {reserve ? (
          <p
            className="ballast-missed-growth__reserve"
            data-testid="missed-growth-reserve"
          >
            {reserve}
          </p>
        ) : null}
        <p className="ballast-missed-growth__source" data-testid="missed-growth-source">
          {sourceLine(estimate)}
        </p>
      </div>
    )
  }

  // Figure present (sufficient && reason == null) — honest both directions.
  return (
    <MissedGrowthFigure estimate={estimate} reduced={reduced} />
  )
}

function MissedGrowthFigure({ estimate, reduced }) {
  // Direction is derived from the figure's SIGN (positive → green ▲, negative →
  // sky-blue ▼) — exactly like RecoveryPrecedent derives it for forward returns.
  // A flat window (or a sub-cent move that rounds to $0.00) suppresses the
  // ▲/▼ entirely, so a non-event is NEVER dressed up as a green +$0.00 gain.
  const value = Number(estimate?.forgone_growth)
  const isFlat = !Number.isFinite(value) || value === 0
  const figure = isFlat ? null : directionAndMagnitude(estimate?.forgone_growth)
  const label = isFlat ? null : amountLabel(estimate)

  // Calm supporting lines (Story 9.2). Both are null unless the honest condition
  // holds — a protected reserve figure is NEVER fabricated when the user has not
  // decided one (reserved==null), and the yield assumption is disclosed ONLY when
  // parked money is actually in the calc. The engine's statement already carries
  // these clauses verbatim; these lines make them individually legible.
  const reserve = reserveLine(estimate)
  const yieldDisclosure = yieldNote(estimate)

  return (
    <div
      className={
        reduced
          ? 'ballast-missed-growth'
          : 'ballast-missed-growth ballast-missed-growth--animated'
      }
      data-testid="missed-growth-figure"
    >
      <p className="ballast-missed-growth__eyebrow">Idle cash, over the past year</p>
      {/* Render the engine's honest statement VERBATIM (AD-1) — the backend owns
          both the number and its framing; the UI never re-authors the sentence. */}
      <p
        className="ballast-missed-growth__headline"
        data-testid="missed-growth-statement"
      >
        {estimate?.statement}
      </p>

      {figure && label ? (
        <p className="ballast-missed-growth__figure" data-testid="missed-growth-amount">
          <MarketIndicator direction={figure.direction} label={label} />
        </p>
      ) : null}

      <p className="ballast-missed-growth__window" data-testid="missed-growth-window">
        {windowLine(estimate)}
      </p>

      {reserve ? (
        <p
          className="ballast-missed-growth__reserve"
          data-testid="missed-growth-reserve"
        >
          {reserve}
        </p>
      ) : null}

      {yieldDisclosure ? (
        <p
          className="ballast-missed-growth__yield"
          data-testid="missed-growth-yield"
        >
          {yieldDisclosure}
        </p>
      ) : null}

      <p className="ballast-missed-growth__source" data-testid="missed-growth-source">
        {sourceLine(estimate)}
      </p>
    </div>
  )
}
