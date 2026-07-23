import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { API_BASE_URL, setToken } from '../lib/session.js'
import './screen.css'

// Warm, plain fallback if the backend gives us no readable message.
const GENERIC_ERROR = 'Something went wrong. Please try again in a moment.'

/**
 * The single auth surface. Presentation-only (AD-1): it collects an email +
 * password and either registers (JSON) or logs in (OAuth2 form), then renders
 * whatever the backend says. On a successful login it stores the returned JWT
 * via the session store and routes to the Dashboard. All real validation,
 * hashing, credential-checking, and token issuance live server-side.
 */
export function Auth() {
  const [mode, setMode] = useState('login') // 'login' | 'signup'
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [status, setStatus] = useState({ state: 'idle' })
  const navigate = useNavigate()

  const isSignup = mode === 'signup'

  function switchMode(next) {
    setMode(next)
    setStatus({ state: 'idle' })
  }

  async function readErrorMessage(res) {
    try {
      const data = await res.json()
      if (data?.error?.message) return data.error.message
    } catch {
      // Non-JSON error body — fall through to the generic message.
    }
    return GENERIC_ERROR
  }

  async function handleSignup() {
    const res = await fetch(`${API_BASE_URL}/api/auth/register`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
    })
    if (res.ok) {
      setStatus({ state: 'success', message: 'Your account is ready. You can log in next.' })
      setMode('login')
      return
    }
    setStatus({ state: 'error', message: await readErrorMessage(res) })
  }

  async function handleLogin() {
    // FastAPI-Users login is an OAuth2 password form: username=email + password.
    const body = new URLSearchParams({ username: email, password })
    const res = await fetch(`${API_BASE_URL}/api/auth/jwt/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body,
    })
    if (res.ok) {
      const data = await res.json()
      setToken(data.access_token)
      setStatus({ state: 'idle' })
      navigate('/')
      return
    }
    setStatus({ state: 'error', message: await readErrorMessage(res) })
  }

  async function handleSubmit(event) {
    event.preventDefault()
    setStatus({ state: 'submitting' })
    try {
      if (isSignup) await handleSignup()
      else await handleLogin()
    } catch {
      setStatus({ state: 'error', message: GENERIC_ERROR })
    }
  }

  const submitting = status.state === 'submitting'
  const submitLabel = isSignup
    ? submitting
      ? 'Creating…'
      : 'Create account'
    : submitting
      ? 'Signing in…'
      : 'Log in'

  return (
    <section className="ballast-screen">
      <p className="ballast-screen__eyebrow">auth</p>
      <h1 className="ballast-screen__title">
        {isSignup ? 'Create your account' : 'Welcome back'}
      </h1>
      <p className="ballast-screen__prose">
        {isSignup
          ? 'Welcome aboard. Pick an email and password to set up your private Ballast account.'
          : 'Log in to pick up right where you left off.'}
      </p>

      <div className="ballast-auth__modes" role="tablist" aria-label="Sign up or log in">
        <button
          type="button"
          role="tab"
          aria-selected={!isSignup}
          className={
            !isSignup ? 'ballast-auth__mode ballast-auth__mode--active' : 'ballast-auth__mode'
          }
          onClick={() => switchMode('login')}
        >
          Log in
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={isSignup}
          className={
            isSignup ? 'ballast-auth__mode ballast-auth__mode--active' : 'ballast-auth__mode'
          }
          onClick={() => switchMode('signup')}
        >
          Sign up
        </button>
      </div>

      <form className="ballast-form" onSubmit={handleSubmit} noValidate={false}>
        <label className="ballast-form__label" htmlFor="auth-email">
          Email
        </label>
        <input
          id="auth-email"
          className="ballast-form__input"
          type="email"
          name="email"
          autoComplete="email"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />

        <label className="ballast-form__label" htmlFor="auth-password">
          Password
        </label>
        <input
          id="auth-password"
          className="ballast-form__input"
          type="password"
          name="password"
          autoComplete={isSignup ? 'new-password' : 'current-password'}
          required
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />

        <button className="ballast-form__submit" type="submit" disabled={submitting}>
          {submitLabel}
        </button>
      </form>

      {status.state === 'success' && (
        <div className="ballast-card ballast-status--ok" role="status">
          {status.message}
        </div>
      )}
      {status.state === 'error' && (
        <div className="ballast-card ballast-status--error" role="alert">
          {status.message}
        </div>
      )}
    </section>
  )
}
