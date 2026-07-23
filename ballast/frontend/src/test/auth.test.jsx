import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { Auth } from '../routes/Auth.jsx'
import { clearToken, getToken } from '../lib/session.js'

beforeEach(() => {
  clearToken()
})

afterEach(() => {
  vi.unstubAllGlobals()
  clearToken()
})

function fillCredentials() {
  fireEvent.change(screen.getByLabelText(/email/i), {
    target: { value: 'new@example.com' },
  })
  fireEvent.change(screen.getByLabelText(/password/i), {
    target: { value: 'supersecret123' },
  })
}

function renderAuth() {
  return render(
    <MemoryRouter>
      <Auth />
    </MemoryRouter>,
  )
}

// --- Sign up (Story 1.2, still supported via the Sign up mode) --------------

describe('Auth sign-up mode', () => {
  it('renders the sign-up form when Sign up is selected', () => {
    renderAuth()
    fireEvent.click(screen.getByRole('tab', { name: /sign up/i }))
    expect(screen.getByLabelText(/email/i)).toBeInTheDocument()
    expect(screen.getByLabelText(/password/i)).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: /create account/i }),
    ).toBeInTheDocument()
  })

  it('submits register and shows a success message', async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ id: 'abc', email: 'new@example.com' }),
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    renderAuth()
    fireEvent.click(screen.getByRole('tab', { name: /sign up/i }))
    fillCredentials()
    fireEvent.click(screen.getByRole('button', { name: /create account/i }))

    await waitFor(() =>
      expect(screen.getByText(/your account is ready/i)).toBeInTheDocument(),
    )
    const [url, options] = fetchMock.mock.calls[0]
    expect(url).toContain('/api/auth/register')
    expect(options.method).toBe('POST')
    expect(options.body).toContain('new@example.com')
  })

  it('shows the backend plain-language error for a duplicate email', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve({
          ok: false,
          status: 400,
          json: () =>
            Promise.resolve({
              error: {
                type: 'auth_error',
                message:
                  'An account with that email already exists. Try logging in instead.',
              },
            }),
        }),
      ),
    )

    renderAuth()
    fireEvent.click(screen.getByRole('tab', { name: /sign up/i }))
    fillCredentials()
    fireEvent.click(screen.getByRole('button', { name: /create account/i }))

    await waitFor(() =>
      expect(
        screen.getByText(/an account with that email already exists/i),
      ).toBeInTheDocument(),
    )
  })
})

// --- Log in (Story 1.3) ------------------------------------------------------

describe('Auth log-in mode', () => {
  it('defaults to the log-in form', () => {
    renderAuth()
    expect(screen.getByRole('button', { name: /log in/i })).toBeInTheDocument()
  })

  it('submits the OAuth2 form, stores the token, and navigates', async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve({
        ok: true,
        json: () =>
          Promise.resolve({ access_token: 'jwt-token-123', token_type: 'bearer' }),
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    renderAuth()
    fillCredentials()
    fireEvent.click(screen.getByRole('button', { name: /^log in$/i }))

    await waitFor(() => expect(getToken()).toBe('jwt-token-123'))

    const [url, options] = fetchMock.mock.calls[0]
    expect(url).toContain('/api/auth/jwt/login')
    expect(options.method).toBe('POST')
    expect(options.headers['Content-Type']).toBe(
      'application/x-www-form-urlencoded',
    )
    // OAuth2 form fields: username=email + password.
    const body = options.body.toString()
    expect(body).toContain('username=new%40example.com')
    expect(body).toContain('password=supersecret123')
  })

  it('shows the generic plain error for wrong credentials', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve({
          ok: false,
          status: 400,
          json: () =>
            Promise.resolve({
              error: {
                type: 'auth_error',
                message: "That email or password doesn't match. Please try again.",
              },
            }),
        }),
      ),
    )

    renderAuth()
    fillCredentials()
    fireEvent.click(screen.getByRole('button', { name: /^log in$/i }))

    await waitFor(() =>
      expect(
        screen.getByText(/that email or password doesn't match/i),
      ).toBeInTheDocument(),
    )
    expect(getToken()).toBeNull()
  })

  it('shows a graceful error when the network fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new Error('network down'))),
    )

    renderAuth()
    fillCredentials()
    fireEvent.click(screen.getByRole('button', { name: /^log in$/i }))

    await waitFor(() =>
      expect(screen.getByText(/something went wrong/i)).toBeInTheDocument(),
    )
  })
})
