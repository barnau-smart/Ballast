import { useEffect, useRef, useState } from 'react'
import { apiFetch } from '../lib/session.js'
import { PrecedentEvidence } from './PrecedentEvidence.jsx'
// Shared `.ballast-precedent*` styles for the failed-state fallback (which
// renders without mounting PrecedentEvidence), so the fallback is styled
// independently of render order.
import './PrecedentEvidence.css'
import './HeadlineContextualizer.css'

/**
 * The on-demand headline contextualizer (Story 3.5, FR20).
 *
 * A user rattled by a scary headline submits it and gets back what the market
 * ACTUALLY did in comparable drops — not a take on the news. Pull-only: nothing
 * is fetched on mount; only on user submit does it POST the headline to
 * `/api/precedent/contextualize`. The backend ignores the headline's content
 * entirely (v1 matching is drawdown-band only) and returns the deterministic
 * drawdown-keyed evidence record, which we render via the shared
 * `PrecedentEvidence` block.
 *
 * Presentation-only (AD-1): computes no market number. Calm + honest +
 * accessible: the framing line explicitly does NOT judge the news; never
 * red/pink, never color alone, never a nudge/CTA/urgency, reduced-motion
 * respected (the evidence block owns its own reduced-motion handling). A fetch
 * failure degrades to a calm static fallback — never an error screen, never a
 * dead end.
 */

// The neutral, non-judging framing shown above the evidence. It states plainly
// that Ballast does not weigh in on the news itself — only on what the market
// has historically done in drops of this size. Calm, no nudge/CTA/urgency.
const FRAMING =
  "Ballast doesn't weigh in on the news itself — here's what the market has " +
  'actually done in drops like today’s.'

// Framing for a HYPOTHETICAL scenario (Story 3.6, FR20). Still non-judging and
// never a prediction — it names the queried size and points to the base rate.
const HYPOTHETICAL_FRAMING =
  "This isn't a forecast — here's what the record shows for drops of that size."

// The calm scenario options (Story 3.6). Each carries a `drawdown` fraction the
// backend uses to centre a HYPOTHETICAL magnitude match. Framed as a neutral
// "want to see a bigger drop?" choice — never a fear nudge, never urgency.
// `null` is the default: current conditions, no hypothetical.
const SCENARIOS = [
  { key: 'current', label: 'Right now', drawdown: null },
  { key: 'dip', label: 'A dip ~5%', drawdown: 0.05 },
  { key: 'correction', label: 'A correction ~10%', drawdown: 0.1 },
  { key: 'bear', label: 'A bear market ~20%', drawdown: 0.2 },
  { key: 'crash', label: 'A crash ~35%', drawdown: 0.35 },
]

// Calm static fallback on transport failure (non-2xx / network error). Reads
// like the strategy default — reassuring, plan-focused, never an error dump.
const FETCH_FALLBACK = {
  headline: 'Stay the course with your plan.',
  body:
    'We could not reach the market history just now. That does not change the ' +
    'plan: broad, steady index investing has recovered from every past drop ' +
    'given time. Make your regular contribution and check back in a moment.',
}

export function HeadlineContextualizer() {
  const [headline, setHeadline] = useState('')
  const [status, setStatus] = useState('idle') // idle | submitting | ready | failed
  const [record, setRecord] = useState(null)
  // Which scenario the result reflects — kept so the framing can name the
  // hypothetical size. `null` (default) = current conditions.
  const [scenario, setScenario] = useState(null)

  // Guard against a response resolving after the component unmounts (a submit
  // in flight when the user navigates away), which would setState on an
  // unmounted component — mirrors the `active` flag in `RecoveryPrecedent`.
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  const trimmedEmpty = headline.trim().length === 0
  const inFlight = status === 'submitting'
  const submitDisabled = trimmedEmpty || inFlight

  // The single fetch path, shared by the headline submit and the scenario
  // chips. `drawdown` (a number) drives a HYPOTHETICAL match when set; when
  // null the body is exactly `{ headline }` (current-conditions default).
  async function fetchPrecedent(drawdown) {
    if (trimmedEmpty) return
    setStatus('submitting')
    setScenario(drawdown)
    const body = drawdown == null ? { headline } : { headline, drawdown }
    try {
      const res = await apiFetch('/api/precedent/contextualize', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      if (!mounted.current) return
      setRecord(data)
      setStatus('ready')
    } catch {
      // Fail quiet: a calm static fallback, never an error screen.
      if (!mounted.current) return
      setStatus('failed')
    }
  }

  async function onSubmit(event) {
    event.preventDefault()
    if (submitDisabled) return
    await fetchPrecedent(null)
  }

  return (
    <div className="ballast-headline" data-testid="headline-contextualizer">
      <form className="ballast-headline__form" onSubmit={onSubmit}>
        <label className="ballast-headline__label" htmlFor="headline-input">
          Reacting to a headline? Paste it here and see what the market has done
          in drops like this one.
        </label>
        <textarea
          id="headline-input"
          className="ballast-headline__input"
          data-testid="headline-input"
          value={headline}
          maxLength={500}
          rows={2}
          placeholder="e.g. “Stocks slide as markets digest the latest news”"
          onChange={(e) => setHeadline(e.target.value)}
        />
        <button
          type="submit"
          className="ballast-headline__submit"
          data-testid="headline-submit"
          disabled={submitDisabled}
        >
          {inFlight ? 'Looking back…' : 'Show me the precedent'}
        </button>

        <div
          className="ballast-headline__scenarios"
          role="group"
          aria-label="Want to see what history shows for a bigger drop?"
          data-testid="headline-scenarios"
        >
          <span className="ballast-headline__scenarios-label">
            Or see what the record shows for a bigger drop:
          </span>
          {SCENARIOS.map((s) => (
            <button
              key={s.key}
              type="button"
              className="ballast-headline__chip"
              data-testid={`headline-scenario-${s.key}`}
              disabled={trimmedEmpty || inFlight}
              aria-pressed={status === 'ready' && scenario === s.drawdown}
              onClick={() => fetchPrecedent(s.drawdown)}
            >
              {s.label}
            </button>
          ))}
        </div>
      </form>

      {status === 'submitting' ? (
        <p className="ballast-headline__note" data-testid="headline-submitting">
          Looking back at how drops like this one have played out…
        </p>
      ) : null}

      {status === 'ready' ? (
        <div className="ballast-headline__result" data-testid="headline-result">
          <p className="ballast-headline__framing" data-testid="headline-framing">
            {scenario == null ? FRAMING : HYPOTHETICAL_FRAMING}
          </p>
          <PrecedentEvidence record={record} idPrefix="headline-precedent" />
        </div>
      ) : null}

      {status === 'failed' ? (
        <div className="ballast-precedent" data-testid="headline-fallback">
          <p className="ballast-precedent__headline">{FETCH_FALLBACK.headline}</p>
          <p className="ballast-precedent__body">{FETCH_FALLBACK.body}</p>
        </div>
      ) : null}
    </div>
  )
}
