import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { Onboarding } from '../routes/Onboarding.jsx'
import { setToken, clearToken } from '../lib/session.js'

beforeEach(() => {
  // A held token so apiFetch attaches the bearer to the backend calls.
  setToken('jwt-token-123')
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  clearToken()
})

function renderOnboarding() {
  return render(
    <MemoryRouter>
      <Onboarding />
    </MemoryRouter>,
  )
}

describe('Onboarding — Schwab link (Story 2.1)', () => {
  it('shows not-connected status and a Connect Schwab action', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ linked: false, provider: null }),
        }),
      ),
    )

    renderOnboarding()

    await waitFor(() =>
      expect(screen.getByText(/isn.t connected yet/i)).toBeInTheDocument(),
    )
    expect(
      screen.getByRole('button', { name: /connect schwab/i }),
    ).toBeInTheDocument()
  })

  it('calls authorize and redirects to the returned URL on Connect', async () => {
    const fetchMock = vi.fn((path) => {
      if (String(path).includes('/api/brokerage/status')) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ linked: false, provider: null }),
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
            }),
        })
      }
      return Promise.reject(new Error(`unexpected fetch: ${path}`))
    })
    vi.stubGlobal('fetch', fetchMock)

    // Mock the navigation hand-off.
    const assign = vi.fn()
    vi.stubGlobal('location', { ...window.location, assign })

    renderOnboarding()

    await waitFor(() =>
      expect(screen.getByText(/isn.t connected yet/i)).toBeInTheDocument(),
    )
    fireEvent.click(screen.getByRole('button', { name: /connect schwab/i }))

    await waitFor(() =>
      expect(assign).toHaveBeenCalledWith(
        'https://fake-broker.ballast.local/oauth/authorize?state=abc',
      ),
    )

    // The authorize endpoint was called (with the bearer via apiFetch).
    const authorizeCall = fetchMock.mock.calls.find((c) =>
      String(c[0]).includes('/api/brokerage/authorize'),
    )
    expect(authorizeCall).toBeTruthy()
    expect(authorizeCall[1].headers.get('Authorization')).toBe(
      'Bearer jwt-token-123',
    )
  })

  it('shows a connected status when already linked', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ linked: true, provider: 'fake' }),
        }),
      ),
    )

    renderOnboarding()

    await waitFor(() =>
      expect(
        screen.getByText(/your schwab account is connected/i),
      ).toBeInTheDocument(),
    )
    // No Connect button once linked.
    expect(
      screen.queryByRole('button', { name: /connect schwab/i }),
    ).not.toBeInTheDocument()
  })

  it('degrades gracefully if the status check fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new Error('network down'))),
    )

    renderOnboarding()

    await waitFor(() =>
      expect(
        screen.getByText(/couldn.t check your link status/i),
      ).toBeInTheDocument(),
    )
  })

  it('fake provider: completes the link in-app (no external redirect)', async () => {
    let linked = false
    const fetchMock = vi.fn((path, opts) => {
      const p = String(path)
      if (p.includes('/api/brokerage/status')) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ linked, provider: linked ? 'fake' : null }),
        })
      }
      if (p.includes('/api/brokerage/authorize')) {
        return Promise.resolve({
          ok: true,
          json: () =>
            Promise.resolve({
              authorization_url: 'https://fake-broker.ballast.local/oauth/authorize?state=abc',
              state: 'abc',
              provider: 'fake',
            }),
        })
      }
      if (p.includes('/api/brokerage/callback')) {
        linked = true // the fake adapter auto-approves; next status is linked
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ linked: true }) })
      }
      return Promise.reject(new Error(`unexpected fetch: ${p}`))
    })
    vi.stubGlobal('fetch', fetchMock)
    const assign = vi.fn()
    vi.stubGlobal('location', { ...window.location, assign })

    renderOnboarding()
    await waitFor(() =>
      expect(screen.getByText(/isn.t connected yet/i)).toBeInTheDocument(),
    )
    fireEvent.click(screen.getByRole('button', { name: /connect schwab/i }))

    // Ends connected, WITHOUT any external navigation.
    await waitFor(() =>
      expect(
        screen.getByText(/your schwab account is connected/i),
      ).toBeInTheDocument(),
    )
    expect(assign).not.toHaveBeenCalled()

    // The callback was posted with the signed state from /authorize.
    const cbCall = fetchMock.mock.calls.find((c) =>
      String(c[0]).includes('/api/brokerage/callback'),
    )
    expect(cbCall).toBeTruthy()
    expect(cbCall[1].method).toBe('POST')
    expect(JSON.parse(cbCall[1].body).state).toBe('abc')
  })
})
