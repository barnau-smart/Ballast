import { useEffect, useState } from 'react'
import { MarketIndicator } from '../components/MarketIndicator.jsx'
import './screen.css'

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'

/**
 * The calm home. On mount it fetches the backend health check and renders
 * the result — the concrete proof of SPA <-> backend connectivity.
 * Presentation-only: fetch + render, no business logic. Degrades gracefully
 * if the backend is unreachable.
 */
export function Dashboard() {
  const [health, setHealth] = useState({ state: 'loading' })

  useEffect(() => {
    let active = true
    fetch(`${API_BASE_URL}/api/health`)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return res.json()
      })
      .then((data) => {
        if (active) setHealth({ state: 'ok', data })
      })
      .catch(() => {
        if (active) setHealth({ state: 'error' })
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
        Your calm home base. Nothing here needs your attention right now — and
        that is a good thing.
      </p>

      <div className="ballast-card" data-testid="health-card">
        {health.state === 'loading' && <span>checking backend…</span>}
        {health.state === 'ok' && (
          <span className="ballast-status--ok">
            backend: {health.data?.status ?? 'unknown'} · db:{' '}
            {health.data?.db ?? 'unknown'}
          </span>
        )}
        {health.state === 'error' && (
          <span className="ballast-status--error">backend unreachable</span>
        )}
      </div>

      <div className="ballast-card">
        <MarketIndicator direction="up" label="portfolio (sample)" />
      </div>
    </section>
  )
}
