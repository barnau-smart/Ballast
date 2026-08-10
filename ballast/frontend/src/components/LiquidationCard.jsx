import { useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { apiFetch } from '../lib/session.js'
import './LiquidationCard.css'

/**
 * The just-in-time liquidation card (Story 9.3).
 *
 * Presentation-only (AD-1): renders a backend-computed money-market SELL plan for
 * a buy whose amount exceeds the user's instantly-spendable (ready-to-trade) cash.
 * It computes no money number — the backend owns the shortfall, sell amount, and
 * est. shares; the LLM narrates. The pre-filled SELL is submitted through the
 * SAME `/api/coach/approve` co-sign path buys use (nothing is auto-placed) — and
 * a durable pending buy has already been recorded by `/liquidation-plan`, so the
 * buy resumes later even if this sell is skipped.
 *
 * Calm + honest + never-red: the user's own money is NEVER shown in red (this
 * card uses no brand-red / accent-pink / line-red). It shows the protected
 * reserve alongside, surfaces `as_of` data freshness on every figure, and is
 * honest about partial coverage ("covers $Y of $X") and the nothing-to-liquidate
 * case ("this will resume when your cash settles"). No FOMO, no nudge, no urgency.
 */
export function LiquidationCard({ plan, onSellPlaced }) {
  // idle | placing | placed | reconnect | in-progress | refused | signed-out
  // | indeterminate
  const [approve, setApprove] = useState('idle')
  const [message, setMessage] = useState('')
  const [outcome, setOutcome] = useState(null)
  const placingRef = useRef(false)

  if (!plan || !plan.needs_liquidation) return null

  const hasSell =
    Boolean(plan.sell_symbol) &&
    Boolean(plan.sell_order_intent) &&
    Boolean(plan.sell_decision_id)

  async function readMessage(res) {
    try {
      const data = await res.json()
      return data?.error?.message ?? data?.detail ?? data?.message ?? ''
    } catch {
      return ''
    }
  }

  async function onApproveSell() {
    if (!hasSell) return
    if (placingRef.current) return
    placingRef.current = true
    setApprove('placing')
    setMessage('')
    try {
      const res = await apiFetch('/api/coach/approve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          decision_id: plan.sell_decision_id,
          order_intent: {
            symbol: plan.sell_order_intent.symbol,
            side: plan.sell_order_intent.side,
            amount: plan.sell_order_intent.amount,
          },
        }),
      })
      if (res.ok) {
        let data
        try {
          data = await res.json()
        } catch {
          setApprove('indeterminate')
          return
        }
        setOutcome(data)
        setApprove('placed')
        if (onSellPlaced) onSellPlaced(data)
        return
      }
      const detail = await readMessage(res)
      if (res.status === 401) {
        setApprove('signed-out')
      } else if (res.status === 409) {
        setMessage(detail)
        setApprove(
          /moment|in progress|being approved|already/i.test(detail)
            ? 'in-progress'
            : 'reconnect',
        )
      } else if (res.status === 422) {
        // An untradeable fund / calm refusal — nothing placed; the pending buy
        // persists so the intent is never lost.
        setMessage(detail)
        setApprove('refused')
      } else {
        setApprove('indeterminate')
      }
    } catch {
      setApprove('indeterminate')
    } finally {
      placingRef.current = false
    }
  }

  return (
    <div className="ballast-liquidation" data-testid="liquidation-card" role="status">
      <p className="ballast-liquidation__eyebrow">A quick cash step first</p>

      {/* The backend's calm, honest narration, rendered verbatim (AD-1). */}
      <p className="ballast-liquidation__reasoning" data-testid="liquidation-reasoning">
        {plan.reasoning}
      </p>

      {hasSell ? (
        <div className="ballast-liquidation__plan" data-testid="liquidation-sell">
          <p className="ballast-liquidation__line">
            Sell about{' '}
            <span data-testid="liquidation-sell-amount">${plan.sell_amount}</span> of
            your{' '}
            <span data-testid="liquidation-sell-symbol">{plan.sell_symbol}</span>{' '}
            money-market fund
            {plan.est_shares != null && plan.est_shares > 0 ? (
              <>
                {' '}
                (about{' '}
                <span data-testid="liquidation-est-shares">{plan.est_shares}</span>{' '}
                {plan.est_shares === 1 ? 'share' : 'shares'})
              </>
            ) : null}
            .
          </p>
          {!plan.coverable ? (
            <p
              className="ballast-liquidation__partial"
              data-testid="liquidation-partial"
            >
              This frees up ${plan.sell_amount} of the ${plan.shortfall} you’re
              short. The rest can be freed up later — no rush.
            </p>
          ) : null}
        </div>
      ) : (
        <p
          className="ballast-liquidation__nothing"
          data-testid="liquidation-nothing"
        >
          There’s nothing to sell right now, so this will resume on its own once
          enough of your cash settles. Nothing to do for now.
        </p>
      )}

      {/* Protected reserve, shown alongside — reassurance, never alarm. Only when
          the user has a reserve to protect (a decided-and-declined reserve is
          $0.00 — don't promise to protect nothing; mirrors the backend guard). */}
      {plan.reserved != null && Number(plan.reserved) > 0 ? (
        <p
          className="ballast-liquidation__reserve"
          data-testid="liquidation-reserve"
        >
          Your ${plan.reserved} reserve stays protected — it’s never sold.
        </p>
      ) : null}

      {/* Data freshness on every figure (as_of). */}
      {plan.as_of ? (
        <p className="ballast-liquidation__asof" data-testid="liquidation-asof">
          Prices as of {new Date(plan.as_of).toLocaleDateString()}.
        </p>
      ) : null}

      {hasSell && approve !== 'placed' ? (
        <div className="ballast-liquidation__actions">
          <button
            type="button"
            className="ballast-liquidation__approve"
            data-testid="liquidation-approve-sell"
            onClick={onApproveSell}
            disabled={approve === 'placing'}
          >
            {approve === 'placing' ? 'Placing…' : 'Review & submit this sell'}
          </button>
        </div>
      ) : null}

      {approve === 'reconnect' ? (
        <p
          className="ballast-liquidation__msg"
          data-testid="liquidation-reconnect"
          role="status"
        >
          {message || 'Reconnect your Schwab account to place this.'}{' '}
          <Link className="ballast-liquidation__link" to="/onboarding">
            Reconnect Schwab
          </Link>
        </p>
      ) : null}

      {approve === 'in-progress' ? (
        <p
          className="ballast-liquidation__msg"
          data-testid="liquidation-in-progress"
          role="status"
        >
          {message ||
            'This is being submitted right now — give it a moment and check your Decisions.'}
        </p>
      ) : null}

      {approve === 'refused' ? (
        <p
          className="ballast-liquidation__msg"
          data-testid="liquidation-refused"
          role="status"
        >
          {message ||
            'That sell can’t be placed as-is. Nothing was placed — your buy is saved and will resume when your cash settles.'}
        </p>
      ) : null}

      {approve === 'signed-out' ? (
        <p
          className="ballast-liquidation__msg"
          data-testid="liquidation-signed-out"
          role="status"
        >
          Your session ended. Sign in and try again.{' '}
          <Link className="ballast-liquidation__link" to="/auth">
            Sign in
          </Link>
        </p>
      ) : null}

      {approve === 'indeterminate' ? (
        <p
          className="ballast-liquidation__msg"
          data-testid="liquidation-indeterminate"
          role="status"
        >
          We couldn’t confirm whether that went through. Check your Decisions
          before trying again.{' '}
          <Link className="ballast-liquidation__link" to="/decisions">
            Open Decisions
          </Link>
        </p>
      ) : null}

      {approve === 'placed' && outcome ? (
        <p
          className="ballast-liquidation__msg"
          data-testid="liquidation-placed"
          role="status"
        >
          Sell submitted (outcome: {outcome.status}). Once your cash settles, your
          buy will resume — you’ll see it waiting for you here.
        </p>
      ) : null}
    </div>
  )
}
