import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { Settings } from '../routes/Settings.jsx'

afterEach(() => {
  vi.unstubAllGlobals()
})

/**
 * A tiny stateful fake backend routed by URL: the digest preference, the cash
 * config (GET + PUT round-trip with server-side normalization of parked
 * symbols), and the portfolio (for the held-symbol checkbox list). Every call is
 * recorded on the returned mock. Story 9.1.
 */
function stubBackend({ config, holdings }) {
  let state = { ...config }
  const fetchMock = vi.fn((url, options = {}) => {
    const method = options.method ?? 'GET'
    const json = (body) => Promise.resolve({ ok: true, json: () => Promise.resolve(body) })

    if (url.includes('/api/digest/preference')) return json({ opted_in: false })
    if (url.includes('/api/portfolio')) return json({ holdings, cash: '0', as_of: null })
    if (url.includes('/api/cash/config')) {
      if (method === 'PUT') {
        const body = JSON.parse(options.body)
        state = {
          reserve_amount: body.reserve_amount,
          reserve_decided: body.reserve_decided,
          // Mirror the server's normalize: trim / upper-case / de-dupe.
          parked_symbols: [
            ...new Set((body.parked_symbols ?? []).map((s) => String(s).trim().toUpperCase()).filter(Boolean)),
          ],
        }
      }
      return json(state)
    }
    return json({})
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function renderSettings() {
  return render(
    <MemoryRouter>
      <Settings />
    </MemoryRouter>,
  )
}

const NEVER_DECIDED = { reserve_amount: null, reserve_decided: false, parked_symbols: [] }

describe('Settings — cash setup (Story 9.1)', () => {
  it('shows calm, non-FOMO copy and the never-decided default', async () => {
    stubBackend({ config: NEVER_DECIDED, holdings: [{ symbol: 'SWVXX' }] })
    renderSettings()

    const card = await screen.findByTestId('cash-setup-card')
    expect(screen.getByTestId('cash-reserve-state')).toHaveTextContent(/not set yet/i)
    // Calm framing, no alarm / urgency / FOMO / red.
    expect(card.textContent).not.toMatch(
      /miss out|missing out|hurry|urgent|act now|last chance|don'?t miss|fear|warning|alarm|panic|crash/i,
    )
    expect(card.innerHTML).not.toMatch(/brand-red|accent-pink|line-red/)
  })

  it('lists the user’s held symbols as parked-tagging checkboxes', async () => {
    stubBackend({ config: NEVER_DECIDED, holdings: [{ symbol: 'VTI' }, { symbol: 'SWVXX' }] })
    renderSettings()

    expect(await screen.findByTestId('cash-parked-VTI')).toBeInTheDocument()
    expect(screen.getByTestId('cash-parked-SWVXX')).toBeInTheDocument()
    expect(screen.getByTestId('cash-parked-SWVXX').checked).toBe(false)
  })

  it('PUTs an explicit reserve when the user saves an amount', async () => {
    const fetchMock = stubBackend({ config: NEVER_DECIDED, holdings: [] })
    renderSettings()

    const input = await screen.findByTestId('cash-reserve-input')
    fireEvent.change(input, { target: { value: '1500' } })
    fireEvent.click(screen.getByTestId('cash-reserve-save'))

    await waitFor(() => {
      const put = fetchMock.mock.calls.find(([u, o]) => u.includes('/api/cash/config') && o?.method === 'PUT')
      expect(put).toBeTruthy()
      const body = JSON.parse(put[1].body)
      expect(body.reserve_amount).toBe('1500')
      expect(body.reserve_decided).toBe(true)
    })
    await waitFor(() =>
      expect(screen.getByTestId('cash-reserve-state')).toHaveTextContent(/your reserve/i),
    )
  })

  it('PUTs a decline (null amount, decided) when the user says they don’t keep one', async () => {
    const fetchMock = stubBackend({ config: NEVER_DECIDED, holdings: [] })
    renderSettings()

    const decline = await screen.findByTestId('cash-reserve-decline')
    fireEvent.click(decline)

    await waitFor(() => {
      const put = fetchMock.mock.calls.find(([u, o]) => u.includes('/api/cash/config') && o?.method === 'PUT')
      expect(put).toBeTruthy()
      const body = JSON.parse(put[1].body)
      expect(body.reserve_amount).toBeNull()
      expect(body.reserve_decided).toBe(true)
    })
    await waitFor(() =>
      expect(screen.getByTestId('cash-reserve-state')).toHaveTextContent(/don’t keep a reserve/i),
    )
  })

  it('PUTs the tagged parked symbol when a checkbox is toggled on', async () => {
    const fetchMock = stubBackend({ config: NEVER_DECIDED, holdings: [{ symbol: 'SWVXX' }] })
    renderSettings()

    const box = await screen.findByTestId('cash-parked-SWVXX')
    fireEvent.click(box)

    await waitFor(() => {
      const put = fetchMock.mock.calls.find(([u, o]) => u.includes('/api/cash/config') && o?.method === 'PUT')
      expect(put).toBeTruthy()
      const body = JSON.parse(put[1].body)
      expect(body.parked_symbols).toContain('SWVXX')
    })
    await waitFor(() => expect(screen.getByTestId('cash-parked-SWVXX').checked).toBe(true))
  })
})
