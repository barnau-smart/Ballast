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

// A HYPOTHETICAL precedent record (Story 3.6) — additive `stats.hypothetical*`
// keys, honest "if it fell about X%" statement, still the 6-field AD-12 shape.
const HYPOTHETICAL_RECORD = {
  id: 'ep-aa11bb22cc33',
  kind: 'event-precedent',
  statement:
    "If VTI fell about 35% from a recent high, here's what the record shows: in 2 comparable drops on record, it recovered to breakeven in a median of 240 trading days, and it was higher a year later in 2 of 2. This isn't a prediction; it's the base rate.",
  stats: {
    initial_drawdown_pct: '0.3500',
    current_velocity: '0.0000',
    instance_count: 2,
    recovery_days_median: 240,
    recovery_days_range: { min: 210, max: 270 },
    forward_return_1yr_median: '0.4500',
    hypothetical: true,
    hypothetical_drawdown_pct: '0.3500',
    windows: [
      {
        peak_date: '2007-10-09',
        trough_date: '2009-03-09',
        recovery_date: '2012-03-15',
        drawdown_pct: '0.3510',
        velocity: '0.0010',
        recovery_days: 240,
        recovered: true,
        forward_return_1yr: '0.4500',
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

  // --- Story 3.6: hypothetical scenarios -------------------------------------

  it('scenario chips are disabled until a headline is typed (no fetch on mount)', () => {
    const fetchFn = stubRecord(HYPOTHETICAL_RECORD)
    renderWidget()

    expect(screen.getByTestId('headline-scenarios')).toBeInTheDocument()
    expect(screen.getByTestId('headline-scenario-crash')).toBeDisabled()
    expect(fetchFn).not.toHaveBeenCalled()

    typeHeadline('Markets tumble')
    expect(screen.getByTestId('headline-scenario-crash')).toBeEnabled()
  })

  it('choosing a scenario POSTs { headline, drawdown } and renders the hypothetical record', async () => {
    const fetchFn = stubRecord(HYPOTHETICAL_RECORD)
    const { container } = renderWidget()

    typeHeadline('Markets tumble on recession fears')
    fireEvent.click(screen.getByTestId('headline-scenario-crash'))

    await waitFor(() =>
      expect(screen.getByTestId('precedent-event')).toBeInTheDocument(),
    )

    // POSTs the drawdown fraction — the NUMBER drives the match, not the headline.
    expect(fetchFn).toHaveBeenCalledTimes(1)
    const [url, options] = fetchFn.mock.calls[0]
    expect(url).toMatch(/\/api\/precedent\/contextualize$/)
    expect(JSON.parse(options.body)).toEqual({
      headline: 'Markets tumble on recession fears',
      drawdown: 0.35,
    })

    // Hypothetical framing — never a prediction/forecast.
    const framing = screen.getByTestId('headline-framing')
    expect(framing).toHaveTextContent(/isn'?t a forecast/i)
    expect(screen.getByTestId('precedent-statement')).toHaveTextContent(
      /If VTI fell about 35%/i,
    )

    // Drops still sky-blue ▼, forward-returns green ▲, never red.
    const drawdown = screen.getByTestId('precedent-drawdown')
    expect(
      drawdown.querySelector('.ballast-market-indicator--down'),
    ).not.toBeNull()

    // HONESTY (NFR8/FR20): the queried target must read as a SCENARIO, never as
    // the symbol's real current position — so it must NOT say "below its recent
    // peak" (which would present the hypothetical as fact).
    expect(drawdown).toHaveTextContent(/in this scenario/i)
    expect(drawdown).toHaveTextContent(/35\.0% drop from a recent high/i)
    expect(drawdown).not.toHaveTextContent(/below its recent peak/i)
    const forward = screen.getByTestId('precedent-forward')
    expect(forward.querySelector('.ballast-market-indicator--up')).not.toBeNull()

    expect(container.innerHTML).not.toMatch(/brand-red|accent-pink|line-red/)
    expect(container.textContent).not.toMatch(NUDGE_OR_CLASSIFY)
  })

  it('the default submit path (no scenario) still POSTs exactly { headline }', async () => {
    const fetchFn = stubRecord(EVENT_RECORD)
    renderWidget()

    typeHeadline('Stocks slide')
    submit()

    await waitFor(() =>
      expect(screen.getByTestId('precedent-event')).toBeInTheDocument(),
    )
    const [, options] = fetchFn.mock.calls[0]
    // Current-conditions default: no drawdown key on the wire.
    expect(JSON.parse(options.body)).toEqual({ headline: 'Stocks slide' })
    // And the default (non-hypothetical) framing is shown.
    expect(screen.getByTestId('headline-framing')).toHaveTextContent(
      /doesn'?t weigh in on the news itself/i,
    )
  })
})
