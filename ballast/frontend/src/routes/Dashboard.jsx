import { useEffect, useState } from 'react'
import { PortfolioPanel } from '../components/PortfolioPanel.jsx'
import { MissedGrowthMeter } from '../components/MissedGrowthMeter.jsx'
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

      <PortfolioPanel status={status} portfolio={portfolio} />
      <MissedGrowthMeter />
    </section>
  )
}
