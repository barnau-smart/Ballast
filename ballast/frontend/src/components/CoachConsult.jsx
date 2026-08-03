import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { apiFetch } from '../lib/session.js'
import { CoachCard } from './CoachCard.jsx'
import './CoachConsult.css'

/**
 * The live coach consult (Story 4.11) — Epic 4's "emotional centerpiece"
 * (EXPERIENCE.md; mockups/coach-card.html). The interactive propose→approve
 * loop the SPA never had: ask the coach (`POST /api/coach/recommend`), read the
 * blessed recommendation (rendered by `CoachCard`), then either **Approve &
 * Co-sign** the order (`POST /api/coach/approve`) or say **not now** — in place.
 *
 * Presentation-only (AD-1): computes no money/market number; renders what the
 * backend blessed. Product contract (ratified): the order approved is the
 * USER'S stated order — `recommendation.order_intent ?? {symbol, side, amount}`
 * from the form — so a null recommendation intent (the default plan) does not
 * erase the order the user came in with (warn-not-block, Epic 4.5; explicit
 * approval is the gate, FR8/FR9). A consult with no concrete order shows
 * guidance only — NO approve control.
 *
 * Calm + honest + never-red: `/approve` needs a live session, so a 409 degrades
 * to a calm reconnect prompt (link to Onboarding), retryable; a 422 shows the
 * backend's calm reason verbatim; any of the five order statuses
 * (filled/partial/rejected/timeout/pending) is shown truthfully, never as an
 * error and never a phantom success. Brand-red appears ONLY on the co-sign
 * action; a loss/outcome value is never red. Guards setState-after-unmount.
 */
