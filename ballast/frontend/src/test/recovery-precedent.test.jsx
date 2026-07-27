import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { Coach } from '../routes/Coach.jsx'

afterEach(() => {
  vi.unstubAllGlobals()
})

function stubRecord(payload, { ok = true } = {}) {
  vi.stubGlobal(
    'fetch',
    vi.fn(() => Promise.resolve({ ok, json: () => Promise.resolve(payload) })),
  )
}

const EVENT_RECORD = {
  id: 'ep-9f2a1c7b3d40',
  kind: 'event-precedent',
  statement:
    'VTI is ~8.0% below its recent peak. In 5 similar drops, it recovered to breakeven in a median of 34 trading days.',
  stats: {
    initial_drawdown_pct: '0.0801',
    current_velocity: '0.0021',
    instance_count: 5,
    recovery_days_median: 34,
    recovery_days_range: { min: 12, max: 71 },
    forward_return_1yr_median: '0.1140',
    windows: [
      {
        peak_date: '2018-09-20',
        trough_date: '2018-12-24',
        recovery_date: '2019-04-23',
        drawdown_pct: '0.0795',
        velocity: '0.0019',
        recovery_days: 34,
        recovered: true,
        forward_return_1yr: '0.1502',
      },
      {
        peak_date: '2020-02-19',
        trough_date: '2020-03-23',
        recovery_date: null,
        drawdown_pct: '0.1100',
        velocity: '0.0090',
        recovery_days: null,
        recovered: false,
        forward_return_1yr: null,
      },
    ],
  },
  source: 'VTI daily close (market_daily)',
  as_of: '2026-07-27',
}

const STRATEGY_RECORD = {
  id: 'strat-abc123456789',
  kind: 'strategy',
  statement:
    'VTI is at or near its all-time high — no comparable drop to learn from right now. Stay the course with your plan.',
  stats: { reason: 'all_time_high', windows: [] },
  source: 'VTI daily close (market_daily)',
  as_of: '2026-07-27',
}

function renderCoach() {
  return render(
    <MemoryRouter>
      <Coach />
    </MemoryRouter>,
  )
}

