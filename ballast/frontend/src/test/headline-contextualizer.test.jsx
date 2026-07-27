import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, waitFor, within, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { HeadlineContextualizer } from '../components/HeadlineContextualizer.jsx'

afterEach(() => {
  vi.unstubAllGlobals()
})

function stubRecord(payload, { ok = true } = {}) {
  const fn = vi.fn(() =>
    Promise.resolve({ ok, json: () => Promise.resolve(payload) }),
  )
  vi.stubGlobal('fetch', fn)
  return fn
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

// A calm/CTA-free copy guard: the framing and all rendered copy must never
// nudge, pressure, or offer a take on the news event itself.
const NUDGE_OR_CLASSIFY =
  /invest now|you should|don'?t wait|buy the dip|this (crash|selloff|news) (means|is)/i

function renderWidget() {
  return render(
    <MemoryRouter>
      <HeadlineContextualizer />
    </MemoryRouter>,
  )
}

function typeHeadline(text) {
  fireEvent.change(screen.getByTestId('headline-input'), {
    target: { value: text },
  })
}

function submit() {
  fireEvent.click(screen.getByTestId('headline-submit'))
}

describe('HeadlineContextualizer — pull-only, calm, non-interpretive', () => {
  it('idle: renders the input, submit disabled, and does NOT fetch on mount', () => {
    const fetchFn = stubRecord(EVENT_RECORD)
    renderWidget()

    expect(screen.getByTestId('headline-input')).toBeInTheDocument()
    expect(screen.getByTestId('headline-submit')).toBeDisabled()
    // Pull-only: nothing fetched until the user submits.
    expect(fetchFn).not.toHaveBeenCalled()
    // Idle shows only the prompt + input — no evidence block yet.
    expect(screen.queryByTestId('headline-result')).not.toBeInTheDocument()
    expect(screen.queryByTestId('precedent-event')).not.toBeInTheDocument()
  })

  it('submit stays disabled for whitespace-only input', () => {
    stubRecord(EVENT_RECORD)
    renderWidget()

    typeHeadline('   ')
    expect(screen.getByTestId('headline-submit')).toBeDisabled()
  })

  it('type + submit an event-precedent payload → neutral framing + shared evidence block', async () => {
    const fetchFn = stubRecord(EVENT_RECORD)
    const { container } = renderWidget()

    typeHeadline('Stocks slide on rate fears')
    expect(screen.getByTestId('headline-submit')).toBeEnabled()
    submit()

    await waitFor(() =>
      expect(screen.getByTestId('precedent-event')).toBeInTheDocument(),
    )

    // POSTed the headline to the contextualize endpoint (pull-only, on submit).
    expect(fetchFn).toHaveBeenCalledTimes(1)
    const [url, options] = fetchFn.mock.calls[0]
    expect(url).toMatch(/\/api\/precedent\/contextualize$/)
    expect(options.method).toBe('POST')
    expect(JSON.parse(options.body)).toEqual({
      headline: 'Stocks slide on rate fears',
    })

    // Neutral framing that does NOT judge the news.
    const framing = screen.getByTestId('headline-framing')
    expect(framing).toHaveTextContent(/doesn'?t weigh in on the news itself/i)

    // Shared evidence block: statement + sky-blue ▼ drawdown.
    expect(screen.getByTestId('precedent-statement')).toHaveTextContent(
      /8\.0% below its recent peak/i,
    )
    const drawdown = screen.getByTestId('precedent-drawdown')
    expect(within(drawdown).getByText('▼')).toBeInTheDocument()
    expect(
      drawdown.querySelector('.ballast-market-indicator--down'),
    ).not.toBeNull()

    // Sign-correct forward return → green ▲, single sign, unsigned magnitude.
    const forward = screen.getByTestId('precedent-forward')
    expect(within(forward).getByText('▲')).toBeInTheDocument()
    expect(forward.querySelector('.ballast-market-indicator--up')).not.toBeNull()
    expect(forward.textContent).not.toMatch(/[+−]\s*[+−]/)

    // Cites source + as-of.
    expect(screen.getByTestId('precedent-source')).toHaveTextContent(
      'VTI daily close (market_daily) · as of 2026-07-27',
    )

    // HARD color rule: never red/pink. And never nudge/classify copy.
    expect(container.innerHTML).not.toMatch(/brand-red|accent-pink|line-red/)
    expect(container.textContent).not.toMatch(NUDGE_OR_CLASSIFY)
  })

  it('a strategy payload renders the rationale — never an empty state', async () => {
    stubRecord(STRATEGY_RECORD)
    const { container } = renderWidget()

    typeHeadline('Markets calm today')
    submit()

    await waitFor(() =>
      expect(screen.getByTestId('precedent-strategy')).toBeInTheDocument(),
    )
    expect(screen.getByTestId('precedent-strategy')).toHaveTextContent(
      /Stay the course with your plan/i,
    )
    // Rationale is non-empty and cites source + as-of.
    expect(screen.getByTestId('precedent-source')).toHaveTextContent(
      'VTI daily close (market_daily) · as of 2026-07-27',
    )
    expect(container.innerHTML).not.toMatch(/brand-red|accent-pink|line-red/)
    expect(container.textContent).not.toMatch(NUDGE_OR_CLASSIFY)
  })

  it('network error → calm static fallback (no error/failed text)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new Error('network down'))),
    )
    const { container } = renderWidget()

    typeHeadline('Anything at all')
    submit()

    await waitFor(() =>
      expect(screen.getByTestId('headline-fallback')).toBeInTheDocument(),
    )
    expect(screen.getByTestId('headline-fallback')).toHaveTextContent(
      /Stay the course with your plan/i,
    )
    expect(container.innerHTML).not.toMatch(/error|failed to load/i)
    expect(container.innerHTML).not.toMatch(/brand-red|accent-pink|line-red/)
  })

  it('non-2xx response → calm static fallback', async () => {
    stubRecord({ detail: 'unauthorized' }, { ok: false })
    renderWidget()

    typeHeadline('Anything at all')
    submit()

    await waitFor(() =>
      expect(screen.getByTestId('headline-fallback')).toBeInTheDocument(),
    )
    expect(screen.queryByTestId('precedent-event')).not.toBeInTheDocument()
  })
})
