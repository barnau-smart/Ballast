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
  // Story 9.2 additive fields — pure-settlement default (no parked, no reserve).
  settlement_cash: '25000.00',
  parked: '0.00',
  reserved: null,
  reserve_decided: false,
  money_market_apy: '0.04',
  investable_base: '25000.00',
}

// Story 9.2: cash+parked with a set reserve — yield-aware, reserve-protected.
const YIELD_AWARE = {
  ...RISING,
  idle_cash: '8000.00',
  investable_base: '8000.00',
  settlement_cash: '5000.00',
  parked: '5000.00',
  reserved: '2000.00',
  reserve_decided: true,
  forgone_growth: '1000.00',
  statement:
    'Over the past year, about $8,000.00 of investable cash sat out roughly $1,000.00 of growth — and your $2,000.00 reserve stayed protected, just as you set it (counting your parked money-market cash as already earning about 4% a year).',
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
  settlement_cash: '0.00',
  parked: '0.00',
  reserved: null,
  reserve_decided: false,
  money_market_apy: '0.04',
  investable_base: '0.00',
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
  settlement_cash: '25000.00',
  parked: '0.00',
  reserved: null,
  reserve_decided: false,
  money_market_apy: '0.04',
  investable_base: '25000.00',
}

// Story 9.2: reserve covers ALL cash → the dedicated fully_reserved block.
const FULLY_RESERVED = {
  idle_cash: '0.00',
  benchmark: 'VTI',
  window_return: null,
  window_start: null,
  window_end: null,
  forgone_growth: '0.00',
  trading_days: 252,
  statement:
    'Your reserve covers all of your cash right now — nothing is sitting idle to invest.',
  source: 'VTI daily close (market_daily)',
  as_of: null,
  sufficient: true,
  reason: 'fully_reserved',
  settlement_cash: '0.00',
  parked: '1000.00',
  reserved: '5000.00',
  reserve_decided: true,
  money_market_apy: '0.04',
  investable_base: '0.00',
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

  // --- Story 9.2: reserve-protected + disclosed-yield lines ------------------

  it('renders the protected-reserve line + disclosed-yield note (parked + reserve)', async () => {
    stubEstimate(YIELD_AWARE)
    const { container } = render(<MissedGrowthMeter />)

    await waitFor(() =>
      expect(screen.getByTestId('missed-growth-figure')).toBeInTheDocument(),
    )

    // Statement rendered VERBATIM (AD-1) — carries the reserve + yield clauses.
    expect(screen.getByTestId('missed-growth-statement')).toHaveTextContent(
      /\$8,000\.00 of investable cash sat out roughly \$1,000\.00 of growth/i,
    )

    // A calm, non-alarmist protected-reserve line with the real amount.
    const reserve = screen.getByTestId('missed-growth-reserve')
    expect(reserve).toHaveTextContent(/\$2,000\.00 stayed protected, as you set it/i)

    // The disclosed money-market yield assumption (parked > 0).
    const note = screen.getByTestId('missed-growth-yield')
    expect(note).toHaveTextContent(/parked money-market cash as already earning about 4% a year/i)

    // HARD color rule + no nudge, still.
    expect(container.innerHTML).not.toMatch(/brand-red|accent-pink|line-red/)
    expect(container.textContent).not.toMatch(NUDGE_RE)
  })

  it('renders the dedicated fully-reserved block (reserve covers all cash) — statement + reserve line, no figure, no yield note', async () => {
    stubEstimate(FULLY_RESERVED)
    const { container } = render(<MissedGrowthMeter />)

    // The dedicated fully_reserved block renders (not the figure/no-cash block).
    await waitFor(() =>
      expect(
        screen.getByTestId('missed-growth-fully-reserved'),
      ).toBeInTheDocument(),
    )

    // The engine's calm statement shows verbatim.
    expect(
      screen.getByTestId('missed-growth-fully-reserved'),
    ).toHaveTextContent(
      /reserve covers all of your cash right now — nothing is sitting idle to invest/i,
    )

    // The protected-reserve line shows the real reserved amount.
    expect(screen.getByTestId('missed-growth-reserve')).toHaveTextContent(
      /\$5,000\.00 stayed protected, as you set it/i,
    )

    // NO $0.00 figure and NO yield note — nothing is investable, the yield
    // assumption is moot.
    expect(screen.queryByTestId('missed-growth-amount')).not.toBeInTheDocument()
    expect(screen.queryByTestId('missed-growth-yield')).not.toBeInTheDocument()
    // Not the plain figure block either.
    expect(screen.queryByTestId('missed-growth-figure')).not.toBeInTheDocument()

    // HARD color rule + no nudge / FOMO copy.
    expect(container.innerHTML).not.toMatch(/brand-red|accent-pink|line-red/)
    expect(container.textContent).not.toMatch(NUDGE_RE)
  })

  it('never fabricates a reserve line when the reserve is never-decided', async () => {
    // RISING carries reserved=null + reserve_decided=false, parked=0.00.
    stubEstimate(RISING)
    render(<MissedGrowthMeter />)

    await waitFor(() =>
      expect(screen.getByTestId('missed-growth-figure')).toBeInTheDocument(),
    )
    // No reserve figure invented; no yield note when there is no parked money.
    expect(screen.queryByTestId('missed-growth-reserve')).not.toBeInTheDocument()
    expect(screen.queryByTestId('missed-growth-yield')).not.toBeInTheDocument()
  })
})
