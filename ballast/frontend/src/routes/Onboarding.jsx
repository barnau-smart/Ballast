import { useCallback, useEffect, useState } from 'react'
import { apiFetch } from '../lib/session.js'
import './screen.css'

/**
 * Onboarding — minimal Schwab link entry (Story 2.1). Presentation-only:
 * it reads link status from /api/brokerage/status and, on "Connect Schwab",
 * calls /api/brokerage/authorize and would send the user to the returned
 * authorization URL. With the fake adapter this exercises the whole flow
 * locally. No token values are ever handled here. The fuller onboarding reveal
 * is Stories 2.2–2.4.
 */
export function Onboarding() {
  const [status, setStatus] = useState({ state: 'loading' })
  const [connecting, setConnecting] = useState(false)

  const loadStatus = useCallback(async () => {
    try {
      const res = await apiFetch('/api/brokerage/status')
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      setStatus({ state: 'ready', linked: data.linked, provider: data.provider })
    } catch {
      setStatus({ state: 'error' })
    }
  }, [])

  useEffect(() => {
    loadStatus()
  }, [loadStatus])

  async function handleConnect() {
    setConnecting(true)
    try {
      const res = await apiFetch('/api/brokerage/authorize')
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      // Hand off to the brokerage's authorization page (fake or real).
      if (data.authorization_url) {
        window.location.assign(data.authorization_url)
      }
    } catch {
      setStatus({ state: 'error' })
    } finally {
      setConnecting(false)
    }
  }

  const linked = status.state === 'ready' && status.linked

  return (
    <section className="ballast-screen">
      <p className="ballast-screen__eyebrow">onboarding</p>
      <h1 className="ballast-screen__title">Link your Schwab account</h1>
      <p className="ballast-screen__prose">
        Connect your brokerage so your coach can see the whole picture. Nothing
        is traded on your behalf.
      </p>

      <div className="ballast-card" data-testid="link-status">
        {status.state === 'loading' && <span>checking link status…</span>}
        {status.state === 'error' && (
          <span className="ballast-status--error">
            couldn’t check your link status
          </span>
        )}
        {status.state === 'ready' && linked && (
          <span className="ballast-status--ok">
            Your Schwab account is connected.
          </span>
        )}
        {status.state === 'ready' && !linked && (
          <span>Your account isn’t connected yet.</span>
        )}
      </div>

      {!linked && (
        <button
          type="button"
          className="ballast-form__submit"
          onClick={handleConnect}
          disabled={connecting || status.state === 'loading'}
        >
          {connecting ? 'Connecting…' : 'Connect Schwab'}
        </button>
      )}
    </section>
  )
}
