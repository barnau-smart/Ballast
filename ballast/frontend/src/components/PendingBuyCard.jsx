import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { apiFetch } from '../lib/session.js'
import './PendingBuyCard.css'

/**
 * The durable pending-buy surface (Story 9.3).
 *
 * Pull-only (surfaces on visit, never pushed): on mount it fetches the user's
 * awaiting-funds pending buys (`GET /api/cash/pending-buys`) and renders them.
 * Each pending buy persists across sessions, so a missed notification can't lose
 * the intent. When `funds_ready` (computed LIVE by the backend from the
 * authoritative ready-to-trade cash — never a fabricated timer), a calm "resume
 * your buy" action mints a proposed buy (`POST …/resume`) and then submits it
 * through the SAME `/api/coach/approve` co-sign path. Otherwise a calm "waiting
 * for your cash to settle" state shows — never a nudge, never red.
 *
 * Presentation-only (AD-1): it renders backend-computed state and routes the
 * user through the unchanged approve flow; it computes no money number.
 */
export function PendingBuyCard() {
  const [status, setStatus] = useState('loading') // loading | ready | empty | failed
  const [pending, setPending] = useState([])
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    apiFetch('/api/cash/pending-buys')
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return res.json()
      })
      .then((data) => {
        if (!mounted.current) return
        const rows = data?.pending_buys ?? []
        setPending(rows)
        setStatus(rows.length === 0 ? 'empty' : 'ready')
      })
      .catch(() => {
        if (!mounted.current) return
        // Fail quiet — this is an ancillary surface; no error screen.
        setStatus('failed')
      })
    return () => {
      mounted.current = false
    }
  }, [])

  // Nothing to surface (or a quiet failure) — render nothing (pull-only, no noise).
  if (status === 'loading' || status === 'empty' || status === 'failed') return null

  return (
    <div className="ballast-pending-buys" data-testid="pending-buys">
      <p className="ballast-pending-buys__eyebrow">Buys waiting on settled cash</p>
      {pending.map((p) => (
        <PendingBuyRow key={p.pending_buy_id} pending={p} />
      ))}
    </div>
  )
}

