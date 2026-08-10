import { useEffect, useState } from 'react'
import { apiFetch } from '../lib/session.js'
import './screen.css'
import './Settings.css'

/**
 * Settings — account, connections, and preferences.
 *
 * Story 5.1 adds the weekly digest opt-in: a calm, off-by-default toggle bound
 * to `GET`/`PUT /api/digest/preference`.
 *
 * Story 9.1 (Epic 9, Cash Intelligence) adds the "Cash setup" card: the user
 * declares a reserve (an amount they never want touched) OR says they don't keep
 * one, and tags which of their held funds are money-market "parked" cash. Bound
 * to `GET`/`PUT /api/cash/config`; the held-symbol list comes from
 * `GET /api/portfolio`.
 *
 * Presentation-only (AD-1): fetch + render + persist; the server owns the config.
 * Fails quiet (a backend hiccup shows last-known/calm-default state, never an
 * error screen) — consistent with the app's degrade-gracefully posture.
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

      <CashSetupCard />
    </section>
  )
}

/**
 * Cash setup (Story 9.1) — declare a reserve (or decline), and tag money-market
 * "parked" funds. Calm, honest, non-alarmist: a reserve is money you'd rather
 * Ballast never suggest investing; parked funds are cash you keep in a
 * money-market fund. Both are entirely optional and editable any time.
 */
