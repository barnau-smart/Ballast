import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { ReauthBanner } from '../components/ReauthBanner.jsx'
import { setToken, clearToken } from '../lib/session.js'

/**
 * Story 2.2 — reauth-banner. Verifies it appears ONLY on an expired session,
 * is styled neutral (never brand-red — a hard color rule, so an acceptance
 * criterion), reconnects via the Story 2.1 authorize flow with a return_to
 * (resume-in-place), and honours prefers-reduced-motion.
 */

function mockReducedMotion(reduce) {
  window.matchMedia = vi.fn().mockImplementation((query) => ({
    matches: reduce,
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }))
}

function mockStatus(state) {
  vi.stubGlobal(
    'fetch',
    vi.fn((path) => {
      if (String(path).includes('/api/brokerage/status')) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ state, linked: state !== 'unlinked' }),
        })
      }
      return Promise.reject(new Error(`unexpected fetch: ${path}`))
    }),
  )
}

function renderBanner(initialPath = '/coach') {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <ReauthBanner />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  setToken('jwt-token-123')
  mockReducedMotion(false)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  clearToken()
})

describe('ReauthBanner — session expired (Story 2.2)', () => {
  it('renders a calm reconnect banner when the session is expired', async () => {
    mockStatus('expired')
    renderBanner()

    const banner = await screen.findByTestId('reauth-banner')
    expect(banner).toBeInTheDocument()
    // Plain, calm copy — "normal", not an error/alarm.
    expect(screen.getByText(/this is normal/i)).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: /reconnect/i }),
    ).toBeInTheDocument()
  })

  it('is hidden when the session is live', async () => {
    mockStatus('live')
    renderBanner()

    // Give the status fetch time to resolve, then assert nothing rendered.
    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalled()
    })
    expect(screen.queryByTestId('reauth-banner')).not.toBeInTheDocument()
  })

  it('is hidden when the session is unlinked', async () => {
    mockStatus('unlinked')
    renderBanner()

    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalled()
    })
    expect(screen.queryByTestId('reauth-banner')).not.toBeInTheDocument()
  })

  it('Reconnect starts authorize with the current path as return_to and redirects', async () => {
    const fetchMock = vi.fn((path) => {
      if (String(path).includes('/api/brokerage/status')) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ state: 'expired', linked: true }),
        })
      }
      if (String(path).includes('/api/brokerage/authorize')) {
        return Promise.resolve({
          ok: true,
          json: () =>
            Promise.resolve({
              authorization_url:
                'https://fake-broker.ballast.local/oauth/authorize?state=abc',
              state: 'abc',
              return_to: '/coach',
            }),
        })
      }
      return Promise.reject(new Error(`unexpected fetch: ${path}`))
    })
    vi.stubGlobal('fetch', fetchMock)

    const assign = vi.fn()
    vi.stubGlobal('location', { ...window.location, assign })

    renderBanner('/coach')

    fireEvent.click(await screen.findByRole('button', { name: /reconnect/i }))

    await waitFor(() =>
      expect(assign).toHaveBeenCalledWith(
        'https://fake-broker.ballast.local/oauth/authorize?state=abc',
      ),
    )

    const authorizeCall = fetchMock.mock.calls.find((c) =>
      String(c[0]).includes('/api/brokerage/authorize'),
    )
    expect(authorizeCall).toBeTruthy()
    // resume-in-place: the current in-app path round-trips as return_to.
    expect(String(authorizeCall[0])).toContain('return_to=%2Fcoach')
    // Sent with the bearer via apiFetch.
    expect(authorizeCall[1].headers.get('Authorization')).toBe(
      'Bearer jwt-token-123',
    )
  })

  it('renders the static (animation-disabled) variant under reduced motion', async () => {
    mockReducedMotion(true)
    mockStatus('expired')
    renderBanner()

    const banner = await screen.findByTestId('reauth-banner')
    expect(banner).toHaveClass('ballast-reauth--static')
    expect(banner).toHaveAttribute('data-reduced-motion', 'true')
  })

  it('uses no brand-red token in its styling (calm, never red)', () => {
    // The color rule is an acceptance criterion, not a nicety: assert the
    // component stylesheet references no brand-red token at all.
    // vitest runs with cwd at the frontend package root.
    const cssPath = path.resolve('src/components/ReauthBanner.css')
    const css = readFileSync(cssPath, 'utf8')
    expect(css).not.toMatch(/brand-red/)
    expect(css).not.toMatch(/#ff2b3a/i)
    expect(css).not.toMatch(/#c01522/i)
  })
})
