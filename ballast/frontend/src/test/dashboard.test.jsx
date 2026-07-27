import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { Dashboard } from '../routes/Dashboard.jsx'
import { describeHolding } from '../lib/holdings.js'

afterEach(() => {
  vi.unstubAllGlobals()
})

function stubPortfolio(payload, { ok = true } = {}) {
  vi.stubGlobal(
    'fetch',
    vi.fn(() =>
      Promise.resolve({ ok, json: () => Promise.resolve(payload) }),
    ),
  )
}

const SAMPLE = {
  holdings: [
    { symbol: 'VTI', quantity: '10', market_value: '2500.00', cost_basis: '2000.00', is_core: true },
    { symbol: 'VXUS', quantity: '20', market_value: '1200.00', cost_basis: '1100.00', is_core: true },
    { symbol: 'BND', quantity: '15', market_value: '1050.00', cost_basis: '1080.00', is_core: true },
  ],
  cash: '750.25',
  as_of: '2026-07-26T12:00:00Z',
}

function renderDashboard() {
  return render(
    <MemoryRouter>
      <Dashboard />
    </MemoryRouter>,
  )
}

describe('Dashboard — plain-English portfolio', () => {
  it('shows each holding with a plain-English description (no bare ticker)', async () => {
    stubPortfolio(SAMPLE)
    renderDashboard()

    await waitFor(() =>
      expect(screen.getByTestId('portfolio-panel')).toBeInTheDocument(),
    )

    for (const holding of SAMPLE.holdings) {
      const row = screen.getByTestId(`holding-${holding.symbol}`)
      // The symbol is shown...
      expect(within(row).getByText(holding.symbol)).toBeInTheDocument()
      // ...and always accompanied by its plain-English description (no jargon).
      expect(
        within(row).getByText(describeHolding(holding.symbol)),
      ).toBeInTheDocument()
    }
  })

  it('shows total value and cash as formatted currency', async () => {
    stubPortfolio(SAMPLE)
    renderDashboard()

    await waitFor(() =>
      expect(screen.getByTestId('portfolio-total')).toBeInTheDocument(),
    )
    // 2500 + 1200 + 1050 + 750.25 = 5500.25 (summed in cents, no float drift).
    expect(screen.getByTestId('portfolio-total')).toHaveTextContent('$5,500.25')
    expect(screen.getByTestId('portfolio-cash')).toHaveTextContent('$750.25')
  })

  it('renders a calm invite (not an error) when there is no portfolio', async () => {
    stubPortfolio({ holdings: [], cash: 0, as_of: null })
    renderDashboard()

    await waitFor(() =>
      expect(screen.getByTestId('portfolio-empty')).toBeInTheDocument(),
    )
    expect(screen.getByText(/Connect Schwab/i)).toBeInTheDocument()
    expect(screen.queryByTestId('portfolio-panel')).not.toBeInTheDocument()
  })

  it('shows a loss with the sky-blue ▼ treatment — never red/pink', async () => {
    stubPortfolio({
      holdings: [
        { symbol: 'BND', quantity: '15', market_value: '1050.00', cost_basis: '1080.00', is_core: true },
      ],
      cash: '0',
      as_of: '2026-07-26T12:00:00Z',
    })
    const { container } = renderDashboard()

    await waitFor(() =>
      expect(screen.getByTestId('holding-BND')).toBeInTheDocument(),
    )
    // Down move → ▼ glyph + "down since you bought" label, via the market
    // indicator's down modifier (sky-blue). Never a brand-red/pink class.
    expect(screen.getByText('▼')).toBeInTheDocument()
    expect(screen.getByText(/down since you bought/i)).toBeInTheDocument()
    expect(container.querySelector('.ballast-market-indicator--down')).not.toBeNull()
    expect(container.querySelector('.ballast-market-indicator--up')).toBeNull()
    // No element opts into a brand-red or pink treatment for the loss.
    expect(container.innerHTML).not.toMatch(/brand-red|accent-pink|line-red/)
  })

  it('maps holdings to index-core vs. the rest, with a plain explainer (Story 2.5)', async () => {
    stubPortfolio({
      holdings: [
        { symbol: 'VTI', quantity: '10', market_value: '2500.00', cost_basis: '2000.00', is_core: true },
        { symbol: 'AAPL', quantity: '5', market_value: '900.00', cost_basis: '800.00', is_core: false },
      ],
      cash: '0',
      as_of: '2026-07-26T12:00:00Z',
    })
    const { container } = renderDashboard()

    await waitFor(() =>
      expect(screen.getByTestId('portfolio-group-core')).toBeInTheDocument(),
    )
    const core = screen.getByTestId('portfolio-group-core')
    const rest = screen.getByTestId('portfolio-group-rest')
    // Core group holds VTI; the rest holds AAPL.
    expect(within(core).getByTestId('holding-VTI')).toBeInTheDocument()
    expect(within(rest).getByTestId('holding-AAPL')).toBeInTheDocument()
    // Plain explainer of what "core" means (no jargon).
    expect(screen.getByText(/steady base of your portfolio/i)).toBeInTheDocument()
    // Value split at a glance.
    expect(screen.getByTestId('portfolio-group-core-total')).toHaveTextContent('$2,500.00')
    expect(screen.getByTestId('portfolio-group-rest-total')).toHaveTextContent('$900.00')
    // Non-core is neutral — never framed as a loss/error (no red/pink).
    expect(container.innerHTML).not.toMatch(/brand-red|accent-pink|line-red/)
  })

  it('shows no "the rest" group when every holding is core', async () => {
    stubPortfolio(SAMPLE)
    renderDashboard()

    await waitFor(() =>
      expect(screen.getByTestId('portfolio-group-core')).toBeInTheDocument(),
    )
    expect(screen.queryByTestId('portfolio-group-rest')).not.toBeInTheDocument()
  })

  it('degrades to the calm invite when the fetch fails', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new Error('network down'))))
    renderDashboard()

    await waitFor(() =>
      expect(screen.getByTestId('portfolio-empty')).toBeInTheDocument(),
    )
  })
})