describe('RecoveryPrecedent — calm, honest, accessible data-block', () => {
  it('renders the event-precedent block: statement, sky-blue ▼, green ▲, source + as-of', async () => {
    stubRecord(EVENT_RECORD)
    const { container } = renderCoach()

    await waitFor(() =>
      expect(screen.getByTestId('precedent-event')).toBeInTheDocument(),
    )

    // The engine's statement, verbatim.
    expect(screen.getByTestId('precedent-statement')).toHaveTextContent(
      /8\.0% below its recent peak/i,
    )

    // Drawdown → sky-blue ▼ with a real text label (never color alone).
    const drawdown = screen.getByTestId('precedent-drawdown')
    expect(within(drawdown).getByText('▼')).toBeInTheDocument()
    expect(within(drawdown).getByText(/8\.0% below its recent peak/i)).toBeInTheDocument()
    expect(
      drawdown.querySelector('.ballast-market-indicator--down'),
    ).not.toBeNull()

    // Forward return → green ▲ with a real text label. MarketIndicator owns
    // the single sign glyph, so the label carries the UNSIGNED magnitude (no
    // doubled sign like "+ +11.4%").
    const forward = screen.getByTestId('precedent-forward')
    expect(within(forward).getByText('▲')).toBeInTheDocument()
    expect(within(forward).getByText(/^11\.4% median one-year return/i)).toBeInTheDocument()
    expect(forward.querySelector('.ballast-market-indicator--up')).not.toBeNull()
    // Single sign only: MarketIndicator's "+" then the magnitude "11.4%". A
    // doubled sign (the old bug, label embedded its own "+") would show two
    // adjacent sign glyphs — assert that never happens.
    expect(forward.textContent).not.toMatch(/[+−]\s*[+−]/)

    // Always cites source + as-of.
    expect(screen.getByTestId('precedent-source')).toHaveTextContent(
      'VTI daily close (market_daily) · as of 2026-07-27',
    )

    // HARD color rule: never red/pink for a drop.
    expect(container.innerHTML).not.toMatch(/brand-red|accent-pink|line-red/)
  })

  it('expands to reveal the matched drops, including a not-yet-recovered episode', async () => {
    stubRecord(EVENT_RECORD)
    renderCoach()

    await waitFor(() =>
      expect(screen.getByTestId('precedent-toggle')).toBeInTheDocument(),
    )

    // Collapsed by default (disclosure is an accessible button).
    const toggle = screen.getByTestId('precedent-toggle')
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByTestId('precedent-windows')).not.toBeInTheDocument()

    toggle.click()

    await waitFor(() =>
      expect(screen.getByTestId('precedent-windows')).toBeInTheDocument(),
    )
    expect(toggle).toHaveAttribute('aria-expanded', 'true')

    // Both windows render; the second has no recovery → honest phrasing.
    expect(screen.getByTestId('precedent-window-0')).toHaveTextContent(
      /recovered to breakeven in 34 trading days/i,
    )
    const notRecovered = screen.getByTestId('precedent-window-1')
    expect(notRecovered).toHaveTextContent(/not yet recovered back to breakeven/i)
  })

  it('renders the strategy rationale — never an empty state — when the engine returns strategy', async () => {
    stubRecord(STRATEGY_RECORD)
    const { container } = renderCoach()

    await waitFor(() =>
      expect(screen.getByTestId('precedent-strategy')).toBeInTheDocument(),
    )
    // The rationale + citation, not a blank/dead end.
    expect(screen.getByTestId('precedent-strategy')).toHaveTextContent(
      /Stay the course with your plan/i,
    )
    expect(screen.getByTestId('precedent-source')).toHaveTextContent(
      'VTI daily close (market_daily) · as of 2026-07-27',
    )
    expect(screen.queryByTestId('precedent-event')).not.toBeInTheDocument()
    expect(container.innerHTML).not.toMatch(/brand-red|accent-pink|line-red/)
  })

  it('degrades to a calm static fallback rationale when the fetch fails — no error screen', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new Error('network down'))))
    const { container } = renderCoach()

    await waitFor(() =>
      expect(screen.getByTestId('precedent-fallback')).toBeInTheDocument(),
    )
    // Reassuring, plan-focused copy — no "error"/"failed" dead end.
    expect(screen.getByTestId('precedent-fallback')).toHaveTextContent(
      /Stay the course with your plan/i,
    )
    expect(container.innerHTML).not.toMatch(/error|failed to load/i)
    expect(container.innerHTML).not.toMatch(/brand-red|accent-pink|line-red/)
  })

  it('degrades to the calm fallback on a non-2xx response', async () => {
    stubRecord({ detail: 'unauthorized' }, { ok: false })
    renderCoach()

    await waitFor(() =>
      expect(screen.getByTestId('precedent-fallback')).toBeInTheDocument(),
    )
  })

  it('renders a NEGATIVE forward return as a sky-blue ▼ loss, never a green ▲ gain', async () => {
    // A drop that kept falling a year out: median forward return is negative.
    const negativeForward = {
      ...EVENT_RECORD,
      stats: {
        ...EVENT_RECORD.stats,
        forward_return_1yr_median: '-0.0320',
        windows: [
          { ...EVENT_RECORD.stats.windows[0], forward_return_1yr: '-0.0500' },
        ],
      },
    }
    stubRecord(negativeForward)
    const { container } = renderCoach()

    await waitFor(() =>
      expect(screen.getByTestId('precedent-forward')).toBeInTheDocument(),
    )

    // Direction follows the SIGN of the value: negative → sky-blue ▼ (down),
    // never the green ▲ gain treatment. This is the hard color invariant.
    const forward = screen.getByTestId('precedent-forward')
    expect(within(forward).getByText('▼')).toBeInTheDocument()
    expect(forward.querySelector('.ballast-market-indicator--down')).not.toBeNull()
    expect(forward.querySelector('.ballast-market-indicator--up')).toBeNull()
    // Magnitude is unsigned in the label; MarketIndicator supplies the − sign.
    expect(within(forward).getByText(/^3\.2% median one-year return/i)).toBeInTheDocument()

    // Never red/pink for the loss.
    expect(container.innerHTML).not.toMatch(/brand-red|accent-pink|line-red/)
  })
})
