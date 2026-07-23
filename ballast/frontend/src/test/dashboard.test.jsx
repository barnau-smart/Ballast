import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { Dashboard } from '../routes/Dashboard.jsx'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('Dashboard health check', () => {
  it('renders backend status on a successful fetch', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ status: 'ok', db: 'ok' }),
        }),
      ),
    )
    render(
      <MemoryRouter>
        <Dashboard />
      </MemoryRouter>,
    )
    await waitFor(() =>
      expect(screen.getByText(/backend: ok · db: ok/)).toBeInTheDocument(),
    )
  })

  it('degrades gracefully when the backend is unreachable', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new Error('network down'))),
    )
    render(
      <MemoryRouter>
        <Dashboard />
      </MemoryRouter>,
    )
    await waitFor(() =>
      expect(screen.getByText(/backend unreachable/)).toBeInTheDocument(),
    )
  })
})
