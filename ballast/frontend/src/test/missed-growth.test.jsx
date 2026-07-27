import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import { MissedGrowthMeter } from '../components/MissedGrowthMeter.jsx'

afterEach(() => {
  vi.unstubAllGlobals()
})

function stubEstimate(payload, { ok = true } = {}) {
  vi.stubGlobal(
    'fetch',
    vi.fn(() => Promise.resolve({ ok, json: () => Promise.resolve(payload) })),
  )
}

const RISING = {
  idle_cash: '25000.00',
  benchmark: 'VTI',
  window_return: '0.1400',
  window_start: '2025-07-28',
  window_end: '2026-07-27',
  forgone_growth: '3500.00',
  trading_days: 252,
  statement:
    'Your ~$25,000.00 in idle cash has sat out ~$3,500.00 of growth over the past year.',
  source: 'VTI daily close (market_daily)',
  as_of: '2026-07-27',
  sufficient: true,
  reason: null,
}

const FALLING = {
  ...RISING,
  window_return: '-0.1000',
  forgone_growth: '-1000.00',
  idle_cash: '10000.00',
  statement:
    'Over the past year the market fell, so your ~$10,000.00 in idle cash avoided ~$1,000.00 of loss.',
}

const FLAT = {
  ...RISING,
  window_return: '0.0000',
  forgone_growth: '0.00',
  statement:
    'Over the past year the market was roughly flat, so your ~$25,000.00 in idle cash has not missed measurable growth.',
}

const NO_CASH = {
  idle_cash: '0.00',
  benchmark: 'VTI',
  window_return: null,
  window_start: null,
  window_end: null,
  forgone_growth: '0.00',
  trading_days: 252,
  statement:
    'You have no idle cash sitting out of the market right now, so there is no forgone growth to show.',
  source: 'VTI daily close (market_daily)',
  as_of: null,
  sufficient: true,
  reason: 'no_idle_cash',
}

const INSUFFICIENT = {
  idle_cash: '25000.00',
  benchmark: 'VTI',
  window_return: null,
  window_start: null,
  window_end: '2016-01-01',
  forgone_growth: '0.00',
  trading_days: 252,
  statement:
    'There is not enough market history yet to estimate what idle cash may have missed over the past year.',
  source: 'VTI daily close (market_daily)',
  as_of: '2016-01-01',
  sufficient: false,
  reason: 'insufficient_history',
}

// A guard shared across states: never a nudge / FOMO / call to action.
const NUDGE_RE = /invest now|you should|don'?t wait|move your cash/i