function CashSetupCard() {
  const [status, setStatus] = useState('loading')
  // The editable box (free text). Kept separate from the server-confirmed state
  // below so an in-progress edit is never persisted as a side effect of another
  // action (e.g. tagging a parked fund).
  const [reserveInput, setReserveInput] = useState('')
  // Server-confirmed reserve — the source of truth for the status line and for
  // any write that isn't itself changing the reserve.
  const [confirmedAmount, setConfirmedAmount] = useState(null) // string | null
  const [confirmedDecided, setConfirmedDecided] = useState(false)
  const [parkedSymbols, setParkedSymbols] = useState([])
  const [heldSymbols, setHeldSymbols] = useState([])

  function applyConfig(data) {
    const amount = data.reserve_amount == null ? null : String(data.reserve_amount)
    setReserveInput(amount ?? '')
    setConfirmedAmount(amount)
    setConfirmedDecided(Boolean(data.reserve_decided))
    setParkedSymbols(Array.isArray(data.parked_symbols) ? data.parked_symbols : [])
  }

  useEffect(() => {
    let active = true
    // Config drives the card; the portfolio gives the checkbox list of the
    // symbols the user actually holds. Fetch them INDEPENDENTLY with their own
    // fail-quiet catch so a hiccup on one never discards the other's result.
    const configP = apiFetch('/api/cash/config')
      .then((res) => (res.ok ? res.json() : null))
      .catch(() => null)
    const portfolioP = apiFetch('/api/portfolio')
      .then((res) => (res.ok ? res.json() : { holdings: [] }))
      .catch(() => ({ holdings: [] }))
    Promise.all([configP, portfolioP]).then(([config, portfolio]) => {
      if (!active) return
      applyConfig(config ?? { reserve_amount: null, reserve_decided: false, parked_symbols: [] })
      setHeldSymbols((portfolio.holdings ?? []).map((h) => h.symbol))
      setStatus('ready')
    })
    return () => {
      active = false
    }
  }, [])

  // Persist the full config in one PUT (the backend writes it atomically).
  // Optimistic; reconcile from the server response, and on failure re-read to
  // restore truth.
  function saveConfig(next) {
    const body = {
      reserve_amount: next.reserve_amount,
      reserve_decided: next.reserve_decided,
      parked_symbols: next.parked_symbols,
    }
    return apiFetch('/api/cash/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return res.json()
      })
      .then((data) => applyConfig(data))
      .catch(() => {
        // Restore server truth if the save didn't land (no error screen).
        apiFetch('/api/cash/config')
          .then((res) => (res.ok ? res.json() : null))
          .then((data) => data && applyConfig(data))
          .catch(() => {})
      })
  }

  function handleSetReserve() {
    // The button is disabled when the box is empty, so this is always a concrete
    // amount. The honest way to say "no reserve" is the dedicated decline button
    // — an empty box is never silently coerced to $0.
    const amount = reserveInput.trim()
    if (amount === '') return
    setConfirmedDecided(true)
    setConfirmedAmount(amount)
    saveConfig({ reserve_amount: amount, reserve_decided: true, parked_symbols: parkedSymbols })
  }

  function handleDecline() {
    setReserveInput('')
    setConfirmedDecided(true)
    setConfirmedAmount(null)
    saveConfig({ reserve_amount: null, reserve_decided: true, parked_symbols: parkedSymbols })
  }

  function handleToggleParked(symbol) {
    const next = parkedSymbols.includes(symbol)
      ? parkedSymbols.filter((s) => s !== symbol)
      : [...parkedSymbols, symbol]
    setParkedSymbols(next)
    // Persist the SERVER-CONFIRMED reserve, never the in-progress input box — a
    // parked toggle must not commit a half-typed, unsaved reserve amount.
    saveConfig({
      reserve_amount: confirmedAmount,
      reserve_decided: confirmedDecided,
      parked_symbols: next,
    })
  }

  const disabled = status !== 'ready'

  return (
    <div className="ballast-card" data-testid="cash-setup-card">
      <div className="ballast-digest__copy">
        <h2 className="ballast-digest__title">Cash setup</h2>
        <p className="ballast-digest__prose">
          Tell Ballast how you actually keep your money, so every suggestion is
          only ever about cash you’d genuinely invest. This is optional and you
          can change it any time.
        </p>
      </div>

      <div className="ballast-cash__section" data-testid="cash-reserve">
        <h3 className="ballast-cash__subtitle">A reserve you never touch</h3>
        <p className="ballast-digest__prose">
          Set aside an amount you’d rather Ballast never suggest investing — an
          emergency cushion, say. It’s entirely up to you.
        </p>
        <div className="ballast-cash__reserve-row">
          <label className="ballast-cash__label" htmlFor="cash-reserve-input">
            Reserve amount ($)
          </label>
          <input
            id="cash-reserve-input"
            type="number"
            min="0"
            step="0.01"
            inputMode="decimal"
            value={reserveInput}
            onChange={(e) => setReserveInput(e.target.value)}
            disabled={disabled}
            data-testid="cash-reserve-input"
          />
          <button
            type="button"
            onClick={handleSetReserve}
            disabled={disabled || reserveInput.trim() === ''}
            data-testid="cash-reserve-save"
          >
            Save reserve
          </button>
          <button
            type="button"
            onClick={handleDecline}
            disabled={disabled}
            data-testid="cash-reserve-decline"
          >
            I don’t keep one
          </button>
        </div>
        <p className="ballast-cash__state" data-testid="cash-reserve-state">
          {!confirmedDecided
            ? 'Not set yet — totally fine to decide later.'
            : confirmedAmount === null
              ? 'You’ve said you don’t keep a reserve.'
              : `Your reserve: $${confirmedAmount}.`}
        </p>
      </div>

      <div className="ballast-cash__section" data-testid="cash-parked">
        <h3 className="ballast-cash__subtitle">Parked cash (money market)</h3>
        <p className="ballast-digest__prose">
          If any of your funds are money-market funds you think of as cash, tag
          them here. Ballast will show them as parked cash instead of a stock
          that moves up or down.
        </p>
        {heldSymbols.length === 0 ? (
          <p className="ballast-cash__state" data-testid="cash-parked-empty">
            Once your holdings are imported, you’ll be able to tag them here.
          </p>
        ) : (
          <ul className="ballast-cash__symbol-list">
            {heldSymbols.map((symbol) => (
              <li key={symbol}>
                <label className="ballast-cash__label">
                  <input
                    type="checkbox"
                    checked={parkedSymbols.includes(symbol)}
                    onChange={() => handleToggleParked(symbol)}
                    disabled={disabled}
                    data-testid={`cash-parked-${symbol}`}
                  />
                  <span>{symbol}</span>
                </label>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}
