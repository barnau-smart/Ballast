import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import App from '../App.jsx'

// The Dashboard fetches /api/portfolio on mount; mock fetch so route smoke
// tests never touch the network. An empty portfolio is fine here (the empty
// invite state renders); we only assert the shared chrome is present.
beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(() =>
      Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ holdings: [], cash: 0, as_of: null }),
      }),
    ),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
})

const ROUTES = ['/', '/dashboard', '/auth', '/onboarding', '/coach', '/decisions', '/settings']

describe('surface routes', () => {
  it.each(ROUTES)('renders %s without error', (path) => {
    render(
      <MemoryRouter initialEntries={[path]}>
        <App />
      </MemoryRouter>,
    )
    // The shared header (Wordmark) is present on every route.
    expect(screen.getByText('BALLAST')).toBeInTheDocument()
  })
})
