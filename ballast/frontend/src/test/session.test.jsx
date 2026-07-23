import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { Layout } from '../components/Layout.jsx'
import { clearToken, getToken, isSignedIn, setToken } from '../lib/session.js'

beforeEach(() => {
  clearToken()
  // Health check + logout calls both go through fetch; stub it.
  vi.stubGlobal(
    'fetch',
    vi.fn(() =>
      Promise.resolve({ ok: true, json: () => Promise.resolve({}) }),
    ),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
  clearToken()
})

describe('session store', () => {
  it('stores and clears the token, reflecting signed-in state', () => {
    expect(isSignedIn()).toBe(false)
    setToken('abc')
    expect(getToken()).toBe('abc')
    expect(isSignedIn()).toBe(true)
    clearToken()
    expect(getToken()).toBeNull()
    expect(isSignedIn()).toBe(false)
  })

  it('persists the token to localStorage', () => {
    setToken('persist-me')
    expect(window.localStorage.getItem('ballast.token')).toBe('persist-me')
    clearToken()
    expect(window.localStorage.getItem('ballast.token')).toBeNull()
  })
})

describe('Layout signed-in chrome + logout', () => {
  it('hides the signed-in indicator and log out when signed out', () => {
    render(
      <MemoryRouter>
        <Layout />
      </MemoryRouter>,
    )
    expect(screen.queryByTestId('signed-in-indicator')).not.toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: /log out/i }),
    ).not.toBeInTheDocument()
  })

  it('shows signed-in state and logs out back to signed-out', async () => {
    setToken('a-real-token')
    render(
      <MemoryRouter>
        <Layout />
      </MemoryRouter>,
    )

    expect(screen.getByTestId('signed-in-indicator')).toBeInTheDocument()
    const logoutBtn = screen.getByRole('button', { name: /log out/i })

    fireEvent.click(logoutBtn)

    // Best-effort logout POST is fired, then the client session is cleared.
    await waitFor(() => expect(getToken()).toBeNull())
    await waitFor(() =>
      expect(
        screen.queryByTestId('signed-in-indicator'),
      ).not.toBeInTheDocument(),
    )

    const logoutCall = fetch.mock.calls.find((c) =>
      String(c[0]).includes('/api/auth/jwt/logout'),
    )
    expect(logoutCall).toBeTruthy()
    expect(logoutCall[1].method).toBe('POST')
  })
})