export function CoachConsult() {
  const [question, setQuestion] = useState('')
  const [symbol, setSymbol] = useState('')
  const [amount, setAmount] = useState('')
  const [side, setSide] = useState('') // '' | 'buy' | 'sell'

  // idle | thinking | ready | recommend-failed | signed-out
  const [phase, setPhase] = useState('idle')
  const [recommendation, setRecommendation] = useState(null)

  // idle | placing | placed | reconnect | refused | approve-failed
  const [approve, setApprove] = useState('idle')
  const [approveMessage, setApproveMessage] = useState('')
  const [outcome, setOutcome] = useState(null)

  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  const trimmedSymbol = symbol.trim()
  const trimmedAmount = amount.trim()
  const trimmedQuestion = question.trim()
  const amountValue = Number(trimmedAmount)
  const hasConcreteOrder =
    trimmedSymbol !== '' &&
    trimmedAmount !== '' &&
    Number.isFinite(amountValue) &&
    amountValue > 0 &&
    (side === 'buy' || side === 'sell')

  // Something must be on the table to ask: a question OR a concrete order.
  const askDisabled =
    phase === 'thinking' || (trimmedQuestion === '' && !hasConcreteOrder)

  // The order to co-sign: prefer the coach's blessed intent, else the user's
  // stated one. Null when there's nothing concrete to approve.
  const orderIntent =
    recommendation?.order_intent ??
    (hasConcreteOrder
      ? { symbol: trimmedSymbol, side, amount: trimmedAmount }
      : null)

  async function readDetail(res) {
    try {
      const data = await res.json()
      return data?.detail ?? data?.message ?? ''
    } catch {
      return ''
    }
  }

  async function onAsk(event) {
    event.preventDefault()
    if (askDisabled) return
    setPhase('thinking')
    setRecommendation(null)
    setApprove('idle')
    setApproveMessage('')
    setOutcome(null)
    try {
      const res = await apiFetch('/api/coach/recommend', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          symbol: trimmedSymbol || null,
          question,
          amount: trimmedAmount || null,
          side: side || null,
        }),
      })
      if (!mounted.current) return
      if (res.status === 401) {
        setPhase('signed-out')
        return
      }
      if (!res.ok) {
        setPhase('recommend-failed')
        return
      }
      const data = await res.json()
      if (!mounted.current) return
      setRecommendation(data)
      setPhase('ready')
    } catch {
      if (!mounted.current) return
      setPhase('recommend-failed')
    }
  }

  async function onApprove() {
    if (!recommendation || !orderIntent || approve === 'placing') return
    setApprove('placing')
    setApproveMessage('')
    try {
      const res = await apiFetch('/api/coach/approve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          decision_id: recommendation.decision_id,
          order_intent: orderIntent,
        }),
      })
      if (!mounted.current) return
      if (res.ok) {
        const data = await res.json()
        if (!mounted.current) return
        setOutcome(data)
        setApprove('placed')
        return
      }
      const detail = await readDetail(res)
      if (!mounted.current) return
      if (res.status === 409) {
        setApproveMessage(detail)
        setApprove('reconnect')
      } else if (res.status === 422) {
        setApproveMessage(detail)
        setApprove('refused')
      } else {
        setApprove('approve-failed')
      }
    } catch {
      if (!mounted.current) return
      setApprove('approve-failed')
    }
  }

  // "not now" — always equally easy, never penalized (EXPERIENCE.md). No network
  // call; dismiss the card back to a calm ready-to-ask state, keeping the form.
  function onDecline() {
    setRecommendation(null)
    setPhase('idle')
    setApprove('idle')
    setApproveMessage('')
    setOutcome(null)
  }

  const showCosign =
    phase === 'ready' && orderIntent !== null && approve !== 'placed'

  return (
    <div className="ballast-consult" data-testid="coach-consult">
      <form className="ballast-form ballast-consult__form" onSubmit={onAsk}>
        <label className="ballast-form__label" htmlFor="coach-ask-input">
          Ask the coach
        </label>
        <textarea
          id="coach-ask-input"
          className="ballast-form__input"
          data-testid="coach-ask-input"
          value={question}
          rows={2}
          maxLength={500}
          placeholder="e.g. “Should I invest my $500 paycheck? The market feels scary right now.”"
          onChange={(e) => setQuestion(e.target.value)}
        />

        <div className="ballast-consult__order">
          <div className="ballast-consult__field">
            <label className="ballast-form__label" htmlFor="coach-symbol-input">
              Symbol (optional)
            </label>
            <input
              id="coach-symbol-input"
              className="ballast-form__input"
              data-testid="coach-symbol-input"
              type="text"
              autoComplete="off"
              value={symbol}
              placeholder="VTI"
              onChange={(e) => setSymbol(e.target.value)}
            />
          </div>
          <div className="ballast-consult__field">
            <label className="ballast-form__label" htmlFor="coach-amount-input">
              Amount (optional)
            </label>
            <input
              id="coach-amount-input"
              className="ballast-form__input"
              data-testid="coach-amount-input"
              type="text"
              inputMode="decimal"
              value={amount}
              placeholder="500"
              onChange={(e) => setAmount(e.target.value)}
            />
          </div>
          <div className="ballast-consult__field">
            <label className="ballast-form__label" htmlFor="coach-side-select">
              Side (optional)
            </label>
            <select
              id="coach-side-select"
              className="ballast-form__input"
              data-testid="coach-side-select"
              value={side}
              onChange={(e) => setSide(e.target.value)}
            >
              <option value="">—</option>
              <option value="buy">buy</option>
              <option value="sell">sell</option>
            </select>
          </div>
        </div>

        <button
          type="submit"
          className="ballast-form__submit"
          data-testid="coach-ask-submit"
          disabled={askDisabled}
        >
          {phase === 'thinking' ? 'Looking at the record…' : 'Ask the coach'}
        </button>
      </form>

      {phase === 'thinking' ? (
        <p className="ballast-consult__note" data-testid="coach-thinking">
          Looking at the record…
        </p>
      ) : null}

      {phase === 'signed-out' ? (
        <p className="ballast-consult__note" data-testid="coach-signed-out">
          Sign in to ask the coach.{' '}
          <Link className="ballast-consult__link" to="/auth">
            Sign in
          </Link>
        </p>
      ) : null}

      {phase === 'recommend-failed' ? (
        <p className="ballast-consult__note" data-testid="coach-recommend-failed">
          We couldn’t reach the coach just now. Your plan hasn’t changed — try
          again in a moment.
        </p>
      ) : null}

      {phase === 'ready' && recommendation ? (
        <div className="ballast-consult__result">
          <CoachCard recommendation={recommendation} question={question} />

          {orderIntent === null ? (
            <p className="ballast-consult__note" data-testid="coach-no-order">
              No trade to approve here — the coach isn’t recommending one.
            </p>
          ) : null}

          {showCosign ? (
            <div className="ballast-consult__cosign" data-testid="coach-cosign">
              <p className="ballast-consult__cosign-note">
                ✎ I’ll put my name on this with you.
              </p>
              <div className="ballast-consult__cosign-actions">
                <button
                  type="button"
                  className="ballast-consult__approve"
                  data-testid="coach-approve"
                  onClick={onApprove}
                  disabled={approve === 'placing'}
                >
                  {approve === 'placing' ? 'Placing…' : 'Approve & Co-sign'}
                </button>
                <button
                  type="button"
                  className="ballast-consult__decline"
                  data-testid="coach-decline"
                  onClick={onDecline}
                >
                  not now
                </button>
              </div>

              {approve === 'reconnect' ? (
                <p
                  className="ballast-consult__cosign-msg"
                  data-testid="coach-reconnect"
                >
                  {approveMessage ||
                    'Reconnect your Schwab account to place this.'}{' '}
                  <Link className="ballast-consult__link" to="/onboarding">
                    Reconnect Schwab
                  </Link>
                </p>
              ) : null}

              {approve === 'refused' ? (
                <p
                  className="ballast-consult__cosign-msg"
                  data-testid="coach-refused"
                >
                  {approveMessage ||
                    'That order can’t be placed as-is. Nothing was placed.'}
                </p>
              ) : null}

              {approve === 'approve-failed' ? (
                <p
                  className="ballast-consult__cosign-msg"
                  data-testid="coach-approve-failed"
                >
                  We couldn’t place that just now. Nothing was placed — try
                  again in a moment.
                </p>
              ) : null}
            </div>
          ) : null}

          {approve === 'placed' && outcome ? (
            <div className="ballast-consult__outcome" data-testid="coach-outcome">
              <p className="ballast-consult__outcome-line">
                Outcome: {outcome.status}
                {outcome.filled_qty != null
                  ? ` · filled ${outcome.filled_qty}`
                  : ''}
                {outcome.avg_price != null ? ` @ ${outcome.avg_price}` : ''}
              </p>
              <p
                className="ballast-consult__chip"
                data-testid="coach-replay-chip"
              >
                ↻ if it dips, I’ll replay this back to you
              </p>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}
