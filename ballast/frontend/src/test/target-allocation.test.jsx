import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { Settings } from '../routes/Settings.jsx'
import { Dashboard } from '../routes/Dashboard.jsx'

afterEach(() => {
  vi.unstubAllGlobals()
  try {
    window.localStorage.clear()
  } catch {
    // jsdom always provides localStorage
  }
})

// The same tone bar as the backend digest FORBIDDEN list (test_digest_compose.py)
// — the spec's testable calm/no-FOMO standard. Word-boundary matched.
const FORBIDDEN = [
  'urgent', 'hurry', 'act now', 'act fast', "don't miss", 'dont miss',
  'missing out', 'miss out', 'last chance', 'limited time', 'warning',
  'alarm', 'panic', 'crash', 'plunge', 'fear', 'red', 'alert', 'immediately',
]

function expectCalm(text) {
  const blob = String(text).toLowerCase()
  for (const word of FORBIDDEN) {
    const pattern = new RegExp('\\b' + word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\b')
    expect(blob).not.toMatch(pattern)
  }
}

const CHOICES = [
  { key: 'conservative', name: 'Conservative', description: 'Mostly bonds for a steadier ride.', weights: { us_equity: '0.30', intl_equity: '0.10', bonds: '0.60' } },
  { key: 'balanced', name: 'Balanced', description: 'A solid stock base with a real bond cushion.', weights: { us_equity: '0.45', intl_equity: '0.20', bonds: '0.35' } },
  { key: 'growth', name: 'Growth', description: 'Mostly stocks for long-term growth.', weights: { us_equity: '0.60', intl_equity: '0.30', bonds: '0.10' } },
]

// A tiny stateful backend routed by URL. /api/target-allocation GET returns the
// current model + choices; PUT sets the model. The other endpoints return calm
// defaults so the Settings card and Dashboard render without noise (portfolio
// carries reserve_decided:true so the reserve prompt never competes).
function stubBackend({ model = null, portfolio } = {}) {
  let current = model
  const fetchMock = vi.fn((url, options = {}) => {
    const method = options.method ?? 'GET'
    const json = (body) => Promise.resolve({ ok: true, json: () => Promise.resolve(body) })
    if (url.includes('/api/target-allocation')) {
      if (method === 'PUT') current = JSON.parse(options.body).model
      return json({
        model: current,
        choices: CHOICES,
        resolved: current ? { model: current, weights: {}, funds: {} } : null,
      })
    }
    if (url.includes('/api/digest/preference')) return json({ opted_in: false })
    if (url.includes('/api/cash/config')) return json({ reserve_amount: null, reserve_decided: false, parked_symbols: [] })
    if (url.includes('/api/portfolio')) {
      return json(
        portfolio ?? {
          holdings: [],
          cash: '0',
          as_of: null,
          cash_states: { ready_to_trade: '0', parked: '0', reserved: null, reserve_decided: true },
        },
      )
    }
    return json({})
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

describe('Settings — target mix card (Story 10.1)', () => {
  it('shows the three choices, calm copy, and the undecided state', async () => {
    stubBackend({ model: null })
    render(<MemoryRouter><Settings /></MemoryRouter>)

    const card = await screen.findByTestId('target-mix-card')
    expect(screen.getByTestId('target-mix-conservative')).toBeInTheDocument()
    expect(screen.getByTestId('target-mix-balanced')).toBeInTheDocument()
    expect(screen.getByTestId('target-mix-growth')).toBeInTheDocument()
    expect(screen.getByTestId('target-mix-state')).toHaveTextContent(/no target mix picked yet/i)
    // Mix shown in plain English percentages.
    expect(card.textContent).toMatch(/60% US stocks · 30% international · 10% bonds/)
    // Calm, no FOMO / urgency / red — full FORBIDDEN tone bar.
    expectCalm(card.textContent)
    expect(card.innerHTML).not.toMatch(/brand-red|accent-pink|line-red/)
  })

  it('PUTs the chosen model and reflects it', async () => {
    const fetchMock = stubBackend({ model: null })
    render(<MemoryRouter><Settings /></MemoryRouter>)

    const growth = await screen.findByTestId('target-mix-growth')
    fireEvent.click(growth)

    await waitFor(() => {
      const put = fetchMock.mock.calls.find(([u, o]) => u.includes('/api/target-allocation') && o?.method === 'PUT')
      expect(put).toBeTruthy()
      expect(JSON.parse(put[1].body)).toEqual({ model: 'growth' })
    })
    await waitFor(() =>
      expect(screen.getByTestId('target-mix-state')).toHaveTextContent(/your target mix: growth/i),
    )
    expect(screen.getByTestId('target-mix-growth').checked).toBe(true)
  })

  it('reflects an already-chosen model from the server', async () => {
    stubBackend({ model: 'balanced' })
    render(<MemoryRouter><Settings /></MemoryRouter>)

    await waitFor(() => expect(screen.getByTestId('target-mix-balanced').checked).toBe(true))
    expect(screen.getByTestId('target-mix-state')).toHaveTextContent(/your target mix: balanced/i)
  })
})

describe('Dashboard — target-mix set-or-decline prompt (Story 10.1)', () => {
  it('shows the prompt only when the target is undecided, and it is dismissible', async () => {
    stubBackend({ model: null })
    render(<MemoryRouter><Dashboard /></MemoryRouter>)

    const prompt = await screen.findByTestId('target-mix-prompt')
    expect(within(prompt).getByTestId('target-mix-prompt-link')).toBeInTheDocument()
    // Calm framing, never alarmist — full FORBIDDEN tone bar.
    expectCalm(prompt.textContent)

    fireEvent.click(screen.getByTestId('target-mix-prompt-dismiss'))
    await waitFor(() => expect(screen.queryByTestId('target-mix-prompt')).not.toBeInTheDocument())
  })

  it('hides the prompt once a model has been chosen', async () => {
    stubBackend({ model: 'balanced' })
    render(<MemoryRouter><Dashboard /></MemoryRouter>)

    // Let the fetches settle.
    await waitFor(() => expect(screen.getByTestId('portfolio-empty')).toBeInTheDocument())
    expect(screen.queryByTestId('target-mix-prompt')).not.toBeInTheDocument()
  })
})