describe('MissedGrowthMeter — calm, honest, accessible, never a nudge', () => {
  it('renders a rising window as a green ▲ with statement + source', async () => {
    stubEstimate(RISING)
    const { container } = render(<MissedGrowthMeter />)

    await waitFor(() =>
      expect(screen.getByTestId('missed-growth-figure')).toBeInTheDocument(),
    )

    // Honest "growth missed" copy with the real dollar amount.
    expect(screen.getByTestId('missed-growth-statement')).toHaveTextContent(
      /\$3,500\.00 of growth/i,
    )

    // Positive figure → green ▲ with a real text label (never color alone).
    const amount = screen.getByTestId('missed-growth-amount')
    expect(within(amount).getByText('▲')).toBeInTheDocument()
    expect(amount.querySelector('.ballast-market-indicator--up')).not.toBeNull()
    expect(within(amount).getByText(/\$3,500\.00 of growth not captured/i)).toBeInTheDocument()
    // No doubled sign (label carries no sign; MarketIndicator owns the glyph).
    expect(amount.textContent).not.toMatch(/[+−]\s*[+−]/)

    // Cites source + window.
    expect(screen.getByTestId('missed-growth-source')).toHaveTextContent(
      'VTI daily close (market_daily) · as of 2026-07-27',
    )
    expect(screen.getByTestId('missed-growth-window')).toHaveTextContent(/VTI/)

    // HARD color rule + no nudge.
    expect(container.innerHTML).not.toMatch(/brand-red|accent-pink|line-red/)
    expect(container.textContent).not.toMatch(NUDGE_RE)
  })

  it('renders a falling window as a sky-blue ▼ "loss avoided" — never a "cost"', async () => {
    stubEstimate(FALLING)
    const { container } = render(<MissedGrowthMeter />)

    await waitFor(() =>
      expect(screen.getByTestId('missed-growth-figure')).toBeInTheDocument(),
    )

    // Honest, calm framing: an AVOIDED loss, never a "cost" of holding cash.
    const statement = screen.getByTestId('missed-growth-statement')
    expect(statement).toHaveTextContent(/avoided/i)
    expect(container.textContent).not.toMatch(/cost/i)

    // Negative figure → sky-blue ▼ (down), never the green ▲ gain treatment.
    const amount = screen.getByTestId('missed-growth-amount')
    expect(within(amount).getByText('▼')).toBeInTheDocument()
    expect(amount.querySelector('.ballast-market-indicator--down')).not.toBeNull()
    expect(amount.querySelector('.ballast-market-indicator--up')).toBeNull()
    expect(within(amount).getByText(/\$1,000\.00 of loss avoided/i)).toBeInTheDocument()

    expect(container.innerHTML).not.toMatch(/brand-red|accent-pink|line-red/)
    expect(container.textContent).not.toMatch(NUDGE_RE)
  })

  it('renders a flat window as a neutral note with no ▲/▼ figure (never a phantom +$0.00 gain)', async () => {
    stubEstimate(FLAT)
    const { container } = render(<MissedGrowthMeter />)

    await waitFor(() =>
      expect(screen.getByTestId('missed-growth-figure')).toBeInTheDocument(),
    )

    // Neutral honest copy — a flat year is a non-event, not "growth missed".
    expect(screen.getByTestId('missed-growth-statement')).toHaveTextContent(
      /roughly flat|not missed measurable/i,
    )
    // The ▲/▼ figure is suppressed entirely — no green +$0.00 "gain".
    expect(screen.queryByTestId('missed-growth-amount')).not.toBeInTheDocument()
    expect(container.innerHTML).not.toMatch(/brand-red|accent-pink|line-red/)
    expect(container.textContent).not.toMatch(NUDGE_RE)
  })

  it('renders the calm "nothing idle" state (not an empty block) when there is no idle cash', async () => {
    stubEstimate(NO_CASH)
    const { container } = render(<MissedGrowthMeter />)

    await waitFor(() =>
      expect(screen.getByTestId('missed-growth-no-cash')).toBeInTheDocument(),
    )
    expect(screen.getByTestId('missed-growth-no-cash')).toHaveTextContent(
      /nothing sitting idle/i,
    )
    // Still cites the source — never a dead end.
    expect(screen.getByTestId('missed-growth-source')).toHaveTextContent(
      /VTI daily close/,
    )
    expect(screen.queryByTestId('missed-growth-figure')).not.toBeInTheDocument()
    expect(container.innerHTML).not.toMatch(/brand-red|accent-pink|line-red/)
    expect(container.textContent).not.toMatch(NUDGE_RE)
  })

  it('renders the calm rationale when there is insufficient history', async () => {
    stubEstimate(INSUFFICIENT)
    const { container } = render(<MissedGrowthMeter />)

    await waitFor(() =>
      expect(screen.getByTestId('missed-growth-insufficient')).toBeInTheDocument(),
    )
    expect(screen.getByTestId('missed-growth-insufficient')).toHaveTextContent(
      /not enough market history/i,
    )
    expect(screen.getByTestId('missed-growth-source')).toHaveTextContent(
      /VTI daily close/,
    )
    expect(screen.queryByTestId('missed-growth-figure')).not.toBeInTheDocument()
    expect(container.innerHTML).not.toMatch(/brand-red|accent-pink|line-red/)
  })

  it('degrades to a calm static fallback on a network failure — no error screen', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new Error('network down'))))
    const { container } = render(<MissedGrowthMeter />)

    await waitFor(() =>
      expect(screen.getByTestId('missed-growth-fallback')).toBeInTheDocument(),
    )
    // Reassuring, plan-focused copy — no "error"/"failed" dead end, no nudge.
    expect(container.innerHTML).not.toMatch(/error|failed to load/i)
    expect(container.innerHTML).not.toMatch(/brand-red|accent-pink|line-red/)
    expect(container.textContent).not.toMatch(NUDGE_RE)
  })

  it('degrades to the calm fallback on a non-2xx response', async () => {
    stubEstimate({ detail: 'unauthorized' }, { ok: false })
    render(<MissedGrowthMeter />)

    await waitFor(() =>
      expect(screen.getByTestId('missed-growth-fallback')).toBeInTheDocument(),
    )
  })
})
