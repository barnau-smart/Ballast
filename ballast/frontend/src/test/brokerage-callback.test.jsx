import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { BrokerageCallback } from '../routes/BrokerageCallback.jsx'
import { setToken, clearToken } from '../lib/session.js'

// The component reads the OAuth params off window.location.search (that's where
// Schwab's redirect puts them), so each test stubs `location` with a search
// string. NOTE: BrokerageCallback caches each single-use code at module scope
// (StrictMode double-fire guard), so every test MUST use a UNIQUE code — a
// reused code would return a prior test's resolved promise.
beforeEach(() => {
  setToken('jwt-token-123')
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  clearToken()
})

function renderCallback(search) {
  vi.stubGlobal('location', { ...window.location, search })
  return render(
    <MemoryRouter>
      <BrokerageCallback />
    </MemoryRouter>,
  )
}

describe('BrokerageCallback — real Schwab OAuth redirect landing', () => {
  it('exchanges the code and shows connected on success', async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve({ ok: true, json: () => Promise.resolve({ linked: true }) }),
    )
    vi.stubGlobal('fetch', fetchMock)

    renderCallback('?code=ok-code-1&state=st-1')

    await waitFor(() =>
      expect(
        screen.getByText(/your schwab account is connected/i),
      ).toBeInTheDocument(),
    )
    // A "Continue to dashboard" action appears on success.
    expect(
      screen.getByRole('button', { name: /continue to dashboard/i }),
    ).toBeInTheDocument()

    // The callback was POSTed with the code+state and the bearer via apiFetch.
    const cb = fetchMock.mock.calls.find((c) =>
      String(c[0]).includes('/api/brokerage/callback'),
    )
    expect(cb).toBeTruthy()
    expect(cb[1].method).toBe('POST')
    const body = JSON.parse(cb[1].body)
    expect(body).toEqual({ code: 'ok-code-1', state: 'st-1' })
    expect(cb[1].headers.get('Authorization')).toBe('Bearer jwt-token-123')
  })

  it('exchanges the single-use code exactly once despite StrictMode double-fire', async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve({ ok: true, json: () => Promise.resolve({ linked: true }) }),
    )
    vi.stubGlobal('fetch', fetchMock)

    // React StrictMode invokes effects twice in dev; the module-level promise
    // cache must collapse that to ONE network exchange (a reused code is dead).
    render(
      <MemoryRouter>
        <BrokerageCallback />
      </MemoryRouter>,
      { wrapper: ({ children }) => children },
    )
    // Re-render a second instance with the SAME code to simulate the double-fire.
    vi.stubGlobal('location', { ...window.location, search: '?code=once-code&state=st-x' })
    render(
      <MemoryRouter>
        <BrokerageCallback />
      </MemoryRouter>,
    )
    render(
      <MemoryRouter>
        <BrokerageCallback />
      </MemoryRouter>,
    )

    await waitFor(() =>
      expect(
        screen.getAllByText(/your schwab account is connected/i).length,
      ).toBeGreaterThan(0),
    )
    const cbCalls = fetchMock.mock.calls.filter((c) =>
      String(c[0]).includes('/api/brokerage/callback'),
    )
    expect(cbCalls.length).toBe(1)
  })

  it('shows a cancelled message when Schwab returns ?error and never calls the API', async () => {
    const fetchMock = vi.fn(() => Promise.reject(new Error('should not be called')))
    vi.stubGlobal('fetch', fetchMock)

    renderCallback('?error=access_denied')

    await waitFor(() =>
      expect(screen.getByText(/cancelled at schwab/i)).toBeInTheDocument(),
    )
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('guides the user back when opened without OAuth params', async () => {
    const fetchMock = vi.fn(() => Promise.reject(new Error('should not be called')))
    vi.stubGlobal('fetch', fetchMock)

    renderCallback('')

    await waitFor(() =>
      expect(screen.getByText(/start from onboarding/i)).toBeInTheDocument(),
    )
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('surfaces a retryable error when the exchange fails', async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve({ ok: false, status: 400, json: () => Promise.resolve({}) }),
    )
    vi.stubGlobal('fetch', fetchMock)

    renderCallback('?code=err-code-1&state=st-err')

    await waitFor(() =>
      expect(screen.getByText(/couldn.t finish the link/i)).toBeInTheDocument(),
    )
    expect(
      screen.getByRole('button', { name: /back to onboarding/i }),
    ).toBeInTheDocument()
  })
})
