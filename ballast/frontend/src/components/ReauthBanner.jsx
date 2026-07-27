import { useCallback, useEffect, useState } from 'react'
import { useLocation } from 'react-router-dom'
import { apiFetch } from '../lib/session.js'
import { useReducedMotion } from '../hooks/useReducedMotion.js'
import './ReauthBanner.css'

/**
 * reauth-banner (Story 2.2) — a calm, neutral banner shown app-wide when the
 * brokerage session has EXPIRED (the ~weekly Schwab re-login). It is
 * deliberately neutral/muted and NEVER red: red is brand-only (DESIGN.md hard
 * color rule), and an expiry is normal, not an error or an alarm.
 *
 * Behaviour (AD-11): read/coach features keep working in degraded mode, so this
 * banner only *invites* a reconnect — it never blocks the UI. It reads session
 * state from /api/brokerage/status and renders nothing unless state ===
 * 'expired'. "Reconnect" reuses the Story 2.1 link flow, passing the current
 * in-app path as `return_to` so the user resumes where they were
 * (resume-in-place). Respects prefers-reduced-motion (no entrance animation).
 * No token values are ever handled here.
 */
export function ReauthBanner() {
  const [expired, setExpired] = useState(false)
  const [reconnecting, setReconnecting] = useState(false)
  const location = useLocation()
  const reducedMotion = useReducedMotion()

  const loadStatus = useCallback(async () => {
    try {
      const res = await apiFetch('/api/brokerage/status')
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      setExpired(data.state === 'expired')
    } catch {
      // Fail quiet: if we can't determine the state, don't nag with a banner.
      setExpired(false)
    }
  }, [])

  useEffect(() => {
    loadStatus()
  }, [loadStatus])

  async function handleReconnect() {
    setReconnecting(true)
    try {
      // Round-trip the current in-app path so the user lands back here after
      // re-auth. The backend validates it against an allowlist (no open
      // redirect); an unknown path simply resolves to the default landing.
      const params = new URLSearchParams({ return_to: location.pathname })
      const res = await apiFetch(`/api/brokerage/authorize?${params.toString()}`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      // Hand off to the brokerage's authorization page (fake or real).
      if (data.authorization_url) {
        window.location.assign(data.authorization_url)
      } else {
        setReconnecting(false)
      }
    } catch {
      // Leave the banner in place so the user can try again.
      setReconnecting(false)
    }
  }

  if (!expired) return null

  return (
    <div
      className={
        reducedMotion ? 'ballast-reauth ballast-reauth--static' : 'ballast-reauth'
      }
      role="status"
      data-reduced-motion={reducedMotion ? 'true' : 'false'}
      data-testid="reauth-banner"
    >
      <p className="ballast-reauth__copy">
        Time for your weekly Schwab reconnect — this is normal, and nothing’s
        wrong. Reconnect to keep everything current; you’ll land right back where
        you are now.
      </p>
      <button
        type="button"
        className="ballast-reauth__action"
        onClick={handleReconnect}
        disabled={reconnecting}
      >
        {reconnecting ? 'Reconnecting…' : 'Reconnect'}
      </button>
    </div>
  )
}
