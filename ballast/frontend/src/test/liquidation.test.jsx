import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { LiquidationCard } from '../components/LiquidationCard.jsx'

afterEach(() => {
  vi.unstubAllGlobals()
})

function renderCard(plan, props = {}) {
  return render(
    <MemoryRouter>
      <LiquidationCard plan={plan} {...props} />
    </MemoryRouter>,
  )
}

// A coverable shortfall: parked SWVXX covers the whole $500 short.
const COVERABLE = {
  needs_liquidation: true,
  coverable: true,
  ready_to_trade: '200.00',
  shortfall: '500.00',
  sell_symbol: 'SWVXX',
  sell_amount: '500.00',
  est_shares: 500,
  sell_order_intent: { symbol: 'SWVXX', side: 'sell', amount: '500.00' },
  reserved: '1000.00',
  reserve_decided: true,
  as_of: '2026-07-27T00:00:00+00:00',
  reasoning:
    'This sells about $500.00 of your SWVXX money-market fund to free up cash for the buy you chose. Your reserve stays protected.',
  pending_buy_id: 'pb-1',
  sell_decision_id: 'dec-1',
}

// Partial coverage: only $300 of the $900 shortfall can be freed up.
// A partial shortfall: the single largest parked fund can only cover part of it.
// The backend still mints a proposed SELL decision (its own id), so the partial
// sell is SUBMITTABLE — the user frees up what they can now and the rest resumes.
const PARTIAL = {
  ...COVERABLE,
  coverable: false,
  shortfall: '900.00',
  sell_amount: '300.00',
  est_shares: 300,
  sell_order_intent: { symbol: 'SWVXX', side: 'sell', amount: '300.00' },
  sell_decision_id: 'dec-partial',
  pending_buy_id: 'pb-partial',
  reasoning:
    'This sells about $300.00 of your SWVXX money-market fund — that covers $300.00 of the $900.00 you’re short.',
}

// Nothing to liquidate: no parked funds above the reserve.
const NOTHING = {
  needs_liquidation: true,
  coverable: false,
  ready_to_trade: '100.00',
  shortfall: '400.00',
  sell_symbol: null,
  sell_amount: null,
  est_shares: null,
  sell_order_intent: null,
  reserved: null,
  reserve_decided: false,
  as_of: '2026-07-27T00:00:00+00:00',
  reasoning:
    'You don’t have any money-market funds to sell right now, so this will resume on its own once enough cash settles.',
  pending_buy_id: 'pb-2',
  sell_decision_id: null,
}

const NUDGE_RE = /invest now|you should|don'?t wait|act now|hurry|last chance/i

describe('LiquidationCard — calm, honest, never-red just-in-time SELL', () => {
  it('renders a coverable sell with amount, est shares, as_of, and protected reserve', () => {
    const { container } = renderCard(COVERABLE)

    expect(screen.getByTestId('liquidation-card')).toBeInTheDocument()
    expect(screen.getByTestId('liquidation-sell-symbol')).toHaveTextContent('SWVXX')
    expect(screen.getByTestId('liquidation-sell-amount')).toHaveTextContent('$500.00')
    expect(screen.getByTestId('liquidation-est-shares')).toHaveTextContent('500')

    // Protected-reserve line shown alongside (reassurance).
    expect(screen.getByTestId('liquidation-reserve')).toHaveTextContent(
      /\$1000\.00 reserve stays protected/i,
    )
    // Data freshness surfaced.
    expect(screen.getByTestId('liquidation-asof')).toHaveTextContent(/as of/i)

    // The submit control routes through the existing /approve flow.
    expect(screen.getByTestId('liquidation-approve-sell')).toBeInTheDocument()

    // HARD color rule + no FOMO.
    expect(container.innerHTML).not.toMatch(/brand-red|accent-pink|line-red/)
    expect(container.textContent).not.toMatch(NUDGE_RE)
  })

  it('renders honest partial-coverage copy ("covers $Y of $X") with a SUBMITTABLE sell', () => {
    const { container } = renderCard(PARTIAL)
    expect(screen.getByTestId('liquidation-partial')).toHaveTextContent(
      /\$300\.00 of the \$900\.00 you’re short/i,
    )
    // A partial sell is real and submittable — never a dead-end: the sell block and
    // the /approve control both render, NOT the "nothing to sell" branch.
    expect(screen.getByTestId('liquidation-sell')).toBeInTheDocument()
    expect(screen.getByTestId('liquidation-approve-sell')).toBeInTheDocument()
    expect(screen.queryByTestId('liquidation-nothing')).not.toBeInTheDocument()
    expect(container.innerHTML).not.toMatch(/brand-red|accent-pink|line-red/)
    expect(container.textContent).not.toMatch(NUDGE_RE)
  })

  it('renders the calm nothing-to-liquidate state (resumes when cash settles)', () => {
    const { container } = renderCard(NOTHING)
    expect(screen.getByTestId('liquidation-nothing')).toHaveTextContent(
      /resume on its own once enough of your cash settles/i,
    )
    // No sell control, and no fabricated reserve line (reserve never-decided).
    expect(screen.queryByTestId('liquidation-approve-sell')).not.toBeInTheDocument()
    expect(screen.queryByTestId('liquidation-reserve')).not.toBeInTheDocument()
    expect(container.innerHTML).not.toMatch(/brand-red|accent-pink|line-red/)
    expect(container.textContent).not.toMatch(NUDGE_RE)
  })

  it('submits the pre-filled SELL through /api/coach/approve on click', async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ status: 'filled', filled_qty: '500' }),
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    renderCard(COVERABLE)
    fireEvent.click(screen.getByTestId('liquidation-approve-sell'))

    await waitFor(() =>
      expect(screen.getByTestId('liquidation-placed')).toBeInTheDocument(),
    )
    // It called the existing approve endpoint with the sell decision + intent.
    const [url, opts] = fetchMock.mock.calls[0]
    expect(String(url)).toMatch(/\/api\/coach\/approve$/)
    const body = JSON.parse(opts.body)
    expect(body.decision_id).toBe('dec-1')
    expect(body.order_intent).toEqual({
      symbol: 'SWVXX',
      side: 'sell',
      amount: '500.00',
    })
  })

  it('surfaces a calm 422 refusal (untradeable fund) without red — the buy persists', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve({
          ok: false,
          status: 422,
          json: () =>
            Promise.resolve({
              error: { type: 'unprocessable', message: 'That fund can’t be traded here.' },
            }),
        }),
      ),
    )
    const { container } = renderCard(COVERABLE)
    fireEvent.click(screen.getByTestId('liquidation-approve-sell'))

    await waitFor(() =>
      expect(screen.getByTestId('liquidation-refused')).toBeInTheDocument(),
    )
    expect(screen.getByTestId('liquidation-refused')).toHaveTextContent(
      /can’t be traded here/i,
    )
    expect(container.innerHTML).not.toMatch(/brand-red|accent-pink|line-red/)
  })
})
