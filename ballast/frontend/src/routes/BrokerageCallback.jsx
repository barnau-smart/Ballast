import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { apiFetch } from '../lib/session.js'
import './screen.css'

/**
 * BrokerageCallback — the landing page for the REAL Schwab OAuth redirect.
 *
 * Schwab redirects the browser back to the registered callback URL
 * (`https://127.0.0.1/callback`) with `?code=…&state=…`. Locally the app is
 * served at that exact https origin (see scripts/dev.sh real-broker mode +
 * vite.config.js), so THIS route catches the redirect and completes the link
 * entirely in-app — no manual code paste, no helper script. The auth code is
 * single-use and expires in ~30s, so the exchange fires immediately on mount.
 *
 * The fake/dev broker never reaches this page (it completes the link in
 * Onboarding without an external redirect); this route only matters when
 * BROKER_ADAPTER=schwab.
 */

// Module-level cache so React 18 StrictMode's double-invoked effect (and any
// remount) exchanges a given single-use code EXACTLY once. Both invocations
// share one in-flight Promise → one network POST → no "code already used"
// false error on the second run. Keyed by code so a fresh link still works.
const exchanges = new Map()

function exchangeOnce(code, state) {
  if (!exchanges.has(code)) {
    exchanges.set(
      code,
      apiFetch('/api/brokerage/callback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code, state }),
      }).then(async (res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return true
      }),
    )
  }
  return exchanges.get(code)
}

export function BrokerageCallback() {
  // linking | linked | error | denied | missing
  const [phase, setPhase] = useState('linking')
  const [detail, setDetail] = useState('')
  const navigate = useNavigate()
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    const params = new URLSearchParams(window.location.search)
    const code = params.get('code')
    const state = params.get('state')
    // Schwab returns ?error=access_denied (etc.) when the user declines.
    const oauthError = params.get('error')

    if (oauthError) {
      setPhase('denied')
      setDetail(oauthError)
      return
    }
    if (!code || !state) {
      // Someone opened /callback directly, or the redirect lost its params.
      setPhase('missing')
      return
    }

    exchangeOnce(code, state)
      .then(() => {
        if (!mounted.current) return
        setPhase('linked')
        // Scrub the single-use code/state out of the URL + history so a reload
        // or back-button can't replay it (it's dead anyway, but don't display it).
        window.history.replaceState({}, '', '/callback')
      })
      .catch((e) => {
        if (!mounted.current) return
        setPhase('error')
        setDetail(String(e?.message ?? e))
      })

    return () => {
      mounted.current = false
    }
  }, [])

  return (
    <section className="ballast-screen">
      <p className="ballast-screen__eyebrow">onboarding</p>
      <h1 className="ballast-screen__title">Linking your Schwab account</h1>

      <div className="ballast-card" data-testid="callback-status">
        {phase === 'linking' && <span>Completing the secure link…</span>}
        {phase === 'linked' && (
          <span className="ballast-status--ok">
            Your Schwab account is connected.
          </span>
        )}
        {phase === 'denied' && (
          <span className="ballast-status--error">
            The link was cancelled at Schwab. You can try again.
          </span>
        )}
        {phase === 'missing' && (
          <span className="ballast-status--error">
            This page completes a Schwab link. Start from onboarding.
          </span>
        )}
        {phase === 'error' && (
          <span className="ballast-status--error">
            We couldn’t finish the link. Please try connecting again.
          </span>
        )}
      </div>

      {phase === 'linked' && (
        <button
          type="button"
          className="ballast-form__submit"
          onClick={() => navigate('/dashboard')}
        >
          Continue to dashboard
        </button>
      )}
      {(phase === 'denied' || phase === 'missing' || phase === 'error') && (
        <button
          type="button"
          className="ballast-form__submit"
          onClick={() => navigate('/onboarding')}
        >
          Back to onboarding
        </button>
      )}
    </section>
  )
}
