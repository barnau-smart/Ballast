import { useEffect, useState } from 'react'
import { apiFetch } from '../lib/session.js'
import './screen.css'
import './Settings.css'

/**
 * Settings — account, connections, and preferences. Story 5.1 adds the weekly
 * digest opt-in: a calm, off-by-default toggle bound to
 * `GET`/`PUT /api/digest/preference`. Presentation-only (AD-1): fetch + render +
 * persist the flag; the server owns the preference and all sending. Fails quiet
 * (a backend hiccup shows the toggle in its last-known/off state, never an error
 * screen) — consistent with the app's degrade-gracefully posture.
 */
export function Settings() {
  const [optedIn, setOptedIn] = useState(false)
  const [status, setStatus] = useState('loading')

  useEffect(() => {
    let active = true
    apiFetch('/api/digest/preference')
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return res.json()
      })
      .then((data) => {
        if (!active) return
        setOptedIn(Boolean(data.opted_in))
        setStatus('ready')
      })
      .catch(() => {
        if (!active) return
        // Fail quiet: default to off rather than an error screen.
        setOptedIn(false)
        setStatus('ready')
      })
    return () => {
      active = false
    }
  }, [])

  function handleToggle(event) {
    const next = event.target.checked
    // Optimistic: reflect the choice immediately; the server is source of truth.
    setOptedIn(next)
    apiFetch('/api/digest/preference', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ opted_in: next }),
    })
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return res.json()
      })
      .then((data) => setOptedIn(Boolean(data.opted_in)))
      .catch(() => {
        // Roll back the optimistic flip if the save didn't land.
        setOptedIn(!next)
      })
  }

  return (
    <section className="ballast-screen">
      <p className="ballast-screen__eyebrow">settings</p>
      <h1 className="ballast-screen__title">Settings</h1>
      <p className="ballast-screen__prose">
        Manage your account, connections, and preferences here.
      </p>

      <div className="ballast-card" data-testid="digest-preference-card">
        <div className="ballast-digest">
          <div className="ballast-digest__copy">
            <h2 className="ballast-digest__title">Weekly digest</h2>
            <p className="ballast-digest__prose">
              A gentle weekly email with where your plan stands — just so you can
              feel on-track between visits. It’s off unless you ask for it, never
              alarmist, and you can unsubscribe any time (there’s a link in every
              email, or flip this off).
            </p>
          </div>
          <label className="ballast-digest__toggle">
            <input
              type="checkbox"
              checked={optedIn}
              onChange={handleToggle}
              disabled={status !== 'ready'}
              data-testid="digest-opt-in-toggle"
            />
            <span data-testid="digest-opt-in-state">
              {optedIn ? 'On' : 'Off'}
            </span>
          </label>
        </div>
      </div>
    </section>
  )
}
