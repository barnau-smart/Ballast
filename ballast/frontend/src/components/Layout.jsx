import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import { Wordmark } from './Wordmark.jsx'
import { Cursor } from './Cursor.jsx'
import { useSession } from '../hooks/useSession.js'
import { apiFetch, clearToken } from '../lib/session.js'
import './Layout.css'

const NAV = [
  { to: '/', label: 'dashboard', end: true },
  { to: '/coach', label: 'coach' },
  { to: '/decisions', label: 'decisions' },
  { to: '/settings', label: 'settings' },
  { to: '/onboarding', label: 'onboarding' },
  { to: '/auth', label: 'auth' },
]

/**
 * Shared chrome for all routes. Renders the Wordmark + Cursor signatures plus
 * the route nav, and — when signed in — a signed-in indicator and a Log out
 * action. Presentation-only (AD-1): Log out clears the client token and makes a
 * best-effort logout call; the token is never logged.
 */
export function Layout() {
  const { signedIn } = useSession()
  const navigate = useNavigate()

  async function handleLogout() {
    // Best-effort server call BEFORE clearing so the bearer token is still
    // attached. JWT is stateless — the server does not revoke the token; this
    // call just completes the honest client/server logout contract. Failures
    // are ignored: the client session ends regardless.
    try {
      await apiFetch('/api/auth/jwt/logout', { method: 'POST' })
    } catch {
      // Network error — still sign out locally.
    }
    clearToken()
    navigate('/auth')
  }

  return (
    <div className="ballast-layout">
      <header className="ballast-header">
        <div className="ballast-header__brand">
          <Wordmark />
          <Cursor />
        </div>
        <nav className="ballast-nav">
          {NAV.map(({ to, label, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                isActive ? 'ballast-nav__link--active' : undefined
              }
            >
              {label}
            </NavLink>
          ))}
          {signedIn && (
            <>
              <span className="ballast-nav__session" data-testid="signed-in-indicator">
                signed in
              </span>
              <button
                type="button"
                className="ballast-nav__logout"
                onClick={handleLogout}
              >
                log out
              </button>
            </>
          )}
        </nav>
      </header>
      <main className="ballast-main">
        <Outlet />
      </main>
    </div>
  )
}