function PendingBuyRow({ pending }) {
  // idle | resuming | resumed | placing | placed | reconnect | in-progress
  // | refused | signed-out | indeterminate | cancelled | error
  const [phase, setPhase] = useState('idle')
  const [message, setMessage] = useState('')
  const [decisionId, setDecisionId] = useState(null)
  const [outcome, setOutcome] = useState(null)
  const busyRef = useRef(false)

  const intent = pending.buy_intent

  async function readMessage(res) {
    try {
      const data = await res.json()
      return data?.error?.message ?? data?.detail ?? data?.message ?? ''
    } catch {
      return ''
    }
  }

  async function onResume() {
    if (busyRef.current) return
    busyRef.current = true
    setPhase('resuming')
    setMessage('')
    try {
      const res = await apiFetch(
        `/api/cash/pending-buys/${pending.pending_buy_id}/resume`,
        { method: 'POST' },
      )
      if (res.ok) {
        const data = await res.json()
        setDecisionId(data.decision_id)
        setPhase('resumed')
        return
      }
      const detail = await readMessage(res)
      if (res.status === 401) {
        setPhase('signed-out')
      } else if (res.status === 409) {
        // Funds haven't settled yet (or already resumed) — calm, honest.
        setMessage(detail)
        setPhase('idle')
      } else {
        setMessage(detail)
        setPhase('error')
      }
    } catch {
      setPhase('error')
    } finally {
      busyRef.current = false
    }
  }

  async function onApprove() {
    if (!decisionId) return
    if (busyRef.current) return
    busyRef.current = true
    setPhase('placing')
    setMessage('')
    try {
      const res = await apiFetch('/api/coach/approve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          decision_id: decisionId,
          order_intent: {
            symbol: intent.symbol,
            side: intent.side,
            amount: intent.amount,
          },
        }),
      })
      if (res.ok) {
        let data
        try {
          data = await res.json()
        } catch {
          setPhase('indeterminate')
          return
        }
        setOutcome(data)
        setPhase('placed')
        return
      }
      const detail = await readMessage(res)
      if (res.status === 401) {
        setPhase('signed-out')
      } else if (res.status === 409) {
        setMessage(detail)
        setPhase(
          /moment|in progress|being approved|already/i.test(detail)
            ? 'in-progress'
            : 'reconnect',
        )
      } else if (res.status === 422) {
        setMessage(detail)
        setPhase('refused')
      } else {
        setPhase('indeterminate')
      }
    } catch {
      setPhase('indeterminate')
    } finally {
      busyRef.current = false
    }
  }

  async function onCancel() {
    if (busyRef.current) return
    busyRef.current = true
    try {
      const res = await apiFetch(
        `/api/cash/pending-buys/${pending.pending_buy_id}/cancel`,
        { method: 'POST' },
      )
      if (res.ok) setPhase('cancelled')
    } catch {
      // Leave the row as-is on a transient failure; the user can retry.
    } finally {
      busyRef.current = false
    }
  }

  if (phase === 'cancelled') return null

  return (
    <div className="ballast-pending-buy" data-testid="pending-buy-row">
      <p className="ballast-pending-buy__line">
        Buy <span data-testid="pending-buy-symbol">{intent.symbol}</span> for{' '}
        <span data-testid="pending-buy-amount">${intent.amount}</span> — set aside
        until your cash settles.
      </p>

      {pending.as_of ? (
        <p className="ballast-pending-buy__asof" data-testid="pending-buy-asof">
          Cash checked as of {new Date(pending.as_of).toLocaleDateString()}.
        </p>
      ) : null}

      {!pending.funds_ready ? (
        <p
          className="ballast-pending-buy__waiting"
          data-testid="pending-buy-waiting"
        >
          Waiting for your cash to settle. This will be ready to resume on its own
          once there’s enough ready-to-trade cash to cover it — no action needed
          now.
        </p>
      ) : phase === 'idle' || phase === 'resuming' ? (
        <div className="ballast-pending-buy__actions">
          <button
            type="button"
            className="ballast-pending-buy__resume"
            data-testid="pending-buy-resume"
            onClick={onResume}
            disabled={phase === 'resuming'}
          >
            {phase === 'resuming' ? 'Resuming…' : 'Resume your buy'}
          </button>
          <button
            type="button"
            className="ballast-pending-buy__cancel"
            data-testid="pending-buy-cancel"
            onClick={onCancel}
          >
            cancel
          </button>
        </div>
      ) : null}

      {phase === 'resumed' ? (
        <div className="ballast-pending-buy__actions" data-testid="pending-buy-resumed">
          <p className="ballast-pending-buy__ready">
            Your cash has settled — this buy is ready. Review and submit it when
            you’re ready.
          </p>
          <button
            type="button"
            className="ballast-pending-buy__resume"
            data-testid="pending-buy-approve"
            onClick={onApprove}
          >
            Approve &amp; Co-sign
          </button>
        </div>
      ) : null}

      {message && (phase === 'idle' || phase === 'error') ? (
        <p
          className="ballast-pending-buy__msg"
          data-testid="pending-buy-message"
          role="status"
        >
          {message}
        </p>
      ) : null}

      {phase === 'placing' ? (
        <p className="ballast-pending-buy__msg" role="status">
          Placing…
        </p>
      ) : null}

      {phase === 'reconnect' ? (
        <p
          className="ballast-pending-buy__msg"
          data-testid="pending-buy-reconnect"
          role="status"
        >
          {message || 'Reconnect your Schwab account to place this.'}{' '}
          <Link className="ballast-pending-buy__link" to="/onboarding">
            Reconnect Schwab
          </Link>
        </p>
      ) : null}

      {phase === 'in-progress' ? (
        <p className="ballast-pending-buy__msg" role="status">
          {message ||
            'This is being approved right now — give it a moment and check your Decisions.'}
        </p>
      ) : null}

      {phase === 'refused' ? (
        <p
          className="ballast-pending-buy__msg"
          data-testid="pending-buy-refused"
          role="status"
        >
          {message || 'That buy can’t be placed as-is. Nothing was placed.'}
        </p>
      ) : null}

      {phase === 'signed-out' ? (
        <p className="ballast-pending-buy__msg" role="status">
          Your session ended. Sign in and try again.{' '}
          <Link className="ballast-pending-buy__link" to="/auth">
            Sign in
          </Link>
        </p>
      ) : null}

      {phase === 'indeterminate' ? (
        <p className="ballast-pending-buy__msg" role="status">
          We couldn’t confirm whether that went through. Check your Decisions
          before trying again.{' '}
          <Link className="ballast-pending-buy__link" to="/decisions">
            Open Decisions
          </Link>
        </p>
      ) : null}

      {phase === 'placed' && outcome ? (
        <p
          className="ballast-pending-buy__msg"
          data-testid="pending-buy-placed"
          role="status"
        >
          Buy submitted (outcome: {outcome.status}).
        </p>
      ) : null}
    </div>
  )
}
