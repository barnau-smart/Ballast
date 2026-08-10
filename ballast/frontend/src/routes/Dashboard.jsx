import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { PortfolioPanel } from '../components/PortfolioPanel.jsx'
import { MissedGrowthMeter } from '../components/MissedGrowthMeter.jsx'
import { PendingBuyCard } from '../components/PendingBuyCard.jsx'
import { apiFetch } from '../lib/session.js'
import './screen.css'

/**
 * The calm home. Fetches the user's cached portfolio (Story 2.3
 * `GET /api/portfolio`) and shows it in plain English (Story 2.4). In degraded
 * mode the cached holdings still render — the reauth-banner (Story 2.2) owns the
 * reconnect prompt. Presentation-only (AD-1): fetch + render, no business logic.
 * Degrades to the calm empty/invite state if the backend is unreachable.
 */
export function Dashboard() {
  const [status, setStatus] = useState('loading')
  const [portfolio, setPortfolio] = useState(null)
  // "Maybe later" persists across reloads (localStorage) so the prompt truly
  // surfaces once, not every visit (AC6) — it only ever reappears if the user
  // clears storage. A real decision hides it regardless (it's gated on
  // reserve_decided === false below).
  const [reservePromptDismissed, setReservePromptDismissed] = useState(() => {
    try {
      return window.localStorage.getItem('ballast.reservePromptDismissed') === '1'
    } catch {
      return false
    }
  })

  function dismissReservePrompt() {
    setReservePromptDismissed(true)
    try {
      window.localStorage.setItem('ballast.reservePromptDismissed', '1')
    } catch {
      // Memory-only dismissal is fine if storage is unavailable.
    }
  }

  useEffect(() => {
    let active = true
    apiFetch('/api/portfolio')
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return res.json()
      })
      .then((data) => {
        if (!active) return
        setPortfolio(data)
        setStatus('ready')
      })
      .catch(() => {
        if (!active) return
        // Fail quiet: fall back to the calm empty/invite state rather than an
        // error screen (consistent with the app's degrade-gracefully posture).
        setPortfolio({ holdings: [], cash: 0, as_of: null })
        setStatus('ready')
      })
    return () => {
      active = false
    }
  }, [])

  return (
    <section className="ballast-screen">
      <p className="ballast-screen__eyebrow">dashboard</p>
      <h1 className="ballast-screen__title">Steady as she goes</h1>
      <p className="ballast-screen__prose">
        Your calm home base — here’s everything you hold, in plain English.
      </p>

      {status === 'ready' &&
      portfolio?.cash_states?.reserve_decided === false &&
      !reservePromptDismissed ? (
        <div className="ballast-card" data-testid="reserve-prompt">
          <p className="ballast-screen__prose">
            One optional thing: tell Ballast how much cash you’d like to keep as a
            reserve — money you never want it to suggest investing — or let us know
            you don’t keep one. It helps every suggestion stay honest. No rush; you
            can decide any time.
          </p>
          <div className="ballast-reserve-prompt__actions">
            <Link to="/settings" data-testid="reserve-prompt-link">
              Set this up in Settings
            </Link>
            <button
              type="button"
              onClick={dismissReservePrompt}
              data-testid="reserve-prompt-dismiss"
            >
              Maybe later
            </button>
          </div>
        </div>
      ) : null}

      {/* Durable pending buys surface on visit (pull-only, Story 9.3) — a buy
          set aside for settled cash resumes here even after a missed note. */}
      <PendingBuyCard />

      <PortfolioPanel status={status} portfolio={portfolio} />
      <MissedGrowthMeter />
    </section>
  )
}
