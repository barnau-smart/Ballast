import { useEffect, useState } from 'react'
import { DecisionReplay } from '../components/DecisionReplay.jsx'
import { apiFetch } from '../lib/session.js'
import { formatDay } from '../lib/datetime.js'
import './screen.css'
import './Decisions.css'

/**
 * The Decisions surface (Story 4.10, FR16): a read-only history of the user's
 * co-signed decisions and, on selection, the verbatim replay INLINE (no new
 * route — the app holds exactly six surfaces; replay is selected-decision
 * state). Fetches `GET /api/coach/decisions` on mount; selecting a decision
 * fetches `GET /api/coach/decisions/{id}` and renders `<DecisionReplay>`.
 *
 * Presentation-only (AD-1): fetch + render, no business logic; the backend is
 * the sole source of the frozen snapshot. Calm, honest states: a calm loading
 * line (no spinner urgency), a gentle empty invite when the record is truly
 * empty, and — distinct from that — a calm "couldn't load" note when the fetch
 * itself failed (so a load failure is never dishonestly reported as "you have
 * no decisions"). Mirrors the Dashboard's `useState`/`useEffect`/`apiFetch` +
 * active-flag pattern; never an alarming error screen.
 */
export function Decisions() {
  const [status, setStatus] = useState('loading')
  const [decisions, setDecisions] = useState([])
  const [selectedId, setSelectedId] = useState(null)
  // A monotonic nonce so re-selecting the SAME decision (e.g. to retry after a
  // detail-fetch error) still re-fires the detail effect.
  const [reloadKey, setReloadKey] = useState(0)
  const [detail, setDetail] = useState(null)
  const [detailStatus, setDetailStatus] = useState('idle')

  useEffect(() => {
    let active = true
    apiFetch('/api/coach/decisions')
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return res.json()
      })
      .then((data) => {
        if (!active) return
        setDecisions(Array.isArray(data.decisions) ? data.decisions : [])
        setStatus('ready')
      })
      .catch(() => {
        if (!active) return
        // Honest degrade: a load failure is NOT an empty history. Show a calm
        // "couldn't load" note (distinct from the empty invite) so we never tell
        // a user with real decisions that they have none. Never an error screen.
        setDecisions([])
        setStatus('error')
      })
    return () => {
      active = false
    }
  }, [])

  function selectDecision(decisionId) {
    setSelectedId(decisionId)
    setReloadKey((key) => key + 1)
  }

  useEffect(() => {
    if (selectedId == null) return
    let active = true
    setDetail(null)
    setDetailStatus('loading')
    apiFetch(`/api/coach/decisions/${selectedId}`)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return res.json()
      })
      .then((data) => {
        if (!active) return
        setDetail(data)
        setDetailStatus('ready')
      })
      .catch(() => {
        if (!active) return
        // Calm fallback: no error screen, just the gentle "couldn't reopen" note.
        setDetail(null)
        setDetailStatus('error')
      })
    return () => {
      active = false
    }
  }, [selectedId, reloadKey])

  return (
    <section className="ballast-screen">
      <p className="ballast-screen__eyebrow">decisions</p>
      <h1 className="ballast-screen__title">Decisions</h1>
      <p className="ballast-screen__prose">
        A running log of the choices you have weighed, so you can look back at
        your reasoning over time.
      </p>

      {status === 'loading' ? (
        <p className="ballast-decisions__calm" data-testid="decisions-loading">
          Looking back at your record…
        </p>
      ) : null}

      {status === 'error' ? (
        <div className="ballast-card" data-testid="decisions-load-error">
          We couldn’t load your record just now. Nothing is lost — check back in
          a moment.
        </div>
      ) : null}

      {status === 'ready' && decisions.length === 0 ? (
        <div className="ballast-card" data-testid="decisions-empty">
          No decisions on the record yet. When you co-sign a recommendation on
          the Coach, it will show up here — reasoning and all — for you to
          revisit.
        </div>
      ) : null}

      {status === 'ready' && decisions.length > 0 ? (
        <div className="ballast-decisions">
          <ul className="ballast-decisions__list" data-testid="decisions-list">
            {decisions.map((decision) => (
              <li key={decision.decision_id}>
                <button
                  type="button"
                  className={
                    decision.decision_id === selectedId
                      ? 'ballast-decisions__item ballast-decisions__item--active'
                      : 'ballast-decisions__item'
                  }
                  aria-pressed={decision.decision_id === selectedId}
                  onClick={() => selectDecision(decision.decision_id)}
                  data-testid={`decisions-item-${decision.decision_id}`}
                >
                  <span className="ballast-decisions__item-label">
                    {decision.action_label}
                  </span>
                  <span className="ballast-decisions__item-meta">
                    {decision.symbol ? `${decision.symbol} · ` : ''}
                    {decision.outcome_status} · {formatDay(decision.co_signed_at)}
                  </span>
                </button>
              </li>
            ))}
          </ul>

          <div
            className="ballast-decisions__replay"
            aria-live="polite"
            data-testid="decisions-replay-region"
          >
            {selectedId == null ? (
              <p
                className="ballast-decisions__calm"
                data-testid="decisions-prompt"
              >
                Pick a decision to replay the reasoning you co-signed.
              </p>
            ) : null}
            {detailStatus === 'loading' ? (
              <p
                className="ballast-decisions__calm"
                data-testid="decisions-detail-loading"
              >
                Reopening that decision…
              </p>
            ) : null}
            {detailStatus === 'error' ? (
              <p
                className="ballast-decisions__calm"
                data-testid="decisions-detail-error"
              >
                Couldn’t reopen that one just now — pick it again in a moment.
              </p>
            ) : null}
            {detailStatus === 'ready' && detail ? (
              <DecisionReplay detail={detail} />
            ) : null}
          </div>
        </div>
      ) : null}
    </section>
  )
}
