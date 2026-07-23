/**
 * Client-side session/token handling. Presentation-only (AD-1): this module
 * ONLY stores, sends, and clears the JWT the backend issues. It holds no auth
 * logic — no decoding, no expiry checks, no refresh. The server is the single
 * source of truth for whether a token is valid.
 *
 * Token storage: kept in memory for the session AND mirrored to localStorage
 * so a page reload stays signed in. localStorage is readable by any script on
 * the page, so this carries an XSS tradeoff (a script injected into the page
 * could read the token). This is an accepted simplification for the Ballast v1
 * SPA; the documented hardening path is httpOnly-cookie auth. See the frontend
 * README ("Auth & session").
 */

const STORAGE_KEY = 'ballast.token'

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'

// In-memory copy — the authoritative value for the running session.
let currentToken = null

// Subscribers notified whenever the token changes, so React chrome (e.g. the
// Layout's signed-in indicator / Log out action) can re-render.
const listeners = new Set()

function notify() {
  for (const listener of listeners) listener()
}

/** Subscribe to token changes. Returns an unsubscribe function. */
export function subscribe(listener) {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

function readPersisted() {
  try {
    return window.localStorage.getItem(STORAGE_KEY)
  } catch {
    // localStorage can throw (private mode, disabled). Degrade to memory-only.
    return null
  }
}

// Hydrate from localStorage on module load so a reload restores the session.
currentToken = readPersisted()

/** Return the current token, or null if signed out. */
export function getToken() {
  return currentToken
}

/** Store the token in memory and (best-effort) localStorage. */
export function setToken(token) {
  currentToken = token
  try {
    if (token) window.localStorage.setItem(STORAGE_KEY, token)
    else window.localStorage.removeItem(STORAGE_KEY)
  } catch {
    // Ignore persistence failures — memory copy still works for the session.
  }
  notify()
}

/** Clear the token from memory and localStorage (sign out, client-side). */
export function clearToken() {
  setToken(null)
}

/** True when a token is held (client's view of "signed in"). */
export function isSignedIn() {
  return Boolean(currentToken)
}

/**
 * fetch wrapper that attaches `Authorization: Bearer <token>` when a token is
 * held. The Authorization header is never logged. Relative paths are resolved
 * against VITE_API_BASE_URL; absolute URLs pass through unchanged.
 *
 * The bearer token is ONLY attached to same-origin (backend) requests — never
 * to a cross-origin URL — so the session token can never leak to a third party
 * even if a caller passes a fully-qualified external URL.
 */
export function apiFetch(path, options = {}) {
  const url = /^https?:\/\//.test(path) ? path : `${API_BASE_URL}${path}`
  const headers = new Headers(options.headers ?? {})
  if (currentToken && _isBackendOrigin(url)) {
    headers.set('Authorization', `Bearer ${currentToken}`)
  }
  return fetch(url, { ...options, headers })
}

function _isBackendOrigin(url) {
  try {
    return new URL(url, API_BASE_URL).origin === new URL(API_BASE_URL).origin
  } catch {
    return false
  }
}

export { API_BASE_URL }
